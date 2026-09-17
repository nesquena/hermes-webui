"""Exercise modern and legacy approval imports without deprecated aliases."""
import builtins
import contextvars
import types

import pytest


@pytest.fixture
def test_server():
    yield


@pytest.mark.parametrize("legacy", [False, True])
def test_turn_approval_import_compatibility(monkeypatch, legacy):
    from api import streaming
    key = contextvars.ContextVar("test_approval_key", default="before")
    calls = []
    module = types.SimpleNamespace(
        set_current_session_key=lambda value: key.set(value),
        reset_current_session_key=lambda token: key.reset(token),
    )
    original = builtins.__import__

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tools.approval_context":
            calls.append(name)
            if legacy:
                raise ModuleNotFoundError(name=name)
            return module
        if name == "tools.approval" and any(
            x in fromlist for x in ("set_current_session_key", "reset_current_session_key")
        ):
            assert legacy, "modern runtime must not touch deprecated aliases"
            calls.append(name)
            return module
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    tokens = streaming._set_turn_session_identity("own-chat")
    try:
        assert key.get() == "own-chat"
    finally:
        streaming._reset_turn_session_identity(tokens)
    assert key.get() == "before"
    assert calls.count("tools.approval_context") == 2
    assert calls.count("tools.approval") == (2 if legacy else 0)
