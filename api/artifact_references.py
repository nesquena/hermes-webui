"""Derive bounded workspace artifact references from file-tool results."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


_MUTATION_TOOL_NAMES = frozenset({'write_file', 'patch'})
_IGNORED_ANY_PATH_PARTS = frozenset({
    '.git', '.hg', '.svn', '.venv', 'venv', '__pycache__',
    'node_modules', '.next', '.cache',
})
_IGNORED_ROOT_PATH_PARTS = frozenset({'dist', 'build'})


def _result_object(result):
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result.strip())
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mutation_landed(tool_name, result) -> bool:
    name = str(tool_name or '').removeprefix('functions.')
    if name not in _MUTATION_TOOL_NAMES:
        return False
    data = _result_object(result)
    if not data or data.get('error'):
        return False
    if name == 'write_file':
        return 'bytes_written' in data
    return data.get('success') is True


def _target_paths(tool_name, args) -> list[str]:
    """Extract fallback paths from canonical file-tool arguments."""
    name = str(tool_name or '').removeprefix('functions.')
    if name not in _MUTATION_TOOL_NAMES or not isinstance(args, dict):
        return []
    if name == 'write_file':
        return [str(args['path'])] if args.get('path') else []
    mode = str(args.get('mode') or 'replace')
    if mode == 'replace':
        return [str(args['path'])] if args.get('path') else []
    if mode != 'patch' or not isinstance(args.get('patch'), str):
        return []
    body = args['patch']
    paths = []
    for match in re.finditer(
        r'^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$',
        body,
        re.MULTILINE,
    ):
        paths.append(match.group(1).strip())
    for match in re.finditer(
        r'^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$',
        body,
        re.MULTILINE,
    ):
        paths.extend((match.group(1).strip(), match.group(2).strip()))
    return [path for path in paths if path]


def _workspace_relative_path(workspace, raw_path) -> str | None:
    if not str(workspace or '').strip() or not isinstance(raw_path, (str, os.PathLike)):
        return None
    value = str(raw_path or '').strip()
    if (
        not value
        or len(value) > 4096
        or '\x00' in value
        or '\n' in value
        or '\r' in value
        or '://' in value
    ):
        return None
    # A foreign drive path is relative on POSIX and must not be misreported as
    # a file inside the local workspace. Native Windows Path handles it below.
    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', value):
        return None
    try:
        root = Path(str(workspace)).expanduser().resolve()
        candidate = Path(value).expanduser()
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    lowered_parts = tuple(part.lower() for part in relative.parts)
    if (
        not lowered_parts
        or any(part in _IGNORED_ANY_PATH_PARTS for part in lowered_parts)
        or lowered_parts[0] in _IGNORED_ROOT_PATH_PARTS
    ):
        return None
    return relative.as_posix()


def derive_file_artifact_references(
    tool_name,
    args,
    result,
    workspace,
    *,
    tool_call_id=None,
) -> list[dict]:
    """Return path-only Anchor payloads for a proven workspace mutation."""
    name = str(tool_name or '').removeprefix('functions.')
    if not _mutation_landed(name, result):
        return []
    data = _result_object(result) or {}
    reported = data.get('files_modified')
    if isinstance(reported, list) and reported:
        raw_paths = reported
    elif data.get('resolved_path'):
        raw_paths = [data['resolved_path']]
    else:
        raw_paths = _target_paths(name, args)
    references = []
    seen = set()
    tid = str(tool_call_id or '').strip()
    for raw_path in raw_paths:
        path = _workspace_relative_path(workspace, raw_path)
        if not path or path in seen:
            continue
        seen.add(path)
        reference = {
            'kind': 'workspace_file',
            'path': path,
            'source_tool': name,
        }
        if tid:
            reference['tool_call_id'] = tid
        references.append(reference)
    return references
