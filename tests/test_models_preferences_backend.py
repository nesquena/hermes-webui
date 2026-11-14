"""Backend contract tests for /api/models/preferences.

Covers the fixed-statistics + persisted-pins feature end to end through the
running isolated test server (route integration, not helper unit tests):

GET  /api/models/preferences -> {profile, pins:[{model,provider}],
      usage:[{model,provider,count,last_used}], period_days:30, limit:6}
POST /api/models/preferences {model,provider,pinned} -> same GET shape,
      idempotent set (not toggle), max 6 pins.

Usage tallies submitted turn-journal sends scoped to native writable WebUI
sessions of the request profile, deduped per (session_id, stream_id or
turn_id), excluding foreign/read_only/deleted sessions, explicit non-webui
source events, and start-compensated interrupted streams.
"""
import json
import time
import urllib.error
import urllib.request

from tests._pytest_port import BASE, TEST_STATE_DIR

SESSIONS_DIR = TEST_STATE_DIR / "sessions"
JOURNAL_DIR = SESSIONS_DIR / "_turn_journal"
PINS_FILE = TEST_STATE_DIR / "model_preferences.json"

NOW = time.time()


def _index_file():
    return SESSIONS_DIR / "_index.json"


def _post(path, body, csrf=None):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}"), e.code


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read()), r.status


def _wipe_state():
    for p in (PINS_FILE, _index_file()):
        if p.exists():
            p.unlink()
    if JOURNAL_DIR.exists():
        for p in JOURNAL_DIR.iterdir():
            try:
                p.unlink()
            except OSError:
                pass
    if SESSIONS_DIR.exists():
        for p in SESSIONS_DIR.glob("*.json"):
            if not p.name.startswith("_"):
                try:
                    p.unlink()
                except OSError:
                    pass


def _write_index(entries):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _index_file().write_text(json.dumps(entries), encoding="utf-8")


def _write_sidecar(sid, **extra):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "session_id": sid, "title": sid, "profile": "default",
        "is_cli_session": False, "source_tag": None, "raw_source": None,
        "session_source": None, "source_label": None, "read_only": False,
        "messages": [], "created_at": NOW - 86400, "updated_at": NOW - 86000,
    }
    data.update(extra)
    (SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")


def _append_journal(sid, events_iter, pid="111"):
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{sid}~{pid}.jsonl"
    lines = []
    for idx, ev in enumerate(events_iter):
        e = dict(ev)
        e.setdefault("version", 1)
        e.setdefault("session_id", sid)
        e.setdefault("turn_id", f"t-{sid}-{idx}")
        lines.append(json.dumps(e, ensure_ascii=False))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ── Fixed shape & pins ────────────────────────────────────────────────────

def test_get_returns_fixed_shape_when_empty():
    _wipe_state()
    data, status = _get("/api/models/preferences")
    assert status == 200
    assert data["profile"] == "default"
    assert data["pins"] == []
    assert data["usage"] == []
    assert data["period_days"] == 30
    assert data["limit"] == 6


def test_post_pin_then_get_returns_pin():
    _wipe_state()
    resp, status = _post("/api/models/preferences",
                         {"model": "gpt-4o", "provider": "openai", "pinned": True})
    assert status == 200
    assert {"model": "gpt-4o", "provider": "openai"} in resp["pins"]
    data, _ = _get("/api/models/preferences")
    assert {"model": "gpt-4o", "provider": "openai"} in data["pins"]


def test_post_is_idempotent_set_not_toggle():
    _wipe_state()
    for _ in range(3):
        _post("/api/models/preferences",
              {"model": "gpt-4o", "provider": "openai", "pinned": True})
    data, _ = _get("/api/models/preferences")
    assert data["pins"].count({"model": "gpt-4o", "provider": "openai"}) == 1
    _post("/api/models/preferences",
          {"model": "gpt-4o", "provider": "openai", "pinned": False})
    data, _ = _get("/api/models/preferences")
    assert {"model": "gpt-4o", "provider": "openai"} not in data["pins"]


def test_pins_cap_at_six():
    _wipe_state()
    for i in range(8):
        _post("/api/models/preferences",
              {"model": f"m{i}", "provider": "openai", "pinned": True})
    data, _ = _get("/api/models/preferences")
    assert len(data["pins"]) == 6


def test_post_rejects_non_bool_pinned():
    _wipe_state()
    resp, status = _post("/api/models/preferences",
                         {"model": "gpt-4o", "provider": "openai", "pinned": "yes"})
    assert status == 400


def test_post_rejects_empty_model_provider():
    _wipe_state()
    resp, status = _post("/api/models/preferences",
                         {"model": "", "provider": "", "pinned": True})
    assert status == 400


# ── Usage statistics ──────────────────────────────────────────────────────

def _usage_map():
    data, _ = _get("/api/models/preferences")
    return {(u["model"], u["provider"]): u["count"] for u in data["usage"]}, data


def test_usage_tallies_native_submitted_sends_deduped():
    _wipe_state()
    for sid in ("sessA", "sessB"):
        _write_sidecar(sid)
    _write_index([
        {"session_id": "sessA", "profile": "default", "is_cli_session": False,
         "source_tag": None, "raw_source": None, "session_source": None, "read_only": False},
        {"session_id": "sessB", "profile": "default", "is_cli_session": False,
         "source_tag": None, "raw_source": None, "session_source": None, "read_only": False},
    ])
    # sessA: same send recorded in two pid shards -> must dedupe to 1.
    for pid in ("111", "222"):
        _append_journal("sessA", [
            {"event": "submitted", "stream_id": "xA-1", "content": "hi",
             "model": "gpt-4o", "model_provider": "openai", "created_at": NOW - 3600},
            {"event": "completed", "stream_id": "xA-1", "created_at": NOW - 3500},
        ], pid=pid)
    _append_journal("sessB", [
        {"event": "submitted", "stream_id": "xB-1", "content": "yo",
         "model": "claude-3-5-sonnet", "model_provider": "anthropic", "created_at": NOW - 7200},
        {"event": "completed", "stream_id": "xB-1", "created_at": NOW - 7100},
    ])
    usage, data = _usage_map()
    assert data["limit"] == 6 and data["period_days"] == 30
    assert usage.get(("gpt-4o", "openai")) == 1, data["usage"]
    assert usage.get(("claude-3-5-sonnet", "anthropic")) == 1, data["usage"]
    # ascending count caps at limit
    assert len(data["usage"]) <= 6


def test_usage_excludes_foreign_readonly_deleted_other_profile_and_compensated():
    _wipe_state()
    now = time.time()
    entries = []
    _write_sidecar("sessNative")
    entries.append({"session_id": "sessNative", "profile": "default",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessNative", [
        {"event": "submitted", "stream_id": "n-1", "content": "hi",
         "model": "keep-me", "model_provider": "prov", "created_at": now - 3600},
        {"event": "completed", "stream_id": "n-1", "created_at": now - 3500},
    ])
    # foreign source (feishu) despite is_cli_session False -> excluded
    _write_sidecar("sessFeishu")
    entries.append({"session_id": "sessFeishu", "profile": "default",
                    "is_cli_session": False, "source_tag": "feishu",
                    "raw_source": "feishu", "session_source": "other", "read_only": False})
    _append_journal("sessFeishu", [
        {"event": "submitted", "stream_id": "f-1", "content": "hi",
         "model": "feishu-m", "model_provider": "feishu", "created_at": now - 3600},
    ])
    # desktop foreign (is_cli_session False) -> excluded
    _write_sidecar("sessDesktop")
    entries.append({"session_id": "sessDesktop", "profile": "default",
                    "is_cli_session": False, "source_tag": "desktop",
                    "raw_source": "desktop", "session_source": "other", "read_only": False})
    _append_journal("sessDesktop", [
        {"event": "submitted", "stream_id": "d-1", "content": "hi",
         "model": "desk-m", "model_provider": "desktop", "created_at": now - 3600},
    ])
    # read_only -> excluded
    _write_sidecar("sessRO")
    entries.append({"session_id": "sessRO", "profile": "default",
                    "is_cli_session": True, "source_tag": "cli",
                    "raw_source": "cli", "session_source": "cli", "read_only": True})
    _append_journal("sessRO", [
        {"event": "submitted", "stream_id": "r-1", "content": "hi",
         "model": "ro-m", "model_provider": "ro", "created_at": now - 3600},
    ])
    # delegate/subagent -> excluded
    _write_sidecar("sessDelegate")
    entries.append({"session_id": "sessDelegate", "profile": "default",
                    "is_cli_session": False, "source_tag": "subagent",
                    "raw_source": "subagent", "session_source": "subagent", "read_only": False})
    _append_journal("sessDelegate", [
        {"event": "submitted", "stream_id": "dl-1", "content": "hi",
         "model": "del-m", "model_provider": "del", "created_at": now - 3600},
    ])
    # other-profile -> excluded
    _write_sidecar("sessOther")
    entries.append({"session_id": "sessOther", "profile": "work",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessOther", [
        {"event": "submitted", "stream_id": "o-1", "content": "hi",
         "model": "other-m", "model_provider": "other", "created_at": now - 3600},
    ])
    # deleted session (index row, no sidecar) -> excluded
    entries.append({"session_id": "sessGone", "profile": "default",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessGone", [
        {"event": "submitted", "stream_id": "g-1", "content": "hi",
         "model": "gone-m", "model_provider": "gone", "created_at": now - 3600},
    ])
    # start-compensated interrupted stream -> excluded
    _write_sidecar("sessInterrupt")
    entries.append({"session_id": "sessInterrupt", "profile": "default",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessInterrupt", [
        {"event": "submitted", "stream_id": "i-1", "content": "hi",
         "model": "inter-m", "model_provider": "inter", "created_at": now - 3600},
        {"event": "interrupted", "stream_id": "i-1", "reason": "start_compensated",
         "created_at": now - 3500},
    ])
    # explicit non-webui source event -> excluded
    _write_sidecar("sessSrc")
    entries.append({"session_id": "sessSrc", "profile": "default",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessSrc", [
        {"event": "submitted", "stream_id": "s-1", "content": "hi",
         "model": "src-m", "model_provider": "src", "source": "desktop", "created_at": now - 3600},
    ])
    # control prompt -> excluded
    _write_sidecar("sessCtrl")
    entries.append({"session_id": "sessCtrl", "profile": "default",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessCtrl", [
        {"event": "submitted", "stream_id": "c-1", "content": "[SILENT]",
         "model": "ctrl-m", "model_provider": "ctrl", "created_at": now - 3600},
    ])
    # future timestamp -> excluded
    _write_sidecar("sessFuture")
    entries.append({"session_id": "sessFuture", "profile": "default",
                    "is_cli_session": False, "source_tag": None,
                    "raw_source": None, "session_source": None, "read_only": False})
    _append_journal("sessFuture", [
        {"event": "submitted", "stream_id": "ft-1", "content": "hi",
         "model": "fut-m", "model_provider": "fut", "created_at": now + 999999},
    ])
    _write_index(entries)
    usage, data = _usage_map()
    assert usage == {("keep-me", "prov"): 1}, data["usage"]


def test_usage_returns_all_candidates_for_available_filter():
    _wipe_state()
    _write_sidecar("sess")
    entries = [{"session_id": "sess", "profile": "default", "is_cli_session": False,
                "source_tag": None, "raw_source": None, "session_source": None, "read_only": False}]
    _write_index(entries)
    submit = []
    create = []
    for i in range(8):
        submit.append({"event": "submitted", "stream_id": f"st-{i}", "content": "x",
                       "model": f"model-{i}", "model_provider": "p", "created_at": NOW - 3600})
        create.append({"event": "completed", "stream_id": f"st-{i}", "created_at": NOW - 3500})
    _append_journal("sess", submit + create)
    usage, data = _usage_map()
    assert len(data["usage"]) == 8
    assert len(usage) == 8


def test_usage_strips_only_known_provider_prefixes():
    _wipe_state()
    _write_sidecar("sPfx")
    _write_index([
        {"session_id": "sPfx", "profile": "default", "is_cli_session": False,
         "source_tag": None, "raw_source": None, "session_source": None, "read_only": False},
    ])
    handles = {
        "openai/gpt-4o": ("gpt-4o", "openai"),
        "@openai:gpt-4o-2": ("gpt-4o-2", "openai"),
        "gpt-4o-mini": ("gpt-4o-mini", "openai"),          # no strip, keep as-is
        "anthropic/claude-3-5-sonnet": ("claude-3-5-sonnet", "anthropic"),
    }
    submit = []
    create = []
    i = 0
    for raw, (_m, _p) in handles.items():
        submit.append({"event": "submitted", "stream_id": f"pfx-{i}", "content": "x",
                       "model": raw, "model_provider": _p, "created_at": NOW - 3600})
        create.append({"event": "completed", "stream_id": f"pfx-{i}", "created_at": NOW - 3500})
        i += 1
    _append_journal("sPfx", submit + create)
    usage, data = _usage_map()
    assert usage == {v: 1 for v in handles.values()}, data["usage"]


def test_corrupt_pins_refuses_overwrite():
    _wipe_state()
    PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PINS_FILE.write_text("{{{ not json", encoding="utf-8")
    resp, status = _post("/api/models/preferences",
                         {"model": "recovered", "provider": "p", "pinned": True})
    assert status == 400
    assert PINS_FILE.read_text(encoding="utf-8") == "{{{ not json"


def open_composer_picker(page):
    """Open the composer model picker without racing the phone-boundary close.

    ``_onPhoneBoundaryChange`` in static/ui.js closes the picker whenever the
    ``(max-width: 640px)`` media query flips, which a headless viewport (or a
    device rotating between portrait and desktop width) can still do right
    after load. Settle the layout first, then open and assert; a late boundary
    event that closed it again is retried instead of surfacing as a hidden-row
    timeout.
    """
    page.wait_for_timeout(400)
    for _ in range(3):
        if page.evaluate("()=>document.getElementById('composerModelDropdown').classList.contains('open')"):
            return
        page.evaluate("()=>toggleModelDropdown()")
        page.wait_for_timeout(150)
    page.wait_for_function(
        "()=>document.getElementById('composerModelDropdown').classList.contains('open')",
        timeout=5000)
