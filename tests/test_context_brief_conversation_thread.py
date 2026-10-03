"""The context brief must read as a CONVERSATION, not as two disjoint lists.

A user report showed that the Context panel displayed "Vos demandes" and
"Accompli" as separate stacked sections, so a returning reader had to mentally
pair the 4th request with the 2nd conclusion. The brief must interleave each
ask with the conclusion that answered it, in transcript order, and the panel
must render asks right-aligned and conclusions left-aligned like a chat.

Covered here:
- ``timeline`` exists, alternates request/conclusion in TRANSCRIPT order;
- ordering survives missing/duplicated timestamps (replayed & merged copies);
- the timeline cap keeps the mission ORIGIN like the per-list caps do;
- panels.js renders one thread with distinct request/conclusion alignment
  classes and still renders a pre-timeline cached payload.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import context_brief


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

SID = "20260818_120000_thread"


def _session(messages):
    return SimpleNamespace(
        session_id=SID,
        title="fil de conversation",
        workspace="/tmp/ws",
        model="k3-256k",
        created_at=1000.0,
        updated_at=1010.0,
        messages=messages,
        active_stream_id=None,
        pending_user_message=None,
        pending_started_at=None,
        pending_turn_id=None,
        pending_attachments=[],
        profile="default",
        path=None,
    )


def _conclusion(text):
    return f"# CONCLUSION\n---\n> 🟢 Réponse / recommandation: {text}"


# ── deterministic timeline ───────────────────────────────────────────────

def test_timeline_interleaves_each_request_with_its_conclusion():
    sess = _session(
        [
            {"role": "user", "content": "demande A", "timestamp": 1.0},
            {"role": "assistant", "content": _conclusion("réponse A"), "timestamp": 2.0},
            {"role": "user", "content": "demande B", "timestamp": 3.0},
            {"role": "assistant", "content": _conclusion("réponse B"), "timestamp": 4.0},
        ]
    )
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    timeline = brief["timeline"]
    assert [item["role"] for item in timeline] == [
        "request",
        "conclusion",
        "request",
        "conclusion",
    ], "asks and their conclusions must alternate, not be grouped by role"
    assert [item["text"] for item in timeline] == [
        "demande A",
        "🟢 Réponse / recommandation: réponse A",
        "demande B",
        "🟢 Réponse / recommandation: réponse B",
    ]


def test_timeline_orders_on_transcript_index_not_timestamp():
    """Replayed/merged copies carry missing or out-of-order stamps.

    Sorting on ``ts`` would scramble the thread exactly on the long, compacted
    sessions the brief exists for. The transcript order is authoritative.
    """
    sess = _session(
        [
            {"role": "user", "content": "demande A"},  # no timestamp at all
            {"role": "assistant", "content": _conclusion("réponse A"), "timestamp": 9999.0},
            {"role": "user", "content": "demande B", "timestamp": 5.0},
            {"role": "assistant", "content": _conclusion("réponse B"), "timestamp": 5.0},
        ]
    )
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    assert [(i["role"], i["text"]) for i in brief["timeline"]] == [
        ("request", "demande A"),
        ("conclusion", "🟢 Réponse / recommandation: réponse A"),
        ("request", "demande B"),
        ("conclusion", "🟢 Réponse / recommandation: réponse B"),
    ]


def test_timeline_excludes_runtime_plumbing_and_silent_turns():
    sess = _session(
        [
            {"role": "user", "content": "demande réelle", "timestamp": 1.0},
            {"role": "assistant", "content": "[[SILENT]]", "timestamp": 2.0},
            {
                "role": "user",
                "content": "[IMPORTANT: Background process x completed]",
                "timestamp": 3.0,
                "_source": "process_wakeup",
            },
            {"role": "assistant", "content": _conclusion("livré"), "timestamp": 4.0},
        ]
    )
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    texts = [item["text"] for item in brief["timeline"]]
    assert texts == ["demande réelle", "🟢 Réponse / recommandation: livré"]
    assert not any("Background process" in text for text in texts)


def test_timeline_cap_keeps_mission_origin():
    messages = []
    ts = 0.0
    for n in range(30):
        ts += 1
        messages.append({"role": "user", "content": f"demande {n}", "timestamp": ts})
        ts += 1
        messages.append({"role": "assistant", "content": _conclusion(f"réponse {n}"), "timestamp": ts})
    brief = context_brief.build_deterministic_brief(_session(messages), SID, source="webui")

    timeline = brief["timeline"]
    assert len(timeline) <= context_brief._TIMELINE_CAP
    # ORIGIN preserved, like _cap_keep_first does for the per-role lists.
    assert timeline[0]["role"] == "request"
    assert timeline[0]["text"] == "demande 0"
    # …and the most recent exchange is the tail.
    assert timeline[-1]["text"] == "🟢 Réponse / recommandation: réponse 29"


def test_timeline_holds_more_than_either_single_list():
    """A thread carrying both roles must not be capped at one role's budget."""
    assert context_brief._TIMELINE_CAP >= context_brief._REQUEST_CAP + context_brief._CONCLUSION_CAP


def test_legacy_request_and_conclusion_lists_stay_available():
    """The split lists remain in the payload: the LLM prompt fallback uses them."""
    sess = _session(
        [
            {"role": "user", "content": "demande A", "timestamp": 1.0},
            {"role": "assistant", "content": _conclusion("réponse A"), "timestamp": 2.0},
        ]
    )
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    assert [r["text"] for r in brief["requests"]] == ["demande A"]
    assert brief["accomplished"]["conclusions"][0]["excerpt"].startswith("🟢")
    # Internal ordering keys must not leak into the API payload.
    assert all("_idx" not in r for r in brief["requests"])
    assert all("_idx" not in c for c in brief["accomplished"]["conclusions"])
    assert all("_idx" not in item for item in brief["timeline"])
    json.dumps(brief["timeline"])  # payload stays JSON-serializable


# ── panel rendering ──────────────────────────────────────────────────────

def _render(brief, tmp_path):
    """Run renderContextBrief() over a stubbed DOM and return the panel HTML."""
    assert NODE, "node is required for the context-brief thread rendering test"
    src = PANELS_JS[PANELS_JS.index("function renderContextBrief(") :]
    src = src[: src.index("\nasync function _contextBriefRefresh(")]
    script = (
        "const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
        ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;');\n"
        "const t = k => k;\n"
        "const $ = () => null;\n"
        f"{src}\n"
        "const panel = {innerHTML:'', _briefData:null};\n"
        f"renderContextBrief({json.dumps(brief)}, panel);\n"
        "console.log(JSON.stringify({html: panel.innerHTML}));\n"
    )
    path = tmp_path / "ctx_thread_render.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["html"]


_THREAD_BRIEF = {
    "meta": {"title": "fil", "message_count": 4},
    "timeline": [
        {"role": "request", "ts": 1.0, "text": "demande A"},
        {"role": "conclusion", "ts": 2.0, "text": "réponse A"},
        {"role": "request", "ts": 3.0, "text": "demande B"},
        {"role": "conclusion", "ts": 4.0, "text": "réponse B"},
    ],
    "requests": [{"ts": 1.0, "text": "demande A"}, {"ts": 3.0, "text": "demande B"}],
    "request_count": 2,
    "accomplished": {
        "conclusions": [{"ts": 2.0, "excerpt": "réponse A"}, {"ts": 4.0, "excerpt": "réponse B"}],
        "conclusion_count": 2,
        "compressions": [],
        "compression_count": 0,
    },
    "todos": None,
    "in_flight": {"active": False},
}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_renders_one_thread_in_conversation_order(tmp_path):
    html = _render(_THREAD_BRIEF, tmp_path)

    roles = re.findall(r"ctx-thread-(request|conclusion)\"", html)
    assert roles == ["request", "conclusion", "request", "conclusion"], (
        "the panel must render the interleaved thread, not requests then conclusions"
    )
    order = [m for m in re.findall(r"demande [AB]|réponse [AB]", html)]
    assert order == ["demande A", "réponse A", "demande B", "réponse B"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_no_longer_renders_two_separate_sections(tmp_path):
    html = _render(_THREAD_BRIEF, tmp_path)

    assert "context_brief_requests" not in html and "context_brief_accomplished" not in html, (
        "requests/accomplished must no longer be two stacked headed sections"
    )
    assert html.count("context_brief_thread\"") <= 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_falls_back_when_cached_brief_predates_timeline(tmp_path):
    legacy = dict(_THREAD_BRIEF)
    legacy.pop("timeline")
    html = _render(legacy, tmp_path)

    # No thread field: still render both roles rather than an empty panel.
    assert "demande A" in html and "réponse A" in html
    assert "ctx-thread-request" in html and "ctx-thread-conclusion" in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_renders_empty_thread_without_crashing(tmp_path):
    empty = dict(_THREAD_BRIEF)
    empty["timeline"] = []
    empty["requests"] = []
    empty["request_count"] = 0
    empty["accomplished"] = {"conclusions": [], "conclusion_count": 0, "compressions": [], "compression_count": 0}
    html = _render(empty, tmp_path)

    assert "context_brief_empty_thread" in html
    assert "ctx-thread-row" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_escapes_hostile_thread_text(tmp_path):
    hostile = dict(_THREAD_BRIEF)
    hostile["timeline"] = [{"role": "request", "ts": 1.0, "text": "<img src=x onerror=alert(1)>"}]
    html = _render(hostile, tmp_path)

    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


# ── styling + i18n contracts ─────────────────────────────────────────────

def test_css_aligns_requests_right_and_conclusions_left():
    assert ".ctx-thread-row.ctx-thread-request{justify-content:flex-end;}" in STYLE_CSS
    assert ".ctx-thread-row.ctx-thread-conclusion{justify-content:flex-start;}" in STYLE_CSS


def test_thread_keys_translated_in_every_locale_that_already_had_brief_keys():
    """Locales that translated the brief headers must translate the new ones."""
    localized = I18N_JS.count("context_brief_summary:")
    for key in ("context_brief_thread:", "context_brief_thread_you:", "context_brief_thread_agent:"):
        assert I18N_JS.count(key) == localized, f"{key} missing in a locale that has brief keys"
