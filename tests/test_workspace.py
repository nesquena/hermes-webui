"""Pagination tests for list_dir and /api/list (#6645)."""
import io
import json
import os
from urllib.parse import urlparse
from unittest.mock import patch

import pytest

import api.workspace as w
from api.workspace import list_dir, _encode_list_cursor, _decode_list_cursor


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Pure unit tests; no running server needed."""


@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """No-op override: these unit tests create no server sessions to clean up."""
    yield []


@pytest.fixture(autouse=True)
def _block_popen(monkeypatch):
    """Prevent accidental server spawns inside workspace unit tests."""
    def _raise(*args, **kwargs):
        raise RuntimeError("subprocess.Popen not permitted; stub it with monkeypatch")
    monkeypatch.setattr("subprocess.Popen", _raise)


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_files(directory, count, prefix="f"):
    for i in range(count):
        (directory / f"{prefix}{i:04d}.txt").write_text("x", encoding="utf-8")


class _MockHandler:
    def __init__(self):
        self._status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self._status = status

    def send_header(self, key, val):
        pass

    def end_headers(self):
        pass


def _call_list_route(workspace, query):
    from api.routes import _handle_list_dir

    handler = _MockHandler()
    parsed = urlparse(f"http://localhost/api/list?session_id=s1&{query}")
    with patch('api.routes.get_session', side_effect=KeyError), \
         patch('api.routes.get_cli_sessions', return_value=[{'session_id': 's1', 'workspace': str(workspace)}]), \
         patch('api.routes.resolve_trusted_workspace', return_value=workspace):
        _handle_list_dir(handler, parsed)
    return handler._status, json.loads(handler.wfile.getvalue().decode('utf-8'))


# ── reproduction: 201-entry directory ───────────────────────────────────────

def test_list_dir_201_entries(tmp_path):
    """The route exposes continuation for a 201-entry directory."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 201)

    status, result = _call_list_route(ws, "path=.")
    assert status == 200
    assert result["has_more"] is True
    assert result["cursor"] is not None
    assert len(result["entries"]) == 200

    status2, result2 = _call_list_route(ws, f"path=.&cursor={result['cursor']}")
    assert status2 == 200
    assert len(result2["entries"]) == 1
    assert result2["has_more"] is False
    assert result2["cursor"] is None

    all_names = {e["name"] for e in result["entries"]} | {e["name"] for e in result2["entries"]}
    assert len(all_names) == 201


# ── page boundary: exactly 200 and 201 ──────────────────────────────────────

def test_list_dir_page_boundary(tmp_path):
    """Exactly 200 entries: has_more False, no cursor. 201: has_more True, cursor issued."""
    ws200 = tmp_path / "ws200"
    ws200.mkdir()
    _make_files(ws200, 200)
    r200 = list_dir(ws200, ".")
    assert len(r200["entries"]) == 200
    assert r200["has_more"] is False
    assert r200["cursor"] is None

    ws201 = tmp_path / "ws201"
    ws201.mkdir()
    _make_files(ws201, 201)
    r201 = list_dir(ws201, ".")
    assert len(r201["entries"]) == 200
    assert r201["has_more"] is True
    assert r201["cursor"] is not None


# ── both backend branches emit the same cursor contract ──────────────────────

def test_list_dir_both_branches(tmp_path, monkeypatch):
    """Both _DIR_FD_OK branches produce the same cursor contract for >200 entries."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 201)

    # dir_fd branch (default on Linux/macOS; may be False on Windows)
    # Run whichever branch is naturally active first.
    result_native = list_dir(ws, ".")

    # Force the fallback branch.
    monkeypatch.setattr(w, "_DIR_FD_OK", False)
    result_fallback = list_dir(ws, ".")

    for r in (result_native, result_fallback):
        assert r["has_more"] is True
        assert r["cursor"] is not None
        assert len(r["entries"]) == 200

    # Both branches return the same names in the same order on the first page.
    assert [e["name"] for e in result_native["entries"]] == \
           [e["name"] for e in result_fallback["entries"]]


def test_list_dir_sort_materialization_is_bounded(tmp_path, monkeypatch):
    """The final sort sees only the page plus one candidate."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 1000)
    real_sorted = sorted
    observed_sizes = []

    def bounded_sorted(iterable, *args, **kwargs):
        values = list(iterable)
        observed_sizes.append(len(values))
        return real_sorted(values, *args, **kwargs)

    monkeypatch.setattr("builtins.sorted", bounded_sorted)
    result = list_dir(ws, ".")
    assert len(result["entries"]) == 200
    assert observed_sizes and max(observed_sizes) <= 201


# ── cursor tampering and cross-directory rejection ───────────────────────────

def test_list_dir_cursor_tamper(tmp_path):
    """Mutated or cross-directory cursors are rejected; containment checks still apply."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 201)
    other = tmp_path / "other"
    other.mkdir()
    _make_files(other, 5)

    result = list_dir(ws, ".")
    cursor = result["cursor"]

    # Bit-flip in the cursor body.
    corrupted = cursor[:-4] + ("XXXX" if not cursor.endswith("XXXX") else "YYYY")
    with pytest.raises(ValueError):
        list_dir(ws, ".", cursor=corrupted)

    # Cursor issued for a different directory.
    # other has <=200 entries so no cursor is issued; build one manually via helper
    other_cursor = _encode_list_cursor(other.resolve(), (True, True, 'a0000.txt', 'a0000.txt'))
    with pytest.raises(ValueError):
        list_dir(ws, ".", cursor=other_cursor)

    # A cursor from ws must not work against a sub-directory of ws
    sub = ws / "sub"
    sub.mkdir()
    with pytest.raises(ValueError):
        list_dir(sub, ".", cursor=cursor)


# ── directory mutation between pages ────────────────────────────────────────

def test_list_dir_mutation(tmp_path):
    """No crash or duplicates across pages; the sort-key cursor survives deletion before it
    and captures insertions after it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 205)  # f0000.txt .. f0204.txt

    result1 = list_dir(ws, ".")
    assert result1["has_more"] is True
    assert len(result1["entries"]) == 200

    # Delete two files that landed on page 1 (both sort before the cursor).
    to_remove = result1["entries"][0]["name"], result1["entries"][1]["name"]
    for name in to_remove:
        (ws / name).unlink()
    # Add three files that sort after every existing entry (after the cursor).
    (ws / "zz_new0.txt").write_text("x", encoding="utf-8")
    (ws / "zz_new1.txt").write_text("x", encoding="utf-8")
    (ws / "zz_new2.txt").write_text("x", encoding="utf-8")

    result2 = list_dir(ws, ".", cursor=result1["cursor"])
    assert isinstance(result2["entries"], list)

    first_names = {e["name"] for e in result1["entries"]}
    second_names = {e["name"] for e in result2["entries"]}

    # No name appears on both pages.
    assert not (first_names & second_names), "duplicate entries across pages"

    # Entries that sort after the cursor must all appear on page 2.
    # f0200..f0204 were not deleted and sort after f0199.txt (the last entry on page 1).
    expected_surviving = {f"f{i:04d}.txt" for i in range(200, 205)}
    assert expected_surviving <= second_names, (
        f"entries after cursor missing from page 2: {expected_surviving - second_names}"
    )

    # Newly inserted entries that sort after the cursor appear on page 2.
    new_names = {"zz_new0.txt", "zz_new1.txt", "zz_new2.txt"}
    assert new_names <= second_names, (
        f"new entries after cursor not captured: {new_names - second_names}"
    )


# ── mode/state matrix ────────────────────────────────────────────────────────

def test_list_dir_state_matrix(tmp_path):
    """All six state-matrix rows produce their declared result."""
    ws = tmp_path / "ws"
    ws.mkdir()

    # row 1: first page, at or under 200 → full listing, has_more False, no cursor
    _make_files(ws, 5)
    r = list_dir(ws, ".")
    assert r["has_more"] is False and r["cursor"] is None and len(r["entries"]) == 5

    # row 2: first page, over 200 → 200 entries, has_more True, cursor issued
    _make_files(ws, 196, prefix="g")  # now 201 total
    r = list_dir(ws, ".")
    assert r["has_more"] is True and r["cursor"] is not None and len(r["entries"]) == 200

    # row 3: continuation, over 200 → next bounded page
    r2 = list_dir(ws, ".", cursor=r["cursor"])
    assert isinstance(r2["entries"], list) and len(r2["entries"]) >= 1

    # row 4: continuation, exhausted → remaining entries, has_more False
    # (r2 is the last page since we have exactly 201 entries)
    assert r2["has_more"] is False and r2["cursor"] is None

    # row 5: tampered cursor → rejected
    with pytest.raises(ValueError):
        list_dir(ws, ".", cursor="notavalidcursor")

    # row 6: mutated between pages — covered by test_list_dir_mutation


# ── negative space: single-page directory is unchanged ──────────────────────

def test_list_dir_single_page_unchanged(tmp_path):
    """A directory with ≤200 entries returns the same payload shape as before."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    (ws / "b.txt").write_text("world", encoding="utf-8")

    result = list_dir(ws, ".")
    assert result["has_more"] is False
    assert result["cursor"] is None
    names = [e["name"] for e in result["entries"]]
    # All entries are regular files, so verify the preserved alpha order.
    assert names == sorted(names, key=str.lower)
    assert "a.txt" in names
    assert "b.txt" in names
    # No load-more field bleeds into the entry dicts themselves
    for entry in result["entries"]:
        assert "has_more" not in entry
        assert "cursor" not in entry


# ── symlink filtered on continuation page ────────────────────────────────────

def test_list_dir_symlink_rejected_on_continuation(tmp_path):
    """A broken symlink that falls after the cursor is filtered on the continuation page."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 201)
    # 'zz_broken' sorts after all f*.txt entries so it appears on page 2 processing.
    broken = ws / "zz_broken"
    broken.symlink_to(ws / "nonexistent_target")

    result1 = list_dir(ws, ".")
    assert result1["has_more"] is True
    # broken symlink sorts after page 1 entries; it will be processed on page 2 but filtered.
    result2 = list_dir(ws, ".", cursor=result1["cursor"])
    names2 = {e["name"] for e in result2["entries"]}
    assert "zz_broken" not in names2, "broken symlink must be filtered on continuation page"


def test_list_dir_external_symlink_continuation_keeps_display_only_shape(tmp_path):
    """Continuation pages retain containment metadata without exposing targets."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 200)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    escapes = []
    try:
        for i in range(201):
            escape = ws / f"link{i:03d}"
            escape.symlink_to(outside)
            escapes.append(escape)
    except OSError:
        pytest.skip("symlink creation unavailable")

    first = list_dir(ws, ".")
    second = list_dir(ws, ".", cursor=first["cursor"])
    entry = next(e for e in second["entries"] if e["name"] == "link200")
    assert entry["type"] == "symlink"
    assert entry["target_outside_workspace"] is True
    assert "target" not in entry
    assert "is_dir" in entry and entry["is_dir"] is False


def test_list_dir_complete_key_handles_case_ties(tmp_path):
    """Case-equivalent names remain reachable after a page boundary."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 199, prefix="a")
    (ws / "Foo.txt").write_text("x", encoding="utf-8")
    if (ws / "foo.txt").exists():
        pytest.skip("case-equivalent names unavailable")
    try:
        (ws / "foo.txt").write_text("x", encoding="utf-8")
    except OSError:
        pytest.skip("case-equivalent names unavailable")
    (ws / "zz.txt").write_text("x", encoding="utf-8")

    first = list_dir(ws, ".")
    second = list_dir(ws, ".", cursor=first["cursor"])
    assert first["entries"][-1]["name"] == "Foo.txt"
    assert {e["name"] for e in second["entries"]} == {"foo.txt", "zz.txt"}


def test_list_dir_cursor_keeps_nonregular_sort_key(tmp_path):
    """A non-regular entry at the boundary does not skip its sibling."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO support unavailable")
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(199):
        (ws / f"a{i:04d}").mkdir()
    fifo = ws / "m-special"
    try:
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("FIFO creation unavailable")
    (ws / "z-special").mkdir()

    first = list_dir(ws, ".")
    second = list_dir(ws, ".", cursor=first["cursor"])
    assert first["entries"][-1]["name"] == "m-special"
    assert [e["name"] for e in second["entries"]] == ["z-special"]


# ── route-level cursor contract ───────────────────────────────────────────────

def test_list_dir_route_contract(tmp_path):
    """The /api/list route parses the cursor query param, returns has_more/cursor fields,
    and returns HTTP 404 on an invalid cursor."""
    import io
    import json as _json
    from urllib.parse import urlparse
    from unittest.mock import patch, MagicMock

    ws = tmp_path / "ws"
    ws.mkdir()
    _make_files(ws, 201)

    class MockHandler:
        def __init__(self):
            self._status = None
            self.headers = {}
            self.wfile = io.BytesIO()
        def send_response(self, status):
            self._status = status
        def send_header(self, key, val):
            pass
        def end_headers(self):
            pass

    from api.routes import _handle_list_dir

    def make_handler():
        h = MockHandler()
        h.wfile = io.BytesIO()
        return h

    # First page: should return 200, has_more True, cursor non-null.
    h1 = make_handler()
    parsed1 = urlparse("http://localhost/api/list?session_id=s1&path=.")
    with patch('api.routes.get_session', side_effect=KeyError), \
         patch('api.routes.get_cli_sessions', return_value=[{'session_id': 's1', 'workspace': str(ws)}]), \
         patch('api.routes.resolve_trusted_workspace', return_value=ws):
        _handle_list_dir(h1, parsed1)
    assert h1._status == 200
    body1 = _json.loads(h1.wfile.getvalue().decode('utf-8'))
    assert body1["has_more"] is True
    assert body1["cursor"] is not None
    assert len(body1["entries"]) == 200

    # Continuation page using the cursor: should return second page.
    h2 = make_handler()
    cursor = body1["cursor"]
    parsed2 = urlparse(f"http://localhost/api/list?session_id=s1&path=.&cursor={cursor}")
    with patch('api.routes.get_session', side_effect=KeyError), \
         patch('api.routes.get_cli_sessions', return_value=[{'session_id': 's1', 'workspace': str(ws)}]), \
         patch('api.routes.resolve_trusted_workspace', return_value=ws):
        _handle_list_dir(h2, parsed2)
    assert h2._status == 200
    body2 = _json.loads(h2.wfile.getvalue().decode('utf-8'))
    assert len(body2["entries"]) == 1
    assert body2["has_more"] is False

    # Bad cursor returns HTTP 404.
    h3 = make_handler()
    parsed3 = urlparse("http://localhost/api/list?session_id=s1&path=.&cursor=INVALIDCURSOR")
    with patch('api.routes.get_session', side_effect=KeyError), \
         patch('api.routes.get_cli_sessions', return_value=[{'session_id': 's1', 'workspace': str(ws)}]), \
         patch('api.routes.resolve_trusted_workspace', return_value=ws):
        _handle_list_dir(h3, parsed3)
    assert h3._status == 404
