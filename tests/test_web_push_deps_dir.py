"""Opt-in HERMES_WEBUI_PUSH_DEPS_DIR lets push find pywebpush under `python -I`."""
import builtins
import sys

from api import web_push


def _block_pywebpush(monkeypatch, allow_after_path=None):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "pywebpush" and (allow_after_path is None or allow_after_path not in sys.path):
            raise ImportError("blocked")
        if name == "pywebpush":
            mod = type(sys)("pywebpush")
            mod.webpush, mod.WebPushException = object(), Exception
            return mod
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_deps_dir_unset_stays_disabled(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_PUSH_DEPS_DIR", raising=False)
    _block_pywebpush(monkeypatch)
    assert web_push._pywebpush() == (None, None)


def test_deps_dir_missing_dir_stays_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_PUSH_DEPS_DIR", str(tmp_path / "nope"))
    _block_pywebpush(monkeypatch)
    assert web_push._pywebpush() == (None, None)


def test_deps_dir_added_to_path(monkeypatch, tmp_path):
    d = str(tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_PUSH_DEPS_DIR", d)
    monkeypatch.setattr(sys, "path", list(sys.path))
    _block_pywebpush(monkeypatch, allow_after_path=d)
    send, exc = web_push._pywebpush()
    assert send is not None and exc is Exception
    assert d in sys.path
