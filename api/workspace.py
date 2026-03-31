"""
Hermes Co-Work Web UI -- Workspace and file system helpers.
"""
import json
import os
from pathlib import Path

from api.config import (
    WORKSPACES_FILE, LAST_WORKSPACE_FILE, DEFAULT_WORKSPACE,
    MAX_FILE_BYTES, IMAGE_EXTS, MD_EXTS
)


def load_workspaces() -> list:
    if WORKSPACES_FILE.exists():
        try:
            return json.loads(WORKSPACES_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return [{'path': str(DEFAULT_WORKSPACE), 'name': 'default'}]


def save_workspaces(workspaces: list):
    WORKSPACES_FILE.write_text(json.dumps(workspaces, ensure_ascii=False, indent=2), encoding='utf-8')


def get_last_workspace() -> str:
    if LAST_WORKSPACE_FILE.exists():
        try:
            p = LAST_WORKSPACE_FILE.read_text(encoding='utf-8').strip()
            if p and Path(p).is_dir():
                return p
        except Exception:
            pass
    return str(DEFAULT_WORKSPACE)


def set_last_workspace(path: str):
    try:
        LAST_WORKSPACE_FILE.write_text(str(path), encoding='utf-8')
    except Exception:
        pass


def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal."""
    resolved = (root / requested).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def list_dir(workspace: Path, rel='.'):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        entries.append({
            'name': item.name,
            'path': str(item.relative_to(workspace)),
            'type': 'dir' if item.is_dir() else 'file',
            'size': item.stat().st_size if item.is_file() else None,
        })
        if len(entries) >= 200:
            break
    return entries


def read_file_content(workspace: Path, rel: str):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {rel}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File too large ({size} bytes, max {MAX_FILE_BYTES})")
    content = target.read_text(encoding='utf-8', errors='replace')
    return {'path': rel, 'content': content, 'size': size, 'lines': content.count('\n') + 1}
