"""Reasoning-effort inheritance across the three session-copy paths (#6809 review).

Blocker 3 (``api/routes.py``)
    ``reasoning_effort`` is a per-session override. Three constructors build a
    child session from a source session and none of them carried the field, so
    the child read ``None`` and silently fell back to the profile-global default:

      - ``POST /api/session/duplicate``                  (duplicate)
      - ``POST /api/session/branch``                     (fork / branch)
      - ``POST /api/session/compression-recovery/start`` (focused continuation)

    A source session set to ``xhigh`` therefore produced a child running at
    whatever the profile said. The user sees a copy of their session that thinks
    at a different level, with no indication anything changed.

Every test here sets the profile-global default to a value DIFFERENT from the
source session's override. That is the whole point: if the global and the
override were the same value, a silent fall-back to the global would still read
as a pass and the test would prove nothing. The assertions check the child's
persisted value AND the effort that ``get_reasoning_status()`` resolves for it.
"""
import io
import json
from urllib.parse import urlparse

import pytest

from api import config as api_config
from api import models, routes
from api.compression_recovery import stamp_compression_exhausted_recovery
from api.models import Session


# The source session's override and the profile-global default must never be
# equal, or a silent fall-back to the global passes the test.
SOURCE_EFFORT = "xhigh"
PROFILE_GLOBAL_EFFORT = "low"
assert SOURCE_EFFORT != PROFILE_GLOBAL_EFFORT


class _FakeHandler:
    def __init__(self, path="/api/session/duplicate"):
        self.status = None
        self.headers = {"Content-Type": "application/json", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _capture_route(monkeypatch):
    cap = {}

    def _bad(_handler, msg, code=400, **_kwargs):
        cap["bad"] = (msg, code)
        return True

    def _j(_handler, obj, *_args, **kwargs):
        cap["ok"] = obj
        cap["status"] = kwargs.get("status", 200)
        return True

    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "j", _j)
    return cap


@pytest.fixture
def isolated_sessions(monkeypatch, tmp_path):
    """Sessions on a scratch dir, with a profile-global default that is NOT the override."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()

    # Profile-global default deliberately DIFFERENT from SOURCE_EFFORT, so a
    # child that drops the override resolves to PROFILE_GLOBAL_EFFORT and the
    # assertions below catch it.
    monkeypatch.setattr(
        api_config,
        "_load_yaml_config_file",
        lambda _path: {"agent": {"reasoning_effort": PROFILE_GLOBAL_EFFORT}},
    )
    monkeypatch.setattr(
        api_config, "resolve_model_reasoning_efforts", lambda *_a, **_k: ["low", "high", "xhigh"]
    )
    monkeypatch.setattr(api_config, "_zai_glm_thinking_toggle_supported", lambda *_a: None)
    return session_dir


def _assert_child_keeps_the_override(child_id, session_dir):
    """The child's persisted value and resolved effort must both be the override."""
    saved = json.loads((session_dir / f"{child_id}.json").read_text(encoding="utf-8"))
    assert saved.get("reasoning_effort") == SOURCE_EFFORT, (
        f"the child session persisted reasoning_effort={saved.get('reasoning_effort')!r} "
        f"instead of the source override {SOURCE_EFFORT!r}; it will silently fall "
        f"back to the profile-global {PROFILE_GLOBAL_EFFORT!r} (#6809 review blocker 3)"
    )
    resolved = api_config.get_reasoning_status(
        model_id="test-model", reasoning_effort=saved.get("reasoning_effort")
    )["reasoning_effort"]
    assert resolved == SOURCE_EFFORT, (
        f"the child resolves to {resolved!r}, not the source override {SOURCE_EFFORT!r}"
    )


def _sanity_check_the_global_differs():
    """The profile-global really is the other value, so the oracle is meaningful."""
    assert (
        api_config.get_reasoning_status(model_id="test-model")["reasoning_effort"]
        == PROFILE_GLOBAL_EFFORT
    )


# ---------------------------------------------------------------------------
# duplicate
# ---------------------------------------------------------------------------


def test_duplicate_carries_reasoning_effort(isolated_sessions, monkeypatch):
    session_dir = isolated_sessions
    _sanity_check_the_global_differs()

    source = Session(
        session_id="dupsrc1",
        title="Deep work",
        workspace=str(session_dir),
        model="gpt-4o",
        model_provider="openai",
        messages=[{"role": "user", "content": "think hard"}],
        reasoning_effort=SOURCE_EFFORT,
    )
    source.save()

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    cap = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler(), urlparse("/api/session/duplicate"))

    assert "bad" not in cap, cap.get("bad")
    child_id = cap["ok"]["session"]["session_id"]
    assert child_id != source.session_id
    _assert_child_keeps_the_override(child_id, session_dir)


def test_duplicate_of_a_session_without_an_override_stays_none(isolated_sessions, monkeypatch):
    """A source with no override must produce a child with no override, not a value.

    Carrying the field must not invent one. ``None`` is what keeps existing
    sessions, the CLI, and cron on the profile-global fall-back.
    """
    session_dir = isolated_sessions
    source = Session(
        session_id="dupsrc2",
        title="No override",
        workspace=str(session_dir),
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    )
    source.save()

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    cap = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler(), urlparse("/api/session/duplicate"))

    child_id = cap["ok"]["session"]["session_id"]
    saved = json.loads((session_dir / f"{child_id}.json").read_text(encoding="utf-8"))
    assert saved.get("reasoning_effort") is None, (
        "a source with no per-session override must not gain one on duplicate"
    )


# ---------------------------------------------------------------------------
# branch / fork
# ---------------------------------------------------------------------------


def test_branch_carries_reasoning_effort(isolated_sessions, monkeypatch):
    session_dir = isolated_sessions
    _sanity_check_the_global_differs()

    source = Session(
        session_id="brnsrc1",
        title="Deep work",
        workspace=str(session_dir),
        model="gpt-4o",
        model_provider="openai",
        messages=[{"role": "user", "content": "think hard"}],
        reasoning_effort=SOURCE_EFFORT,
    )
    source.save()
    models.SESSIONS[source.session_id] = source
    routes.SESSIONS[source.session_id] = source

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    cap = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler("/api/session/branch"), urlparse("/api/session/branch"))

    assert "bad" not in cap, cap.get("bad")
    child_id = cap["ok"]["session_id"]
    assert cap["ok"]["parent_session_id"] == source.session_id
    _assert_child_keeps_the_override(child_id, session_dir)


# ---------------------------------------------------------------------------
# focused continuation (compression recovery)
# ---------------------------------------------------------------------------


def test_focused_continuation_carries_reasoning_effort(isolated_sessions, monkeypatch):
    session_dir = isolated_sessions
    _sanity_check_the_global_differs()

    source = Session(
        session_id="recsrc1",
        title="Long task",
        workspace=str(session_dir),
        model="gpt-4o",
        model_provider="openai",
        profile="default",
        messages=[{"role": "user", "content": "long task"}],
        context_messages=[{"role": "user", "content": "large context"}],
        reasoning_effort=SOURCE_EFFORT,
    )
    stamp_compression_exhausted_recovery(source, message="Context length exceeded.")
    source.save()
    models.SESSIONS[source.session_id] = source
    routes.SESSIONS[source.session_id] = source

    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    cap = _capture_route(monkeypatch)

    routes._handle_session_compression_recovery_start(
        _FakeHandler("/api/session/compression-recovery/start"),
        {"session_id": source.session_id},
    )

    assert "bad" not in cap, cap.get("bad")
    child_id = cap["ok"]["session"]["session_id"]
    assert child_id != source.session_id
    _assert_child_keeps_the_override(child_id, session_dir)


# ---------------------------------------------------------------------------
# static backstop — all three constructors, anchored individually
# ---------------------------------------------------------------------------


def _constructor_block(src: str, anchor: str, var: str) -> str:
    """Slice the ``<var> = Session(...)`` call that follows ``anchor`` in src."""
    at = src.index(anchor)
    start = src.index(f"{var} = Session(", at)
    open_paren = src.index("(", start + len(var))
    depth = 1
    i = open_paren + 1
    while depth and i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    assert depth == 0, f"unbalanced parens slicing the {var} constructor"
    return src[start:i]


def test_all_three_copy_constructors_pass_reasoning_effort():
    """Guard against a fourth copy path landing without the field.

    Each constructor is sliced individually so this cannot be satisfied by the
    ``reasoning_effort=getattr(...)`` hunks in the GET and POST ``/api/reasoning``
    ENDPOINTS, which are a different fix and would otherwise make a bare
    repo-wide count unfailable. This reads the source rather than the behaviour,
    so it is a backstop for the three behavioural tests above, not a replacement.
    """
    src = open(routes.__file__, encoding="utf-8").read()

    duplicate = _constructor_block(src, 'parsed.path == "/api/session/duplicate"', "copied_session")
    assert 'reasoning_effort=getattr(session, "reasoning_effort", None)' in duplicate, (
        "the /api/session/duplicate constructor must carry reasoning_effort"
    )

    branch = _constructor_block(src, 'parsed.path == "/api/session/branch"', "branch")
    assert 'reasoning_effort=getattr(source, "reasoning_effort", None)' in branch, (
        "the /api/session/branch constructor must carry reasoning_effort"
    )

    focused = _constructor_block(
        src, "def _handle_session_compression_recovery_start(", "copied_session"
    )
    assert 'reasoning_effort=getattr(source, "reasoning_effort", None)' in focused, (
        "the focused-continuation constructor must carry reasoning_effort"
    )
