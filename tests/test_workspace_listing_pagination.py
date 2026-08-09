"""Pagination for workspace directory listings (#6645).

list_dir() used to silently stop at 200 entries with no signal in the
response, so the Workspace → Files pane rendered incomplete listings with
no indication anything was missing. It now returns a page dict with
entries / total / has_more / limit / offset, and the route exposes
offset/limit query params.
"""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from api import routes
from api.workspace import dir_signature, list_dir


def _make_dir(tmp_path, names):
    d = tmp_path / "ws"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_text("x", encoding="utf-8")
    return d


def test_list_dir_reports_total_and_has_more(tmp_path):
    d = _make_dir(tmp_path, [f"f{i:03}.txt" for i in range(5)])
    result = list_dir(d, ".")
    assert len(result["entries"]) == 5
    assert result["total"] == 5
    assert result["has_more"] is False
    assert result["offset"] == 0
    assert result["limit"] == 200


def test_list_dir_paginates_with_offset_and_limit(tmp_path):
    d = _make_dir(tmp_path, [f"f{i:03}.txt" for i in range(5)])
    page1 = list_dir(d, ".", limit=2, offset=0)
    assert [e["name"] for e in page1["entries"]] == ["f000.txt", "f001.txt"]
    assert page1["total"] == 5
    assert page1["has_more"] is True

    page2 = list_dir(d, ".", limit=2, offset=2)
    assert [e["name"] for e in page2["entries"]] == ["f002.txt", "f003.txt"]
    assert page2["has_more"] is True

    page3 = list_dir(d, ".", limit=2, offset=4)
    assert [e["name"] for e in page3["entries"]] == ["f004.txt"]
    assert page3["has_more"] is False

    past = list_dir(d, ".", limit=2, offset=9)
    assert past["entries"] == []
    assert past["has_more"] is False
    assert past["total"] == 5


def test_list_dir_default_cap_surfaces_truncation(tmp_path):
    """The default page is still bounded at 200, but truncation is explicit."""
    d = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    result = list_dir(d, ".")
    assert len(result["entries"]) == 200
    assert result["total"] == 205
    assert result["has_more"] is True

    tail = list_dir(d, ".", offset=200)
    assert len(tail["entries"]) == 5
    assert tail["has_more"] is False


def test_list_dir_limit_none_returns_full_listing(tmp_path):
    d = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    result = list_dir(d, ".", limit=None)
    assert len(result["entries"]) == 205
    assert result["total"] == 205
    assert result["has_more"] is False


def test_list_dir_clamps_bad_params(tmp_path):
    d = _make_dir(tmp_path, ["a.txt"])
    assert list_dir(d, ".", offset=-3)["offset"] == 0
    # negative limit falls back to the default page size
    assert list_dir(d, ".", limit=-1)["limit"] == 200


def test_dir_signature_covers_full_listing_when_entries_omitted(tmp_path):
    """dir_signature() without entries hashes the whole directory (no 200 cap),
    so a change past the first page still invalidates the signature."""
    d = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    full = list_dir(d, ".", limit=None)["entries"]
    assert len(full) == 205
    assert dir_signature(d, ".") == dir_signature(d, ".", full)
    # the default page (first 200) hashes to a DIFFERENT signature
    assert dir_signature(d, ".") != dir_signature(d, ".", list_dir(d, ".")["entries"])


def test_handle_list_dir_exposes_pagination(monkeypatch, tmp_path):
    ws = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    session = SimpleNamespace(session_id="sess-page", workspace=str(ws), profile=None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        routes,
        "resolve_implicit_workspace_with_recovery",
        lambda candidate, _fallback: (Path(str(candidate)), False),
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-page&path=.")
    )
    assert len(payload["entries"]) == 200
    assert payload["total"] == 205
    assert payload["has_more"] is True
    assert payload["offset"] == 0

    page2 = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-page&path=.&offset=200")
    )
    assert len(page2["entries"]) == 5
    assert page2["total"] == 205
    assert page2["has_more"] is False
    assert page2["offset"] == 200

    # bad offset values fall back to page 1 instead of erroring
    safe = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-page&path=.&offset=notanumber")
    )
    assert safe["offset"] == 0
    assert len(safe["entries"]) == 200
