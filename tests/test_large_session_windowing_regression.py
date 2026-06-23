from pathlib import Path
from types import SimpleNamespace

import api.routes as routes

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_limited_webui_messages_uses_sidecar_without_old_state_merge(monkeypatch):
    """Windowed /api/session must not fuzzy-merge old state.db mirror rows."""

    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "old", "timestamp": 10},
            {"role": "assistant", "content": "tail", "timestamp": 20},
        ],
        truncation_watermark=None,
    )

    def forbidden_lineage(_session):  # pragma: no cover - failure path
        raise AssertionError("lineage stitch should not run when current sidecar has messages")

    def forbidden_merge(*_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("old state mirror rows should not trigger full fuzzy merge")

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", forbidden_lineage)
    monkeypatch.setattr(routes, "merge_session_messages_append_only", forbidden_merge)

    result = routes._limited_webui_messages_for_display(
        session,
        [{"role": "user", "content": "old mirror", "timestamp": 5}],
    )

    assert result is not session.messages
    assert result == session.messages


def test_limited_webui_messages_merges_only_newer_state_rows(monkeypatch):
    session = SimpleNamespace(
        messages=[{"role": "assistant", "content": "sidecar tail", "timestamp": 20}],
        truncation_watermark=None,
    )
    calls = []

    def fake_merge(sidecar, state_rows, **kwargs):
        calls.append((list(sidecar), list(state_rows), kwargs))
        return list(sidecar) + list(state_rows)

    monkeypatch.setattr(routes, "merge_session_messages_append_only", fake_merge)

    result = routes._limited_webui_messages_for_display(
        session,
        [
            {"role": "user", "content": "old mirror", "timestamp": 10},
            {"role": "assistant", "content": "new state tail", "timestamp": 30},
        ],
    )

    assert len(calls) == 1
    assert [m["content"] for m in calls[0][1]] == ["new state tail"]
    assert [m["content"] for m in result] == ["sidecar tail", "new state tail"]


def test_session_message_window_constants_are_bounded():
    assert routes._DEFAULT_SESSION_MSG_LIMIT == 100
    assert routes._MAX_SESSION_MSG_LIMIT == 300


def test_outline_does_not_hidden_load_full_transcript():
    outline = (STATIC / "outline.js").read_text(encoding="utf-8")

    assert "msg_limit=9999" not in outline
    assert "_ensureAllMessagesLoaded" not in outline
    assert "Large sessions must not trigger a hidden full-history fetch" in outline


def test_frontend_full_history_loader_is_page_based():
    sessions = (STATIC / "sessions.js").read_text(encoding="utf-8")

    assert "messages=1&resolve_model=0`, {timeoutMs:120000}" not in sessions
    assert "msg_before=${oldest}&msg_limit=300" in sessions
    assert "msg_before=${_oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}" in sessions
    assert "let nextMessages = [...olderMsgs, ...S.messages];" in sessions


def test_static_session_message_fetches_are_bounded_or_metadata_only():
    offenders = []
    for path in STATIC.glob("*.js"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "/api/session?session_id" not in line:
                continue
            if "messages=0" in line or "msg_limit=" in line or "${reloadLimitParam}" in line:
                continue
            offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert offenders == []
