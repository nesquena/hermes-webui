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
_MAX_RESULT_BYTES = 64 * 1024
_MAX_PATCH_BYTES = 64 * 1024
_MAX_RAW_PATHS = 64
_MAX_REFERENCES = 32
_MAX_PATH_BYTES = 4096
_MAX_TOTAL_PATH_BYTES = 16 * 1024
_MAX_TOOL_CALL_ID_BYTES = 256


def _utf8_size(value: str) -> int:
    return len(value.encode('utf-8', 'surrogatepass'))


def _raw_path_string(value) -> str | None:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    return value if isinstance(value, str) else None


def _raw_paths_within_limits(paths) -> bool:
    if not isinstance(paths, list) or len(paths) > _MAX_RAW_PATHS:
        return False
    total = 0
    for raw_path in paths:
        value = _raw_path_string(raw_path)
        if value is None:
            continue
        total += _utf8_size(value)
        if total > _MAX_TOTAL_PATH_BYTES:
            return False
    return True


def _result_object(result):
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    if _utf8_size(result) > _MAX_RESULT_BYTES:
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
    if _utf8_size(body) > _MAX_PATCH_BYTES:
        return []
    paths = []
    for match in re.finditer(
        r'^\*\*\*\s*(?:Update|Add|Delete)\s+File: (.+)$',
        body,
        re.MULTILINE,
    ):
        paths.append(match.group(1))
        if len(paths) > _MAX_RAW_PATHS:
            return []
    for line in body.splitlines():
        prefix = '*** Move File: '
        if not line.startswith(prefix) or ' -> ' not in line:
            continue
        source, destination = line[len(prefix):].split(' -> ', 1)
        paths.extend((source, destination))
        if len(paths) > _MAX_RAW_PATHS:
            return []
    return [path for path in paths if path]


def _workspace_relative_path(workspace, raw_path) -> str | None:
    if not str(workspace or '').strip() or not isinstance(raw_path, (str, os.PathLike)):
        return None
    value = _raw_path_string(raw_path)
    if (
        not value
        or value != value.strip()
        or _utf8_size(value) > _MAX_PATH_BYTES
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
    if tid and _utf8_size(tid) > _MAX_TOOL_CALL_ID_BYTES:
        return []
    if not _raw_paths_within_limits(raw_paths):
        return []
    total_path_bytes = 0
    for raw_path in raw_paths:
        path = _workspace_relative_path(workspace, raw_path)
        if not path or path in seen:
            continue
        if len(references) >= _MAX_REFERENCES:
            return []
        total_path_bytes += _utf8_size(path)
        if total_path_bytes > _MAX_TOTAL_PATH_BYTES:
            return []
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
