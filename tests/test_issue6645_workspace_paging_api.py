"""Regression coverage for paginated workspace directory listings."""

from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

import pytest

from api import routes
import api.workspace as workspace


def _make_files(directory, count, prefix="entry-"):
    for index in range(count):
        (directory / f"{prefix}{index:03d}.txt").write_text("x", encoding="utf-8")


def test_list_route_pages_and_serializes_workspace_timestamps(monkeypatch, tmp_path):
    for index in range(201):
        (tmp_path / f"entry-{index:03d}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        workspace, "_birthtime_ns", lambda _stat: 1_750_000_000_000_000_000
    )

    session = SimpleNamespace(session_id="paging-session", workspace=str(tmp_path))
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        routes,
        "resolve_implicit_workspace_with_recovery",
        lambda stored, _last, **_kwargs: (tmp_path, False),
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    first = routes._handle_list_dir(
        object(),
        urlparse("/api/list?session_id=paging-session&path=.")
    )

    assert len(first["entries"]) == 200
    assert first["has_more"] is True
    assert first["cursor"]
    assert all(isinstance(entry["mtime_ns"], str) for entry in first["entries"])
    assert all(isinstance(entry["birthtime_ns"], str) for entry in first["entries"])
    assert all("workspace_sort_rank" in entry for entry in first["entries"])
    assert all("birthtime_ns" in entry for entry in first["entries"])

    second = routes._handle_list_dir(
        object(),
        urlparse(
            f"/api/list?session_id=paging-session&path=.&cursor={first['cursor']}"
        ),
    )

    assert [entry["name"] for entry in second["entries"]] == ["entry-200.txt"]
    assert second["has_more"] is False
    assert second["cursor"] is None


def test_list_dir_page_has_exact_200_and_201_boundaries(tmp_path):
    exactly = tmp_path / "exactly"
    exactly.mkdir()
    _make_files(exactly, 200)
    page = workspace.list_dir_page(exactly)
    assert len(page["entries"]) == 200
    assert page["has_more"] is False
    assert page["cursor"] is None

    larger = tmp_path / "larger"
    larger.mkdir()
    _make_files(larger, 201)
    first = workspace.list_dir_page(larger)
    second = workspace.list_dir_page(larger, cursor=first["cursor"])
    assert len(first["entries"]) == 200
    assert first["has_more"] is True
    assert [entry["name"] for entry in second["entries"]] == ["entry-200.txt"]
    assert second["has_more"] is False
    assert second["cursor"] is None


def test_list_dir_page_uses_workspace_rank_and_preserves_entry_fields(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("x", encoding="utf-8")
    try:
        for index in range(199):
            (root / f"link-{index:03d}").symlink_to(external)
    except OSError:
        pytest.skip("symlink creation unavailable")
    (root / "folder").mkdir()
    (root / "only-file.txt").write_text("x", encoding="utf-8")

    first = workspace.list_dir_page(root)
    second = workspace.list_dir_page(root, cursor=first["cursor"])
    entries = first["entries"] + second["entries"]

    assert len(entries) == 201
    assert [entry["workspace_sort_rank"] for entry in entries] == [0] * 199 + [1, 2]
    assert all("mtime_ns" in entry and "birthtime_ns" in entry for entry in entries)
    assert first["entries"][-1]["name"] == "folder"
    assert second["entries"][0]["name"] == "only-file.txt"


def test_list_dir_page_validates_signed_directory_cursor(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    _make_files(root, 201)
    other = tmp_path / "other"
    other.mkdir()
    first = workspace.list_dir_page(root)
    cursor = first["cursor"]

    tampered = cursor[:8] + ("A" if cursor[8] != "A" else "B") + cursor[9:]
    try:
        workspace.list_dir_page(root, cursor=tampered)
    except workspace.WorkspaceDirectoryCursorError:
        pass
    else:
        raise AssertionError("a modified cursor must be rejected")

    try:
        workspace.list_dir_page(other, cursor=cursor)
    except workspace.WorkspaceDirectoryCursorError:
        pass
    else:
        raise AssertionError("a cursor from another directory must be rejected")


def test_list_dir_page_keyset_remains_unique_after_mutation(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    _make_files(root, 205)
    first = workspace.list_dir_page(root)
    removed = [entry["name"] for entry in first["entries"][:2]]
    for name in removed:
        (root / name).unlink()
    late_names = {"zz-new-0.txt", "zz-new-1.txt"}
    for name in late_names:
        (root / name).write_text("x", encoding="utf-8")

    second = workspace.list_dir_page(root, cursor=first["cursor"])
    first_names = {entry["name"] for entry in first["entries"]}
    second_names = {entry["name"] for entry in second["entries"]}
    assert not first_names & second_names
    assert {f"entry-{index:03d}.txt" for index in range(200, 205)} <= second_names
    assert late_names <= second_names


def test_list_dir_page_fallback_matches_anchored_order(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    _make_files(root, 201)
    if not workspace._DIR_FD_OK:
        first = workspace.list_dir_page(root)
        assert len(first["entries"]) == 200
        assert first["has_more"] is True
        assert first["cursor"]
        return

    anchored = workspace.list_dir_page(root)
    monkeypatch.setattr(workspace, "_DIR_FD_OK", False)
    fallback = workspace.list_dir_page(root)
    assert [entry["name"] for entry in anchored["entries"]] == [
        entry["name"] for entry in fallback["entries"]
    ]
    assert anchored["has_more"] is fallback["has_more"] is True


def test_list_dir_page_bounds_the_final_sort(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    _make_files(root, 1000)
    real_sorted = sorted
    observed_sizes = []

    def record_sorted(iterable, *args, **kwargs):
        values = list(iterable)
        observed_sizes.append(len(values))
        return real_sorted(values, *args, **kwargs)

    monkeypatch.setattr("builtins.sorted", record_sorted)
    page = workspace.list_dir_page(root)
    assert len(page["entries"]) == 200
    assert observed_sizes and max(observed_sizes) <= 201


def test_escape_list_route_pages_virtual_read_only_entries(monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    _make_files(outside, 201)
    monkeypatch.setattr(
        workspace, "_birthtime_ns", lambda _stat: 1_750_000_000_000_000_000
    )
    (root / "escape").symlink_to(outside, target_is_directory=True)
    session_id = "paging-session"
    grant = workspace.authorize_escape_target(root, session_id, "escape")
    monkeypatch.setattr(
        routes, "get_session_for_file_ops",
        lambda _sid: SimpleNamespace(workspace=str(root)),
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    first = routes._handle_escape_list_dir(
        object(),
        urlparse("/api/escape/list?" + urlencode({
            "session_id": session_id,
            "token": grant["token"],
            "path": "escape",
        })),
    )
    second = routes._handle_escape_list_dir(
        object(),
        urlparse("/api/escape/list?" + urlencode({
            "session_id": session_id,
            "token": grant["token"],
            "path": "escape",
            "cursor": first["cursor"],
        })),
    )

    assert len(first["entries"]) == 200
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert second["cursor"] is None
    assert second["entries"][0]["path"] == "escape/entry-200.txt"
    assert second["entries"][0]["escape_read_only"] is True
    assert isinstance(second["entries"][0]["mtime_ns"], str)
    assert isinstance(second["entries"][0]["birthtime_ns"], str)
    assert isinstance(second["entries"][0]["workspace_sort_rank"], int)
