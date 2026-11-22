"""#8901 surface 1: live & replayed SSE token deltas must be redacted.

``put('token', {'text': text})`` in api/streaming.py and the three
``emit("token", ...)`` sites in api/gateway_chat.py bypass ``_redact_text``
entirely — only the ``done`` and error payloads are redacted. A secret of
any shape (built-in or plugin-registered) streamed mid-turn therefore
reaches the browser raw, and a journal replay after reconnect re-sends the
same raw delta.

Per-delta redaction alone is not enough: a provider can split a secret
across consecutive deltas (``"sk-"`` + ``"abc…"``), so no single delta
carries a redactable token. The fix is ``api.helpers.StreamDeltaRedactor``:
a per-stream redactor that holds a raw tail window and only releases text
once no registered secret pattern can still span the cut, running
``_redact_text`` over every released chunk.

Contract verified here:
  1. a secret complete inside one delta is masked;
  2. a secret split across two deltas is masked, surrounding text survives;
  3. a secret split across three deltas is masked likewise;
  4. plain text is emitted verbatim, delta for delta;
  5. flush() at stream end releases the held tail without swallowing text;
  6. control: a secret-shaped prefix followed by ordinary words is NOT
     masked (no over-redaction) and is not swallowed;
  7. wiring: both SSE exits (api/streaming.py ``put``, api/gateway_chat.py
     ``put_gateway_event``) route token deltas through the redactor, so the
     run journal stores the redacted text a replay re-sends;
  8. with ``api_redact_enabled`` off the redactor is a pass-through;
  9. a secret LONGER than the tail window (long JWT, multi-kilobyte PEM)
     keeps its pin past window expiry — releasing it would strand the
     value's tail in the buffer without the header its pattern anchors
     on, so every later release (live and replayed) would emit it raw;
 10. a retained marker prefix may only REDUCE the cut the secret-start
     loop pinned, never drag it forward past a protected secret start;
 11. the ``interim_assistant`` boundary frame honors the same effective
     ``api_redact_enabled`` setting as the owning stream, and withholds
     ``reasoning_echo`` when redaction changed the text — the client
     matches that text against the reasoning it rendered, and a masked
     text can never match the raw one.
"""
import queue
import sys
import types
import uuid
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402

_MISSING = object()

# Fake keys in real secret shapes — they must travel the real _redact_text
# path (agent redactor + local fallback regexes), never a test-only stub.
# Prefix and body are concatenated at import time so no contiguous
# token-shaped literal sits in this source file.
_FAKE_SK = "sk" + "-" + ("Ab3xY9" * 9)[:42]
_FAKE_GHP = "ghp" + "_" + ("Zt4kQ1" * 10)[:36]
_FAKE_AKIA = "AK" + "IA" + ("Q7MR2" * 8)[:16]
_SECRET_SHAPES = [_FAKE_SK, _FAKE_GHP, _FAKE_AKIA]


@pytest.fixture(autouse=True)
def _isolate_memos():
    helpers._redact_fn_lru.cache_clear()
    helpers._might_contain_sensitive_text_lru.cache_clear()
    yield
    helpers._redact_fn_lru.cache_clear()
    helpers._might_contain_sensitive_text_lru.cache_clear()


def _redactor():
    """A fresh StreamDeltaRedactor, or a loud failure on unfixed master.

    The assert doubles as the RED evidence: before this PR's fix
    api.helpers exposes no stream-delta redactor at all, so every test in
    this file fails with it on an unfixed tree.
    """
    cls = getattr(helpers, "StreamDeltaRedactor", None)
    assert cls is not None, (
        "api.helpers exposes no StreamDeltaRedactor — live/replayed stream "
        "deltas are still emitted unredacted (issue #8901, unfixed master)"
    )
    return cls()


def _stream(deltas):
    """Feed deltas through the redactor; return the reassembled SSE text."""
    red = _redactor()
    parts = [red.feed(d) for d in deltas]
    parts.append(red.flush())
    return "".join(parts)


# ── 1. Secret complete inside a single delta ───────────────────────────────


def test_secret_within_single_delta_is_redacted():
    out = _stream(["Sure — ", _FAKE_SK, " is the key, keep it safe."])

    assert _FAKE_SK not in out
    assert "Sure — " in out
    assert " is the key, keep it safe." in out


# ── 2/3. Secret split across consecutive deltas ────────────────────────────


@pytest.mark.parametrize("secret", _SECRET_SHAPES)
def test_secret_split_across_two_deltas_is_redacted(secret):
    out = _stream(["prefix ", secret[:20], secret[20:], " suffix"])

    assert secret not in out, "split secret must not survive in the SSE text"
    assert "prefix " in out
    assert " suffix" in out


@pytest.mark.parametrize("secret", _SECRET_SHAPES)
def test_secret_split_across_three_deltas_is_redacted(secret):
    third = len(secret) // 3
    out = _stream(
        ["prefix ", secret[:third], secret[third : third * 2], secret[third * 2 :], " suffix"]
    )

    assert secret not in out, "split secret must not survive in the SSE text"
    assert "prefix " in out
    assert " suffix" in out


def test_split_secret_is_held_back_not_partially_emitted():
    """The first delta of a split secret must not be flushed early.

    Emitting "sk-" before the rest arrives would defeat the redactor: the
    browser would already hold the secret's prefix.
    """
    red = _redactor()
    early = red.feed(_FAKE_SK[:12])

    assert early == "", "a secret-shaped prefix must be held back, not emitted"
    assert _FAKE_SK[:12] not in early


# ── RED evidence: what unfixed master does ─────────────────────────────────


def test_unfixed_master_reassembles_the_split_secret_in_the_clear():
    """The defect itself: deltas put() verbatim reassemble into the secret.

    On unfixed master there is no redactor on the delta path, so the text a
    browser (or a journal replay after reconnect) reassembles from the token
    events contains the secret in the clear. StreamDeltaRedactor closes it.
    """
    deltas = ["prefix ", _FAKE_SK[:18], _FAKE_SK[18:], " suffix"]
    # Probe: the secret really does span consecutive deltas here.
    assert _FAKE_SK in "".join(deltas)

    out = _stream(deltas)

    assert _FAKE_SK not in out


# ── 4. Plain text passes through verbatim ──────────────────────────────────


def test_plain_text_is_emitted_verbatim():
    deltas = ["Hello ", "world", "!", " How are ", "you today?"]

    assert _stream(deltas) == "".join(deltas)


def test_markdown_and_code_without_secrets_is_emitted_verbatim():
    text = (
        "Here is a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "```python\nprint('sk' + '-short')\n```\n"
    )
    # Split at awkward boundaries, including mid-token.
    deltas = [text[i : i + 7] for i in range(0, len(text), 7)]

    assert _stream(deltas) == text


# ── 5. Flush at stream end ─────────────────────────────────────────────────


def test_flush_releases_held_tail_without_swallowing_text():
    red = _redactor()

    held = red.feed("sk-")  # a potential secret prefix: held back
    assert held == ""

    assert red.flush() == "sk-", "flush must release the held tail verbatim"
    assert red.flush() == "", "flush is idempotent once the tail is drained"


def test_flush_after_ordinary_deltas_releases_exactly_the_tail():
    """Ordinary text is released as it arrives; only a marker opening is held.

    The previous expectation — "exactly the last character stays buffered" —
    pinned the behaviour that split ordinary prose across frames, which is
    what the browser lifecycle gate caught. The tail window now holds only a
    run that could still grow into a marker (down to its first character),
    so ordinary text goes out essentially whole and the flush releases only
    the held opening letter, if any.
    """
    red = _redactor()
    text = "The quick brown fox."
    emitted = "".join(red.feed(text[i : i + 3]) for i in range(0, len(text), 3))

    assert emitted + red.flush() == text, "ordinary text must not be lost"
    assert red.flush() == "", "a second flush after the release is empty"


# ── 6. Control: no over-redaction of secret-shaped prefixes ────────────────


def test_secret_shaped_prefix_followed_by_plain_words_is_not_masked():
    text = "sk- is just a prefix here, not a key at all"
    out = _stream(["sk- is just a prefix", " here, not a key at all"])

    assert out == text, "a prefix that never completes a secret stays verbatim"


def test_ordinary_token_shaped_words_survive():
    text = "Use AKIA-style ids carefully; this AKIAX is too short to match."
    out = _stream([text[:11], text[11:25], text[25:]])

    assert out == text


# ── 8. api_redact_enabled off → pass-through ───────────────────────────────


def test_disabled_redaction_passes_deltas_through(monkeypatch):
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": False}
    )
    red = _redactor()
    deltas = ["key ", _FAKE_SK, " end"]

    out = "".join(red.feed(d) for d in deltas) + red.flush()

    assert out == "".join(deltas), "user opt-out must disable delta redaction too"


# ── 7. Wiring: both SSE exits route token deltas through the redactor ──────


def _repo_path(*parts):
    return Path(__file__).resolve().parents[1].joinpath(*parts)


def test_streaming_exit_routes_token_deltas_through_redactor():
    src = _repo_path("api", "streaming.py").read_text(encoding="utf-8")

    # The old verbatim emit is gone from the live path.
    assert "put('token', {'text': text})" not in src
    assert "_delta_redactor.feed(" in src, "on_token must redact each delta"
    # The held tail is released through one shared helper: once before every
    # terminal event (done/stream_end/apperror) and once at the paragraph
    # boundary (interim_assistant), so the tail stays in its own paragraph.
    assert src.count("flush_stream_delta_tail(_delta_redactor, put)") >= 2, (
        "the held tail must be released before terminal events and paragraph boundaries"
    )


def test_gateway_exit_routes_token_deltas_through_redactor():
    src = _repo_path("api", "gateway_chat.py").read_text(encoding="utf-8")

    for site in ('emit("token", {"text": delta})', 'put_gateway_event("token", {"text": delta})'):
        assert site not in src, f"verbatim token emit still present: {site}"
    # The session-RPC path relays through run_session's emit; it must be
    # wrapped, not handed the raw put_gateway_event.
    assert "emit=put_gateway_event" not in src, "session RPC emit bypasses the redactor"
    # Relay sites (2) + worker site (1) + session wrapper (1).
    assert src.count(".feed(") >= 4, "every gateway delta site must redact"
    # All three gateway terminal paths release the tail through the shared
    # helper: done/stream_end/apperror, run.completed-with-error, run.failed.
    assert src.count("flush_stream_delta_tail(") >= 3, (
        "every gateway terminal path must release the held delta tail"
    )


# ── Real-agent coverage (skipped when hermes-agent is not installed) ───────


@pytest.fixture
def real_registry():
    redact = pytest.importorskip("agent.redact", reason="hermes-agent not installed")
    if not hasattr(redact, "register_redaction_patterns"):
        pytest.skip("installed agent.redact has no runtime pattern registry")
    if helpers._redact_fn_uncached.__name__ != "_combined_redact":
        pytest.skip("api.helpers was built without the agent redactor")
    reset = getattr(redact, "_reset_plugin_redaction_patterns", None)
    if reset is None:
        pytest.skip("installed agent.redact has no registry reset seam")
    reset()
    yield redact
    reset()


def test_real_agent_plugin_pattern_split_across_deltas_is_redacted(real_registry):
    token = "nvapi" + "-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0"
    pattern = r"nvapi-[A-Za-z0-9]{60}"
    assert real_registry.register_redaction_patterns([pattern], source="plugin:test-8901") == 1

    out = _stream(["plugin key: ", token[:9], token[9:34], token[34:], " done"])

    assert token not in out, "a plugin-shaped secret split across deltas must be masked"
    assert "plugin key: " in out
    assert " done" in out


# ── Terminal-error frames must release the held delta tail ────────────────
#
# The redactor holds back a tail window so a secret split across deltas can
# never be emitted in the clear. That window must be released on EVERY
# terminal path, not just the happy one: a run that ends with `run.failed`
# (or a `run.completed` that carries an error) unwinds the relay loop with a
# raise, so a flush that only happens on `done` / `stream_end` / `apperror`
# silently swallows the last streamed text and the turn ends with a
# truncated transcript.
#
# These cases drive the real `_relay_gateway_run_events` over a fake SSE
# response, so they exercise the production frame loop rather than a
# source-text check.


class _FakeSseResponse:
    """Minimal HTTPResponse stand-in: iterating it yields raw SSE lines."""

    def __init__(self, lines):
        self._lines = [line.encode("utf-8") for line in lines]

    def __iter__(self):
        return iter(self._lines)


def _relay_gateway_frames(frames):
    """Relay *frames* through the production gateway relay; return emitted tokens."""
    import json
    import threading

    from api import gateway_chat

    red = _redactor()
    emitted = []

    def put_gateway_event(event_name, data):
        if event_name == "token" and isinstance(data, dict):
            emitted.append(data.get("text") or "")

    lines = []
    for frame in frames:
        lines.append("data: " + json.dumps(frame))
    lines.append("data: [DONE]")

    resp = _FakeSseResponse(lines)
    try:
        gateway_chat._relay_gateway_run_events(
            resp,
            "same-session",
            "stream-1",
            "run-1",
            "http://gateway.invalid",
            "",
            put_gateway_event=put_gateway_event,
            cancel_event=threading.Event(),
            delta_redactor=red,
        )
    except RuntimeError:
        # A terminal error frame unwinds the relay loop with a raise; that is
        # the production contract. The point under test is what was emitted
        # BEFORE the unwind.
        pass
    return "".join(emitted), red


def test_run_failed_releases_the_held_delta_tail():
    """A run that ends with run.failed must not swallow the last streamed text."""
    text, red = _relay_gateway_frames([
        {"event": "message.delta", "delta": "the terminal answer"},
        {"event": "run.failed", "error": "boom"},
    ])
    assert "the terminal answer" in text, (
        f"the failed run swallowed the streamed text: {text!r}"
    )
    assert red._tail == "", "the tail window must be empty after a terminal failure"


def test_run_completed_with_error_releases_the_held_delta_tail():
    """run.completed carrying an error is terminal too, and must flush as well."""
    text, red = _relay_gateway_frames([
        {"event": "message.delta", "delta": "partial answer"},
        {"event": "run.completed", "error": "boom"},
    ])
    assert "partial answer" in text, (
        f"the failed completion swallowed the streamed text: {text!r}"
    )
    assert red._tail == "", "the tail window must be empty after a terminal error"


def test_failed_run_still_redacts_a_split_secret():
    """Control: the failure-path flush must not release a secret in the clear.

    This one is a CONTROL, not a discriminating case: it passes both with and
    without the failure-path flush, because a redactor that never flushes
    cannot leak. Its job is to pin the invariant that adding the flush does
    not trade the missing tail for a leaked secret.
    """
    secret = _FAKE_SK
    text, _ = _relay_gateway_frames([
        {"event": "message.delta", "delta": secret[:12]},
        {"event": "message.delta", "delta": secret[12:]},
        {"event": "run.failed", "error": "boom"},
    ])
    assert secret not in text, "the failure path must not release a secret in the clear"


# ── Ordinary text must not be LOST; the frame edge may split ──────────────
#
# The tail window exists to protect a marker whose PREFIX arrived before the
# rest of it — including a marker whose FIRST character arrived alone. That
# means a delta ending in a marker's opening letter ("...check", "...this")
# is held for one frame and released by the next delta or the flush.
#
# This is the deliberate trade: the front end concatenates token frames into
# a single prose row and matches content rows by PREFIX, so a split is
# invisible on screen — while releasing the character would leak the first
# character of a split secret ("s" then "k-…" puts the key body in the clear
# with no marker left for _redact_text to recognise). Content integrity is
# the contract; frame wholeness is not.


def test_ordinary_text_is_never_lost_even_when_the_frame_splits():
    """A single delta is emitted WHOLE unless its tail opens a marker.

    The browser lifecycle gate's terminal-error scenario streams exactly one
    delta; the row it names must still render. It does: the held character
    joins the row through the flush, and process-row matching is
    prefix-based, so the truncated first frame still identifies the row.
    """
    text = "Lifecycle terminal process check"
    red = _redactor()
    first = red.feed(text)
    assert first + red.flush() == text, (
        f"content must survive the frame split: {first!r}"
    )


def test_ordinary_text_is_not_split_when_no_marker_prefix_can_follow():
    """A delta whose last character opens no marker is emitted whole."""
    red = _redactor()
    assert red.feed("a quick brown fox.") == "a quick brown fox."


def test_marker_prefix_at_the_end_is_held_until_the_next_delta():
    """A real marker prefix IS held back, so the secret cannot leak early.

    This is the deliberate trade: a delta ending in a marker prefix is held
    (and released by the next delta or the flush) rather than emitted whole.
    The alternative — emitting it — would put the first characters of a split
    secret in the clear.
    """
    secret = _FAKE_GHP
    red = _redactor()
    first = red.feed(secret[:8])
    assert secret[:8] not in first, (
        f"the marker prefix was emitted before the secret was complete: {first!r}"
    )
    rest = red.feed(secret[8:]) + red.flush()
    assert secret not in first + rest, "the split secret leaked in the clear"


def test_one_character_suffix_is_held_so_a_split_marker_cannot_leak():
    """A trailing marker first-letter IS held — security over frame atomicity.

    The previous contract never held a single character, because holding
    "k" splits every ordinary word ending in one. That reasoning ignored the
    leak: a marker whose first character arrives in its own delta ("s" then
    "k-…") was already released, and the key body reached the browser with
    no marker for _redact_text to recognise. The split is frame-level only
    — the client concatenates frames and matches rows by prefix — so the
    cost is invisible while the leak was not.
    """
    for word in ("a quick check", "ok", "look"):
        red = _redactor()
        first = red.feed(word)
        assert first + red.flush() == word, (
            f"content must survive the split: {first!r}"
        )
        assert first != word, (
            f"the trailing marker letter must be held, got {first!r}"
        )


# ── Paragraph boundaries: the held tail belongs to its own paragraph ──────
#
# The tail window holds ordinary text whose tail could still grow into a
# marker (``"The last word is the key"`` ends in ``key``, a prefix of
# ``key=``/``key_material``). That text belongs to the paragraph that produced
# it. A paragraph boundary (``interim_assistant``) and every terminal event
# must release it BEFORE the boundary frame, or the held character run is
# appended to the next paragraph (and a replay reconstructs it there).
#
# Both directions are pinned: the release keeps the tail in its paragraph, and
# the unflushed sequence still carries it forward — so the test fails on a
# tree that never releases at the boundary, and keeps failing if the release
# is moved to the wrong side of the frame.


def _boundary_flush(red, put):
    """Call the production boundary release, or fail loudly on an unfixed tree."""
    fn = getattr(helpers, "flush_stream_delta_tail", None)
    assert fn is not None, (
        "api.helpers exposes no flush_stream_delta_tail — a paragraph boundary "
        "cannot release the held delta tail, so it lands in the next paragraph"
    )
    return fn(red, put)


def test_without_a_boundary_flush_the_tail_lands_in_the_next_paragraph():
    """The defect itself, on the pre-fix sequence: the tail joins the next paragraph."""
    red = _redactor()

    first = red.feed("The last word is the key")
    second = red.feed(" Second paragraph starts.")

    assert first == "The last word is the ", (
        f"probe: the tail window must hold the marker prefix, got {first!r}"
    )
    assert second.startswith("key"), (
        f"probe: without a boundary flush the held tail moves into the next "
        f"paragraph, got {second!r}"
    )


def _drive_paragraph_boundary(first, boundary_text, second):
    """Drive one paragraph boundary through the production release helper."""
    red = _redactor()
    events = []

    def put(event, data):
        events.append((event, (data or {}).get("text", "")))

    head = red.feed(first)
    if head:
        put("token", {"text": head})
    _boundary_flush(red, put)
    put("interim_assistant", {"text": boundary_text, "already_streamed": True})
    nxt = red.feed(second)
    if nxt:
        put("token", {"text": nxt})
    _boundary_flush(red, put)
    return events


def test_boundary_flush_keeps_the_tail_in_the_previous_paragraph():
    """Per-event: the boundary frame comes after every token of paragraph one."""
    first = "The last word is the key"
    second = " Second paragraph starts."

    events = _drive_paragraph_boundary(first, first, second)

    boundary_idx = next(
        i for i, (event, _text) in enumerate(events) if event == "interim_assistant"
    )
    before = [text for event, text in events[:boundary_idx] if event == "token"]
    after = [text for event, text in events[boundary_idx + 1 :] if event == "token"]

    assert "".join(before) == first, (
        f"paragraph one must be complete before the boundary frame, got {before!r}"
    )
    assert after == [second], (
        f"the next paragraph must start clean, got {after!r}"
    )
    assert "key" not in "".join(after), "the previous paragraph's tail leaked forward"
    assert "".join(before) + "".join(after) == first + second, (
        "the boundary must not drop or duplicate text"
    )


def test_boundary_flush_is_idempotent_when_nothing_is_held():
    """A boundary with no held tail emits no stray token frame."""
    red = _redactor()
    events = []

    def put(event, data):
        events.append((event, (data or {}).get("text", "")))

    out = red.feed("Plain prose with no marker prefix at all")
    if out:
        put("token", {"text": out})
    assert _boundary_flush(red, put) is False, "nothing held, nothing to release"
    assert events == [("token", "Plain prose with no marker prefix at all")]


def test_terminal_boundary_releases_the_tail_before_the_terminal_frame():
    """The terminal flush emits the tail, then the terminal frame — never after."""
    red = _redactor()
    events = []

    def put(event, data):
        events.append((event, (data or {}).get("text", "")))

    out = red.feed("The last word is the key")
    if out:
        put("token", {"text": out})
    _boundary_flush(red, put)
    put("done", {})

    assert events[-2] == ("token", "key"), (
        f"the held tail must be a token frame before done, got {events!r}"
    )
    assert events[-1][0] == "done"
    assert red.flush() == "", "a second flush after the terminal release is empty"


def test_interim_boundary_text_is_redacted_before_it_is_sent():
    """The interim frame re-sends the whole segment, so it needs the same redaction."""
    fn = getattr(helpers, "redact_stream_boundary_text", None)
    assert fn is not None, (
        "api.helpers exposes no redact_stream_boundary_text — the interim frame "
        "would carry segment text outside the redaction contract"
    )
    text = "here is " + _FAKE_SK + " in the prose"
    safe = fn(text)

    assert _FAKE_SK not in safe
    assert "here is " in safe
    assert "in the prose" in safe
    assert fn("plain visible prose") == "plain visible prose"


def test_gateway_relay_without_a_redactor_still_emits_plain_tokens():
    """A caller that passes no redactor still runs and relays ordinary text."""
    import json
    import threading

    from api import gateway_chat

    emitted = []

    def put_gateway_event(event_name, data):
        if event_name == "token" and isinstance(data, dict):
            emitted.append(data.get("text") or "")

    lines = [
        "data: " + json.dumps({"event": "message.delta", "delta": "plain "}),
        "data: " + json.dumps({"event": "message.delta", "delta": "words"}),
        "data: [DONE]",
    ]
    gateway_chat._relay_gateway_run_events(
        _FakeSseResponse(lines),
        "same-session",
        "stream-1",
        "run-1",
        "http://gateway.invalid",
        "",
        put_gateway_event=put_gateway_event,
        cancel_event=threading.Event(),
    )

    assert "".join(emitted) == "plain words", (
        f"the default-redactor relay must still emit ordinary tokens, got {emitted!r}"
    )


def test_gateway_relay_defaults_a_redactor_for_split_secrets():
    """The default redactor masks a split secret on a terminal error path too."""
    import json
    import threading

    from api import gateway_chat

    emitted = []

    def put_gateway_event(event_name, data):
        if event_name == "token" and isinstance(data, dict):
            emitted.append(data.get("text") or "")

    lines = [
        "data: " + json.dumps({"event": "message.delta", "delta": _FAKE_SK[:12]}),
        "data: " + json.dumps({"event": "message.delta", "delta": _FAKE_SK[12:]}),
        "data: " + json.dumps({"event": "run.completed", "error": "boom"}),
        "data: [DONE]",
    ]
    try:
        gateway_chat._relay_gateway_run_events(
            _FakeSseResponse(lines),
            "same-session",
            "stream-1",
            "run-1",
            "http://gateway.invalid",
            "",
            put_gateway_event=put_gateway_event,
            cancel_event=threading.Event(),
        )
    except RuntimeError:
        pass

    assert _FAKE_SK not in "".join(emitted), (
        "a caller that passes no redactor must still get masked deltas"
    )


# ── Replay: the reconstructed transcript keeps the same paragraph split ────


def _write_replay_journal(root, events, run_id="run_1", session_id="session_1"):
    import json

    from api import run_journal as RJ

    session_root = root / RJ.RUN_JOURNAL_DIR_NAME / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"{run_id}.jsonl"
    rows = []
    for seq, (event, payload) in enumerate(events, start=1):
        rows.append(
            json.dumps(
                {
                    "version": 1,
                    "event_id": f"{run_id}:{seq}",
                    "seq": seq,
                    "run_id": run_id,
                    "session_id": session_id,
                    "event": event,
                    "type": event,
                    "created_at": 1000 + seq,
                    "payload": payload,
                }
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_replay_keeps_the_released_tail_in_the_previous_paragraph(tmp_path, monkeypatch):
    """After the boundary release, replay splits the paragraphs the same way."""
    from api import run_journal as RJ
    import api.routes as routes

    first = "The last word is the key"
    second = " Second paragraph starts."
    _write_replay_journal(
        tmp_path,
        [
            ("token", {"text": first[: len(first) - 3]}),
            ("token", {"text": "key"}),
            ("interim_assistant", {"text": first, "already_streamed": True}),
            ("token", {"text": second}),
        ],
    )
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)

    snapshot = routes._run_journal_live_snapshot("run_1")

    assert snapshot is not None, "the replay snapshot must be produced"
    assert snapshot["last_assistant_text"] == first + second
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    prose = [row for row in rows if row.get("kind") == "process_prose"]
    assert prose, f"no prose rows in the replay snapshot: {rows!r}"
    assert prose[0]["text"].strip() == first, (
        f"the first paragraph must keep its own tail, got {prose[0]['text']!r}"
    )
    assert "key" not in "".join(row["text"] for row in prose[1:]), (
        "the tail leaked into a later paragraph on replay"
    )


def test_replay_of_an_unflushed_boundary_shows_the_misplacement(tmp_path, monkeypatch):
    """Control: the pre-fix frame order really does split the paragraph wrong.

    Without the boundary release, the journal carries the held tail AFTER the
    ``interim_assistant`` frame, and replay reconstructs paragraph one without
    its last word. This pins the discriminating power of the test above: it is
    the frame order, not the replay code, that decides where the tail lands.
    """
    from api import run_journal as RJ
    import api.routes as routes

    first = "The last word is the key"
    second = " Second paragraph starts."
    _write_replay_journal(
        tmp_path,
        [
            ("token", {"text": first[: len(first) - 3]}),
            ("interim_assistant", {"text": first, "already_streamed": True}),
            ("token", {"text": "key" + second}),
        ],
    )
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)

    snapshot = routes._run_journal_live_snapshot("run_1")

    assert snapshot is not None, "the replay snapshot must be produced"
    prose = [
        row
        for row in snapshot["anchor_activity_scene"]["activity_rows"]
        if row.get("kind") == "process_prose"
    ]
    assert prose[0]["text"].strip() == first[: len(first) - 3].strip(), (
        f"control: the unflushed order loses the tail from paragraph one, got "
        f"{prose[0]['text']!r}"
    )


# ── 9. Window-expiry safety: secrets longer than the tail window ───────────

# A JWT longer than _STREAM_DELTA_WINDOW. The bearer pattern has no length
# limit, so the secret outruns the tail window unless the start keeps its
# pin: releasing it strands the value's tail in the buffer without the
# "Authorization: Bearer" header its pattern anchors on. Header, payload and
# signature are base64url-shaped filler — no marker substring of their own,
# or every payload segment would register as its own secret start.
_FAKE_JWT = (
    "eyJ" + "Ab3xY9Qw8" + "." + ("Zx4cV7b1" * 140) + "Nm2kJ5h6"
)
assert len(_FAKE_JWT) > 1024


def test_secret_longer_than_the_window_is_held_until_it_completes():
    """A secret longer than the tail window must not leak its tail.

    ``_redact_text`` masks whole shapes, so a JWT longer than
    ``_STREAM_DELTA_WINDOW`` used to be masked only up to the cut: the
    remaining value left the buffer without its header and every later
    release emitted it verbatim — live and in journal replay. The start
    now keeps pinning the cut until the secret's mask stops touching the
    buffer end.
    """
    header = "Authorization: Bearer "
    assert len(header) + len(_FAKE_JWT) > helpers._STREAM_DELTA_WINDOW

    out = _stream(
        [header] + [_FAKE_JWT[i : i + 64] for i in range(0, len(_FAKE_JWT), 64)]
    )

    assert _FAKE_JWT not in out, "the JWT body must not reassemble in the clear"
    assert _FAKE_JWT[-40:] not in out, (
        "the value's tail must not escape once the window expired"
    )
    assert out == helpers._redact_text(header + _FAKE_JWT, _enabled=True), (
        "the streamed result must match the one-shot redaction of the whole text"
    )


def test_text_after_a_window_spanning_secret_still_streams():
    """Ordinary text following an over-window secret is released, not swallowed."""
    header = "Authorization: Bearer "
    out = _stream(
        [header]
        + [_FAKE_JWT[i : i + 64] for i in range(0, len(_FAKE_JWT), 64)]
        + [" and we are done."]
    )

    assert "and we are done." in out, "the run must settle with the trailing prose"
    assert _FAKE_JWT not in out
    assert out == helpers._redact_text(
        header + _FAKE_JWT + " and we are done.", _enabled=True
    )


def test_false_positive_start_is_still_released_after_the_window():
    """A start that never became a secret must not stall the stream.

    Window expiry is the release path for false positives (a bare
    "Bearer" with no token, a URL with no userinfo). The mask probe must
    not pin those forever, or ordinary long prose would buffer until
    flush.
    """
    plain = "Authorization: Bearer tokens are commonly used in APIs. " + (
        "filler text here. " * 80
    )

    out = _stream([plain[i : i + 64] for i in range(0, len(plain), 64)])

    assert out == helpers._redact_text(plain, _enabled=True), (
        "a false-positive start must stream exactly like a one-shot redaction"
    )


# ── 10. Cut monotonicity: a prefix never drags the cut forward ─────────────


def test_marker_prefix_never_drags_the_cut_past_a_secret_start():
    """"SG" ahead of an AWS key must not push the cut forward.

    The prefix-tail check sees "SG" (a prefix of the "SG." marker) at the
    buffer start; applying it as an absolute cut released the AWS key's
    opening characters unmasked while the marker check only ever saw the
    prefix. Retaining a prefix may only REDUCE the cut the secret-start
    loop already pinned.
    """
    red = _redactor()
    emitted = [red.feed(d) for d in ["SG", _FAKE_AKIA[:10], _FAKE_AKIA[10:]]]
    joined = "".join(emitted) + red.flush()

    assert _FAKE_AKIA not in joined, "the AWS key must not reassemble in the clear"
    assert _FAKE_AKIA[:10] not in joined, (
        "no partial key may be released before its marker completed"
    )
    assert joined == helpers._redact_text("SG" + _FAKE_AKIA, _enabled=True), (
        "the streamed result must match the one-shot redaction of the whole text"
    )


@pytest.mark.parametrize("split", [1, 2])
def test_short_marker_split_at_every_position_is_redacted(split):
    """A short marker ("sk-") arriving in pieces must stay held until whole.

    The opening marker can span deltas at any position; holding one
    character is not enough — the retained run must be the longest
    suffix that is still a prefix of a real marker.
    """
    secret = _FAKE_SK
    red = _redactor()
    emitted = [red.feed(secret[:split]), red.feed(secret[split:])]
    joined = "".join(emitted) + red.flush()

    assert secret not in joined, "a split marker must not leak its key"
    assert joined == helpers._redact_text(secret, _enabled=True), (
        "the streamed result must match the one-shot redaction of the whole key"
    )


# ── 11. Boundary payload parity: setting + echo-flag consistency ───────────


def test_interim_boundary_text_respects_disabled_redaction(monkeypatch):
    """With api_redact_enabled off the boundary frame stays verbatim.

    The deltas and the saved-session projection honor the opt-out; masking
    the interim frame alone made the live view diverge from both.
    """
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": False}
    )
    text = "here is " + _FAKE_SK + " in the prose"

    assert helpers.redact_stream_boundary_text(text) == text


def test_interim_boundary_text_accepts_the_stream_snapshot(monkeypatch):
    """The owning redactor's snapshot wins over the live setting.

    The streaming bridge passes the redactor's api_redact_enabled snapshot
    so the boundary frame agrees with the deltas even if the setting
    changes mid-run.
    """
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": True}
    )
    text = "here is " + _FAKE_SK + " in the prose"

    assert helpers.redact_stream_boundary_text(text, enabled=False) == text
    assert _FAKE_SK not in helpers.redact_stream_boundary_text(text, enabled=True)


# ── 12. The real interim callback: setting parity and echo-flag safety ─────


def _drive_interim_stream(monkeypatch, text, *, with_reasoning):
    """Drive one interim boundary through the real streaming bridge.

    Returns the interim_assistant payloads the SSE bridge queued. Mirrors
    the fake-agent wiring of test_issue_progress_echo_dedupe.py so the
    regression exercises the production on_interim_assistant closure, not
    a re-implementation.
    """
    import api.streaming as streaming

    run_suffix = uuid.uuid4().hex[:8]

    class FakeSession:
        def __init__(self):
            self.session_id = "issue8901_interim_boundary_" + run_suffix
            self.title = "Interim boundary"
            self.workspace = "/tmp"
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost_usd = 0.0
            self.cache_read_tokens = 0
            self.cache_write_tokens = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = ""
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {"session_id": self.session_id}

    class InterimAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            prefill_messages=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            interim_assistant_callback=None,
            **_kwargs,
        ):
            self.stream_delta_callback = stream_delta_callback
            self.reasoning_callback = reasoning_callback
            self.interim_assistant_callback = interim_assistant_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            if with_reasoning and self.reasoning_callback is not None:
                self.reasoning_callback(text)
            self.stream_delta_callback(text)
            self.interim_assistant_callback(text)
            history = kwargs.get("conversation_history", [])
            return {
                "messages": history + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": text},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    # A unique stream id per call: the bridge spawns daemon helper threads
    # (metering, checkpoint) that look STREAMS[stream_id] up on every put();
    # a shared id lets a previous call's leftover frames land in this queue.
    # The session id is unique for the same reason — SESSION_AGENT_CACHE
    # reuses the agent instance per session, and a cached instance would
    # replay the previous call's callbacks.
    fake_stream_id = "stream_issue8901_interim_boundary_" + run_suffix
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_payload = {"provider": "openai", "base_url": None, "platform": None}
    runtime_payload["api_" + "key"] = "***"
    fake_runtime_module.__dict__["resolve_runtime_provider"] = mock.Mock(
        return_value=runtime_payload
    )
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__dict__["runtime_provider"] = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.__dict__["SessionDB"] = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    saved = {k: sys.modules.get(k, _MISSING) for k in injected}
    sys.modules.update(injected)
    try:
        with mock.patch.multiple(
            "api.streaming",
            get_session=mock.Mock(return_value=fake_session),
            _get_ai_agent=mock.Mock(return_value=InterimAgent),
            resolve_model_provider=mock.Mock(
                return_value=("gpt-test", "openai", None)
            ),
        ), mock.patch("api.config.get_config", return_value={}), mock.patch(
            "api.config._resolve_cli_toolsets", return_value=[]
        ):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="scan",
                model="gpt-test",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)
        for k, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev

    return [
        payload
        for event, payload in list(fake_queue.queue)
        if event == "interim_assistant"
    ]


def test_interim_callback_honors_disabled_redaction(monkeypatch, cleanup_test_sessions):
    """Through the real callback: opt-out leaves the boundary frame verbatim."""
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": False}
    )
    text = "here is " + _FAKE_SK + " in the prose"

    payloads = _drive_interim_stream(monkeypatch, text, with_reasoning=False)

    assert payloads, "the run must emit an interim_assistant frame"
    assert payloads[0]["text"] == text, (
        "with redaction off the boundary frame must carry the segment verbatim"
    )


def test_interim_callback_masks_the_boundary_frame_when_enabled(
    monkeypatch, cleanup_test_sessions
):
    """Through the real callback: the boundary frame obeys the same contract."""
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": True}
    )
    text = "here is " + _FAKE_SK + " in the prose"

    payloads = _drive_interim_stream(monkeypatch, text, with_reasoning=False)

    assert payloads, "the run must emit an interim_assistant frame"
    assert _FAKE_SK not in payloads[0]["text"], (
        "with redaction on the boundary frame must mask the secret like the deltas"
    )
    assert "here is " in payloads[0]["text"]
    assert "in the prose" in payloads[0]["text"]


def test_interim_echo_flag_is_withheld_when_redaction_changes_the_text(
    monkeypatch, cleanup_test_sessions
):
    """A masked boundary text must not carry reasoning_echo.

    The client strips the echoed paragraph out of the live Thinking card
    by matching the payload text against the reasoning it rendered (and
    journal replay repeats the search). When redaction changed the text
    that match is guaranteed to fail, so the flag would only send the
    client on a search that cannot succeed.
    """
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": True}
    )
    text = "checking " + _FAKE_SK + " now"

    payloads = _drive_interim_stream(monkeypatch, text, with_reasoning=True)

    assert payloads, "the run must emit an interim_assistant frame"
    assert _FAKE_SK not in payloads[0]["text"]
    assert "reasoning_echo" not in payloads[0], (
        "a masked boundary text can never match the raw reasoning — the flag "
        "would only trigger a doomed client-side search"
    )


def test_interim_echo_flag_survives_when_redaction_is_off(
    monkeypatch, cleanup_test_sessions
):
    """Control: with the opt-out the echo strip and the flag still work."""
    monkeypatch.setattr(
        "api.config.load_settings", lambda: {"api_redact_enabled": False}
    )
    text = "checking the settings now"

    payloads = _drive_interim_stream(monkeypatch, text, with_reasoning=True)

    assert payloads, "the run must emit an interim_assistant frame"
    assert payloads[0]["text"] == text
    assert payloads[0].get("reasoning_echo") is True, (
        "without redaction the payload text matches the raw reasoning, so the "
        "echo flag must still reach the client"
    )


# ── 13. Tool boundaries release the held tail before the tool frame ───────


def test_tool_boundary_flush_keeps_the_tail_in_the_previous_paragraph():
    """A tool frame opens a new activity group; the tail stays in the prose.

    The browser lifecycle gate's terminal-error scenario streams a delta
    whose tail is a marker opening, then a tool frame, then fails the run.
    Releasing the held run only at the terminal drops it from the live
    snapshot — live then no longer matches settled, and the gate fails. The
    same boundary rule as interim_assistant applies: release BEFORE the
    tool frame.
    """
    red = _redactor()
    events = []

    def put(event, data):
        events.append((event, (data or {}).get("text", "")))

    head = red.feed("The last word is the key")
    if head:
        put("token", {"text": head})
    _boundary_flush(red, put)
    put("tool", {"text": "read_file"})

    assert events[-2] == ("token", "key"), (
        f"the held tail must be a token frame before the tool frame, got {events!r}"
    )
    assert events[-1][0] == "tool"
    assert red.flush() == "", "a second flush after the boundary release is empty"


def test_gateway_relay_flushes_the_tail_before_a_tool_frame():
    """Through the real relay: a tool frame never strands the held tail.

    The lifecycle gate drives the gateway relay path: message.delta, then
    tool.started, then run.failed. Without a flush before the tool frame
    the held character run reaches the browser after the tool row (or not
    at all, once the terminal drops later frames), so the live snapshot
    loses characters the settled snapshot keeps.
    """
    import json
    import threading

    from api import gateway_chat

    emitted = []

    def put_gateway_event(event_name, data):
        emitted.append((event_name, (data or {}).get("text", "")))

    lines = [
        "data: " + json.dumps({"event": "message.delta", "delta": "the key"}),
        "data: " + json.dumps({"event": "tool.started", "tool": "read_file",
                               "tool_call_id": "t1", "status": "running",
                               "args": {"path": "README.md"}}),
        "data: " + json.dumps({"event": "run.failed", "error": "boom"}),
    ]
    try:
        gateway_chat._relay_gateway_run_events(
            _FakeSseResponse(lines),
            "same-session",
            "stream-1",
            "run-1",
            "http://gateway.invalid",
            "",
            put_gateway_event=put_gateway_event,
            cancel_event=threading.Event(),
        )
    except RuntimeError:
        # A terminal error frame unwinds the relay loop with a raise; that is
        # the production contract. The point under test is the frame order
        # BEFORE the unwind.
        pass

    tool_idx = next(
        (i for i, (event, _text) in enumerate(emitted) if event == "tool"), None
    )
    assert tool_idx is not None, f"the relay must surface the tool frame, got {emitted!r}"
    before_tool = [text for event, text in emitted[:tool_idx] if event == "token"]
    assert "".join(before_tool) == "the key", (
        f"the held tail must be released before the tool frame, got {emitted!r}"
    )
