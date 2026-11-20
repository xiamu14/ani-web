"""基于 SQLModel + aiosqlite + FTS5 的同进程分层记忆仓储（实现 MemoryStorePort，零独立 Worker / 零 HTTP）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import or_, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlmodel import col, select

from agent.db._memory_serde import (
    build_fts_match_expr,
    dump_anchors,
    file_specificity_score,
    merge_unique_strs,
    now_epoch,
    parse_str_map,
    parse_str_tuple,
    row_to_observation,
    row_to_summary,
    row_to_work_state,
)
from agent.db.engine import create_session_factory
from agent.db.models import ObservationRow, SessionSummaryRow, WorkStateRow
from agent.memory.anchors import normalize_anchor_path
from agent.memory.budget import sanitize_and_redact_text, truncate_field_60_30_10
from agent.memory.dedup import build_idf_lookup, classify_near_duplicate, tokenize_ws
from agent.memory.ports import (
    CreateObservationInput,
    CreateSessionSummaryInput,
    ObservationRecord,
    ObservationType,
    SaveObservationResult,
    SessionSummaryRecord,
    WorkStateEntryRecord,
    WorkStateStatus,
)
from agent.memory.rank import iso_day_from_epoch


class SQLModelMemoryStore:
    """管理 observations (含 FTS5)、session_summaries 与 work_states 的同进程异步仓储。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine: AsyncEngine = engine
        self._sessions: async_sessionmaker[AsyncSession] = (
            session_factory or create_session_factory(engine)
        )

    async def save_observation(self, item: CreateObservationInput) -> SaveObservationResult:
        """写入 Observation：自动执行 60/30/10 清洗、Tier-0 强化合并及 Tier-1 同文件覆盖（Supersede）。"""
        clean_title = " ".join(sanitize_and_redact_text(item.title).split())[:120]
        if not clean_title:
            raise ValueError("Observation title must not be empty")
        epoch = item.created_at_epoch if item.created_at_epoch is not None else now_epoch()
        day_iso = iso_day_from_epoch(epoch)
        norm_reads = tuple(normalize_anchor_path(p) for p in item.files_read if p.strip())
        norm_mods = tuple(normalize_anchor_path(p) for p in item.files_modified if p.strip())
        clean_facts = tuple(truncate_field_60_30_10(f, max_chars=2_000) for f in item.facts if f.strip())
        clean_narrative = truncate_field_60_30_10(item.narrative)
        clean_subtitle = sanitize_and_redact_text(item.subtitle).strip()[:240]
        norm_hashes = {normalize_anchor_path(k): v for k, v in item.file_hashes.items()}

        async with self._sessions() as session:
            recent_stmt = (
                select(ObservationRow)
                .where(col(ObservationRow.project_key) == item.project_key)
                .where(col(ObservationRow.superseded_by).is_(None))
                .order_by(col(ObservationRow.created_at_epoch).desc(), col(ObservationRow.id).desc())
                .limit(100)
            )
            recent_rows = (await session.execute(recent_stmt)).scalars().all()
            idf_fn = build_idf_lookup([tokenize_ws(r.title) for r in recent_rows])
            tier1_superseded_ids: list[int] = []

            for existing in recent_rows:
                pair = classify_near_duplicate(existing.title, clean_title, idf_fn)
                if pair.tier == "exact":
                    merged_dates = merge_unique_strs(
                        parse_str_tuple(existing.reinforcement_dates_json),
                        (day_iso,),
                    )
                    merged_facts = merge_unique_strs(parse_str_tuple(existing.facts_json), clean_facts)
                    merged_concepts = merge_unique_strs(
                        parse_str_tuple(existing.concepts_json),
                        tuple(item.concepts),
                    )
                    merged_reads = merge_unique_strs(parse_str_tuple(existing.files_read_json), norm_reads)
                    merged_mods = merge_unique_strs(parse_str_tuple(existing.files_modified_json), norm_mods)
                    existing.reinforcement_dates_json = json.dumps(list(merged_dates), ensure_ascii=False)
                    existing.facts_json = json.dumps(list(merged_facts), ensure_ascii=False)
                    existing.concepts_json = json.dumps(list(merged_concepts), ensure_ascii=False)
                    existing.files_read_json = json.dumps(list(merged_reads), ensure_ascii=False)
                    existing.files_modified_json = json.dumps(list(merged_mods), ensure_ascii=False)
                    if item.line_anchors:
                        existing.line_anchors_json = dump_anchors(item.line_anchors)
                    if norm_hashes:
                        merged_hashes = {**parse_str_map(existing.file_hashes_json), **norm_hashes}
                        existing.file_hashes_json = json.dumps(merged_hashes, ensure_ascii=False)
                    session.add(existing)
                    await session.commit()
                    await session.refresh(existing)
                    return SaveObservationResult(
                        record=row_to_observation(existing),
                        merged_into_existing=True,
                    )

                if pair.tier == "candidate" and existing.id is not None and norm_mods:
                    existing_mods = set(parse_str_tuple(existing.files_modified_json))
                    if existing_mods & set(norm_mods):
                        tier1_superseded_ids.append(existing.id)

            row = ObservationRow(
                project_key=item.project_key,
                conversation_id=item.conversation_id,
                role_name=item.role_name,
                type=item.type,
                title=clean_title,
                subtitle=clean_subtitle,
                facts_json=json.dumps(list(clean_facts), ensure_ascii=False),
                narrative=clean_narrative,
                concepts_json=json.dumps([c.strip() for c in item.concepts if c.strip()], ensure_ascii=False),
                files_read_json=json.dumps(list(norm_reads), ensure_ascii=False),
                files_modified_json=json.dumps(list(norm_mods), ensure_ascii=False),
                line_anchors_json=dump_anchors(item.line_anchors),
                file_hashes_json=json.dumps(norm_hashes, ensure_ascii=False),
                provenance=item.provenance,
                discovery_tokens=max(0, item.discovery_tokens),
                reinforcement_dates_json=json.dumps([day_iso], ensure_ascii=False),
                superseded_by=None,
                created_at_epoch=epoch,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)

            if tier1_superseded_ids and row.id is not None:
                _ = await session.execute(
                    update(ObservationRow)
                    .where(col(ObservationRow.project_key) == item.project_key)
                    .where(col(ObservationRow.id).in_(tier1_superseded_ids))
                    .values(superseded_by=row.id)
                )
                await session.commit()

            return SaveObservationResult(
                record=row_to_observation(row),
                merged_into_existing=False,
                superseded_ids=tuple(tier1_superseded_ids),
            )

    async def save_summary(self, item: CreateSessionSummaryInput) -> SessionSummaryRecord:
        epoch = item.created_at_epoch if item.created_at_epoch is not None else now_epoch()
        norm_reads = [normalize_anchor_path(p) for p in item.files_read if p.strip()]
        norm_edits = [normalize_anchor_path(p) for p in item.files_edited if p.strip()]
        row = SessionSummaryRow(
            project_key=item.project_key,
            conversation_id=item.conversation_id,
            task_id=item.task_id,
            request=truncate_field_60_30_10(item.request, max_chars=4_000),
            investigated=truncate_field_60_30_10(item.investigated, max_chars=4_000),
            learned=truncate_field_60_30_10(item.learned, max_chars=4_000),
            completed=truncate_field_60_30_10(item.completed, max_chars=4_000),
            next_steps=truncate_field_60_30_10(item.next_steps, max_chars=4_000),
            notes=truncate_field_60_30_10(item.notes, max_chars=4_000),
            files_read_json=json.dumps(norm_reads, ensure_ascii=False),
            files_edited_json=json.dumps(norm_edits, ensure_ascii=False),
            discovery_tokens=max(0, item.discovery_tokens),
            created_at_epoch=epoch,
        )
        async with self._sessions() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row_to_summary(row)

    async def append_work_state(
        self,
        *,
        project_key: str,
        list_name: str,
        task_name: str,
        status: WorkStateStatus = "open",
        fields: Mapping[str, str | None] | None = None,
        created_at_epoch: int | None = None,
    ) -> WorkStateEntryRecord:
        epoch = created_at_epoch if created_at_epoch is not None else now_epoch()
        clean_fields = {
            str(k): (None if v is None else sanitize_and_redact_text(str(v)))
            for k, v in (fields or {}).items()
        }
        row = WorkStateRow(
            project_key=project_key,
            list_name=list_name.strip(),
            task_name=task_name.strip(),
            fields_json=json.dumps(clean_fields, ensure_ascii=False),
            status=status,
            created_at_epoch=epoch,
        )
        async with self._sessions() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row_to_work_state(row)

    async def search_observations(
        self,
        *,
        project_key: str,
        query: str,
        type_filter: ObservationType | None = None,
        concept_filter: str | None = None,
        file_path: str | None = None,
        include_superseded: bool = False,
        limit: int = 15,
    ) -> list[ObservationRecord]:
        fts_expr = build_fts_match_expr(query)
        norm_file = normalize_anchor_path(file_path) if file_path else None

        async with self._sessions() as session:
            if fts_expr:
                sql = text(
                    """
                    SELECT o.id
                    FROM observations_fts f
                    JOIN observations o ON o.id = f.rowid
                    WHERE o.project_key = :project_key
                      AND (:include_superseded = 1 OR o.superseded_by IS NULL)
                      AND (:type_filter IS NULL OR o.type = :type_filter)
                      AND observations_fts MATCH :fts_expr
                    ORDER BY bm25(observations_fts) ASC, o.created_at_epoch DESC, o.id DESC
                    LIMIT :fetch_limit
                    """
                )
                id_rows: Sequence[object] = (
                    await session.execute(
                        sql,
                        {
                            "project_key": project_key,
                            "include_superseded": 1 if include_superseded else 0,
                            "type_filter": type_filter,
                            "fts_expr": fts_expr,
                            "fetch_limit": max(limit * 3, 30),
                        },
                    )
                ).scalars().all()
                matched_ids: list[int] = [i for i in id_rows if isinstance(i, int)]
                if not matched_ids:
                    return []
                stmt = select(ObservationRow).where(col(ObservationRow.id).in_(matched_ids))
                row_map = {
                    (r.id or 0): row_to_observation(r)
                    for r in (await session.execute(stmt)).scalars().all()
                }
                candidates = [row_map[i] for i in matched_ids if i in row_map]
            else:
                stmt = (
                    select(ObservationRow)
                    .where(col(ObservationRow.project_key) == project_key)
                    .order_by(col(ObservationRow.created_at_epoch).desc(), col(ObservationRow.id).desc())
                    .limit(max(limit * 3, 30))
                )
                if not include_superseded:
                    stmt = stmt.where(col(ObservationRow.superseded_by).is_(None))
                if type_filter is not None:
                    stmt = stmt.where(col(ObservationRow.type) == type_filter)
                candidates = [row_to_observation(r) for r in (await session.execute(stmt)).scalars().all()]

        filtered: list[ObservationRecord] = []
        for obs in candidates:
            if concept_filter and concept_filter not in obs.concepts:
                continue
            if norm_file and norm_file not in obs.files_read and norm_file not in obs.files_modified:
                continue
            filtered.append(obs)
            if len(filtered) >= limit:
                break
        return filtered

    async def get_timeline(
        self,
        *,
        project_key: str,
        anchor_id: int,
        depth_before: int = 3,
        depth_after: int = 3,
    ) -> list[ObservationRecord]:
        """利用 (project_key, created_at_epoch, id) 复合索引执行 O(log N) 双向局部邻域查询。"""
        async with self._sessions() as session:
            anchor_stmt = (
                select(ObservationRow)
                .where(col(ObservationRow.project_key) == project_key)
                .where(col(ObservationRow.id) == anchor_id)
            )
            anchor = (await session.execute(anchor_stmt)).scalars().first()
            if anchor is None or anchor.id is None:
                return []

            before_rows: list[ObservationRow] = []
            if depth_before > 0:
                before_stmt = (
                    select(ObservationRow)
                    .where(col(ObservationRow.project_key) == project_key)
                    .where(
                        or_(
                            col(ObservationRow.created_at_epoch) < anchor.created_at_epoch,
                            (col(ObservationRow.created_at_epoch) == anchor.created_at_epoch)
                            & (col(ObservationRow.id) < anchor.id),
                        )
                    )
                    .order_by(col(ObservationRow.created_at_epoch).desc(), col(ObservationRow.id).desc())
                    .limit(depth_before)
                )
                before_rows = list(reversed((await session.execute(before_stmt)).scalars().all()))

            after_rows: list[ObservationRow] = []
            if depth_after > 0:
                after_stmt = (
                    select(ObservationRow)
                    .where(col(ObservationRow.project_key) == project_key)
                    .where(
                        or_(
                            col(ObservationRow.created_at_epoch) > anchor.created_at_epoch,
                            (col(ObservationRow.created_at_epoch) == anchor.created_at_epoch)
                            & (col(ObservationRow.id) > anchor.id),
                        )
                    )
                    .order_by(col(ObservationRow.created_at_epoch).asc(), col(ObservationRow.id).asc())
                    .limit(depth_after)
                )
                after_rows = list((await session.execute(after_stmt)).scalars().all())

        return [row_to_observation(r) for r in (*before_rows, anchor, *after_rows)]

    async def get_observations_by_ids(
        self,
        *,
        project_key: str,
        ids: Sequence[int],
    ) -> list[ObservationRecord]:
        if not ids:
            return []
        async with self._sessions() as session:
            stmt = (
                select(ObservationRow)
                .where(col(ObservationRow.project_key) == project_key)
                .where(col(ObservationRow.id).in_(list(ids)))
            )
            rows = (await session.execute(stmt)).scalars().all()
        by_id = {(r.id or 0): row_to_observation(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def list_recent_observations(
        self,
        *,
        project_key: str,
        limit: int = 50,
        include_superseded: bool = False,
    ) -> list[ObservationRecord]:
        async with self._sessions() as session:
            stmt = (
                select(ObservationRow)
                .where(col(ObservationRow.project_key) == project_key)
                .order_by(col(ObservationRow.created_at_epoch).desc(), col(ObservationRow.id).desc())
                .limit(limit)
            )
            if not include_superseded:
                stmt = stmt.where(col(ObservationRow.superseded_by).is_(None))
            rows = (await session.execute(stmt)).scalars().all()
        return [row_to_observation(r) for r in rows]

    async def list_observations_for_file(
        self,
        *,
        project_key: str,
        file_path: str,
        limit: int = 15,
        include_superseded: bool = False,
    ) -> list[ObservationRecord]:
        """在 SQLite 层通过 json_each 下推路径过滤，再按文件特异性评分排序。"""
        norm_target = normalize_anchor_path(file_path)
        sql = text(
            """
            SELECT o.id
            FROM observations o
            WHERE o.project_key = :project_key
              AND (:include_superseded = 1 OR o.superseded_by IS NULL)
              AND (
                EXISTS (SELECT 1 FROM json_each(o.files_modified_json) WHERE value = :file_path)
                OR EXISTS (SELECT 1 FROM json_each(o.files_read_json) WHERE value = :file_path)
              )
            ORDER BY o.created_at_epoch DESC, o.id DESC
            LIMIT :fetch_limit
            """
        )
        async with self._sessions() as session:
            id_rows: Sequence[object] = (
                await session.execute(
                    sql,
                    {
                        "project_key": project_key,
                        "include_superseded": 1 if include_superseded else 0,
                        "file_path": norm_target,
                        "fetch_limit": max(limit * 4, 40),
                    },
                )
            ).scalars().all()
            matched_ids = [i for i in id_rows if isinstance(i, int)]
            if not matched_ids:
                return []
            stmt = select(ObservationRow).where(col(ObservationRow.id).in_(matched_ids))
            matched = [row_to_observation(r) for r in (await session.execute(stmt)).scalars().all()]

        matched.sort(
            key=lambda obs: (file_specificity_score(obs, norm_target), obs.created_at_epoch, obs.id),
            reverse=True,
        )
        return matched[:limit]

    async def list_recent_summaries(
        self,
        *,
        project_key: str,
        limit: int = 5,
    ) -> list[SessionSummaryRecord]:
        async with self._sessions() as session:
            stmt = (
                select(SessionSummaryRow)
                .where(col(SessionSummaryRow.project_key) == project_key)
                .order_by(col(SessionSummaryRow.created_at_epoch).desc(), col(SessionSummaryRow.id).desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [row_to_summary(r) for r in rows]

    async def list_work_states(
        self,
        *,
        project_key: str,
    ) -> list[WorkStateEntryRecord]:
        async with self._sessions() as session:
            stmt = (
                select(WorkStateRow)
                .where(col(WorkStateRow.project_key) == project_key)
                .order_by(col(WorkStateRow.created_at_epoch).asc(), col(WorkStateRow.id).asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [row_to_work_state(r) for r in rows]

    async def mark_superseded(
        self,
        *,
        project_key: str,
        observation_ids: Sequence[int],
        superseded_by: int,
    ) -> None:
        target_ids = [i for i in observation_ids if i != superseded_by]
        if not target_ids:
            return
        async with self._sessions() as session:
            stmt = (
                update(ObservationRow)
                .where(col(ObservationRow.project_key) == project_key)
                .where(col(ObservationRow.id).in_(target_ids))
                .values(superseded_by=superseded_by)
            )
            _ = await session.execute(stmt)
            await session.commit()
