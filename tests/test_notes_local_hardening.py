"""Regression tests for local notes search/read hardening."""

from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse


class _Handler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


def test_notes_local_responses_omit_wildcard_cors():
    from api import notes_local

    handler = _Handler()
    notes_local._json(handler, {"results": []})
    assert handler.status == 200
    assert not any(k.lower() == "access-control-allow-origin" for k, _ in handler.headers)

    bad = _Handler()
    notes_local._bad(bad, "nope", 400)
    assert not any(k.lower() == "access-control-allow-origin" for k, _ in bad.headers)


def test_notes_local_rg_uses_explicit_pattern_flag(monkeypatch, tmp_path):
    from api import notes_local

    (tmp_path / "workspace" / "knowledge").mkdir(parents=True)
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)

        class R:
            stdout = ""

        return R()

    monkeypatch.setattr(notes_local.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    handler = _Handler()
    notes_local.handle_notes_local_search(handler, urlparse("/api/notes/local/search?q=-v"))
    cmd = captured["cmd"]
    assert "-e" in cmd
    assert cmd[cmd.index("-e") + 1] == "-v"
    assert "--" in cmd
    assert cmd.index("-e") < cmd.index("--")
