import json


def _tool_partial(reasoning="same reasoning", args=None, *, timestamp=123, token=None):
    row = {
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
    if token is not None:
        row["_active_turn_token"] = token
    return row


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


def test_runtime_partial_dedupe_preserves_foreign_turn_tokens():
    from api.streaming import _partial_marker_already_present

    existing = [
        {"role": "user", "content": "current", "_active_turn_token": "current:1"},
        _tool_partial(token="foreign:1"),
    ]

    assert not _partial_marker_already_present(
        existing,
        _tool_partial(token="current:1"),
        before_idx=len(existing),
    )
    assert _partial_marker_already_present(
        existing,
        _tool_partial(token="foreign:1"),
        before_idx=len(existing),
    )


def test_runtime_partial_marker_requires_both_or_neither_turn_tokens():
    from api.streaming import _partial_marker_already_present

    for existing_token, candidate_token in ((None, "current:1"), ("current:1", None)):
        existing = [
            {"role": "user", "content": "current"},
            _tool_partial(token=existing_token),
        ]
        assert not _partial_marker_already_present(
            existing,
            _tool_partial(token=candidate_token),
            before_idx=len(existing),
        )


def test_runtime_partial_upsert_requires_matching_token_presence():
    from api.streaming import _upsert_current_turn_partial

    for existing_token, candidate_token, identity in (
        (None, "current:1", {"token": "current:1"}),
        ("current:1", None, None),
    ):
        messages = [
            {"role": "user", "content": "current"},
            _tool_partial(token=existing_token),
        ]
        _upsert_current_turn_partial(
            messages,
            _tool_partial(token=candidate_token),
            active_turn_identity=identity,
        )
        assert sum(message.get("_partial") is True for message in messages) == 2


def test_runtime_partial_upsert_keeps_same_token_and_untagged_controls():
    from api.streaming import _upsert_current_turn_partial

    for token, identity in (("current:1", {"token": "current:1"}), (None, None)):
        messages = [
            {"role": "user", "content": "current"},
            _tool_partial(token=token),
        ]
        _upsert_current_turn_partial(
            messages,
            _tool_partial(token=token),
            active_turn_identity=identity,
        )
        assert sum(message.get("_partial") is True for message in messages) == 1


def test_current_partial_upsert_merges_untagged_canonical_result():
    from api.streaming import _upsert_current_turn_partial

    token = "stream-revision-stale-result:10"
    canonical = {
        "role": "assistant",
        "content": "Partial work completed before the conflict.",
    }
    messages = [
        {"role": "user", "content": "current", "_active_turn_token": token},
        {
            "role": "assistant",
            "content": "Partial work completed before the conflict with a newer suffix.",
            "_partial": True,
            "_active_turn_token": token,
        },
        canonical,
    ]

    result = _upsert_current_turn_partial(
        messages,
        {
            "role": "assistant",
            "content": "Partial work completed before the conflict.",
            "_partial": True,
        },
        active_turn_identity={"token": token},
    )

    assistant_rows = [message for message in messages if message.get("role") == "assistant"]
    assert len(assistant_rows) == 1
    assert result is assistant_rows[0]
    assert result["_active_turn_token"] == token
    assert result["content"].endswith("with a newer suffix.")


def test_current_partial_upsert_does_not_merge_foreign_or_mismatched_canonical():
    from api.streaming import _upsert_current_turn_partial

    current_token = "current:1"
    cases = (
        ("foreign:1", None, current_token),
        (None, "foreign:1", current_token),
        (None, current_token, "foreign:1"),
    )
    for canonical_token, candidate_token, identity_token in cases:
        canonical = {
            "role": "assistant",
            "content": "same exact result",
            **({"_active_turn_token": canonical_token} if canonical_token else {}),
        }
        messages = [
            {"role": "user", "content": "current", "_active_turn_token": current_token},
            canonical,
        ]
        candidate = {
            "role": "assistant",
            "content": "same exact result",
            "_partial": True,
            **({"_active_turn_token": candidate_token} if candidate_token else {}),
        }

        _upsert_current_turn_partial(
            messages,
            candidate,
            active_turn_identity={"token": identity_token},
        )

        assistant_rows = [message for message in messages if message.get("role") == "assistant"]
        assert len(assistant_rows) == 2
        assert canonical in assistant_rows


def test_load_partial_dedupe_preserves_foreign_turn_tokens():
    import api.models as models

    first = _tool_partial(token="current:1")
    second = _tool_partial(token="foreign:1")
    collapsed, changed = models._collapse_adjacent_duplicate_partials([first, second])
    assert collapsed == [first, second]
    assert changed is False


def test_load_partial_dedupe_does_not_treat_untagged_rows_as_wildcards():
    import api.models as models

    untagged = _tool_partial()
    current = _tool_partial(token="current:1")
    foreign = _tool_partial(token="foreign:1")
    rows = [untagged, current, foreign]

    collapsed, changed = models._collapse_adjacent_duplicate_partials(rows)

    assert collapsed == rows
    assert changed is False


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
