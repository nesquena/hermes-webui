"""Shared helpers for TTS endpoint tests.

Used by tests that exercise the TTS handler in api/routes.py and need
to mock the agent's text_to_speech_tool. Both `test_issue2931_edge_tts_endpoint.py`
and `test_tts_delegation.py` import from here.
"""
import json
import sys
import types
from types import SimpleNamespace


def mock_text_to_speech_tool(monkeypatch, *, side_effect=None, return_value=None):
    """Mock the agent's text_to_speech_tool.

    Patches both sys.modules and the name in api.routes' namespace so
    the import inside _handle_tts picks up the mock. The default mock
    writes fake audio to output_path so the file exists when the
    handler tries to read it.

    Args:
        side_effect: callable that takes (text, output_path, **kwargs)
            and returns a JSON string. Mutually exclusive with return_value.
        return_value: static JSON string to return. The default writes
            fake audio to output_path.
    """
    # Import here to avoid pulling in api.routes at module import time
    # (which would trigger the conftest's autouse test_server fixture)
    import api.routes as routes

    if side_effect is None and return_value is None:
        def side_effect(text, output_path=None, **_):
            if output_path:
                with open(output_path, "wb") as f:
                    f.write(b"\xff\xfb" * 10)
            return json.dumps({
                "success": True,
                "file_path": output_path or "/tmp/fake.mp3",
                "provider": "edge",
            })
    if side_effect is None:
        rv = return_value
        def side_effect(text, output_path=None, **_):
            return rv
    tool = SimpleNamespace(text_to_speech_tool=side_effect)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tool)
    # `from tools.tts_tool import text_to_speech_tool` inside _handle_tts
    # binds the name at first call — patch it on the routes module too.
    monkeypatch.setattr(routes, "text_to_speech_tool",
                        tool.text_to_speech_tool, raising=False)


def install_fake_hermes_cli(monkeypatch, *, config=None):
    """Install a minimal fake ``hermes_cli`` package for offline CI.

    Tests that monkeypatch ``hermes_cli.config.load_config`` need the
    package to exist in ``sys.modules`` even when hermes-agent is not
    installed (the normal CI environment).  If the real package is
    already present, this is a no-op.
    """
    if "hermes_cli" in sys.modules:
        return  # real package available, nothing to do
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = lambda: config or {}
    pkg.config = config_mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)