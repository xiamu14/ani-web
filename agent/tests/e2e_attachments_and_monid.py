"""真实端到端测试（E2E）：
1. 使用 docs/001.jpg、docs/002.webp、docs/003.pdf 三个真实文件，通过 AttachmentRegistry + open_runtime + 真实 LLM 执行附件引用提交、OCR 识别、OPT-3 外置压缩验证与 [[cite:...]] 溯源核验。
2. 使用 .env 中的 MONID_API_KEY 调用 Monid API (provider="openai", endpoint="/v1/image/gpt-image-2") 生成图像，注册为全局附件与内部 Artifact，再由独立 Role Agent 仅凭轻量引用句柄按需加载图像并完成视觉分析与溯源核验。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import httpx
from dotenv import dotenv_values
from sqlmodel import select

from agent.attachments import extract_and_verify_citations
from agent.config import load_settings
from agent.core import RoleRegistry, RoleSpec, build_model, open_runtime
from agent.db import ContextEntryRow, create_session_factory

MONID_PROMPT = (
    "A cinematic, romantic fine-art fashion portrait of a beautiful young woman "
    + "reclining gracefully in an elegant rose garden beside an ornate classical stone fountain. "
    + "She has delicate symmetrical facial features, luminous natural skin texture, soft brown eyes, "
    + "naturally defined eyebrows, subtle rosy makeup, and softly parted nude-pink lips. "
    + "Her dark, slightly damp, wavy hair falls loosely around her face and shoulders. "
    + "A delicate golden laurel-leaf headpiece rests across her hair, paired with a slim black velvet choker. "
    + "She wears a flowing Greco-Roman-inspired draped dress in muted grey, layered with a sheer "
    + "dusty-blue shawl featuring fine gold edging and a black waist detail. "
    + "A subtle black garter is visible around her upper thigh. "
    + "She reclines sideways on luxurious black velvet fabric, supporting herself with one hand "
    + "while resting the other gently on her thigh, her legs extended elegantly to the right. "
    + "Her gaze turns thoughtfully toward the distant garden. "
    + "An elaborate ivory marble fountain with ornate gold decorations and a sculpted cherub statue stands beside her. "
    + "Surrounding her are abundant pink roses, lush green foliage, distant rolling hills, and a dramatic overcast sky. "
    + "Soft diffused daylight, muted cinematic colors, classical romantic atmosphere, sophisticated editorial photography, "
    + "realistic anatomy, natural skin texture, intricate fabric details. "
    + "Preserve the original composition, outfit, accessories, pose, and environment. "
    + "Ignore all text, logos, and watermarks. No cartoon face, no anime face, no animated face."
)

MONID_BASE_URL = "https://api.monid.ai"
ARTIFACT_OUTPUT_DIR = Path(
    "/Users/ben/.gemini/antigravity-cli/brain/5c5a4ede-7fbc-4878-bdc9-c952a2c18674"
)


def _load_monid_api_key(repo_root: Path) -> str:
    env_key = os.environ.get("MONID_API_KEY", "").strip()
    if env_key:
        return env_key
    env_file = repo_root / ".env"
    if env_file.is_file():
        values = dotenv_values(env_file)
        file_key = (values.get("MONID_API_KEY") or "").strip()
        if file_key:
            return file_key
    raise RuntimeError("MONID_API_KEY not found in environment or .env")


def _extract_image_from_monid_payload(payload: object) -> tuple[str | None, bytes | None]:
    """递归从 Monid /v1/run 响应中提取图像 URL 或 base64 PNG 字节。"""
    urls: list[str] = []
    b64_candidates: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            s = node.strip()
            if s.startswith(("http://", "https://")) and any(
                hint in s.lower()
                for hint in (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    "image",
                    "r2.cloudflare",
                    "storage",
                    "monid",
                    "openai",
                )
            ):
                urls.append(s)
            elif len(s) > 1000 and not s.startswith("http"):
                b64_candidates.append(s)
        elif isinstance(node, Mapping):
            mapping_node = cast("Mapping[object, object]", node)
            for k, v in mapping_node.items():
                if (
                    k in ("b64_json", "base64", "image_base64")
                    and isinstance(v, str)
                    and len(v) > 200
                ):
                    b64_candidates.append(v)
                elif (
                    k in ("url", "image_url", "downloadUrl", "signedUrl", "signed_url")
                    and isinstance(v, str)
                    and v.startswith(("http://", "https://"))
                ):
                    urls.insert(0, v)
                else:
                    _walk(v)
        elif isinstance(node, Sequence):
            seq_node: Sequence[object] = node
            for item in seq_node:
                _walk(item)

    _walk(payload)
    if urls:
        return urls[0], None
    for b64 in b64_candidates:
        cleaned = b64.split(",", 1)[1] if b64.startswith("data:image") and "," in b64 else b64
        try:
            raw = base64.b64decode(cleaned, validate=False)
        except (binascii.Error, ValueError):
            continue
        if raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
            return None, raw
    return None, None


def _parse_json_dict(raw_text: str) -> dict[str, object]:
    loaded: object = json.loads(raw_text)  # pyright: ignore[reportAny]
    if not isinstance(loaded, dict):
        raise TypeError(f"Expected JSON object from Monid API, got {type(loaded).__name__}")
    result: dict[str, object] = {}
    for k, v in loaded.items():  # pyright: ignore[reportUnknownVariableType]
        if isinstance(k, str):
            result[k] = v
    return result


async def _call_monid_gpt_image_2(
    api_key: str,
    prompt: str,
) -> tuple[dict[str, object], str | None, bytes]:
    """调用 Monid /v1/run (provider=openai, endpoint=/v1/image/gpt-image-2) 并获取生成图 PNG 字节。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    request_body = {
        "provider": "openai",
        "endpoint": "/v1/image/gpt-image-2",
        "input": {
            "prompt": prompt,
            "size": "1024x1024",
            "quality": "medium",
        },
    }

    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        t0 = time.monotonic()
        resp = await client.post(
            f"{MONID_BASE_URL}/v1/run",
            headers=headers,
            json=request_body,
        )
        elapsed = time.monotonic() - t0
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Monid /v1/run failed HTTP {resp.status_code} ({elapsed:.1f}s): {resp.text[:800]}"
            )
        data = _parse_json_dict(resp.text)
        print(f"[Monid] /v1/run HTTP {resp.status_code} in {elapsed:.1f}s | keys={list(data.keys())}")

        status = str(data.get("status", "")).upper()
        run_id = data.get("runId") or data.get("id")
        if status in ("PENDING", "QUEUED", "RUNNING", "IN_PROGRESS") and isinstance(run_id, str):
            print(f"[Monid] Async run {run_id} status={status}, polling...")
            for _ in range(45):
                await asyncio.sleep(4.0)
                poll = await client.get(f"{MONID_BASE_URL}/v1/runs/{run_id}", headers=headers)
                if poll.status_code >= 400:
                    continue
                data = _parse_json_dict(poll.text)
                status = str(data.get("status", "")).upper()
                if status in ("COMPLETED", "SUCCEEDED", "SUCCESS", "DONE", "FAILED", "ERROR"):
                    break

        image_url, raw_bytes = _extract_image_from_monid_payload(data)
        if raw_bytes is None and image_url is not None:
            dl = await client.get(image_url)
            _ = dl.raise_for_status()
            raw_bytes = dl.content
            print(f"[Monid] Downloaded image from signed URL ({len(raw_bytes):,} bytes)")
        elif raw_bytes is not None:
            print(f"[Monid] Decoded inline base64 image ({len(raw_bytes):,} bytes)")
        else:
            snippet = json.dumps(data, ensure_ascii=False)[:1200]
            raise RuntimeError(f"Could not find image URL or base64 in Monid response: {snippet}")

        return data, image_url, raw_bytes


async def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    settings = load_settings()
    monid_key = _load_monid_api_key(repo_root)
    real_model = build_model(settings)

    print("=" * 72)
    print(f"E2E Config: model={settings.model_name} | base_url={settings.openai_base_url}")
    print("=" * 72)

    role_registry = RoleRegistry(
        [
            RoleSpec(
                name="ocr_analyst",
                description="Attachment OCR and document extraction specialist",
                instructions=(
                    "You are an OCR and attachment analysis specialist. "
                    + "When given an [[attachment:...]] handle, you MUST first call "
                    + "inspect_attachment(attachment_id=<id>, load_binary=True) to load the file. "
                    + "Then perform accurate OCR / content extraction and summarize all visible text, "
                    + "numbers, headings, and key visual details in Chinese. "
                    + "Include at least one provenance citation using the exact format: "
                    + '[[cite:<attachment_id>#bbox=0.0,0.0,1.0,1.0 | "<short exact quote or summary>"]] '
                    + 'for images, or [[cite:<attachment_id>#page=1 | "<short exact quote>"]] for PDFs. '
                    + "Finish your final response with [TASK_STATUS: COMPLETED]."
                ),
                model=real_model,
                default_lock_mode="shared",
            ),
            RoleSpec(
                name="vision_reviewer",
                description="Fine-art image critique and provenance verification specialist",
                instructions=(
                    "You are a fine-art photography and visual QA reviewer. "
                    + "When given an [[attachment:...]] handle and an [[artifact:...]] handle, "
                    + "you MUST first call inspect_attachment(attachment_id=<id>, load_binary=True) "
                    + "and get_artifact(artifact_id='monid_gen_result') to inspect both the generated image "
                    + "and its generation metadata. "
                    + "Then provide a structured visual analysis in Chinese evaluating: "
                    + "(1) 构图与人物姿态, (2) 服饰与配饰细节（灰罗马长裙、蓝金披肩、金色月桂头饰、黑丝绒项圈）, "
                    + "(3) 场景元素（象牙大理石喷泉与天使雕像、粉红玫瑰、阴天光线）, "
                    + "(4) 写实质感与有无卡通/伪影。 "
                    + "Include at least two provenance citations: "
                    + '[[cite:<attachment_id>#bbox=x1,y1,x2,y2 | "<observed visual detail>"]] '
                    + 'and [[cite:art:monid_gen_result#key=endpoint | "/v1/image/gpt-image-2"]]. '
                    + "Finish your final response with [TASK_STATUS: COMPLETED]."
                ),
                model=real_model,
                default_lock_mode="shared",
            ),
        ]
    )

    sample_files = [
        (repo_root / "docs" / "001.jpg", "image/jpeg"),
        (repo_root / "docs" / "002.webp", "image/webp"),
        (repo_root / "docs" / "003.pdf", "application/pdf"),
    ]
    for path, _ in sample_files:
        if not path.is_file():
            raise FileNotFoundError(f"Missing sample file: {path}")

    with tempfile.TemporaryDirectory(prefix="e2e_att_monid_") as tmp:
        root = Path(tmp)
        async with open_runtime(
            db_path=root / "ctx.db",
            step_db_path=root / "steps.db",
            role_registry=role_registry,
            global_mcp_path=root / "no_mcp.json",
            mcp_workspace_root=repo_root,
            mcp_token_root=root / "mcp_tokens",
        ) as rt:
            conv = await rt.context_tree.ensure_active_conversation(work_path=repo_root)

            # ------------------------------------------------------------------
            # Part A: 3 个真实文件附件提交 + OCR 识别 + OPT-3 存储验证 + 溯源核验
            # ------------------------------------------------------------------
            print("\n" + "=" * 72)
            print("PART A: 真实附件提交与 OCR E2E (docs/001.jpg, docs/002.webp, docs/003.pdf)")
            print("=" * 72)

            total_raw_bytes = 0
            for path, mime in sample_files:
                ref = rt.attachment_registry.register_file(
                    path,
                    title=path.name,
                    media_type=mime,
                )
                total_raw_bytes += ref.byte_size
                print(
                    f"\n--- [{path.name}] registered -> {ref.to_reference_handle()} "
                    + f"(sha256={ref.sha256[:12]}, raw={ref.byte_size:,}B) ---"
                )

                t0 = time.monotonic()
                res = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="ocr_analyst",
                    mandate=(
                        f"请使用 inspect_attachment(attachment_id='{ref.attachment_id}', load_binary=True) "
                        + f"加载并识别附件 {ref.to_reference_handle()} 的全部文字内容（OCR）与核心信息，"
                        + "并在回答末尾附上 [[cite:...]] 溯源引用。"
                    ),
                    work_path=str(repo_root),
                )
                await rt.scheduler.wait_all()
                elapsed = time.monotonic() - t0

                ticket = await rt.ticket_store.get_ticket(conv.conversation_id, res.task_id)
                assert ticket is not None
                summary = ticket.result_summary or ""
                citations = extract_and_verify_citations(summary, rt.attachment_registry)
                verified_cnt = sum(1 for c in citations if c.verified)

                print(
                    f"Status: {ticket.status} | Elapsed: {elapsed:.1f}s | "
                    + f"Citations: {verified_cnt}/{len(citations)} verified"
                )
                print("OCR Output:\n" + summary[:1200])
                for c in citations:
                    print(f"  Cite: {c.raw_tag} -> verified={c.verified} ({c.verification_reason})")

            # 检查 SQLite context_entries 中 OPT-3 二进制外置效果
            session_factory = create_session_factory(rt.engine)
            async with session_factory() as session:
                result = await session.execute(select(ContextEntryRow))
                rows = list(result.scalars().all())
                total_json_bytes = sum(len((r.message_json or "").encode("utf-8")) for r in rows)
                ext_uri_count = sum((r.message_json or "").count("media+sha256://") for r in rows)
            blob_files = list((root / "media_blobs").glob("*"))
            print(
                f"\n[OPT-3 Storage Check] Raw attachments={total_raw_bytes:,}B | "
                + f"SQLite context_entries={total_json_bytes:,}B ({len(rows)} rows) | "
                + f"Externalized media+sha256:// refs={ext_uri_count} | "
                + f"media_blobs files={len(blob_files)}"
            )

            # ------------------------------------------------------------------
            # Part B: Monid /v1/image/gpt-image-2 生图 + 引用分析生成图
            # ------------------------------------------------------------------
            print("\n" + "=" * 72)
            print("PART B: Monid /v1/image/gpt-image-2 生图 + 跨 Role 引用分析")
            print("=" * 72)

            monid_data, image_url, png_bytes = await _call_monid_gpt_image_2(
                monid_key, MONID_PROMPT
            )
            saved_path = ARTIFACT_OUTPUT_DIR / "generated_gpt_image_2.png"
            saved_path.parent.mkdir(parents=True, exist_ok=True)
            _ = saved_path.write_bytes(png_bytes)
            print(f"[Monid] Saved generated image to: {saved_path} ({len(png_bytes):,} bytes)")

            gen_ref = rt.attachment_registry.register_bytes(
                png_bytes,
                title="generated_gpt_image_2.png",
                media_type="image/png",
                width=1024,
                height=1024,
                summary="Monid /v1/image/gpt-image-2 generated fine-art portrait in rose garden",
            )
            monid_meta = {
                "provider": "openai",
                "endpoint": "/v1/image/gpt-image-2",
                "run_id": monid_data.get("runId") or monid_data.get("id") or "unknown",
                "status": monid_data.get("status") or "completed",
                "size": "1024x1024",
                "quality": "medium",
                "attachment_id": gen_ref.attachment_id,
                "bytes": len(png_bytes),
                "has_signed_url": image_url is not None,
            }
            art_ref = rt.attachment_registry.save_artifact(
                "monid_gen_result",
                monid_meta,
                description="Monid /v1/image/gpt-image-2 generation metadata",
            )
            print(f"[Registry] Registered generated image: {gen_ref.to_reference_handle()}")
            print(f"[Registry] Registered metadata artifact: {art_ref.to_reference_handle()}")

            t0 = time.monotonic()
            r_vis = await rt.scheduler.dispatch_task(
                main_conversation_id=conv.conversation_id,
                role_name="vision_reviewer",
                mandate=(
                    f"请仅通过轻量引用句柄加载并详细分析生成图 {gen_ref.to_reference_handle()}，"
                    + f"同时读取内部产物 {art_ref.to_reference_handle()} 核对生成参数。"
                    + "请逐项点评构图、人物姿态、灰罗马长裙与蓝金披肩、金色月桂头饰、黑丝绒项圈、"
                    + "象牙大理石喷泉与天使雕像、粉红玫瑰、阴天光线以及写实质感，并附上带 [[cite:...]] 的溯源标注。"
                ),
                work_path=str(repo_root),
            )
            await rt.scheduler.wait_all()
            elapsed = time.monotonic() - t0

            vis_ticket = await rt.ticket_store.get_ticket(conv.conversation_id, r_vis.task_id)
            assert vis_ticket is not None
            vis_summary = vis_ticket.result_summary or ""
            vis_citations = extract_and_verify_citations(vis_summary, rt.attachment_registry)
            vis_verified = sum(1 for c in vis_citations if c.verified)

            print(
                f"\n[Vision Reviewer] Status: {vis_ticket.status} | Elapsed: {elapsed:.1f}s | "
                + f"Citations: {vis_verified}/{len(vis_citations)} verified"
            )
            print("Visual Analysis Output:\n" + vis_summary)
            for c in vis_citations:
                print(f"  Cite: {c.raw_tag} -> verified={c.verified} ({c.verification_reason})")


if __name__ == "__main__":
    asyncio.run(main())
