"""规则边界加载器: 仅读取全局设定或所选工作目录自身的 AGENTS.md。"""

from __future__ import annotations

from pathlib import Path

AGENTS_FILENAME = "AGENTS.md"


def normalize_workspace_path(path: str | Path) -> Path:
    """将工作区路径解析为规范化绝对目录路径，不存在或非目录时抛出 ValueError。"""
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"工作区路径不存在或不是目录: {resolved}")
    return resolved


def read_agents_file(directory: Path) -> str | None:
    """只读取指定目录自身的 AGENTS.md，不向上级父目录继承。"""
    candidate = directory / AGENTS_FILENAME
    if not candidate.is_file():
        return None
    try:
        content = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取工作区规则文件 {candidate}: {exc}") from exc
    return content or None


def load_workspace_agents_md(work_path: str | Path) -> str | None:
    """规范化工作区路径并读取该目录自身的 AGENTS.md。"""
    directory = normalize_workspace_path(work_path)
    return read_agents_file(directory)


def load_global_instructions(global_agents_path: str | Path | None = None) -> str | None:
    """读取应用级全局 AGENTS.md（若指定路径存在且非空）。"""
    if global_agents_path is None:
        return None
    target = Path(global_agents_path).expanduser().resolve()
    if not target.is_file():
        return None
    try:
        content = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取全局规则文件 {target}: {exc}") from exc
    return content or None
