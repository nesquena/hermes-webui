"""Imported Agent sessions inherit native project membership from persisted cwd."""

from __future__ import annotations

from pathlib import Path

import api.models as models
import pytest


def _agent_row(
    session_id: str,
    *,
    cwd: str | None,
    source: str = "tui",
    started_at: float = 9.0,
) -> dict:
    return {
        "id": session_id,
        "title": session_id,
        "model": "test-model",
        "source": source,
        "raw_source": source,
        "message_count": 2,
        "actual_message_count": 2,
        "actual_user_message_count": 1,
        "last_activity": started_at + 1,
        "started_at": started_at,
        "cwd": cwd,
    }


def _load_rows(
    monkeypatch,
    tmp_path: Path,
    rows: list[dict],
    *,
    profile: str | None = None,
    source_filter: str | None = None,
    include_native_project_membership: bool | None = None,
) -> list[dict]:
    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "read_importable_agent_session_rows", lambda *_a, **_kw: rows)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)
    kwargs = {
        "source_filter": source_filter,
        "cron_project_limit": False,
        "webhook_project_limit": False,
        "kanban_project_limit": False,
        "include_claude_code": False,
    }
    if include_native_project_membership is not None:
        kwargs["include_native_project_membership"] = include_native_project_membership
    return models._load_cli_sessions_uncached(
        tmp_path,
        db,
        _cli_profile=profile,
        **kwargs,
    )


def test_ordinary_imported_row_uses_native_project_matched_from_cwd(monkeypatch, tmp_path):
    cwd = "/work/parent/file"
    calls = []

    def resolve(paths, profile_name=None):
        calls.append((list(paths), profile_name))
        return {cwd: "parent-project"}

    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        resolve,
        raising=False,
    )

    result = _load_rows(monkeypatch, tmp_path, [_agent_row("ordinary", cwd=cwd)])

    assert calls == [([cwd], "default")]
    assert result[0]["project_id"] == "parent-project"


def test_mapping_batches_distinct_nonblank_cwds_and_preserves_row_order(monkeypatch, tmp_path):
    calls = []

    def resolve(paths, profile_name=None):
        calls.append((list(paths), profile_name))
        return {
            "/work/one": "one-project",
            "/work/two": "two-project",
            "": "must-not-assign",
        }

    monkeypatch.setattr(models, "native_project_ids_for_paths", resolve, raising=False)
    rows = [
        _agent_row("first", cwd="/work/one", started_at=30.0),
        _agent_row("second", cwd="/work/one", started_at=20.0),
        _agent_row("unmatched", cwd="/work/unmatched", started_at=15.0),
        _agent_row("blank", cwd="", started_at=10.0),
        _agent_row("missing", cwd=None, started_at=5.0),
        _agent_row("third", cwd="/work/two", started_at=1.0),
    ]

    result = _load_rows(monkeypatch, tmp_path, rows, profile="named-profile")

    assert calls == [
        (["/work/one", "/work/unmatched", "/work/two"], "named-profile")
    ]
    assert [row["session_id"] for row in result] == [
        "first",
        "second",
        "unmatched",
        "blank",
        "missing",
        "third",
    ]
    assert [row["project_id"] for row in result] == [
        "one-project",
        "one-project",
        None,
        None,
        None,
        "two-project",
    ]


def test_adapter_none_keeps_ordinary_rows_visible_and_unassigned(monkeypatch, tmp_path):
    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        lambda paths, profile_name=None: None,
        raising=False,
    )

    result = _load_rows(
        monkeypatch,
        tmp_path,
        [_agent_row("ordinary", cwd="/work/unmatched")],
    )

    assert [row["session_id"] for row in result] == ["ordinary"]
    assert result[0]["project_id"] is None


def test_empty_distinct_path_batch_skips_adapter(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        lambda paths, profile_name=None: calls.append((list(paths), profile_name)) or {},
        raising=False,
    )

    result = _load_rows(
        monkeypatch,
        tmp_path,
        [
            _agent_row("blank", cwd=""),
            _agent_row("whitespace", cwd="   "),
            _agent_row("missing", cwd=None),
        ],
    )

    assert calls == []
    assert [row["project_id"] for row in result] == [None, None, None]


def test_native_membership_false_skips_adapter_and_leaves_row_unassigned(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        lambda paths, profile_name=None: calls.append((list(paths), profile_name))
        or {"/work/requested": "orphan-native-project"},
        raising=False,
    )

    result = _load_rows(
        monkeypatch,
        tmp_path,
        [_agent_row("ordinary", cwd="/work/requested")],
        include_native_project_membership=False,
    )

    assert calls == []
    assert [row["session_id"] for row in result] == ["ordinary"]
    assert result[0]["project_id"] is None


def test_all_profiles_scan_disables_native_membership_for_every_context(
    monkeypatch, tmp_path
):
    contexts = [
        (tmp_path / "default", tmp_path / "default" / "state.db", "default"),
        (tmp_path / "alpha", tmp_path / "alpha" / "state.db", "alpha"),
    ]
    calls = []

    def load(_home, _db_path, profile, *, include_native_project_membership=True, **kwargs):
        calls.append((profile, include_native_project_membership))
        return [{
            "session_id": f"{profile}-ordinary",
            "project_id": (
                "orphan-native-project" if include_native_project_membership else None
            ),
        }]

    monkeypatch.setattr(models, "_all_profiles_cli_contexts", lambda: (contexts, "key"))
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 0.0)

    result = models.get_cli_sessions(all_profiles=True, include_claude_code=False)

    assert calls == [("default", False), ("alpha", False)]
    assert [row["project_id"] for row in result] == [None, None]


def test_all_profiles_scan_respects_loader_without_new_membership_keyword(
    monkeypatch, tmp_path
):
    contexts = [(tmp_path, tmp_path / "state.db", "default")]
    calls = []

    def legacy_load(
        _home,
        _db_path,
        profile,
        source_filter=None,
        *,
        visible_session_limit=None,
        cron_project_limit=None,
        webhook_project_limit=None,
        kanban_project_limit=None,
        include_claude_code=True,
    ):
        calls.append(profile)
        return [{"session_id": "legacy-loader-row", "project_id": None}]

    monkeypatch.setattr(models, "_all_profiles_cli_contexts", lambda: (contexts, "key"))
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", legacy_load)
    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 0.0)

    result = models.get_cli_sessions(all_profiles=True, include_claude_code=False)

    assert calls == ["default"]
    assert result == [{"session_id": "legacy-loader-row", "project_id": None}]


def test_adapter_list_result_keeps_rows_visible_and_unassigned(monkeypatch, tmp_path):
    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        lambda paths, profile_name=None: ["malformed-project-id"],
        raising=False,
    )

    result = _load_rows(
        monkeypatch,
        tmp_path,
        [_agent_row("ordinary", cwd="/work/requested")],
    )

    assert [row["session_id"] for row in result] == ["ordinary"]
    assert result[0]["project_id"] is None


def test_adapter_mapping_retains_only_requested_nonblank_string_ids(
    monkeypatch, tmp_path
):
    class MalformedMapping(dict):
        def items(self):
            return [
                (["unhashable-key"], "unhashable-key-project"),
                *super().items(),
            ]

    mapping = MalformedMapping({
        "/work/valid": "  project-id-preserved  ",
        "/work/list": ["project-list"],
        "/work/none": None,
        "/work/blank": "   ",
        "/work/non-string": 42,
        "/work/foreign": "foreign-project",
        7: "numeric-key-project",
        ("tuple-key",): "tuple-key-project",
    })
    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        lambda paths, profile_name=None: mapping,
        raising=False,
    )
    rows = [
        _agent_row("valid", cwd="/work/valid"),
        _agent_row("list", cwd="/work/list"),
        _agent_row("none", cwd="/work/none"),
        _agent_row("blank", cwd="/work/blank"),
        _agent_row("non-string", cwd="/work/non-string"),
    ]

    result = _load_rows(monkeypatch, tmp_path, rows)

    assert [row["session_id"] for row in result] == [
        "valid",
        "list",
        "none",
        "blank",
        "non-string",
    ]
    assert [row["project_id"] for row in result] == [
        "  project-id-preserved  ",
        None,
        None,
        None,
        None,
    ]


def test_adapter_error_keeps_rows_unassigned_without_logging_cwd(
    monkeypatch, tmp_path, caplog
):
    cwd = "/private/do-not-log"

    def fail(_paths, profile_name=None):
        raise RuntimeError(cwd)

    monkeypatch.setattr(models, "native_project_ids_for_paths", fail, raising=False)

    with caplog.at_level("DEBUG", logger=models.__name__):
        result = _load_rows(monkeypatch, tmp_path, [_agent_row("ordinary", cwd=cwd)])

    assert [row["session_id"] for row in result] == ["ordinary"]
    assert result[0]["project_id"] is None
    assert "Native project membership lookup failed" in caplog.text
    assert cwd not in caplog.text


@pytest.mark.parametrize(
    ("source", "expected_project_id"),
    [
        (" Cron ", "cron-project"),
        (" WebHook ", "webhook-project"),
        (" KANBAN ", None),
    ],
)
def test_system_only_pass_preserves_system_precedence_without_native_lookup(
    monkeypatch, tmp_path, source, expected_project_id
):
    calls = []
    monkeypatch.setattr(
        models,
        "native_project_ids_for_paths",
        lambda paths, profile_name=None: calls.append((list(paths), profile_name))
        or {"/work/matched": "native-project"},
        raising=False,
    )
    monkeypatch.setattr(models, "_profile_has_user_projects", lambda: True)
    monkeypatch.setattr(
        models,
        "ensure_cron_project",
        lambda **_kwargs: "cron-project",
    )
    monkeypatch.setattr(
        models,
        "ensure_webhook_project",
        lambda: "webhook-project",
    )

    result = _load_rows(
        monkeypatch,
        tmp_path,
        [_agent_row("plain-system-row", cwd="/work/matched", source=source)],
        source_filter=source,
    )

    assert calls == []
    assert result[0]["project_id"] == expected_project_id


def test_mixed_pass_maps_only_ordinary_rows_and_keeps_system_projects(
    monkeypatch, tmp_path
):
    calls = []

    def resolve(paths, profile_name=None):
        calls.append((list(paths), profile_name))
        return {
            "/work/nested/file": "nested-native-project",
            "/work/system": "must-not-win",
        }

    monkeypatch.setattr(models, "native_project_ids_for_paths", resolve, raising=False)
    monkeypatch.setattr(models, "_profile_has_user_projects", lambda: True)
    monkeypatch.setattr(
        models,
        "ensure_cron_project",
        lambda **_kwargs: "cron-project",
    )
    monkeypatch.setattr(
        models,
        "ensure_webhook_project",
        lambda: "webhook-project",
    )
    rows = [
        _agent_row("nested", cwd="/work/nested/file", source="acp"),
        _agent_row("cron-row", cwd="/work/system", source=" Cron "),
        _agent_row("webhook-row", cwd="/work/system", source=" WebHook "),
        _agent_row("kanban-row", cwd="/work/system", source=" KANBAN "),
    ]

    result = _load_rows(monkeypatch, tmp_path, rows)

    assert calls == [(["/work/nested/file"], "default")]
    assert {row["session_id"]: row["project_id"] for row in result} == {
        "nested": "nested-native-project",
        "cron-row": "cron-project",
        "webhook-row": "webhook-project",
        "kanban-row": None,
    }
    assert {row["session_id"]: row["source_tag"] for row in result} == {
        "nested": "acp",
        "cron-row": " Cron ",
        "webhook-row": " WebHook ",
        "kanban-row": " KANBAN ",
    }
