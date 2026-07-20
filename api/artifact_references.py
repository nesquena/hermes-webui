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
MAX_ANCHOR_ARTIFACT_REFERENCES = 64
MAX_ANCHOR_ARTIFACT_BYTES = 32 * 1024
_ANCHOR_ARTIFACT_STRING_LIMITS = {
    'kind': 64,
    'path': _MAX_PATH_BYTES,
    'source_tool': 128,
    'tool_call_id': _MAX_TOOL_CALL_ID_BYTES,
}
_ANCHOR_ARTIFACT_EVENT_STRING_LIMITS = {
    'event_id': 512,
    'local_id': 512,
    'session_id': 512,
    'turn_id': 512,
    'run_id': 512,
    'stream_id': 512,
}


def _utf8_size(value: str) -> int:
    return len(value.encode('utf-8', 'surrogatepass'))


def _raw_path_string(value) -> str | None:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    return value if isinstance(value, str) else None


def _bounded_clean_string(value, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value or value != value.strip():
        return None
    if _utf8_size(value) > limit:
        return None
    return value


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


def sanitize_anchor_artifact_payload(payload) -> dict | None:
    """Return the path-only artifact payload allowed in persisted Anchor scenes."""
    if not isinstance(payload, dict):
        return None
    path = _bounded_clean_string(payload.get('path'), _MAX_PATH_BYTES)
    if not path:
        return None
    out = {
        'kind': (
            _bounded_clean_string(payload.get('kind'), _ANCHOR_ARTIFACT_STRING_LIMITS['kind'])
            or 'workspace_file'
        ),
        'path': path,
    }
    for key in ('source_tool', 'tool_call_id'):
        value = _bounded_clean_string(payload.get(key), _ANCHOR_ARTIFACT_STRING_LIMITS[key])
        if value:
            out[key] = value
    return out


def _bounded_event_string(value, key: str) -> str | None:
    return _bounded_clean_string(value, _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS[key])


def _coerce_positive_seq(value):
    try:
        seq = int(value)
    except (TypeError, ValueError):
        return None
    return seq if seq > 0 else None


def anchor_artifact_event_from_payload(
    payload,
    *,
    session_id=None,
    turn_id=None,
    run_id=None,
    stream_id=None,
    event_id=None,
    seq=None,
    created_at=None,
    local_id=None,
) -> dict | None:
    """Build a bounded Anchor artifact event from a path-only payload."""
    clean_payload = sanitize_anchor_artifact_payload(payload)
    if not clean_payload:
        return None
    clean_event_id = _bounded_event_string(event_id, 'event_id')
    clean_session_id = _bounded_event_string(session_id, 'session_id')
    clean_turn_id = _bounded_event_string(turn_id, 'turn_id')
    clean_run_id = _bounded_event_string(run_id, 'run_id')
    clean_stream_id = _bounded_event_string(stream_id, 'stream_id')
    clean_seq = _coerce_positive_seq(seq)
    clean_local_id = (
        _bounded_event_string(local_id, 'local_id')
        or (f'artifact:{clean_event_id}' if clean_event_id else None)
        or (
            f'artifact:{clean_stream_id}:{clean_seq}'
            if clean_stream_id and clean_seq is not None
            else None
        )
        or f'artifact:{clean_payload["path"]}'
    )
    artifact = {
        'event_id': clean_event_id,
        'local_id': clean_local_id,
        'session_id': clean_session_id,
        'turn_id': clean_turn_id,
        'run_id': clean_run_id,
        'stream_id': clean_stream_id,
        'seq': clean_seq,
        'kind': 'artifact_reference',
        'source_event_type': 'artifact_reference',
        'created_at': created_at,
        'status': None,
        'identity': {
            'event_id': clean_event_id,
            'local_id': clean_local_id,
            'session_id': clean_session_id,
            'turn_id': clean_turn_id,
            'run_id': clean_run_id,
            'stream_id': clean_stream_id,
            'seq': clean_seq,
        },
        'payload': clean_payload,
    }
    return artifact


def anchor_artifact_event_from_raw(raw, *, session_id=None, run_id=None, stream_id=None) -> dict | None:
    """Normalize an existing persisted/live artifact object into the bounded event shape."""
    if not isinstance(raw, dict):
        return None
    payload = raw.get('payload') if isinstance(raw.get('payload'), dict) else raw
    identity = raw.get('identity') if isinstance(raw.get('identity'), dict) else {}
    return anchor_artifact_event_from_payload(
        payload,
        session_id=raw.get('session_id') or identity.get('session_id') or session_id,
        turn_id=raw.get('turn_id') or identity.get('turn_id'),
        run_id=raw.get('run_id') or identity.get('run_id') or run_id,
        stream_id=raw.get('stream_id') or identity.get('stream_id') or stream_id,
        event_id=raw.get('event_id') or identity.get('event_id'),
        seq=raw.get('seq') if raw.get('seq') is not None else identity.get('seq'),
        created_at=raw.get('created_at'),
        local_id=raw.get('local_id') or identity.get('local_id'),
    )


def _artifact_dedupe_key(event: dict) -> tuple:
    event_id = event.get('event_id')
    if event_id:
        return ('event_id', event_id)
    run_id = event.get('run_id')
    seq = event.get('seq')
    if run_id and seq is not None:
        return ('run_seq', run_id, str(seq))
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    return (
        'payload',
        payload.get('path'),
        payload.get('source_tool'),
        payload.get('tool_call_id'),
    )


def bound_anchor_artifact_events(
    events,
    *,
    session_id=None,
    run_id=None,
    stream_id=None,
    max_count: int = MAX_ANCHOR_ARTIFACT_REFERENCES,
    max_bytes: int = MAX_ANCHOR_ARTIFACT_BYTES,
) -> list[dict]:
    """Return a deterministic bounded prefix of valid Anchor artifact events."""
    out: list[dict] = []
    seen = set()
    total = 0
    for raw in events if isinstance(events, list) else []:
        event = anchor_artifact_event_from_raw(
            raw,
            session_id=session_id,
            run_id=run_id,
            stream_id=stream_id,
        )
        if not event:
            continue
        key = _artifact_dedupe_key(event)
        if key in seen:
            continue
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')
        if len(out) >= max_count or total + len(encoded) > max_bytes:
            break
        seen.add(key)
        total += len(encoded)
        out.append(event)
    return out


def bound_anchor_activity_scene_artifacts(scene):
    """Defensively cap scene artifacts without rejecting the whole Anchor scene."""
    if not isinstance(scene, dict):
        return scene
    next_scene = dict(scene)
    next_scene['artifacts'] = bound_anchor_artifact_events(scene.get('artifacts') or [])
    return next_scene
