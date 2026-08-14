"""Regression tests for the race-safe JS source loader (issue #6972).

The serial JS/DOM harness suites were flaky under load: a truncated read of
``static/ui.js`` (partial read observed mid-rewrite or under I/O starvation)
was handed to textual brace matching, which then returned a garbled fragment
instead of failing loudly. These tests prove the shared loader retries on a
partial read and fails closed (``SourceReadError``) when the file never reads
consistently — never handing a truncated fragment to extraction.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_loader import (
    MAX_READ_ATTEMPTS,
    SourceReadError,
    node_validated_read_snippet,
    read_js_source,
)


@pytest.fixture
def sample_source(tmp_path):
    real = tmp_path / "ui.js"
    # Multi-line body with a distinctive tail sentinel, mimicking ui.js.
    full_text = (
        "function loadSession(sid){\n"
        "  const rows=[];\n"
        "  for(const s of sessions) rows.push(s);\n"
        "  clearCompressionUi();\n"
        "  return rows;\n"
        "}\n"
    )
    real.write_text(full_text, encoding="utf-8")
    return real, full_text


def _install_partial_reads(path: Path, full_text: str, partial_reads: int, monkeypatch):
    """Make ``Path.read_bytes`` return a truncated view the first
    ``partial_reads`` times, then the full content — simulating a read that
    races a concurrent rewrite of the shared static source."""
    full_bytes = full_text.encode("utf-8")
    partial_bytes = full_bytes[: len(full_bytes) // 2]
    reads = {"count": 0}

    def fake_read_bytes(self):
        reads["count"] += 1
        if reads["count"] <= partial_reads:
            return partial_bytes
        return full_bytes

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    return reads


def test_read_js_source_retries_partial_read_until_stable(sample_source, monkeypatch):
    """A first truncated read must be retried, not handed to extraction."""
    real, full_text = sample_source
    reads = _install_partial_reads(real, full_text, partial_reads=2, monkeypatch=monkeypatch)

    text = read_js_source(real)
    assert text == full_text
    assert reads["count"] == 3  # two partial attempts, then a stable full read


def test_read_js_source_fails_closed_when_read_always_truncated(sample_source, monkeypatch):
    """A permanently truncated read must raise SourceReadError, not return
    a fragment that brace matching would silently mangle."""
    real, full_text = sample_source
    reads = _install_partial_reads(real, full_text, partial_reads=10**6, monkeypatch=monkeypatch)

    with pytest.raises(SourceReadError, match="truncated read"):
        read_js_source(real)
    assert reads["count"] == MAX_READ_ATTEMPTS


def test_read_js_source_validates_expected_tail(sample_source):
    """When a caller requires the known end-of-file sentinel, a read that
    fails to reach it is retried/failed instead of accepted."""
    real, full_text = sample_source

    text = read_js_source(real, expected_tail="  return rows;\n}\n")
    assert text == full_text

    # Tail never reached (wrong sentinel) -> fail closed.
    with pytest.raises(SourceReadError, match="truncated read"):
        read_js_source(real, expected_tail="/* never-present sentinel */")


def test_read_js_source_reads_real_ui_js_to_completion():
    """The real ~1MB static/ui.js must be read to completion (size-validated),
    which is the exact read that used to truncate under load."""
    root = Path(__file__).resolve().parents[1]
    ui_js = root / "static" / "ui.js"
    text = read_js_source(ui_js)
    assert len(text.encode("utf-8")) == ui_js.stat().st_size
    assert "clearCompressionUi()" in text  # sentinel from the issue evidence


def test_node_validated_read_snippet_reads_full_file(sample_source):
    """The Node-side snippet must read the same file to completion."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Node-side snippet check")
    real, full_text = sample_source

    script = (
        node_validated_read_snippet()
        + f"const src = readValidated({str(real)!r});\n"
        + "if(src !== " + repr(full_text) + ") throw new Error('content mismatch');\n"
        + "console.log('OK');\n"
    )
    result = subprocess.run(
        [node, "-e", script],
        cwd=str(real.parent),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_node_validated_read_snippet_fails_closed_on_truncated_target(tmp_path):
    """A read that returns fewer bytes than stat reports (the race) must
    throw an explicit source-read error instead of returning a fragment."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Node-side snippet check")

    script = f"""
const fs = require('fs');
const p = {str(tmp_path / "ui.js")!r};
fs.writeFileSync(p, 'function loadSession(sid){{ return rows; }}\\n');
const origRead = fs.readFileSync;
fs.readFileSync = function(path, enc){{
  // Simulate a partial read racing a concurrent rewrite: return half.
  const full = origRead(path, 'utf8');
  return full.slice(0, Math.floor(full.length / 2));
}};
{node_validated_read_snippet()}
try {{
  readValidated(p);
  console.log('NO_ERROR');
}} catch(e) {{
  console.log('EXPECTED_ERROR: ' + e.message);
}}
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=str(tmp_path),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "EXPECTED_ERROR: source read incomplete" in result.stdout, result.stdout
