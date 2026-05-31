import importlib
import re
import subprocess
import threading
from collections import Counter
from pathlib import Path


def test_session_recall_payload_requires_query_and_does_not_dump_sessions(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")

    def fail_if_sources_are_read(*args, **kwargs):  # pragma: no cover - defensive assertion helper
        raise AssertionError("blank session recall query must not read or dump sessions")

    monkeypatch.setattr(recall, "_read_session_recall_sources", fail_if_sources_are_read, raising=False)

    payload = recall.build_operator_session_recall_payload(query_text="", now=123.0)

    assert payload["would_execute"] is False
    assert payload["results"] == []
    assert payload["count"] == 0
    assert payload["status"] in {"unknown", "stale"}
    assert any("query" in issue.lower() and "required" in issue.lower() for issue in payload["issues"])


def test_session_recall_payload_has_version_mode_timestamp_sources_and_count(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    monkeypatch.setattr(recall, "_read_session_recall_sources", lambda all_profiles=False: [], raising=False)

    payload = recall.build_operator_session_recall_payload(
        query_text="operator commitments",
        limit=99,
        per_session=0,
        all_profiles=True,
        now=456.5,
    )

    assert {
        "version",
        "mode",
        "generated_at",
        "status",
        "query",
        "sources",
        "results",
        "count",
        "issues",
        "would_execute",
    }.issubset(payload.keys())
    assert payload["version"] == 1
    assert payload["mode"] == "session-recall-read-only"
    assert payload["generated_at"] == 456.5
    assert payload["would_execute"] is False
    assert payload["query"] == {
        "text": "operator commitments",
        "limit": 50,
        "per_session": 1,
        "all_profiles": True,
    }
    assert payload["sources"] == []
    assert payload["results"] == []
    assert payload["count"] == 0


def test_routes_exposes_operator_session_recall_get_near_operator_routes():
    routes_text = Path("api/routes.py").read_text(encoding="utf-8")

    assert '"/api/operator/session-recall"' in routes_text
    assert "build_operator_session_recall_payload" in routes_text

    memory_review_idx = routes_text.index('if parsed.path == "/api/operator/memory-skill-review":')
    session_recall_idx = routes_text.index('if parsed.path == "/api/operator/session-recall":')
    models_idx = routes_text.index('if parsed.path == "/api/models":')
    assert memory_review_idx < session_recall_idx < models_idx

    session_recall_block = routes_text[session_recall_idx:models_idx]
    for param in ('"q"', '"limit"', '"per_session"', '"all_profiles"'):
        assert param in session_recall_block
    assert "j(handler, payload)" in session_recall_block


def test_session_recall_finds_message_match_with_snippet_timestamp_source_and_hash(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    long_content = (
        "intro "
        + ("before " * 80)
        + "needle-context proof-bearing detail"
        + (" after" * 80)
    )
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_alpha",
                "title": "Research decisions for recall",
                "profile": "default",
                "source_label": "WebUI",
                "source_tag": "webui",
                "messages": [
                    {"role": "user", "content": "no match here", "timestamp": 1700000000.0},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": long_content}],
                        "timestamp": 1700000123.5,
                    },
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(
        query_text="needle-context",
        limit=10,
        per_session=3,
        now=1700000200.0,
    )

    assert payload["would_execute"] is False
    assert payload["count"] == 1
    result = payload["results"][0]
    assert result["session"]["session_id"] == "session_alpha"
    assert result["session"]["title"] == "Research decisions for recall"
    assert result["session"]["profile"] == "default"
    assert result["session"]["source_label"] == "WebUI"
    assert result["session"]["source_tag"] == "webui"
    assert result["match"]["type"] == "message"
    assert result["match"]["message_index"] == 1
    assert result["match"]["role"] == "assistant"
    assert result["match"]["timestamp"] == 1700000123.5
    assert "needle-context proof-bearing detail" in result["match"]["snippet"]
    assert len(result["match"]["snippet"]) <= 280
    assert long_content not in result["match"]["snippet"]
    content_hash = result["match"]["content_hash"]
    assert content_hash.startswith("sha256:")
    assert len(content_hash) == len("sha256:") + 64
    assert result["promotion"]["task"]["would_execute"] is False
    assert result["promotion"]["memory_review"]["would_execute"] is False
    task_source = result["promotion"]["task"]["source"]
    assert task_source["kind"] == "session_message"
    assert task_source["session_id"] == "session_alpha"
    assert task_source["message_index"] == 1
    assert task_source["content_hash"] == content_hash
    memory_source = result["promotion"]["memory_review"]["source_evidence"][0]
    assert memory_source["kind"] == "session_message"
    assert memory_source["session_id"] == "session_alpha"
    assert memory_source["message_index"] == 1
    assert memory_source["content_hash"] == content_hash


def test_session_recall_does_not_promote_title_only_matches_as_session_message_evidence(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_title_only",
                "title": "Needle-only planning title",
                "profile": "default",
                "messages": [
                    {"role": "user", "content": "ordinary setup without the search term", "timestamp": 1700000000.0},
                    {"role": "assistant", "content": "follow-up also lacks that evidence", "timestamp": 1700000001.0},
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle-only", now=1700000002.0)

    assert payload["results"] == []
    assert payload["count"] == 0


def test_session_recall_labels_historical_matches_older_than_30_days(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    now = 1700000000.0
    old_timestamp = now - (31 * 24 * 60 * 60)
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_old",
                "title": "Old discussion",
                "messages": [
                    {"role": "user", "content": "needle old commitment", "timestamp": old_timestamp},
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=now)

    assert payload["count"] == 1
    assert payload["results"][0]["recency"]["label"] == "historical"
    assert "30" in payload["results"][0]["recency"]["reason"]


def test_session_recall_labels_unknown_when_timestamp_missing(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_no_timestamp",
                "title": "Missing timestamp",
                "messages": [
                    {"role": "assistant", "content": "needle without a timestamp"},
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000000.0)

    assert payload["count"] == 1
    assert payload["results"][0]["recency"]["label"] == "unknown"
    assert "timestamp" in payload["results"][0]["recency"]["reason"].lower()


def test_session_recall_redacts_secret_before_snippet_windowing(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    very_long_secret = "".join(f"SECRET{i:03d}" for i in range(80))
    leaked_chunk = very_long_secret[500:560]
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_secret_window",
                "title": "Secret window regression",
                "messages": [
                    {
                        "role": "user",
                        "content": f"prefix password={very_long_secret} needle after",
                        "timestamp": 1700000000.0,
                    },
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000001.0)

    assert payload["count"] == 1
    snippet = payload["results"][0]["match"]["snippet"]
    assert leaked_chunk not in snippet
    assert "[redacted]" in snippet.lower()


def test_session_recall_redacts_snippets_and_titles(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    raw_password = "hunter2-correct-horse"
    raw_token = "sk-live-abcdef1234567890abcdef1234567890"
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_secret",
                "title": f"Rotate password={raw_password} and api_key={raw_token}",
                "messages": [
                    {
                        "role": "user",
                        "content": f"needle shows token={raw_token} and password: {raw_password}",
                        "timestamp": 1700000000.0,
                    },
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000001.0)

    assert payload["count"] == 1
    result = payload["results"][0]
    assert raw_password not in result["session"]["title"]
    assert raw_token not in result["session"]["title"]
    assert raw_password not in result["match"]["snippet"]
    assert raw_token not in result["match"]["snippet"]
    assert "[redacted]" in result["session"]["title"].lower()
    assert "[redacted]" in result["match"]["snippet"].lower()


def test_session_recall_redacts_github_token_shapes_from_title_snippet_and_source_quotes(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    raw_classic_pat = "ghp_" + "A" * 36
    raw_fine_grained_pat = "github_pat_" + "B" * 22 + "_" + "C" * 59
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_github_secret",
                "title": f"Rotate GitHub tokens {raw_classic_pat} {raw_fine_grained_pat}",
                "messages": [
                    {
                        "role": "user",
                        "content": f"needle must not leak {raw_classic_pat} or {raw_fine_grained_pat}",
                        "timestamp": 1700000000.0,
                    },
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000001.0)

    assert payload["count"] == 1
    result = payload["results"][0]
    fields = [
        result["session"]["title"],
        result["match"]["snippet"],
        result["promotion"]["task"]["source"]["quote"],
        result["promotion"]["memory_review"]["source_evidence"][0]["quote"],
    ]
    for field in fields:
        assert raw_classic_pat not in field
        assert raw_fine_grained_pat not in field
        assert "[redacted]" in field.lower()


def test_session_recall_redacts_github_tokens_before_punctuation_boundaries(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    token_cases = [
        ("classic", "ghp_" + "A" * 36),
        ("fine-grained", "github_pat_" + "B" * 22 + "_" + "C" * 59),
    ]
    delimiter_cases = [
        ("period", "."),
        ("close paren", ")"),
        ("close bracket", "]"),
        ("double quote", '"'),
        ("newline", "\nnext line"),
    ]

    for token_label, raw_token in token_cases:
        for delimiter_label, delimiter in delimiter_cases:
            monkeypatch.setattr(
                recall,
                "_read_session_recall_sources",
                lambda all_profiles=False, raw_token=raw_token, delimiter=delimiter, token_label=token_label, delimiter_label=delimiter_label: [
                    {
                        "session_id": f"session_{token_label}_{delimiter_label}".replace(" ", "_"),
                        "title": f"Rotate token {raw_token}{delimiter} before release",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"needle must redact {raw_token}{delimiter} from all recall surfaces",
                                "timestamp": 1700000000.0,
                            },
                        ],
                    }
                ],
                raising=False,
            )

            payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000001.0)

            assert payload["count"] == 1, f"{token_label} followed by {delimiter_label}"
            result = payload["results"][0]
            fields = [
                result["session"]["title"],
                result["match"]["snippet"],
                result["promotion"]["task"]["source"]["quote"],
                result["promotion"]["memory_review"]["source_evidence"][0]["quote"],
            ]
            for field in fields:
                assert raw_token not in field, f"{token_label} followed by {delimiter_label}: {field}"
                assert "[redacted]" in field.lower(), f"{token_label} followed by {delimiter_label}: {field}"


def test_session_recall_clamps_limits_and_per_session(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    rows = []
    for session_number in range(12):
        rows.append(
            {
                "session_id": f"session_{session_number}",
                "title": f"Session {session_number}",
                "messages": [
                    {
                        "role": "user",
                        "content": f"needle match {session_number}-{message_number}",
                        "timestamp": 1700000000.0 + message_number,
                    }
                    for message_number in range(7)
                ],
            }
        )
    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: rows,
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(
        query_text="needle",
        limit=999,
        per_session=999,
        now=1700000100.0,
    )

    assert payload["query"]["limit"] == 50
    assert payload["query"]["per_session"] == 5
    assert payload["count"] == 50
    assert len(payload["results"]) == 50
    counts = Counter(result["session"]["session_id"] for result in payload["results"])
    assert counts
    assert max(counts.values()) <= 5


def test_session_recall_does_not_call_mutating_helpers(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")
    source_text = Path("api/operator_session_recall.py").read_text(encoding="utf-8")

    forbidden_literals = (
        "all_sessions",
        "Session.load",
        ".save(",
        "write_text",
        "subprocess",
        "threading.Thread",
        "threading.Timer",
        "kanban",
        "cron",
        "goal",
        "/api/chat",
        "/api/memory/write",
        "/api/skills/save",
        "/apply",
    )
    for forbidden_text in forbidden_literals:
        assert forbidden_text not in source_text
    assert re.search(r"\bopen\s*\([^\n)]*(?:,\s*|mode\s*=)[\"']w[\"']", source_text) is None

    def fail_if_called(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("session recall must not call mutating/background helpers")

    monkeypatch.setattr(subprocess, "run", fail_if_called, raising=True)
    monkeypatch.setattr(subprocess, "Popen", fail_if_called, raising=True)
    monkeypatch.setattr(threading, "Thread", fail_if_called, raising=True)
    monkeypatch.setattr(threading, "Timer", fail_if_called, raising=True)

    monkeypatch.setattr(
        recall,
        "_read_session_recall_sources",
        lambda all_profiles=False: [
            {
                "session_id": "session_read_only_guard",
                "title": "Read-only recall guard",
                "profile": "default",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "needle proves the recall path only searches source rows",
                        "timestamp": 1700000000.0,
                    }
                ],
            }
        ],
        raising=False,
    )

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000001.0)

    assert payload["would_execute"] is False
    assert payload["count"] == 1
    assert payload["results"][0]["session"]["session_id"] == "session_read_only_guard"


def test_session_recall_handles_source_reader_failure_as_unknown_without_fake_results(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")

    def fail_source_read(*args, **kwargs):
        raise RuntimeError("session index unavailable: token=supersecret")

    monkeypatch.setattr(recall, "_read_session_recall_sources", fail_source_read, raising=False)

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000000.0)

    assert payload["status"] == "unknown"
    assert payload["results"] == []
    assert payload["count"] == 0
    assert payload["would_execute"] is False

    source_states = {(source.get("id"), source.get("kind"), source.get("state")) for source in payload["sources"]}
    issue_text = "\n".join(payload["issues"])
    assert ("webui_sessions", "session_json", "unknown") in source_states or "session index unavailable" in issue_text
    assert "RuntimeError" in issue_text
    assert "session index unavailable" in issue_text
    payload_text = repr(payload)
    assert "supersecret" not in payload_text


def test_session_recall_reports_real_source_reader_glob_failure_with_sanitized_issue(monkeypatch):
    recall = importlib.import_module("api.operator_session_recall")

    def fail_glob(*args, **kwargs):
        raise RuntimeError("session dir unavailable: token=supersecret " + "x" * 1000)

    monkeypatch.setattr(recall.Path, "glob", fail_glob, raising=True)

    payload = recall.build_operator_session_recall_payload(query_text="needle", now=1700000000.0)

    assert payload["status"] == "unknown"
    assert payload["results"] == []
    assert payload["count"] == 0
    assert payload["would_execute"] is False

    source_states = {(source.get("id"), source.get("kind"), source.get("state")) for source in payload["sources"]}
    assert ("webui_sessions", "session_json", "unknown") in source_states

    issue_text = "\n".join(payload["issues"])
    source_issue_text = "\n".join(str(source.get("issue", "")) for source in payload["sources"])
    combined_issue_text = f"{issue_text}\n{source_issue_text}"
    assert "RuntimeError" in combined_issue_text
    assert "session dir unavailable" in combined_issue_text
    assert "supersecret" not in repr(payload)
    assert payload["issues"]
    assert all(len(issue) <= 260 for issue in payload["issues"])
    assert all(len(str(source.get("issue", ""))) <= 260 for source in payload["sources"])


def test_session_recall_does_not_modify_existing_sessions_search_contract_static():
    routes_text = Path("api/routes.py").read_text(encoding="utf-8")

    legacy_idx = routes_text.index('if parsed.path == "/api/sessions/search":')
    legacy_block = "\n".join(routes_text[legacy_idx:].splitlines()[:3])
    assert "return _handle_sessions_search(handler, parsed)" in legacy_block
    assert "build_operator_session_recall_payload" not in legacy_block
    assert '"/api/operator/session-recall"' in routes_text
    assert routes_text.index('if parsed.path == "/api/operator/session-recall":') != legacy_idx
