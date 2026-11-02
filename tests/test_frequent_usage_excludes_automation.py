"""Automation-injected turns must never rank in the frequent-model list.

The usage scan counts real user turns in native WebUI sessions. Two kinds of
machine-injected message reach the same journal and must be dropped:

* delegation/background notifier headers (they can carry an explicit
  ``source`` value, which the old "legacy, no source" guard missed), and
* cron notifications (``⚠️ Cron job ...``), which arrive in cron-owned
  sessions that carry no source metadata at all.

A user message that merely *mentions* these phrases must still count.
"""

import time

from tests.test_models_preferences_backend import (
    _append_journal,
    _get,
    _wipe_state,
    _write_index,
    _write_sidecar,
)

NOW = time.time()
MODEL_REAL = "freq-real-model"
MODEL_NOISE = "freq-noise-model"


def _usage_models(sid="excl1"):
    _wipe_state()
    _write_sidecar(sid)
    _write_index([{"session_id": sid, "profile": "default"}])
    return sid


def _events(sid, extra_events):
    base = [
        {
            "event": "submitted",
            "stream_id": f"{sid}-real",
            "model": MODEL_REAL,
            "model_provider": "openai",
            "created_at": NOW - 120,
            "source": None,
            "content": "帮我查一下明天的天气",
        }
    ]
    _append_journal(sid, base + extra_events)
    data, status = _get("/api/models/preferences")
    assert status == 200
    return {(r["model"], r["provider"]): r["count"] for r in data["usage"]}


def _notice(stream_id, content, model=MODEL_NOISE, source=None):
    ev = {
        "event": "submitted",
        "stream_id": stream_id,
        "model": model,
        "model_provider": "openai",
        "created_at": NOW - 60,
        "content": content,
    }
    if source is not None:
        ev["source"] = source
    return ev


def test_background_process_header_with_source_is_not_counted():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n1", "[IMPORTANT: Background process 42 completed — output written to /tmp/x.log",
                source="webui"),
    ])
    assert (MODEL_NOISE, "openai") not in usage
    assert usage[(MODEL_REAL, "openai")] == 1


def test_delegation_batch_notice_with_source_is_not_counted():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n2", "[ASYNC DELEGATION BATCH COMPLETE — deleg_abc] 1 subagent has finished.",
                source="webui"),
    ])
    assert (MODEL_NOISE, "openai") not in usage
    assert usage[(MODEL_REAL, "openai")] == 1


def test_cron_notification_is_not_counted():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n3", "⚠️ Cron job 'WebUI 每日自动升级' failed: Script exited with code 1"),
        _notice("n4", "⚠️ Cron job 'AI热点深度研究' was interrupted — the gateway is shutting down"),
    ])
    assert (MODEL_NOISE, "openai") not in usage
    assert usage[(MODEL_REAL, "openai")] == 1


def test_cronjob_response_delivery_text_is_not_counted():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n7", "Cronjob Response: memory-usage-watchdog (job_id: 9effd121efdd) 记忆占用 82%"),
    ])
    assert (MODEL_NOISE, "openai") not in usage
    assert usage[(MODEL_REAL, "openai")] == 1


def test_user_reply_inside_a_cron_thread_still_counts():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n8", "那就改，上限多少合理？", model=MODEL_REAL, source=None),
    ])
    # A real reply typed by the user inside a cron notification thread is usage.
    assert usage[(MODEL_REAL, "openai")] == 2


def test_user_message_mentioning_notifier_phrases_still_counts():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n5", "刚才那个 [Background process 42 completed] 是什么意思？",
                model=MODEL_REAL, source="webui"),
    ])
    # Mid-text mention is prose, not an injected header: it must still count.
    assert usage[(MODEL_REAL, "openai")] == 2
    assert (MODEL_NOISE, "openai") not in usage


def test_process_wakeup_source_is_not_counted():
    sid = _usage_models()
    usage = _events(sid, [
        _notice("n6", "继续之前的任务", source="process_wakeup"),
    ])
    assert (MODEL_NOISE, "openai") not in usage
    assert usage[(MODEL_REAL, "openai")] == 1
