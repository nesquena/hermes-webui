"""api.agent_compat.agent_attr resolves Hermes Agent names that moved modules.

The Agent's September 2026 decomposition moved names such as
``tools.approval.set_current_session_key`` into sibling modules. The old paths
resolved only through warning ``__getattr__`` pointers that are removed on
schedule; with the pointers gone, the WebUI's broad ``except Exception`` call
sites silently dropped approval session binding, MCP discovery/status, Claude
Code credential linking, and every kanban connection.
"""

import sys
import types
import warnings

import pytest

from api.agent_compat import agent_attr


class _CompatWarning(FutureWarning):
    pass


def _real(value):
    return value


def _install(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _split_facade(monkeypatch, *, with_pointer):
    """An Agent after the split: the name lives in the home module; the facade
    optionally keeps a warning PEP 562 pointer (the temporary compat layer)."""
    _install(monkeypatch, "fakeagent_home", moved=_real)
    facade = _install(monkeypatch, "fakeagent_facade", native=_real)
    if with_pointer:
        def __getattr__(name):
            if name == "moved":
                warnings.warn("`fakeagent_facade.moved` moved", _CompatWarning, stacklevel=2)
                return _real
            raise AttributeError(name)
        facade.__getattr__ = __getattr__
    return facade


def test_pre_split_agent_uses_original_module(monkeypatch):
    orig = _install(monkeypatch, "fakeagent_facade", moved=lambda: "pre-split")
    assert agent_attr("fakeagent_facade", "moved", "fakeagent_missing_home")() == "pre-split"
    assert agent_attr(orig, "moved", "fakeagent_missing_home")() == "pre-split"


def test_split_agent_with_pointer_uses_home_without_compat_warning(monkeypatch):
    _split_facade(monkeypatch, with_pointer=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error", _CompatWarning)
        assert agent_attr("fakeagent_facade", "moved", "fakeagent_home") is _real


def test_split_agent_after_pointer_removal_uses_home(monkeypatch):
    facade = _split_facade(monkeypatch, with_pointer=False)
    with pytest.raises(AttributeError):
        _ = facade.moved  # what `from facade import moved` hits after the removal
    assert agent_attr(facade, "moved", "fakeagent_home") is _real


def test_stub_on_original_module_still_wins(monkeypatch):
    _split_facade(monkeypatch, with_pointer=False)
    stub = lambda: "stub"  # noqa: E731
    monkeypatch.setattr(sys.modules["fakeagent_facade"], "moved", stub, raising=False)
    assert agent_attr("fakeagent_facade", "moved", "fakeagent_home") is stub


def test_non_module_double_uses_plain_attribute(monkeypatch):
    class FakeKanbanDB:
        def connect(self, *, board=None):
            return board

    double = FakeKanbanDB()
    assert agent_attr(double, "connect", "fakeagent_home")(board="b") == "b"
    assert agent_attr(double, "connect_closing", "fakeagent_home", None) is None


def test_default_when_name_is_absent_everywhere(monkeypatch):
    _split_facade(monkeypatch, with_pointer=False)
    assert agent_attr("fakeagent_facade", "nope", "fakeagent_home", None) is None
    with pytest.raises(AttributeError):
        agent_attr("fakeagent_facade", "nope", "fakeagent_home")


def test_missing_agent_raises_import_error_or_returns_default():
    with pytest.raises(ImportError):
        agent_attr("fakeagent_not_installed", "moved", "fakeagent_not_installed_home")
    assert agent_attr("fakeagent_not_installed", "moved", "fakeagent_not_installed_home", None) is None


def test_webui_does_not_use_removed_old_agent_paths():
    """Guard the old paths the Agent removed from being reintroduced in api/."""
    import ast
    from pathlib import Path

    moved_imports = {
        "tools.approval": {"set_current_session_key", "reset_current_session_key"},
        "tools.mcp_tool": {"discover_mcp_tools", "shutdown_mcp_servers", "get_mcp_status"},
        "agent.anthropic_adapter": {"read_claude_code_credentials", "is_claude_code_token_valid"},
        "hermes_cli.models": {"lmstudio_model_reasoning_options"},
    }
    moved_kb = {"connect", "connect_closing", "dispatch_once"}
    offenders = []
    for path in sorted((Path(__file__).resolve().parent.parent / "api").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module in moved_imports:
                offenders += [(path.name, node.lineno, a.name) for a in node.names
                              if a.name in moved_imports[node.module]]
            elif (isinstance(node, ast.Attribute) and node.attr in moved_kb
                  and isinstance(node.value, ast.Name) and node.value.id == "kb"):
                offenders.append((path.name, node.lineno, f"kb.{node.attr}"))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in {"getattr", "hasattr"} and len(node.args) >= 2
                  and isinstance(node.args[0], ast.Name) and node.args[0].id == "kb"
                  and isinstance(node.args[1], ast.Constant) and node.args[1].value in moved_kb):
                offenders.append((path.name, node.lineno, f"{node.func.id}(kb, {node.args[1].value!r})"))
    assert not offenders, f"old agent paths (use api.agent_compat.agent_attr): {offenders}"
