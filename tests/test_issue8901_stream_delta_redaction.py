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
  8. with ``api_redact_enabled`` off the redactor is a pass-through.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402

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
    """Ordinary text is released as it arrives; only a marker prefix is held.

    The previous expectation — "exactly the last character stays buffered" —
    pinned the behaviour that split ordinary prose across frames, which is
    what the browser lifecycle gate caught. The tail window now holds only a
    run that could still grow into a marker, so ordinary text goes out whole
    and the flush has nothing to release.
    """
    red = _redactor()
    text = "The quick brown fox"
    emitted = "".join(red.feed(text[i : i + 3]) for i in range(0, len(text), 3))

    assert emitted == text, "ordinary text must not be held back"
    assert red.flush() == "", "nothing of ordinary text should remain buffered"


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
    assert "_delta_redactor.flush()" in src, "the held tail must be flushed at stream end"


def test_gateway_exit_routes_token_deltas_through_redactor():
    src = _repo_path("api", "gateway_chat.py").read_text(encoding="utf-8")

    for site in ('emit("token", {"text": delta})', 'put_gateway_event("token", {"text": delta})'):
        assert site not in src, f"verbatim token emit still present: {site}"
    # The session-RPC path relays through run_session's emit; it must be
    # wrapped, not handed the raw put_gateway_event.
    assert "emit=put_gateway_event" not in src, "session RPC emit bypasses the redactor"
    # Relay sites (2) + worker site (1) + session wrapper (1).
    assert src.count(".feed(") >= 4, "every gateway delta site must redact"
    assert "_delta_redactor.flush()" in src


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


# ── Ordinary text must not be split across frames ────────────────────────
#
# The tail window exists to protect a marker whose PREFIX arrived before the
# rest of it. Holding back anything else splits ordinary prose in the middle:
# the front end then receives a truncated first frame and a detached tail
# frame, and a message row that the front end matches by content (a process
# row, for instance) never renders.
#
# The browser lifecycle gate caught exactly this: the terminal-error scenario
# streams one delta and expects it intact.


def test_ordinary_text_is_emitted_in_one_piece_not_split():
    """A single delta must be emitted WHOLE, not split across frames.

    The frame boundary is what matters, not the eventual concatenation: the
    front end matches a process row by content, so a delta that arrives in
    two pieces (a truncated head plus a detached tail) never renders as the
    row it names. The lifecycle gate's terminal-error scenario streams exactly
    one delta and expects that one frame intact.
    """
    text = "Lifecycle terminal process check"
    red = _redactor()
    first = red.feed(text)
    assert first == text, (
        f"ordinary text was split across frames: {first!r}"
    )


def test_ordinary_text_is_not_split_even_when_it_ends_mid_word():
    """A delta whose last characters are not a marker prefix stays whole."""
    red = _redactor()
    assert red.feed("a quick brown fox") == "a quick brown fox"


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


def test_one_character_suffix_is_never_held():
    """A single trailing character is not a prefix worth holding.

    Markers like ``key_material`` and ``key=`` begin with ``k``, so holding a
    trailing ``k`` would split every ordinary word ending in one.
    """
    red = _redactor()
    assert red.feed("a quick check") == "a quick check"
    red2 = _redactor()
    assert red2.feed("ok") == "ok"
    red3 = _redactor()
    assert red3.feed("look") == "look"
