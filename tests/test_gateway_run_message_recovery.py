"""Recovery of gateway-run messages that never reached the sidecar (mid-run restart).

When a WebUI restart kills the run event pump, the gateway still persists the turn into
state.db — but the sidecar only receives what was written before the restart plus (later)
the terminal result. The append-only merge used to skip every state row at or below the
newest sidecar timestamp as "already observed", which swallowed exactly that turn block.
With the gateway-run recovery watermark (sidecar timestamp at run admission) those rows
must be recovered chronologically instead.
"""
from api.models import _merge_session_messages_append_only_impl


def _msg(role, content, ts):
    return {"role": role, "content": content, "timestamp": ts}


def test_recovery_watermark_recovers_unobserved_run_rows_below_sidecar_tail():
    admission = 100.0
    sidecar = [
        _msg("user", "starte die analyse", 90.0),
        # restart happened here; only the terminal result made it into the sidecar later
        _msg("assistant", "Ergebnis der Runde", 400.0),
    ]
    state = [
        _msg("user", "starte die analyse", 90.0),
        _msg("tool", '{"output": "schritt 1 ok"}', 110.0),
        _msg("assistant", "Zwischenstand: laeuft", 150.0),
        _msg("user", "Noch da?", 160.0),
        _msg("assistant", "Ja, noch da", 170.0),
        _msg("assistant", "Ergebnis der Runde", 400.0),
    ]
    merged = _merge_session_messages_append_only_impl(
        sidecar, state, recovery_watermark=admission,
    )
    texts = [str(m.get("content")) for m in merged]
    assert "Zwischenstand: laeuft" in texts, f"run rows must be recovered: {texts}"
    assert "Ja, noch da" in texts
    # correct chronological placement: recovered rows before the terminal result
    assert texts.index("Zwischenstand: laeuft") < texts.index("Ergebnis der Runde")
    assert texts.index("Ergebnis der Runde") == len(merged) - 1
    # no duplicates of rows the sidecar already has
    assert texts.count("Ergebnis der Runde") == 1
    assert texts.count("starte die analyse") == 1


def test_without_recovery_watermark_behaviour_is_unchanged():
    sidecar = [
        _msg("user", "starte die analyse", 90.0),
        _msg("assistant", "Ergebnis der Runde", 400.0),
    ]
    state = [
        _msg("user", "starte die analyse", 90.0),
        _msg("assistant", "Zwischenstand: laeuft", 150.0),
        _msg("assistant", "Ergebnis der Runde", 400.0),
    ]
    merged = _merge_session_messages_append_only_impl(sidecar, state)
    texts = [str(m.get("content")) for m in merged]
    assert "Zwischenstand: laeuft" not in texts, "legacy gate must still skip stale rows"


def test_recovery_watermark_ignores_rows_at_or_below_admission():
    sidecar = [
        _msg("user", "frage", 100.0),
        _msg("assistant", "antwort", 120.0),
    ]
    state = [
        _msg("user", "frage", 100.0),
        _msg("assistant", "alte zeile vor dem run", 60.0),
        _msg("assistant", "antwort", 120.0),
    ]
    merged = _merge_session_messages_append_only_impl(
        sidecar, state, recovery_watermark=100.0,
    )
    texts = [str(m.get("content")) for m in merged]
    assert "alte zeile vor dem run" not in texts, "rows at/below admission stay skipped"
