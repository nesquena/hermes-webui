"""Focused route coverage for issue #5763 native projects."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _get_projects(
    monkeypatch,
    *,
    legacy,
    native,
    active_profile="alpha",
    query="",
    native_loader=None,
):
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(routes, "load_projects", lambda: legacy)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_profile)
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(
        routes,
        "load_native_projects",
        native_loader or (lambda profile: native),
        raising=False,
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)

    return routes.handle_get(
        SimpleNamespace(), SimpleNamespace(path="/api/projects", query=query)
    )


def test_active_native_project_appears_with_metadata(monkeypatch):
    native = {
        "project_id": "native-1",
        "name": "Native",
        "profile": "alpha",
        "native_project_id": "native-1",
        "project_source": "hermes-agent",
        "read_only": True,
    }

    response = _get_projects(monkeypatch, legacy=[], native=[native])

    assert response["projects"] == [native]


def test_legacy_duplicate_wins_and_unrelated_native_appends(monkeypatch):
    legacy = {"project_id": "shared", "name": "Legacy", "profile": "alpha"}
    native_duplicate = {
        "project_id": "shared",
        "name": "Native duplicate",
        "profile": "alpha",
        "read_only": True,
    }
    native_unrelated = {
        "project_id": "native-2",
        "name": "Native unrelated",
        "profile": "alpha",
        "read_only": True,
    }

    response = _get_projects(
        monkeypatch,
        legacy=[legacy],
        native=[native_duplicate, native_unrelated],
    )

    assert response["projects"] == [legacy, native_unrelated]


def test_foreign_native_with_same_id_is_excluded_without_hiding_active_row(monkeypatch):
    active_legacy = {"project_id": "shared", "name": "Active", "profile": "alpha"}
    foreign_native = {
        "project_id": "shared",
        "name": "Foreign",
        "profile": "beta",
        "read_only": True,
    }

    response = _get_projects(
        monkeypatch,
        legacy=[active_legacy],
        native=[foreign_native],
        active_profile="alpha",
    )

    assert response["projects"] == [active_legacy]


def test_root_default_alias_duplicate_is_deduped(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    is_root = lambda name: name in {"default", "kinni"}
    monkeypatch.setattr(profiles, "_is_root_profile", is_root)
    monkeypatch.setattr(routes, "_is_root_profile", is_root)
    legacy = {"project_id": "root-shared", "name": "Legacy", "profile": "default"}
    native = {
        "project_id": "root-shared",
        "name": "Native",
        "profile": "kinni",
        "read_only": True,
    }

    response = _get_projects(
        monkeypatch,
        legacy=[legacy],
        native=[native],
        active_profile="kinni",
    )

    assert response["projects"] == [legacy]


def test_adapter_none_leaves_scoped_legacy_response_unchanged(monkeypatch):
    active = {"project_id": "active", "name": "Active", "profile": "alpha"}
    foreign = {"project_id": "foreign", "name": "Foreign", "profile": "beta"}

    response = _get_projects(
        monkeypatch,
        legacy=[active, foreign],
        native=None,
        active_profile="alpha",
    )

    assert response["projects"] == [active]


def test_optional_adapter_failure_leaves_scoped_legacy_response_unchanged(monkeypatch):
    active = {"project_id": "active", "name": "Active", "profile": "alpha"}

    def unavailable(_profile):
        raise ImportError("optional backend unavailable")

    response = _get_projects(
        monkeypatch,
        legacy=[active],
        native=[],
        native_loader=unavailable,
    )

    assert response["projects"] == [active]


def test_all_profiles_returns_legacy_aggregate_without_calling_adapter(monkeypatch):
    legacy = [
        {"project_id": "active", "name": "Active", "profile": "alpha"},
        {"project_id": "foreign", "name": "Foreign", "profile": "beta"},
    ]

    def fail_if_called(_profile):
        raise AssertionError("native adapter must not be called for all_profiles")

    response = _get_projects(
        monkeypatch,
        legacy=legacy,
        native=[],
        query="all_profiles=1",
        native_loader=fail_if_called,
    )

    assert response["projects"] == legacy
    assert response["all_profiles"] is True
    assert response["other_profile_count"] == 0


def test_native_rows_do_not_change_legacy_other_profile_count(monkeypatch):
    legacy = [
        {"project_id": "active", "name": "Active", "profile": "alpha"},
        {"project_id": "foreign", "name": "Foreign", "profile": "beta"},
    ]
    native = [
        {
            "project_id": "native",
            "name": "Native",
            "profile": "alpha",
            "read_only": True,
        }
    ]

    response = _get_projects(monkeypatch, legacy=legacy, native=native)

    assert [row["project_id"] for row in response["projects"]] == ["active", "native"]
    assert response["other_profile_count"] == 1


def test_merge_helper_ignores_malformed_native_rows_without_mutating_inputs(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_root_profile", lambda _name: False)
    monkeypatch.setattr(
        routes,
        "_profiles_match",
        lambda row_profile, active_profile: row_profile == active_profile,
    )
    legacy = [{"project_id": "legacy", "profile": "alpha", "name": "Legacy"}]
    native = [
        None,
        "not-a-row",
        {"profile": "alpha", "name": "Missing ID"},
        {"project_id": "native", "profile": "alpha", "name": "Native"},
    ]
    legacy_before = [dict(row) for row in legacy]
    native_before = [dict(row) if isinstance(row, dict) else row for row in native]

    merged = routes._merge_active_profile_projects(legacy, native, "alpha")

    assert [row["project_id"] for row in merged] == ["legacy", "native"]
    assert legacy == legacy_before
    assert native == native_before


def test_merge_helper_skips_native_row_with_missing_profile_for_root(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    is_root = lambda name: name in {"default", "kinni"}
    monkeypatch.setattr(profiles, "_is_root_profile", is_root)
    monkeypatch.setattr(routes, "_is_root_profile", is_root)
    native = {"project_id": "native", "name": "Missing profile"}

    merged = routes._merge_active_profile_projects([], [native], "default")

    assert merged == []


def test_merge_helper_skips_native_row_with_unhashable_project_id(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_root_profile", lambda _name: False)
    monkeypatch.setattr(
        routes,
        "_profiles_match",
        lambda row_profile, active_profile: row_profile == active_profile,
    )
    native = {"project_id": ["native"], "profile": "alpha"}

    merged = routes._merge_active_profile_projects([], [native], "alpha")

    assert merged == []


@pytest.mark.parametrize(
    "profile",
    [None, 7, "", "   "],
    ids=["none", "non-string", "empty", "blank"],
)
def test_merge_helper_skips_native_rows_with_invalid_profiles(monkeypatch, profile):
    import api.routes as routes

    monkeypatch.setattr(routes, "_profiles_match", lambda *_args: True)
    native = {"project_id": "native", "profile": profile}

    merged = routes._merge_active_profile_projects([], [native], "alpha")

    assert merged == []


@pytest.mark.parametrize(
    "project_id",
    [None, 7, "", "   "],
    ids=["none", "non-string", "empty", "blank"],
)
def test_merge_helper_skips_native_rows_with_invalid_project_ids(
    monkeypatch, project_id
):
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_root_profile", lambda _name: False)
    monkeypatch.setattr(routes, "_profiles_match", lambda *_args: True)
    native = {"project_id": project_id, "profile": "alpha"}

    merged = routes._merge_active_profile_projects([], [native], "alpha")

    assert merged == []
