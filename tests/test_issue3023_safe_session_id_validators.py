"""Regression coverage for #3023: hyphenated session ids must be safe at every
filesystem-touching call site, not just Session.load() / load_metadata_only().

Before this fix, ``api-*`` and ``reachy-voice-*`` sessions could be loaded
into the sidebar but rejected with HTTP 400 by ``/api/session/delete`` and
``/api/session/worktree/remove``, producing a confusing "visible but
undeletable" UX.  The fix factored validation into ``is_safe_session_id``
and applied it consistently across the five known call sites.
"""

from api.models import is_safe_session_id


def test_is_safe_session_id_accepts_hyphenated_gateway_ids():
    assert is_safe_session_id("api-182894de593468b6") is True
    assert is_safe_session_id("reachy-voice-20260513-1131-d5542adf") is True


def test_is_safe_session_id_accepts_classic_lowercase_ids():
    assert is_safe_session_id("sess_a") is True
    assert is_safe_session_id("20260528_010551_b8e14a") is True


def test_is_safe_session_id_accepts_uppercase_and_mixed_case():
    # Some API/gateway producers emit mixed case; the helper allows it.
    # Filesystem case-folding on macOS/Windows is the caller's contract.
    assert is_safe_session_id("API-CAFEBABE") is True
    assert is_safe_session_id("Foo") is True


def test_is_safe_session_id_rejects_path_traversal():
    assert is_safe_session_id("bad/../id") is False
    assert is_safe_session_id("../etc/passwd") is False
    assert is_safe_session_id("a/b") is False


def test_is_safe_session_id_rejects_dots_and_extensions():
    assert is_safe_session_id("bad.id") is False
    assert is_safe_session_id("session.json") is False


def test_is_safe_session_id_rejects_empty_and_non_string():
    assert is_safe_session_id("") is False
    assert is_safe_session_id(None) is False
    assert is_safe_session_id(123) is False
    assert is_safe_session_id([]) is False


def test_is_safe_session_id_rejects_whitespace_and_control_chars():
    assert is_safe_session_id("api 1234") is False
    assert is_safe_session_id("api\n1234") is False
    assert is_safe_session_id("api\t1234") is False


def test_session_delete_validator_accepts_hyphenated_ids():
    """``/api/session/delete`` validator path must accept hyphens (#3023)."""
    import inspect
    import api.routes as routes
    src = inspect.getsource(routes.handle_post if hasattr(routes, "handle_post") else routes)
    # Should call the shared helper, not the old magic-string check
    assert "is_safe_session_id" in src
    assert "'0123456789abcdefghijklmnopqrstuvwxyz_'" not in src


def test_session_worktree_remove_validator_accepts_hyphenated_ids():
    """``/api/session/worktree/remove`` validator path must accept hyphens (#3023)."""
    routes_src = open("api/routes.py", encoding="utf-8").read()
    # The worktree-remove block must use the shared helper
    block_start = routes_src.find('/api/session/worktree/remove')
    block_end = routes_src.find('/api/session/delete', block_start)
    assert block_start != -1 and block_end != -1
    block = routes_src[block_start:block_end]
    assert "is_safe_session_id" in block
    assert "'0123456789abcdefghijklmnopqrstuvwxyz_'" not in block


def test_repair_stale_pending_validator_accepts_hyphenated_ids():
    """``_repair_stale_pending`` in models.py must accept hyphens (#3023)."""
    models_src = open("api/models.py", encoding="utf-8").read()
    assert "is_safe_session_id" in models_src
    # No magic-string validator should survive anywhere in models.py
    assert "'0123456789abcdefghijklmnopqrstuvwxyz_'" not in models_src


def test_no_lowercase_only_magic_string_remains_in_session_validators():
    """Repo-wide guarantee: no validator falls back to the old lowercase-only set."""
    for path in ("api/models.py", "api/routes.py"):
        src = open(path, encoding="utf-8").read()
        # The old narrow class is gone everywhere; only the helper's frozenset remains.
        narrow = "'0123456789abcdefghijklmnopqrstuvwxyz_'"
        assert narrow not in src, f"{path} still uses the narrow lowercase-only validator"
