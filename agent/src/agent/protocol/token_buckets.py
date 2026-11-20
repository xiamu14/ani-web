"""Layer 0/1 Context Window 9 桶 Token 估算与分类器（基于 tiktoken + LRU 缓存）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from functools import lru_cache

import tiktoken
from pydantic_ai.messages import (
    AudioUrl,
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    RetryFeedbackPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UploadedFile,
    UserPromptPart,
    VideoUrl,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from agent.protocol.models import ContextBucketCategory, ContextSavingsView

BUCKET_ORDER: tuple[ContextBucketCategory, ...] = (
    "system_prompt",
    "environment_info",
    "skills",
    "mcp",
    "builtin_tools",
    "memory",
    "attachments",
    "messages",
    "overhead",
)

_MODEL_LIMIT_RULES: tuple[tuple[str, int], ...] = (
    ("gpt-4o", 128_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-5", 400_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("claude-3", 200_000),
    ("claude-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-opus-4", 200_000),
    ("gemini-2", 1_000_000),
    ("gemini-3", 1_000_000),
    ("deepseek", 128_000),
    ("qwen", 128_000),
)

_MEMORY_TOOL_NAMES: frozenset[str] = frozenset(
    {"memory_search", "memory_timeline", "get_observations", "save_memory"}
)
_ATTACHMENT_TOOL_NAMES: frozenset[str] = frozenset(
    {"inspect_attachment", "register_attachment", "list_attachments"}
)
_ENV_TAGS: tuple[str, ...] = (
    "[Workspace Active:",
    "<ticket_report",
    "<steer_instruction>",
    "<user_interjection>",
    "<budget_wrapup_warning>",
)


def estimate_model_context_limit(model_name: str) -> tuple[int | None, bool]:
    lowered = model_name.lower()
    for prefix, limit in _MODEL_LIMIT_RULES:
        if prefix in lowered:
            return limit, True
    return None, False


def encoding_name_for_model(model_name: str) -> str:
    lowered = model_name.lower()
    if any(k in lowered for k in ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4")):
        return "o200k_base"
    return "cl100k_base"


_ENCODER_CACHE: dict[str, tiktoken.Encoding | None] = {}


def _get_encoder(encoding_name: str) -> tiktoken.Encoding | None:
    if encoding_name in _ENCODER_CACHE:
        return _ENCODER_CACHE[encoding_name]
    try:
        enc = tiktoken.get_encoding(encoding_name)
    except (OSError, ValueError, KeyError, RuntimeError):
        enc = None
    except Exception:  # noqa: BLE001  # 离线/代理拦截时兜底，避免 tiktoken 下载失败阻断推理
        enc = None
    _ENCODER_CACHE[encoding_name] = enc
    return enc


def _offline_estimate_tokens(text_val: str) -> int:
    cjk_count = sum(1 for ch in text_val if "\u4e00" <= ch <= "\u9fff")
    other_count = len(text_val) - cjk_count
    return max(1, cjk_count + (other_count + 3) // 4)


@lru_cache(maxsize=4096)
def _count_tokens_cached(encoding_name: str, _digest: str, text_val: str) -> int:
    enc = _get_encoder(encoding_name)
    if enc is None:
        return _offline_estimate_tokens(text_val)
    return len(enc.encode(text_val, disallowed_special=()))


def count_tokens_text(text_val: str, *, model_name: str = "") -> tuple[int, str]:
    enc_name = encoding_name_for_model(model_name)
    if not text_val:
        return 0, enc_name
    digest = hashlib.sha256(text_val.encode("utf-8", errors="replace")).hexdigest()[:16]
    return _count_tokens_cached(enc_name, digest, text_val), enc_name


def _estimate_media_tokens(obj: object) -> int:
    if isinstance(obj, ImageUrl):
        return 765
    if isinstance(obj, (AudioUrl, VideoUrl)):
        return 1200
    if isinstance(obj, (DocumentUrl, UploadedFile)):
        return 1000
    if isinstance(obj, BinaryContent):
        if obj.media_type.startswith("image/"):
            return 765
        return max(256, min(4096, len(obj.data) // 64))
    return 256


class BucketAccumulator:
    """单次 ModelRequest 的 9 桶 Token 累加器。"""

    def __init__(self, enc_name: str, model_name: str) -> None:
        self.enc_name: str = enc_name
        self.model_name: str = model_name
        self.totals: dict[ContextBucketCategory, int] = {k: 0 for k in BUCKET_ORDER}
        self.sub_items: dict[ContextBucketCategory, dict[str, int]] = {
            k: {} for k in BUCKET_ORDER
        }
        self.savings: ContextSavingsView = ContextSavingsView()

    def add_text(self, cat: ContextBucketCategory, sub_name: str, text_val: str) -> None:
        if not text_val:
            return
        tok, _ = count_tokens_text(text_val, model_name=self.model_name)
        self.add_tokens(cat, sub_name, tok)

    def add_tokens(self, cat: ContextBucketCategory, sub_name: str, tokens: int) -> None:
        if tokens <= 0:
            return
        self.totals[cat] += tokens
        sub = self.sub_items[cat]
        sub[sub_name] = sub.get(sub_name, 0) + tokens


def _classify_text_block(
    acc: BucketAccumulator,
    text_val: str,
    *,
    default_cat: ContextBucketCategory,
    default_sub: str,
) -> None:
    if any(tag in text_val for tag in _ENV_TAGS):
        acc.add_text("environment_info", "runtime_env_or_notice", text_val)
        return
    if "[L2 File Memory Timeline:" in text_val or "## [L0" in text_val or "## [L1" in text_val:
        acc.add_text("memory", "memory_context", text_val)
        return
    if "[Externalized Historical " in text_val or "[[attachment:" in text_val:
        acc.add_text("attachments", "attachment_refs_or_stubs", text_val)
        if "[Externalized Historical " in text_val:
            acc.savings = acc.savings.model_copy(
                update={"opt6_omitted_media_count": acc.savings.opt6_omitted_media_count + 1}
            )
        return
    acc.add_text(default_cat, default_sub, text_val)


def _is_mcp_tool(
    tool_name: str, tool_def: ToolDefinition | None, mcp_prefixes: tuple[str, ...]
) -> bool:
    if (
        tool_def is not None
        and isinstance(tool_def.metadata, dict)
        and tool_def.metadata.get("is_mcp_tool")
    ):
        return True
    return any(tool_name.startswith(p) for p in mcp_prefixes)


def estimate_context_buckets(
    request_context: ModelRequestContext,
    *,
    model_name: str,
    mcp_prefixes: tuple[str, ...] = (),
) -> tuple[BucketAccumulator, str]:
    """对即将发送给 LLM 的 ModelRequestContext 执行 9 桶分类计数。"""
    enc_name = encoding_name_for_model(model_name)
    acc = BucketAccumulator(enc_name, model_name)
    params = request_context.model_request_parameters

    for ipart in params.instruction_parts or ():
        raw_part_id: object = getattr(ipart, "id", None) or getattr(ipart, "name", None) or ""
        part_id = str(raw_part_id) if raw_part_id else ""
        content = getattr(ipart, "content", "")
        if not isinstance(content, str):
            continue
        if part_id.startswith("skill:"):
            acc.add_text("skills", part_id, content)
        elif part_id.startswith("mcp:"):
            acc.add_text("mcp", part_id, content)
        elif part_id.startswith("memory:") or "[L0" in content or "[L1" in content:
            acc.add_text("memory", "l0_l1_instructions", content)
        elif any(tag in content for tag in _ENV_TAGS):
            acc.add_text("environment_info", "dynamic_instructions", content)
        else:
            acc.add_text("system_prompt", part_id or "agent_instructions", content)

    all_tools: Sequence[object] = [
        *(params.function_tools or ()),
        *(params.native_tools or ()),
    ]
    tool_mcp_map: dict[str, bool] = {}
    for td in all_tools:
        td_name = str(getattr(td, "name", None) or getattr(td, "kind", "native_tool"))
        td_def = td if isinstance(td, ToolDefinition) else None
        is_mcp = _is_mcp_tool(td_name, td_def, mcp_prefixes)
        tool_mcp_map[td_name] = is_mcp
        schema_str = json.dumps(
            {
                "name": td_name,
                "description": str(getattr(td, "description", "") or ""),
                "params": getattr(td, "parameters_json_schema", {}),
            },
            ensure_ascii=False,
        )
        if bool(getattr(td, "defer_loading", False)):
            tok, _ = count_tokens_text(schema_str, model_name=model_name)
            if is_mcp:
                acc.savings = acc.savings.model_copy(
                    update={
                        "deferred_mcp_tools_count": acc.savings.deferred_mcp_tools_count + 1,
                        "deferred_mcp_tokens_saved_est": acc.savings.deferred_mcp_tokens_saved_est + tok,
                    }
                )
            continue
        acc.add_text(
            "mcp" if is_mcp else "builtin_tools", f"schema:{td_name}", schema_str
        )

    raw_deferred = getattr(params, "deferred_capabilities", None)
    deferred_caps = raw_deferred if isinstance(raw_deferred, Sequence) else ()
    if deferred_caps:
        acc.savings = acc.savings.model_copy(
            update={
                "deferred_skills_count": len(deferred_caps),
                "deferred_skills_tokens_saved_est": len(deferred_caps) * 400,
            }
        )

    for msg in request_context.messages:
        for part in msg.parts:
            if isinstance(part, SystemPromptPart):
                _classify_text_block(
                    acc, str(part.content), default_cat="system_prompt", default_sub="system_message"
                )
            elif isinstance(part, UserPromptPart):
                if isinstance(part.content, str):
                    _classify_text_block(
                        acc, part.content, default_cat="messages", default_sub="user_text"
                    )
                else:
                    for item in part.content:
                        if isinstance(item, str):
                            _classify_text_block(
                                acc, item, default_cat="messages", default_sub="user_text"
                            )
                        else:
                            acc.add_tokens("attachments", type(item).__name__, _estimate_media_tokens(item))
            elif isinstance(part, TextPart):
                acc.add_text("messages", "assistant_text", part.content)
            elif isinstance(part, ThinkingPart):
                acc.add_text("messages", "thinking", part.content)
            elif isinstance(part, ToolCallPart):
                call_str = f"{part.tool_name}({part.args_as_json_str()})"
                if tool_mcp_map.get(part.tool_name) or _is_mcp_tool(part.tool_name, None, mcp_prefixes):
                    acc.add_text("mcp", f"call:{part.tool_name}", call_str)
                else:
                    acc.add_text("messages", "tool_calls", call_str)
            elif isinstance(part, (ToolReturnPart, RetryFeedbackPart)):
                tname = getattr(part, "tool_name", None) or ""
                content_str = str(part.content)
                if tname in _MEMORY_TOOL_NAMES:
                    acc.add_text("memory", f"l3:{tname}", content_str)
                elif tname in _ATTACHMENT_TOOL_NAMES:
                    acc.add_text("attachments", f"tool:{tname}", content_str)
                elif tool_mcp_map.get(tname) or _is_mcp_tool(tname, None, mcp_prefixes):
                    acc.add_text("mcp", f"return:{tname}", content_str)
                else:
                    _classify_text_block(
                        acc, content_str, default_cat="messages", default_sub="tool_results"
                    )

    return acc, enc_name
