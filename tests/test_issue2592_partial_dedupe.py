import json


def _tool_partial(reasoning="same reasoning", args=None, *, timestamp=123):
    return {
        "role": "assistant",
        "content": "",
        "_partial": True,
        "timestamp": timestamp,
        "reasoning": reasoning,
        "_partial_tool_calls": [
            {
                "name": "execute_code",
                "args": args or {"code": "raise RuntimeError('boom')"},
                "done": True,
                "is_error": True,
                "duration": 3.87,
            }
        ],
    }


def test_tool_only_partial_dedupe_uses_reasoning_and_tool_signature():
    from api.streaming import _partial_marker_already_present

    existing = [
        {"role": "user", "content": "run this"},
        _tool_partial(),
        {"role": "assistant", "content": "**Task cancelled.**", "_error": True},
    ]

    assert _partial_marker_already_present(existing, _tool_partial(), before_idx=2)
    assert not _partial_marker_already_present(
        existing,
        _tool_partial(args={"code": "print('different tool body')"}),
        before_idx=2,
    )


def test_tool_only_partial_dedupe_is_scoped_to_current_user_turn():
    from api.streaming import _partial_marker_already_present

    existing = [
        {"role": "user", "content": "first run"},
        _tool_partial(),
        {"role": "assistant", "content": "**Task cancelled.**", "_error": True},
        {"role": "user", "content": "repeat it"},
    ]

    assert not _partial_marker_already_present(existing, _tool_partial(), before_idx=len(existing))


def test_session_load_collapses_adjacent_duplicate_partials(tmp_path, monkeypatch):
    import api.models as models

    sid = "abc123"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    payload = {
        "session_id": sid,
        "title": "bloated partials",
        "workspace": str(tmp_path),
        "model": "gpt-5.5",
        "created_at": 100.0,
        "updated_at": 200.0,
        "messages": [
            {"role": "user", "content": "run this"},
            _tool_partial(timestamp=123),
            _tool_partial(timestamp=123),
            _tool_partial(timestamp=123),
            {"role": "assistant", "content": "**Task cancelled.**", "_error": True},
        ],
        "tool_calls": [],
    }
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert sum(1 for message in loaded.messages if message.get("_partial")) == 1
    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert sum(1 for message in persisted["messages"] if message.get("_partial")) == 1
    assert persisted["updated_at"] == 200.0
    assert (session_dir / f"{sid}.json.bak").exists()
