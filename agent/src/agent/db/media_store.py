"""OPT-3 / OPT-4 / OPT-6 多媒体与大文本内容寻址存储层（CompressedFileMediaStore）。

1. OPT-3: 已压缩多媒体（image/*, audio/*, video/*, application/pdf）纯二进制 SHA-256 去重直存，零二次压缩。
2. OPT-4: 超大文本/日志/JSON 载荷（text/*, application/json）自动采用 zlib(level=2) 快速压缩外置。
3. OPT-6: 支持窗口化按需还原（recent media_window 完整还原，更早历史轮次自动降级为紧凑文本占位符）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import cast, override

import anyio.to_thread
from pydantic_ai_harness.media._store import (
    MediaContext,
    MediaStore,
    parse_media_uri,
)
from pydantic_ai_harness.media._walker import (
    externalize_media,
    restore_media,
)

DEFAULT_MEDIA_THRESHOLD_BYTES = 64 * 1024
DEFAULT_MEDIA_WINDOW_MESSAGES = 4
_ZLIB_MAGIC_HEADER = b"AGYZLIB1"
_EXTERNAL_MARKER = "__harness_external_media__"
_TEXT_MARKER = "__harness_external_text__"
_URI_KEY = "__harness_external_uri__"
_DEFAULT_CONTEXT = MediaContext()


def _should_compress_payload(media_type: str | None) -> bool:
    """判断载荷是否应当执行 zlib 快速压缩（仅对纯文本/JSON/XML 压缩，跳过已压缩图像/音视频/PDF）。"""
    if media_type is None:
        return False
    lowered = media_type.lower().strip()
    if lowered.startswith("text/"):
        return True
    return lowered in ("application/json", "application/xml", "application/javascript")


class CompressedFileMediaStore(MediaStore):
    """基于本地文件系统的内容寻址（CAS）媒体存储：多媒体原样直存（OPT-3），大文本自动 zlib 压缩（OPT-4）。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        zlib_level: int = 2,
    ) -> None:
        self._root: Path = Path(directory).expanduser().resolve()
        self._zlib_level: int = min(max(1, zlib_level), 9)

    @property
    def directory(self) -> Path:
        return self._root

    def blob_path_for_uri(self, uri: str) -> Path:
        digest = parse_media_uri(uri)
        return self._root / f"{digest}.bin"

    @override
    async def put(self, data: bytes, *, context: MediaContext = _DEFAULT_CONTEXT) -> str:
        return await anyio.to_thread.run_sync(self.put_sync, data, context)

    def put_sync(self, data: bytes, context: MediaContext = _DEFAULT_CONTEXT) -> str:
        """同步写入 CAS 媒体/大文本载荷（UUID 唯一临时文件原子替换 + 精确记录 codec）。"""
        digest = hashlib.sha256(data).hexdigest()
        uri = f"media+sha256://{digest}"
        blob_path = self._root / f"{digest}.bin"
        self._root.mkdir(parents=True, exist_ok=True)

        want_compress = _should_compress_payload(context.media_type)
        used_zlib = False
        if not blob_path.exists():
            if want_compress:
                compressed = _ZLIB_MAGIC_HEADER + zlib.compress(data, level=self._zlib_level)
                if len(compressed) < len(data):
                    payload = compressed
                    used_zlib = True
                else:
                    payload = data
            else:
                payload = data
            tmp_path = self._root / f"{digest}.{uuid.uuid4().hex[:8]}.tmp"
            _ = tmp_path.write_bytes(payload)
            _ = tmp_path.replace(blob_path)
        else:
            try:
                with blob_path.open("rb") as fh:
                    used_zlib = fh.read(len(_ZLIB_MAGIC_HEADER)) == _ZLIB_MAGIC_HEADER
            except OSError:
                used_zlib = False

        meta_dict: dict[str, str] = dict(context.metadata) if context.metadata else {}
        meta_dict["raw_size"] = str(len(data))
        if context.media_type:
            meta_dict["media_type"] = context.media_type
        meta_dict["codec"] = "zlib" if used_zlib else "raw"
        meta_path = blob_path.with_name(blob_path.name + ".meta.json")
        if not meta_path.exists() or context.metadata:
            tmp_meta = self._root / f"{digest}.{uuid.uuid4().hex[:8]}.meta.tmp"
            _ = tmp_meta.write_text(json.dumps(meta_dict, ensure_ascii=False), encoding="utf-8")
            _ = tmp_meta.replace(meta_path)
        return uri

    @override
    async def get(self, uri: str, *, context: MediaContext = _DEFAULT_CONTEXT) -> bytes:
        _ = context
        return await anyio.to_thread.run_sync(self.get_sync, uri)

    def get_sync(self, uri: str) -> bytes:
        """同步读取 CAS 载荷，自动识别 AGYZLIB1 魔数头并解压还原原始字节。"""
        blob_path = self.blob_path_for_uri(uri)
        if not blob_path.is_file():
            raise FileNotFoundError(f"Media not found: {uri}")
        raw = blob_path.read_bytes()
        if raw.startswith(_ZLIB_MAGIC_HEADER):
            return zlib.decompress(raw[len(_ZLIB_MAGIC_HEADER) :])
        return raw

    @override
    async def exists(self, uri: str, *, context: MediaContext = _DEFAULT_CONTEXT) -> bool:
        _ = context
        return await anyio.to_thread.run_sync(self._sync_exists, uri)

    def _sync_exists(self, uri: str) -> bool:
        return self.blob_path_for_uri(uri).is_file()

    @override
    async def public_url(self, uri: str, *, context: MediaContext = _DEFAULT_CONTEXT) -> str | None:
        _ = (uri, context)
        return None

    @override
    async def get_metadata(
        self, uri: str, *, context: MediaContext = _DEFAULT_CONTEXT
    ) -> Mapping[str, str]:
        _ = context
        return await anyio.to_thread.run_sync(self._sync_get_metadata, uri)

    def _sync_get_metadata(self, uri: str) -> Mapping[str, str]:
        blob_path = self.blob_path_for_uri(uri)
        if not blob_path.is_file():
            raise FileNotFoundError(f"Media not found: {uri}")
        meta_path = blob_path.with_name(blob_path.name + ".meta.json")
        if not meta_path.is_file():
            return {}
        loaded: object = json.loads(meta_path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        if isinstance(loaded, dict):
            return {str(k): str(v) for k, v in cast(dict[object, object], loaded).items()}
        return {}


def replace_external_markers_with_stubs(node: object) -> object:
    """OPT-6: 将窗口外旧历史消息中的外置大媒体/大文本 marker 替换为轻量级文本占位符（零磁盘读取）。"""
    if isinstance(node, list):
        return [replace_external_markers_with_stubs(item) for item in cast(list[object], node)]
    if isinstance(node, dict):
        typed = cast(dict[str, object], node)
        if typed.get(_EXTERNAL_MARKER) is True:
            uri = str(typed.get(_URI_KEY) or typed.get("uri") or "media+sha256://unknown")
            is_text = typed.get(_TEXT_MARKER) is True
            if is_text:
                stub_text = f"[Externalized Historical Text ({uri}) — omitted outside active media_window]"
                cleaned: dict[str, object] = {
                    k: replace_external_markers_with_stubs(v)
                    for k, v in typed.items()
                    if not k.startswith("__harness_external_") and k != "uri"
                }
                cleaned["content"] = stub_text
                return cleaned
            # BinaryContent marker -> 替换为轻量 TextContent 摘要，避免向旧轮次注入大二进制
            media_type = str(typed.get("media_type") or "application/octet-stream")
            ident = str(typed.get("identifier") or "")
            id_suffix = f" id={ident}" if ident else ""
            return {
                "kind": "text-content",
                "content": f"[Externalized Historical Media ({media_type}{id_suffix}, {uri}) — omitted outside active media_window]",
            }
        return {k: replace_external_markers_with_stubs(v) for k, v in typed.items()}
    return node


async def externalize_message_tree(
    tree: object,
    *,
    media_store: MediaStore,
    threshold_bytes: int = DEFAULT_MEDIA_THRESHOLD_BYTES,
) -> object:
    """将序列化后的消息 JSON 树中的大二进制/大文本外置到 MediaStore。"""
    return await externalize_media(
        tree,
        media_store=media_store,
        threshold_bytes=threshold_bytes,
    )


async def restore_message_tree(
    tree: object,
    *,
    media_store: MediaStore | None,
    hydrate_media: bool = True,
) -> object:
    """还原消息 JSON 树：hydrate_media=True 时从磁盘完整加载，hydrate_media=False 时降级为紧凑占位符（OPT-6）。"""
    if not hydrate_media or media_store is None:
        return replace_external_markers_with_stubs(tree)
    return await restore_media(tree, media_store=media_store)
