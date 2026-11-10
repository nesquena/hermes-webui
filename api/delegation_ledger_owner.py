"""Resolve which profile ledger owns an async delegation, without writing any DB."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping


@dataclass(frozen=True)
class LedgerOwner:
    # found: one authorized ledger has the row; missing: none has it (legacy
    # no-row event); unknown: unreadable, incomplete or ambiguous.
    status: Literal["found", "missing", "unknown"]
    home: Path | None = None


def _authorized_homes() -> tuple[list[Path], bool]:
    """Return existing profile homes and whether the enumeration was complete."""
    from api import profiles

    if profiles._is_isolated_profile_mode():
        return [Path(profiles.get_hermes_home_for_profile(profiles._isolated_profile_name())).resolve()], True
    homes = [Path(profiles._DEFAULT_HERMES_HOME).resolve()]
    complete = True
    try:
        rows = profiles.list_profiles_api()
    except Exception:
        return homes, False
    for row in rows if isinstance(rows, list) else []:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip():
            complete = False
            continue
        if name == "default" or row.get("is_default"):
            continue
        try:
            home = Path(profiles.get_hermes_home_for_profile(name.strip())).resolve()
            home.stat()
        except FileNotFoundError:
            continue  # listed but never created: no ledger to own a row
        except Exception:
            complete = False
            continue
        if home.is_dir() and home not in homes:
            homes.append(home)
    return homes, complete and isinstance(rows, list)


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
