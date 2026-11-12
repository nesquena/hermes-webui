"""Resolve which profile ledger owns an async delegation, without writing any DB."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping


@dataclass(frozen=True)
class LedgerOwner:
    # found: the origin session's ledger has the row, or else exactly one
    # authorized ledger has it; missing: none has it (legacy no-row event);
    # unknown: no provenance match and unreadable, incomplete or ambiguous.
    status: Literal["found", "missing", "unknown"]
    home: Path | None = None


def _profile_is_deleted(home: Path) -> bool:
    """The Agent's tombstone check; Agent cores without it have no tombstones."""
    try:
        from hermes_constants import named_profile_is_deleted
    except (ImportError, AttributeError):
        return False
    try:
        return bool(named_profile_is_deleted(home))
    except OSError:
        return False


def _authorized_homes() -> tuple[list[Path], bool]:
    """Return existing profile homes and whether the enumeration was complete."""
    from api import profiles

    if profiles._is_isolated_profile_mode():
        return [Path(profiles.get_hermes_home_for_profile(profiles._isolated_profile_name())).resolve()], True
    homes = [Path(profiles._DEFAULT_HERMES_HOME).resolve()]
    complete = True
    # Read the profiles directory itself: list_profiles_api() is a display list
    # cached for seconds and can miss a profile created outside WebUI.
    try:
        entries = sorted(profiles._profiles_root().iterdir())
    except FileNotFoundError:
        return homes, True
    except OSError:
        return homes, False
    for entry in entries:
        if not profiles._PROFILE_ID_RE.fullmatch(entry.name):
            continue
        if not entry.is_dir():
            continue  # stray file, dangling or looping symlink: not a profile
        try:
            home = Path(profiles.get_hermes_home_for_profile(entry.name)).resolve()
            home.stat()
        except FileNotFoundError:
            continue  # removed while listing
        except Exception:
            complete = False
            continue
        if home.is_dir() and home not in homes and not _profile_is_deleted(home):
            homes.append(home)  # the Agent refuses to open a tombstoned profile's ledger
    return homes, complete


def authorized_ledger_homes() -> list[Path]:
    homes, complete = _authorized_homes()
    if not complete:
        raise RuntimeError("Authorized delegation ledger enumeration is incomplete")
    return homes


def _has_row(home: Path, delegation_id: str) -> bool | None:
    """True/False when readable; None when the ledger exists but cannot be read."""
    db_path = home / "state.db"
    try:
        # Not exists(): it hides permission errors as "missing" on newer Pythons.
        db_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        from api.agent_sessions import state_db_readonly_uri

        conn = sqlite3.connect(state_db_readonly_uri(db_path.resolve()), uri=True, timeout=2.0)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'async_delegations'"
            ).fetchone():
                return False
            return conn.execute(
                "SELECT 1 FROM async_delegations WHERE delegation_id = ? LIMIT 1",
                (delegation_id,),
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return None


def _session_home(evt: Mapping[str, Any]) -> Path | None:
    session_id = evt.get("origin_ui_session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        from api import profiles
        from api.models import get_session_profile_readonly

        name = get_session_profile_readonly(session_id) or "default"
        # The lookup silently maps invalid names to root; treat them as no provenance instead.
        if name != "default" and not (isinstance(name, str) and profiles._PROFILE_ID_RE.fullmatch(name)):
            return None
        return Path(profiles.get_hermes_home_for_profile(name)).resolve()
    except Exception:
        return None


def resolve_ledger_owner(evt: Mapping[str, Any]) -> LedgerOwner:
    """Find the owning ledger: the origin session's profile first, then a lookup."""
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
        return LedgerOwner("missing")
    delegation_id = evt.get("delegation_id")
    if not isinstance(delegation_id, str) or not delegation_id:
        return LedgerOwner("missing")
    try:
        homes, complete = _authorized_homes()
    except Exception:
        return LedgerOwner("unknown")

    session_home = _session_home(evt)
    if session_home in homes and _has_row(session_home, delegation_id):
        return LedgerOwner("found", session_home)

    results = [(home, _has_row(home, delegation_id)) for home in homes]
    matches = [home for home, found in results if found]
    if len(matches) > 1 or not complete or any(found is None for _home, found in results):
        return LedgerOwner("unknown")
    return LedgerOwner("found", matches[0]) if matches else LedgerOwner("missing")


_REPLAY_COLUMNS = frozenset(
    {"delegation_id", "state", "delivery_state", "event_json", "completed_at", "dispatched_at", "origin_ui_session_id"}
)
_DEFAULT_REPLAY_AGE_CAP_S = 48 * 3600.0


def _replay_age_cap() -> float:
    try:
        from tools.async_delegation import _MAX_COMPLETION_REPLAY_AGE_S

        return float(_MAX_COMPLETION_REPLAY_AGE_S)
    except Exception:
        return _DEFAULT_REPLAY_AGE_CAP_S


def _session_runs_in_process(session_id: str) -> bool:
    """True only for a WebUI session whose turns run in this process.

    Gateway-backend sessions are left to the Gateway that ran the delegation
    (it delivers through ``gateway_delegation_wakeup``) and runner-owned turns to
    the runner. Anything that cannot be determined is False.
    """
    try:
        from api import profiles
        from api.gateway_delegation_wakeup import _profile_uses_gateway
        from api.models import get_session_profile_readonly
        from api.runtime_adapter import runtime_adapter_runner_enabled

        name = get_session_profile_readonly(session_id) or "default"
        if name != "default" and not (isinstance(name, str) and profiles._PROFILE_ID_RE.fullmatch(name)):
            return False
        if runtime_adapter_runner_enabled():
            return False
        return not _profile_uses_gateway(name)
    except Exception:
        return False


def pending_webui_completions(home: Path, *, now: float | None = None) -> list[dict]:
    """Read *home*'s pending completions that an in-process WebUI session should get.

    The read-only counterpart of the Agent's startup replay, for ledgers WebUI
    does not own. It selects rows the same way (``state != 'running'``, pending,
    with an event) and leaves out rows past the replay age cap, which belong to
    their owner's cleanup. Nothing is written. Raises when an existing ledger
    cannot be read, so the caller retries.
    """
    db_path = Path(home) / "state.db"
    try:
        db_path.stat()
    except FileNotFoundError:
        return []
    from api.agent_sessions import state_db_readonly_uri

    conn = sqlite3.connect(state_db_readonly_uri(db_path.resolve()), uri=True, timeout=2.0)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
        if not _REPLAY_COLUMNS <= columns:
            return []  # no ledger table, or a core without session provenance
        rows = conn.execute(
            """SELECT delegation_id, event_json, completed_at, dispatched_at, origin_ui_session_id
               FROM async_delegations
               WHERE state != 'running' AND delivery_state = 'pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
    finally:
        conn.close()
    try:
        from tools.async_delegation import redact_completion_event
    except Exception:
        redact_completion_event = None
    now = time.time() if now is None else now
    cap = _replay_age_cap()
    in_process: dict[str, bool] = {}
    events = []
    for delegation_id, payload, completed_at, dispatched_at, session_id in rows:
        age_basis = completed_at or dispatched_at
        if age_basis and now - age_basis > cap:
            continue
        session_id = str(session_id or "")
        if not session_id:
            continue
        if session_id not in in_process:
            in_process[session_id] = _session_runs_in_process(session_id)
        if not in_process[session_id]:
            continue
        try:
            evt = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(evt, dict) or evt.get("delegation_id", delegation_id) != delegation_id:
            continue
        if callable(redact_completion_event):
            evt = redact_completion_event(evt)
        evt["restored"] = True
        events.append(evt)
    return events


@contextmanager
def ledger_home_scope(home: Path | None) -> Iterator[None]:
    """Bind the Agent's context-local Hermes home; never touches os.environ."""
    if home is None:
        yield
        return
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except (ImportError, AttributeError):
        # shortcut: Agent cores without the override API keep the ambient ledger, as before this fix.
        yield
        return
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)
