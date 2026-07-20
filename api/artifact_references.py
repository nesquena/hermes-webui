"""Derive bounded workspace artifact references from file-tool results."""

from __future__ import annotations

import json
import os
import re
import copy
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
_INVALID_OWNER_CLAIM = object()


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


def _owner_claim_value(container: dict, key: str):
    limit = _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS.get(key, 512)
    if not isinstance(container, dict) or key not in container or container.get(key) is None:
        return None
    value = _bounded_clean_string(container.get(key), limit)
    return value if value else _INVALID_OWNER_CLAIM


def _anchor_owner_claim(raw: dict, identity: dict, key: str):
    return (
        _owner_claim_value(raw, key),
        _owner_claim_value(identity, key),
    )


def anchor_artifact_owner_mismatch(
    raw,
    *,
    session_id=None,
    run_id=None,
    stream_id=None,
    require_owner_authority: bool = False,
) -> str | None:
    """Return the first explicit owner field that conflicts with the expected owner."""
    if not isinstance(raw, dict):
        return None
    identity = raw.get('identity') if isinstance(raw.get('identity'), dict) else {}
    expected_session_id = _bounded_clean_string(
        session_id,
        _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS['session_id'],
    )
    expected_run_id = _bounded_clean_string(
        run_id,
        _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS['run_id'],
    )
    expected_stream_id = _bounded_clean_string(
        stream_id,
        _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS['stream_id'],
    )
    raw_session_id, identity_session_id = _anchor_owner_claim(raw, identity, 'session_id')
    if raw_session_id is _INVALID_OWNER_CLAIM or identity_session_id is _INVALID_OWNER_CLAIM:
        return 'session_id'
    if raw_session_id and identity_session_id and raw_session_id != identity_session_id:
        return 'session_id'
    explicit_session_id = raw_session_id or identity_session_id
    if explicit_session_id and expected_session_id and explicit_session_id != expected_session_id:
        return 'session_id'
    raw_run_id, identity_run_id = _anchor_owner_claim(raw, identity, 'run_id')
    if raw_run_id is _INVALID_OWNER_CLAIM or identity_run_id is _INVALID_OWNER_CLAIM:
        return 'run_id'
    if raw_run_id and identity_run_id and raw_run_id != identity_run_id:
        return 'run_id'
    explicit_run_id = raw_run_id or identity_run_id
    if explicit_run_id and require_owner_authority and not expected_run_id:
        return 'run_id'
    if explicit_run_id and expected_run_id and explicit_run_id != expected_run_id:
        return 'run_id'
    raw_stream_id, identity_stream_id = _anchor_owner_claim(raw, identity, 'stream_id')
    if raw_stream_id is _INVALID_OWNER_CLAIM or identity_stream_id is _INVALID_OWNER_CLAIM:
        return 'stream_id'
    if raw_stream_id and identity_stream_id and raw_stream_id != identity_stream_id:
        return 'stream_id'
    explicit_stream_id = raw_stream_id or identity_stream_id
    if explicit_stream_id and require_owner_authority and not expected_stream_id:
        return 'stream_id'
    if explicit_stream_id and expected_stream_id and explicit_stream_id != expected_stream_id:
        # Durable run ids can outlive a transport stream rotation. Only allow a
        # stream mismatch when the caller supplied a distinct durable run owner
        # and the event explicitly matches it.
        same_durable_run = (
            explicit_run_id
            and expected_run_id
            and expected_stream_id
            and expected_run_id != expected_stream_id
            and explicit_run_id == expected_run_id
        )
        if not same_durable_run:
            return 'stream_id'
    return None


def anchor_activity_scene_owner_mismatch(
    scene,
    *,
    session_id=None,
    run_id=None,
    stream_id=None,
    require_artifact_owner_authority: bool = False,
) -> str | None:
    """Return the first explicit scene/artifact owner mismatch, if any."""
    if not isinstance(scene, dict):
        return None
    identity = scene.get('identity') if isinstance(scene.get('identity'), dict) else {}
    artifacts = scene.get('artifacts') if isinstance(scene.get('artifacts'), list) else []
    if identity:
        mismatch = anchor_artifact_owner_mismatch(
            {'identity': identity},
            session_id=session_id,
            run_id=run_id,
            stream_id=stream_id,
            require_owner_authority=require_artifact_owner_authority and bool(artifacts),
        )
        if mismatch:
            return f'identity.{mismatch}'
    for raw in artifacts:
        mismatch = anchor_artifact_owner_mismatch(
            raw,
            session_id=session_id,
            run_id=run_id,
            stream_id=stream_id,
            require_owner_authority=require_artifact_owner_authority,
        )
        if mismatch:
            return f'artifact.{mismatch}'
    return None


def bound_anchor_artifact_events(
    events,
    *,
    session_id=None,
    run_id=None,
    stream_id=None,
    max_count: int = MAX_ANCHOR_ARTIFACT_REFERENCES,
    max_bytes: int = MAX_ANCHOR_ARTIFACT_BYTES,
    reject_owner_mismatch: bool = False,
    require_owner_authority: bool = False,
) -> list[dict]:
    """Return a deterministic bounded prefix of valid Anchor artifact events."""
    out: list[dict] = []
    seen = set()
    total = 0
    for raw in events if isinstance(events, list) else []:
        if reject_owner_mismatch:
            mismatch = anchor_artifact_owner_mismatch(
                raw,
                session_id=session_id,
                run_id=run_id,
                stream_id=stream_id,
                require_owner_authority=require_owner_authority,
            )
            if mismatch:
                raise ValueError(f'artifact owner mismatch: {mismatch}')
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


def bound_anchor_activity_scene_artifacts(
    scene,
    *,
    session_id=None,
    run_id=None,
    stream_id=None,
    reject_owner_mismatch: bool = False,
    require_owner_authority: bool = False,
):
    """Defensively cap scene artifacts without rejecting the whole Anchor scene."""
    if not isinstance(scene, dict):
        return scene
    next_scene = dict(scene)
    next_scene['artifacts'] = bound_anchor_artifact_events(
        scene.get('artifacts') or [],
        session_id=session_id,
        run_id=run_id,
        stream_id=stream_id,
        reject_owner_mismatch=reject_owner_mismatch,
        require_owner_authority=require_owner_authority,
    )
    return next_scene


def _scene_list(scene: dict, key: str) -> list:
    value = scene.get(key)
    return copy.deepcopy(value) if isinstance(value, list) else []


def _scene_dict(scene: dict, key: str) -> dict:
    value = scene.get(key)
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def merge_anchor_activity_scene(
    existing_scene,
    incoming_scene,
    *,
    session_id=None,
    run_id=None,
    stream_id=None,
    terminal_state=None,
    final_answer=None,
    final_message_ref=None,
    turn_duration=None,
    reject_owner_mismatch: bool = False,
    owner_session_id=None,
    owner_run_id=None,
    owner_stream_id=None,
    require_owner_authority: bool = False,
) -> dict:
    """Merge browser and worker Anchor scenes with shared artifact ownership rules."""
    existing = copy.deepcopy(existing_scene) if isinstance(existing_scene, dict) else {}
    incoming = copy.deepcopy(incoming_scene) if isinstance(incoming_scene, dict) else {}
    validation_session_id = owner_session_id if owner_session_id is not None else session_id
    validation_run_id = owner_run_id if owner_run_id is not None else run_id
    validation_stream_id = owner_stream_id if owner_stream_id is not None else stream_id
    if reject_owner_mismatch:
        for scene in (existing, incoming):
            mismatch = anchor_activity_scene_owner_mismatch(
                scene,
                session_id=validation_session_id,
                run_id=validation_run_id,
                stream_id=validation_stream_id,
                require_artifact_owner_authority=require_owner_authority,
            )
            if mismatch:
                raise ValueError(f'anchor scene owner mismatch: {mismatch}')
    merged = dict(existing)
    merged.update(incoming)
    merged['version'] = 'activity_scene_v1'
    merged['mode'] = incoming.get('mode') or existing.get('mode') or 'compact_worklog'

    identity = _scene_dict(incoming, 'identity')
    identity.update(_scene_dict(existing, 'identity'))
    clean_session_id = _bounded_clean_string(
        session_id,
        _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS['session_id'],
    )
    clean_run_id = _bounded_clean_string(
        run_id,
        _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS['run_id'],
    )
    clean_stream_id = _bounded_clean_string(
        stream_id,
        _ANCHOR_ARTIFACT_EVENT_STRING_LIMITS['stream_id'],
    )
    if clean_session_id:
        identity['session_id'] = clean_session_id
    if clean_run_id:
        identity['run_id'] = clean_run_id
    elif clean_stream_id and not identity.get('run_id'):
        identity['run_id'] = clean_stream_id
    if clean_stream_id:
        identity['stream_id'] = clean_stream_id
    source_refs = [
        str(item)
        for item in identity.get('source_message_refs', [])
        if str(item or '').strip()
    ] if isinstance(identity.get('source_message_refs'), list) else []
    if final_message_ref and final_message_ref not in source_refs:
        source_refs.append(final_message_ref)
    identity['source_message_refs'] = source_refs
    merged['identity'] = identity

    lifecycle = _scene_dict(incoming, 'lifecycle')
    lifecycle.update(_scene_dict(existing, 'lifecycle'))
    if terminal_state:
        lifecycle['status'] = terminal_state
        lifecycle['terminal_state'] = terminal_state
        merged['terminal_state'] = terminal_state
    elif existing.get('terminal_state'):
        merged['terminal_state'] = existing.get('terminal_state')
    elif incoming.get('terminal_state'):
        merged['terminal_state'] = incoming.get('terminal_state')
    merged['lifecycle'] = lifecycle

    if isinstance(final_answer, str):
        merged['final_answer'] = final_answer
    elif isinstance(incoming.get('final_answer'), str):
        merged['final_answer'] = incoming.get('final_answer')
    elif isinstance(existing.get('final_answer'), str):
        merged['final_answer'] = existing.get('final_answer')
    if final_message_ref:
        merged['final_message_ref'] = final_message_ref
    elif existing.get('final_message_ref') and not incoming.get('final_message_ref'):
        merged['final_message_ref'] = existing.get('final_message_ref')
    if turn_duration is not None and merged.get('turn_duration') is None:
        merged['turn_duration'] = turn_duration

    merged['activity_rows'] = (
        _scene_list(incoming, 'activity_rows')
        if isinstance(incoming.get('activity_rows'), list)
        else _scene_list(existing, 'activity_rows')
    )
    merged['side_effects'] = (
        _scene_list(incoming, 'side_effects')
        if isinstance(incoming.get('side_effects'), list)
        else _scene_list(existing, 'side_effects')
    )
    merged['artifacts'] = bound_anchor_artifact_events(
        [*_scene_list(existing, 'artifacts'), *_scene_list(incoming, 'artifacts')],
        session_id=validation_session_id if reject_owner_mismatch else clean_session_id,
        run_id=validation_run_id if reject_owner_mismatch else (clean_run_id or clean_stream_id),
        stream_id=validation_stream_id if reject_owner_mismatch else clean_stream_id,
        reject_owner_mismatch=reject_owner_mismatch,
        require_owner_authority=require_owner_authority,
    )
    return merged
