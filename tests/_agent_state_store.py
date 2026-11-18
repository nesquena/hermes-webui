"""Open the installed Agent's state store in tests through the production opener, failing loudly.

``api.state_sync._open_state_db`` returns None both when no Agent store API is importable and when
opening an installed store raises (production swallows that error and logs it). Tests must tell the
two apart: skip only when neither ``hermes_state`` nor ``persistence_assembly`` imports, and fail when
one does but the opener returned nothing (a broken constructor or an incompatible
``open_persistence`` call would otherwise turn into a skipped test).
"""

import importlib

import pytest

from api import state_sync

LEGACY_SESSION_DB_VERBS = ("ensure_session", "update_session_cwd", "set_auto_title", "update_token_counts")


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def open_agent_store(db_path):
    """``state_sync._open_state_db(db_path)``; skip without an Agent store API, fail when it returns None."""
    db = state_sync._open_state_db(db_path)
    if db is not None:
        return db
    if not (_importable("hermes_state") or _importable("persistence_assembly")):
        pytest.skip("no hermes-agent state store API importable")
    pytest.fail(f"a hermes-agent store API is installed, but state_sync._open_state_db({db_path}) returned None")


def expected_store_is_storage_interface() -> bool:
    """What ``_open_state_db`` must pick on the installed Agent, from what is installed: a SessionDB with
    every verb used is kept; otherwise the storage interface when ``persistence_assembly`` imports; an
    older SessionDB (no storage interface, e.g. no ``set_auto_title`` before v2026.8.13) is kept too."""
    try:
        from hermes_state import SessionDB
    except ImportError:
        SessionDB = None
    if SessionDB is not None and all(hasattr(SessionDB, verb) for verb in LEGACY_SESSION_DB_VERBS):
        return False
    return _importable("persistence_assembly")
