"""Profile-scoped model shortcuts: journal-derived usage and atomic pins.

Only model identities, counts and timestamps leave this module. Legacy journals
lack source metadata; only native writable WebUI sessions qualify for fallback.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

PERIOD_DAYS = 30
LIMIT = MAX_PINS = 6
MAX_PROVIDER_LEN = 100
MAX_MODEL_LEN = 300
_scan_cache = {}
_scan_lock = threading.RLock()
_pin_lock = threading.RLock()
logger = logging.getLogger(__name__)
_SAFE_ID = re.compile(r'^[A-Za-z0-9_.-]+$')


class PreferencesError(ValueError):
    """Invalid request or damaged preferences; never overwrite damaged data."""


def _request_profile():
    from api.profiles import get_active_profile_name
    return get_active_profile_name() or 'default'


def _canonical_profile_owner(profile):
    """Canonical owner identity for stored preferences.

    api.profiles treats a renamed root profile as the SAME owner as the legacy
    'default' alias (#1612: ``_is_root_profile`` / ``_profiles_match``).  Keying
    the pin store on the raw request string instead would silently switch stores
    the moment the root profile is renamed — the old file stays on disk, the new
    code just reads a different path, and every existing pin looks lost.  Resolve
    the alias through the canonical contract so both names share one store.
    """
    name = profile.strip() if isinstance(profile, str) and profile.strip() else 'default'
    from api.profiles import _is_root_profile
    return 'default' if _is_root_profile(name) else name


def _state_dir():
    from api.config import STATE_DIR
    return Path(STATE_DIR)


def _pins_file(state_dir):
    profile = _request_profile()
    if not isinstance(profile, str) or not _SAFE_ID.fullmatch(profile) or profile in ('.', '..'):
        raise PreferencesError('Invalid profile')
    owner = _canonical_profile_owner(profile)
    root = Path(state_dir)
    if owner != 'default':
        root = root / 'model_preferences' / owner
    return root / 'model_preferences.json'


def _is_valid_model_identity(model, provider):
    return (isinstance(model, str) and isinstance(provider, str)
            and 0 < len(model.strip()) <= MAX_MODEL_LEN
            and 0 < len(provider.strip()) <= MAX_PROVIDER_LEN
            and not any(ord(c) < 32 for c in model + provider))


def _normalize_model(model, provider):
    model = model.strip()
    for prefix in (f'@{provider}:', f'{provider}/'):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _identity(model, provider):
    if not _is_valid_model_identity(model, provider):
        raise PreferencesError('model and provider must be non-empty strings')
    provider = provider.strip()
    model = _normalize_model(model, provider)
    if not _is_valid_model_identity(model, provider):
        raise PreferencesError('Invalid model identity')
    return model, provider


def _read_pins_path(path):
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
        items = data['pins']
        if not isinstance(items, list) or len(items) > MAX_PINS:
            raise ValueError()
        pins, seen = [], set()
        for p in items:
            model, provider = _identity(p['model'], p['provider'])
            if (model, provider) in seen:
                raise ValueError()
            seen.add((model, provider))
            pins.append({'model': model, 'provider': provider})
        return pins
    except (ValueError, TypeError, KeyError):
        raise PreferencesError('Saved model preferences are damaged; existing file was preserved') from None


def read_pins(state_dir):
    return _read_pins_path(_pins_file(state_dir))


@contextmanager
def _locked_path(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _pin_lock:
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            try:
                import fcntl
            except ImportError:  # Windows: process-level RLock still serializes.
                fcntl = None
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_pins_path(path, pins):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'pins': pins}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        if hasattr(os, 'O_DIRECTORY'):
            dfd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def set_pin(state_dir, model, provider, pinned):
    model, provider = _identity(model, provider)
    if not isinstance(pinned, bool):
        raise PreferencesError('pinned must be a boolean')
    path = _pins_file(state_dir)
    with _locked_path(path):
        pins = _read_pins_path(path)
        entry = {'model': model, 'provider': provider}
        if pinned and entry not in pins:
            if len(pins) >= MAX_PINS:
                raise PreferencesError('At most 6 models can be pinned; unpin one first')
            pins.append(entry)
        elif not pinned and entry in pins:
            pins.remove(entry)
        _write_pins_path(path, pins)
        return pins


def _native_writable_sessions(session_dir, profile):
    from api.profiles import _profiles_match
    try:
        entries = json.loads((session_dir / '_index.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return set()
    if not isinstance(entries, list):
        raise PreferencesError('Invalid session index')
    result = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        sid = e.get('session_id')
        if not isinstance(sid, str) or not _SAFE_ID.fullmatch(sid) or sid in ('.', '..'):
            continue
        # Alias match, not a raw string compare: a renamed root profile owns the
        # same sessions the legacy 'default' tag names (api/profiles contract).
        if not _profiles_match(e.get('profile'), profile):
            continue
        # A parent link is lineage provenance, not automation provenance:
        # /api/session/branch (and the compression-recovery continuation) create
        # an ordinary writable WebUI session that carries parent_session_id with
        # session_source="fork". Automation children that also carry a parent
        # link stay excluded via read_only / is_cli_session / the source check
        # below (subagent children are recovered view-only, CLI imports are CLI).
        if e.get('read_only') or e.get('is_cli_session'):
            continue
        sources = [e.get(k) for k in ('source_tag', 'raw_source', 'session_source')]
        if any(s and s not in ('webui', 'fork') for s in sources):
            continue
        if (session_dir / f'{sid}.json').is_file():
            result.add(sid)
    return result


def _dedupe_key(sid, event):
    key = event.get('stream_id') or event.get('turn_id')
    return (sid, key) if isinstance(key, str) and key else None


# Runtime-injected messages: notifier headers and cron notices. Matched only as a
# leading header so ordinary prose that merely mentions them still counts as real
# usage. The variation selector is stripped before matching because the cron
# notice emoji may appear as U+26A0 alone or U+26A0 U+FE0F.
_NOTIFIER_HEADERS = (
    '[ASYNC DELEGATION BATCH COMPLETE — ',
    '[ASYNC DELEGATION TASK FAILED — ',
    '[ASYNC DELEGATION COMPLETE — ',
    '[IMPORTANT: Background process ',
    '[Background process ',
    '⚠ Cron ',
    'Cronjob Response: ',
)


def _automation_turn(event):
    """True when the turn was injected by the runtime rather than typed by a user.

    ``source`` is deliberately ignored: injected notifiers reach the journal
    through the ordinary WebUI submit path on some flows, and cron-owned sessions
    carry no source metadata at all, so a source-only guard misses them.
    """
    content = event.get('content')
    if not isinstance(content, str):
        return False
    text = content.strip()
    if text == '[SILENT]':
        return True
    return text.replace('\ufe0f', '').startswith(_NOTIFIER_HEADERS)


def _scan_usage(state_dir, profile, now):
    from api.profiles import _profiles_match
    sessions = Path(state_dir) / 'sessions'
    native = _native_writable_sessions(sessions, profile)
    submitted, compensated = {}, set()
    cutoff = now - PERIOD_DAYS * 86400
    # One directory traversal, not one glob scan per session (quadratic).
    for shard in (sessions / '_turn_journal').glob('*.jsonl'):
        sid = shard.stem.split('~', 1)[0]
        if sid not in native:
            continue
        try:
            with shard.open(encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(e, dict) or e.get('session_id', sid) != sid:
                        continue
                    key = _dedupe_key(sid, e)
                    if key is None:
                        continue
                    if e.get('event') == 'interrupted' and e.get('reason') == 'start_compensated':
                        compensated.add(key)
                        continue
                    if e.get('event') != 'submitted':
                        continue
                    if e.get('source') not in (None, '', 'webui'):
                        continue
                    # Same alias contract as the index filter above.  An event
                    # with no profile stamp inherits its session's owner, which
                    # the index filter already resolved: legacy journals predate
                    # the profile field, and re-tagging history is not this
                    # module's job.  A tagged row must alias-match the active
                    # profile, so a renamed root still owns its 'default' rows.
                    row_profile = e.get('profile')
                    if row_profile and not _profiles_match(row_profile, profile):
                        continue
                    if _automation_turn(e):
                        continue
                    ts = e.get('created_at')
                    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
                        continue
                    if not cutoff <= ts <= now:
                        continue
                    try:
                        model, provider = _identity(e.get('model'), e.get('model_provider') or e.get('provider'))
                    except PreferencesError:
                        continue
                    # Store metadata only, not journal message bodies.
                    candidate = (ts, model, provider)
                    if key not in submitted or candidate < submitted[key]:
                        submitted[key] = candidate
        except FileNotFoundError:
            continue
    agg = {}
    for key, (ts, model, provider) in submitted.items():
        if key in compensated:
            continue
        row = agg.setdefault((model, provider), dict(model=model, provider=provider, count=0, last_used=ts))
        row['count'] += 1
        row['last_used'] = max(ts, row['last_used'])
    # Do not truncate before the browser filters unavailable models and pins.
    return sorted(agg.values(), key=lambda r: (-r['count'], -r['last_used'], r['provider'], r['model']))


def _compute_usage(state_dir):
    profile = _request_profile()
    root = Path(state_dir).resolve()
    now = time.time()
    index = root / 'sessions' / '_index.json'
    try:
        st = index.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        stamp = None
    key = (str(root), profile)
    with _scan_lock:
        hit = _scan_cache.get(key)
        if hit and hit[1] == stamp and 0 <= now - hit[0] < 15:
            return hit[2]
        rows = _scan_usage(root, profile, now)
        if len(_scan_cache) > 64:
            _scan_cache.clear()
        _scan_cache[key] = (now, stamp, rows)
        return rows


def get_preferences():
    state = _state_dir()
    return dict(profile=_request_profile(), pins=read_pins(state), usage=_compute_usage(state),
                period_days=PERIOD_DAYS, limit=LIMIT)


def apply_preferences(model, provider, pinned):
    state = _state_dir()
    set_pin(state, model, provider, pinned)
    return get_preferences()
