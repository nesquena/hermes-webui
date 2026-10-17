"""The context brief must preserve the prompt→conclusion sequence across compaction.

User report (2026-08-17): after live compaction, the brief no longer showed the
initial user request — the child sidecar starts at the compression anchor, and
build_deterministic_brief read only the child's own messages.

Contract under test:
- _session_messages stitches pre_compression_snapshot parents (same lineage
  helper the transcript uses), so the ORIGINAL ask and PRE-compaction
  conclusions are visible to the brief;
- the request/conclusion caps keep the FIRST entry plus the recent tail;
- forks / no-parent sessions / stitch failures fall back to own messages.
"""

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def brief_env(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.context_brief as brief

    # Force-import every module context_brief imports lazily at call time
    # BEFORE patching path constants: their `from api.config import STATE_DIR`
    # style bindings would otherwise capture the patched tmp paths permanently
    # for the rest of the pytest process (observed in CI: api.routes first
    # imported inside build_deterministic_brief, then later tests saw sessions
    # written to/read from a deleted tmp dir).
    import api.background  # noqa: F401
    import api.goals  # noqa: F401
    import api.process_event_utils  # noqa: F401
    import api.profiles  # noqa: F401
    import api.routes  # noqa: F401
    import api.todo_state  # noqa: F401

    state = tmp_path / "state"
    sessions = state / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state))
    # Redirect the module-level path constants captured at import time to the
    # tmp store. monkeypatch reverts everything, so nothing leaks into the
    # rest of the pytest process (unlike module re-imports, which split state
    # for later tests — seen in CI: dozens of unrelated failures after this
    # file ran).
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "SESSION_DIR", sessions)
    monkeypatch.setattr(config, "SETTINGS_FILE", state / "settings.json")
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "index.json")
    # The goal/in-flight extractors construct the native goal manager, whose
    # constructor eagerly initializes the full state.db schema over a
    # long-lived writable connection. Under pytest that perturbs the shared
    # test database for unrelated followers. These tests never assert goal
    # or in-flight content, so stub both extractors.
    monkeypatch.setattr(brief, "_extract_goal", lambda sid, session: None)
    monkeypatch.setattr(brief, "_extract_in_flight", lambda sid, session: {})
    sessions_before = set(config.SESSIONS.keys())
    yield types.SimpleNamespace(config=config, models=models, brief=brief)
    # Session.save() registers into the shared in-memory SESSIONS registry:
    # evict the sessions this test created so later tests' listings and
    # lineage computations stay clean.
    for sid in set(config.SESSIONS.keys()) - sessions_before:
        config.SESSIONS.pop(sid, None)
    # The lineage stitch goes through the cached CLI-session projection: drop
    # entries warmed under the tmp env so later tests recompute from the real
    # store (official invalidation hook, documented for test isolation).
    models.clear_cli_sessions_cache()


def _build_compacted_lineage(env):
    """Parent snapshot with the original ask; child continuation without it."""
    models = env.models
    now = time.time()
    parent = models.Session(session_id="20260101_000000_parent")
    parent.messages = [
        {"role": "user", "content": "DEMANDE INITIALE: corrige le module planning", "timestamp": now},
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 Réponse / recommandation: premier correctif livré", "timestamp": now + 10},
    ]
    parent.pre_compression_snapshot = True
    parent.save()

    child = models.Session(session_id="20260101_000100_child0")
    child.parent_session_id = parent.session_id
    child.messages = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION] resumé du travail", "timestamp": now + 20},
        {"role": "user", "content": "poursuis avec le second lot", "timestamp": now + 30},
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 Réponse / recommandation: second lot livré", "timestamp": now + 40},
    ]
    child.save()
    return parent, child


def test_brief_includes_pre_compaction_request(brief_env):
    _parent, child = _build_compacted_lineage(brief_env)
    brief = brief_env.brief.build_deterministic_brief(child, child.session_id, source="webui")
    texts = [r["text"] for r in brief["requests"]]
    assert any("DEMANDE INITIALE" in t for t in texts), texts


def test_brief_includes_pre_compaction_conclusion(brief_env):
    _parent, child = _build_compacted_lineage(brief_env)
    brief = brief_env.brief.build_deterministic_brief(child, child.session_id, source="webui")
    excerpts = [c["excerpt"] for c in brief["accomplished"]["conclusions"]]
    assert any("premier correctif" in e for e in excerpts), excerpts
    assert any("second lot" in e for e in excerpts), excerpts


def test_request_cap_keeps_first_request(brief_env):
    models = brief_env.models
    now = time.time()
    s = models.Session(session_id="20260101_000200_caps00")
    s.messages = [
        {"role": "user", "content": "PREMIERE DEMANDE fondatrice", "timestamp": now},
    ]
    for i in range(20):
        s.messages.append({
            "role": "user", "content": f"demande de suivi {i}", "timestamp": now + 10 + i,
        })
    s.save()
    brief = brief_env.brief.build_deterministic_brief(s, s.session_id, source="webui")
    texts = [r["text"] for r in brief["requests"]]
    assert any("PREMIERE DEMANDE" in t for t in texts), texts
    assert brief["request_count"] == 21


def test_no_parent_session_unchanged(brief_env):
    models = brief_env.models
    now = time.time()
    s = models.Session(session_id="20260101_000300_plain0")
    s.messages = [
        {"role": "user", "content": "simple ask", "timestamp": now},
        {"role": "assistant", "content": "simple answer", "timestamp": now + 1},
    ]
    s.save()
    msgs = brief_env.brief._session_messages(s)
    assert [m["content"] for m in msgs] == ["simple ask", "simple answer"]


def test_requests_exclude_synthetic_user_messages(brief_env):
    """Only messages the user actually typed may appear as requests.

    User report (2026-08-18): the brief showed '[Your active task list was
    preserved across context compression]' blocks as if they were his prompts.
    Those user-role messages are injected by the runtime around compression
    boundaries, as are pruned-skill markers; both must be filtered out.
    The same typed prompt persisted twice with different timestamps (lineage /
    state.db merge) must also collapse to a single request entry.
    """
    models = brief_env.models
    now = time.time()
    s = models.Session(session_id="20260101_000500_synth0")
    s.messages = [
        {"role": "user", "content": "vraie demande de correction", "timestamp": now},
        {
            "role": "user",
            "content": "[Your active task list was preserved across context compression]\n- [>] 1. tâche",
            "timestamp": now + 1,
        },
        {
            "role": "user",
            "content": "[Skills pruned during compression — reload before acting on these tasks]",
            "timestamp": now + 2,
        },
        {
            "role": "user",
            "content": "[SKILL_PRUNED: content lost in compression]",
            "timestamp": now + 3,
        },
        # Same typed prompt duplicated with a drifted timestamp (lineage merge).
        {"role": "user", "content": "vraie demande de correction", "timestamp": now + 4.5},
        {"role": "assistant", "content": "ok", "timestamp": now + 5},
    ]
    s.save()
    payload = brief_env.brief.build_deterministic_brief(s, s.session_id, source="webui")
    texts = [r["text"] for r in payload["requests"]]
    assert texts == ["vraie demande de correction"]


def test_fork_parent_not_stitched(brief_env):
    """Ordinary forks share parent_session_id but must stay independent."""
    models = brief_env.models
    now = time.time()
    parent = models.Session(session_id="20260101_000400_forkpa")
    parent.messages = [
        {"role": "user", "content": "parent-only content", "timestamp": now},
    ]
    parent.save()  # NOT pre_compression_snapshot
    child = models.Session(session_id="20260101_000401_forkch")
    child.parent_session_id = parent.session_id
    child.messages = [
        {"role": "user", "content": "fork child ask", "timestamp": now + 5},
    ]
    child.save()
    msgs = brief_env.brief._session_messages(child)
    assert [m["content"] for m in msgs] == ["fork child ask"]


def test_brief_merges_state_db_history_when_sidecar_parent_empty(brief_env, monkeypatch):
    """LIVE compaction: parent sidecar holds only the anchor; state.db holds history.

    Observed 2026-08-17 on session 20260817_215809_7842fc: the
    pre_compression_snapshot parents contained 1-3 messages while state.db
    carried the full pre-compaction history. The brief must union the state.db
    continuation rows (including their NUL-JSON content envelopes) with the
    sidecar lineage so the ORIGINAL ask and its conclusion survive.
    """
    models = brief_env.models
    now = time.time()
    parent = models.Session(session_id="20260101_000500_dbpar0")
    parent.messages = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION] anchor only", "timestamp": now + 50},
    ]
    parent.pre_compression_snapshot = True
    parent.save()
    child = models.Session(session_id="20260101_000501_dbchi0")
    child.parent_session_id = parent.session_id
    child.messages = [
        {"role": "user", "content": "poursuis", "timestamp": now + 60},
    ]
    child.save()

    import json as _json

    db_rows = [
        {"role": "user", "content": "DEMANDE INITIALE state.db", "timestamp": now},
        {
            "role": "assistant",
            "content": "\x00json:" + _json.dumps(
                [{"type": "text", "text": "# CONCLUSION\n---\n> 🟢 Réponse / recommandation: conclusion state.db"}]
            ),
            "timestamp": now + 10,
        },
        # duplicate of a sidecar turn: must not appear twice
        {"role": "user", "content": "poursuis", "timestamp": now + 60.4},
    ]
    monkeypatch.setattr(
        brief_env.models,
        "get_state_db_session_messages",
        lambda sid, **kw: list(db_rows) if sid == child.session_id else [],
    )

    brief = brief_env.brief.build_deterministic_brief(child, child.session_id, source="webui")
    texts = [r["text"] for r in brief["requests"]]
    assert any("DEMANDE INITIALE state.db" in t for t in texts), texts
    assert sum("poursuis" in t for t in texts) == 1, texts
    excerpts = [c["excerpt"] for c in brief["accomplished"]["conclusions"]]
    assert any("conclusion state.db" in e for e in excerpts), excerpts
    # chronological: the state.db original ask precedes the continuation ask
    msgs = brief_env.brief._session_messages(child)
    contents = [str(m.get("content")) for m in msgs]
    idx_db = next(i for i, c in enumerate(contents) if "DEMANDE INITIALE" in c)
    idx_child = next(i for i, c in enumerate(contents) if c == "poursuis")
    assert idx_db < idx_child


def test_state_db_read_failure_falls_back_to_sidecar(brief_env, monkeypatch):
    models = brief_env.models
    now = time.time()
    parent = models.Session(session_id="20260101_000600_dbfail")
    parent.messages = [
        {"role": "user", "content": "ask sidecar", "timestamp": now},
    ]
    parent.pre_compression_snapshot = True
    parent.save()
    child = models.Session(session_id="20260101_000601_dbfai2")
    child.parent_session_id = parent.session_id
    child.messages = [{"role": "user", "content": "suite", "timestamp": now + 5}]
    child.save()

    def _boom(sid, **kw):
        raise RuntimeError("state.db unavailable")

    monkeypatch.setattr(brief_env.models, "get_state_db_session_messages", _boom)
    brief = brief_env.brief.build_deterministic_brief(child, child.session_id, source="webui")
    texts = [r["text"] for r in brief["requests"]]
    assert any("ask sidecar" in t for t in texts), texts
