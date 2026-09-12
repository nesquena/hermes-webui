"""Regression tests for the race-safe JS source loader (issue #6972).

The serial JS/DOM harness tests were flaky under load: a truncated read of
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
    EXTRACTION_TAILS,
    MAX_READ_ATTEMPTS,
    SourceReadError,
    node_validated_read_options,
    node_validated_read_snippet,
    read_extraction_source,
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


# --- Regression tests for the three defects fixed in #7023 ---
#
# 1. Zero-byte stat/read must be rejected (fail-closed, not silently "").
# 2. A stat() that observes a partial nonzero rewrite must not be accepted.
# 3. A stable replacement of a *different* size between attempts must be
#    accepted (metadata recomputed per attempt, not frozen once).


def test_read_js_source_rejects_zero_byte_file(tmp_path, monkeypatch):
    """Regression 1a: a zero-byte source must raise SourceReadError, never
    return "" (which brace matching would silently mangle)."""
    real = tmp_path / "ui.js"
    real.write_text("", encoding="utf-8")

    with pytest.raises(SourceReadError, match="zero-byte"):
        read_js_source(real, retry_delay_seconds=0.001)


def test_read_js_source_rejects_zero_byte_read_even_with_nonzero_stat(tmp_path, monkeypatch):
    """Regression 1b: a read that returns 0 bytes while stat reports a
    non-zero size (file truncated between stat and read) must fail closed."""
    real = tmp_path / "ui.js"
    full_text = "function loadSession(sid){ return rows; }\n"
    real.write_text(full_text, encoding="utf-8")

    def fake_read_bytes(self):
        return b""

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    with pytest.raises(SourceReadError, match="zero-byte"):
        read_js_source(real, retry_delay_seconds=0.001)


def test_read_js_source_rejects_partial_rewrite_observed_by_stat(sample_source, monkeypatch):
    """Regression 2: stat() observing a partial nonzero rewrite must not be
    accepted even when the read matches that partial size.

    The reviewer reproduced this: a 41-byte file paused mid-rewrite was
    observed as 31 bytes by the single stat() and the 31-byte read was
    accepted. With per-attempt pre/post identity validation, the attempt
    whose post-read stat differs from its pre-read stat is discarded.
    """
    real, full_text = sample_source
    full_bytes = full_text.encode("utf-8")
    partial_bytes = full_bytes[:31]
    state = {"calls": 0}

    def fake_stat(self):
        # Stat 1 (pre-read of attempt 0): observes partial rewrite (31 bytes).
        # Stat 2 (post-read of attempt 0): file has grown to full size.
        # Stats 3+ (attempt 1): stable at full size.
        state["calls"] += 1
        if state["calls"] == 1:
            return _StatLike(len(partial_bytes), ino=100, mtime_ns=1)
        return _StatLike(len(full_bytes), ino=100, mtime_ns=2)

    def fake_read_bytes(self):
        # First read returns the partial bytes; subsequent reads return full.
        if state["calls"] == 1:
            return partial_bytes
        return full_bytes

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    text = read_js_source(real, retry_delay_seconds=0.001)
    assert text == full_text


class _StatLike:
    """Minimal stand-in for os.stat_result used by the monkeypatched stat."""

    def __init__(self, size, *, ino, mtime_ns):
        self.st_size = size
        self.st_ino = ino
        self.st_mtime_ns = mtime_ns


def test_read_js_source_accepts_stable_different_size_between_attempts(sample_source, monkeypatch):
    """Regression 3: a stable replacement of a different size between the
    initial stat and the first read must be accepted, not rejected for all
    attempts (expected_size frozen once)."""
    real, full_text = sample_source
    full_bytes = full_text.encode("utf-8")
    state = {"calls": 0}

    def fake_stat(self):
        # Stat 1 (pre-read of attempt 0): observes the *old* size.
        # Stat 2 (post-read of attempt 0): file already replaced -> new size.
        # Stats 3+ (attempt 1): stable at the new size.
        state["calls"] += 1
        if state["calls"] == 1:
            return _StatLike(9, ino=100, mtime_ns=1)
        return _StatLike(len(full_bytes), ino=100, mtime_ns=2)

    def fake_read_bytes(self):
        # Every read returns the new (larger) content — the file was already
        # replaced by the time the first read ran.
        return full_bytes

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    text = read_js_source(real, retry_delay_seconds=0.001)
    assert text == full_text


def test_read_js_source_require_tail_enforces_eof_sentinel(tmp_path):
    """The EOF sentinel must be enforced for extraction sources
    (require_tail=True), so a partial rewrite that passes size checks still
    fails closed instead of feeding truncated text to brace matching."""
    real = tmp_path / "ui.js"
    full_text = "function loadSession(sid){\n  return rows;\n}\n"
    real.write_text(full_text, encoding="utf-8")

    text = read_js_source(real, expected_tail="  return rows;\n}\n", require_tail=True)
    assert text == full_text

    # Wrong/missing sentinel -> fail closed even though size matches.
    with pytest.raises(SourceReadError, match="EOF sentinel missing"):
        read_js_source(real, expected_tail="/* never-present */", require_tail=True)
    # Caller contract: require_tail=True demands a sentinel value.
    with pytest.raises(ValueError, match="require_tail=True requires expected_tail"):
        read_js_source(real, expected_tail=None, require_tail=True)


def _run_node_read_script(tmp_path, node_body: str, read_call: str | None = None) -> str:
    """Run a Node script built on the shared snippet; return stdout or raise
    with stderr on failure.

    ``node_body`` is injected before the snippet (mock setup etc.).
    ``read_call`` defaults to ``const src = readValidated(p);``; pass a
    custom call (e.g. with extraction-tail options) to exercise it.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Node-side snippet check")
    if read_call is None:
        read_call = "const src = readValidated(p);"
    script = (
        "const fs = require('fs');\n"
        f"const p = {str(tmp_path / 'ui.js')!r};\n"
        + node_body
        + "\n"
        + node_validated_read_snippet()
        + "\ntry {\n"
        + read_call
        + "\n  console.log('RESULT:' + src);\n"
        "} catch(e) {\n"
        "  console.log('EXPECTED_ERROR: ' + e.message);\n"
        "}\n"
    )
    result = subprocess.run(
        [node, "-e", script],
        cwd=str(tmp_path),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    # console.log adds a trailing newline after the content; strip exactly one.
    return result.stdout.removesuffix("\n")


def test_node_validated_read_snippet_rejects_zero_byte(tmp_path):
    """Regression 1 (Node): zero-byte stat/read must throw, not return ''."""
    full = "function loadSession(sid){ return rows; }\n"
    (tmp_path / "ui.js").write_text(full, encoding="utf-8")
    stdout = _run_node_read_script(
        tmp_path,
        "fs.writeFileSync(p, '');",  # truncate to zero bytes before the read
    )
    assert "EXPECTED_ERROR: source read incomplete" in stdout, stdout


def test_node_validated_read_snippet_rejects_partial_rewrite_observed_by_stat(tmp_path):
    """Regression 2 (Node): stat() observing a partial nonzero rewrite must
    not be accepted even when the read matches that partial size."""
    full = "function loadSession(sid){ return rows; }\n"
    (tmp_path / "ui.js").write_text(full, encoding="utf-8")
    full_len = len(full.encode("utf-8"))
    partial_len = 31
    stdout = _run_node_read_script(
        tmp_path,
        (
            "const realStat = fs.statSync;\n"
            "const realRead = fs.readFileSync;\n"
            "let statCalls = 0;\n"
            "fs.statSync = function(path, opts){\n"
            "  statCalls++;\n"
            "  // Attempt 0 pre-read stat observes a 31-byte partial rewrite.\n"
            "  // BigInt fields: the snippet reads with { bigint: true } and\n"
            "  // requires BigInt size/mtimeNs (parity with the Python path).\n"
            "  if(statCalls === 1) return { size: 31n, ino: 100n, mtimeNs: 1n };\n"
            "  // Attempt 0 post-read stat observes the full 41-byte file.\n"
            "  if(statCalls === 2) return { size: " + str(full_len) + "n, ino: 100n, mtimeNs: 2n };\n"
            "  // Attempt 1: stable at full size.\n"
            "  return realStat(path, opts);\n"
            "};\n"
            "fs.readFileSync = function(path, enc){\n"
            "  const full = realRead(path, 'utf8');\n"
            "  // Attempt 0 read returns the 31-byte partial view.\n"
            "  if(statCalls === 1) return full.slice(0, " + str(partial_len) + ");\n"
            "  return full;\n"
            "};\n"
        ),
    )
    # Must NOT accept the 31-byte fragment; must retry and return full content.
    assert "RESULT:" in stdout, stdout
    assert stdout.split("RESULT:")[1] == full, stdout


def test_node_validated_read_snippet_accepts_stable_different_size_between_attempts(tmp_path):
    """Regression 3 (Node): a stable replacement of a different size between
    attempts must be accepted (metadata recomputed per attempt)."""
    new_full = "function loadSession(sid){ return rows; }\n"
    (tmp_path / "ui.js").write_text(new_full, encoding="utf-8")
    stdout = _run_node_read_script(
        tmp_path,
        (
            "const realStat = fs.statSync;\n"
            "let statCalls = 0;\n"
            "fs.statSync = function(path, opts){\n"
            "  statCalls++;\n"
            "  // Attempt 0 pre-read stat observes the *old* 9-byte size.\n"
            "  // BigInt fields: the snippet reads with { bigint: true } and\n"
            "  // requires BigInt size/mtimeNs (parity with the Python path).\n"
            "  if(statCalls === 1) return { size: 9n, ino: 100n, mtimeNs: 1n };\n"
            "  // Attempt 0 post-read stat (and attempt 1) observe the stable\n"
            "  // new size — the file was legitimately replaced.\n"
            "  return realStat(path, opts);\n"
            "};\n"
        ),
    )
    assert "RESULT:" in stdout, stdout
    assert stdout.split("RESULT:")[1] == new_full, stdout


# --- Round-3 regressions (#7023 re-gate) ---
#
# 1. Extraction sources must enforce the registered EOF tail (Python and
#    Node), and the tail registry itself must not drift from the real files.
# 2. The Node snippet must use BigInt stats; a stat without mtimeNs (the
#    old dead-code shape) must throw instead of passing silently.


def test_extraction_tail_registry_matches_real_static_files():
    """Every EXTRACTION_TAILS entry must be an exact suffix of the real
    static file it names — a drift in a registered source's final line
    fails loudly here instead of silently weakening the EOF sentinel.

    Also proves each registered source can be read end-to-end through
    ``read_extraction_source`` (the extraction entry point)."""
    root = Path(__file__).resolve().parents[1]
    assert EXTRACTION_TAILS, "tail registry must not be empty"
    for name, tail in EXTRACTION_TAILS.items():
        real = root / "static" / name
        assert real.is_file(), f"registered extraction source missing: {real}"
        text = real.read_text(encoding="utf-8")
        assert text.endswith(tail), (
            f"extraction tail for {name} drifted: {tail!r} is no longer a "
            f"suffix of static/{name}; update EXTRACTION_TAILS in "
            "tests/js_source_loader.py"
        )
        # The extraction path must succeed on the real file (tail enforced).
        assert read_extraction_source(real) == text


def test_read_extraction_source_rejects_stable_partial_rewrite(tmp_path):
    """A *stable* partial rewrite — the file is smaller but consistent
    between attempts, so size/identity checks pass — must still fail closed
    via the registered EOF tail sentinel instead of reaching extraction."""
    root = Path(__file__).resolve().parents[1]
    real_ui = root / "static" / "ui.js"
    partial = real_ui.read_bytes()[: real_ui.stat().st_size // 2]
    target = tmp_path / "ui.js"
    target.write_bytes(partial)

    with pytest.raises(SourceReadError, match="EOF sentinel missing"):
        read_extraction_source(target, retry_delay_seconds=0.001)


def test_read_extraction_source_refuses_unregistered_source(tmp_path):
    """A source without a registered tail must raise ValueError — a new
    extraction source can never silently read without the sentinel."""
    target = tmp_path / "mystery.js"
    target.write_text("function x(){ return 1; }\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no registered extraction tail"):
        read_extraction_source(target)


def test_node_validated_read_snippet_requires_registered_tail(tmp_path):
    """Node parity for extraction: readValidated with the registry options
    must reject a stable partial source (identity/size consistent, tail
    missing) exactly like the Python path."""
    full = "function loadSession(sid){\n  return rows;\n}\n"
    (tmp_path / "ui.js").write_text(full[: len(full) // 2], encoding="utf-8")
    stdout = _run_node_read_script(
        tmp_path,
        "",
        read_call=(
            "const src = readValidated(p, "
            + node_validated_read_options(Path("static") / "ui.js")
            + ");"
        ),
    )
    assert "EXPECTED_ERROR: source read incomplete" in stdout, stdout


def test_node_validated_read_snippet_throws_on_stat_without_bigint_fields(tmp_path):
    """The Node mtimeNs check must never be dead code: a stat result without
    BigInt fields (the old `fs.statSync(p)` shape, where mtimeNs is
    undefined) throws instead of silently passing the identity check."""
    full = "function loadSession(sid){ return rows; }\n"
    (tmp_path / "ui.js").write_text(full, encoding="utf-8")
    stdout = _run_node_read_script(
        tmp_path,
        (
            "fs.statSync = function(path, opts){\n"
            "  // Non-bigint stat: mtimeNs absent -> the snippet must throw.\n"
            "  return { size: " + str(len(full.encode("utf-8"))) + ", ino: 100 };\n"
            "};\n"
        ),
    )
    assert "EXPECTED_ERROR: source stat missing BigInt" in stdout, stdout
