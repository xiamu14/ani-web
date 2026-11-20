"""Layer 0 跨语言契约 Schema 导出工具（供 TypeScript / Rust typify 生成客户端类型）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from agent.protocol.event_bus import PROTOCOL_EVENT_ADAPTER


def build_events_json_schema() -> dict[str, JsonValue]:
    """生成 ProtocolEvent 判别联合的 JSON Schema (Draft 2020-12)。"""
    raw = PROTOCOL_EVENT_ADAPTER.json_schema()
    raw["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    raw["title"] = "AgentProtocolEvent"
    return cast(dict[str, JsonValue], raw)


def write_events_schema_file(output_path: str | Path) -> Path:
    """将 events.schema.json 原子写入指定路径。"""
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = build_events_json_schema()
    _ = target.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
