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
    red = _redactor()
    text = "The quick brown fox"
    emitted = "".join(red.feed(text[i : i + 3]) for i in range(0, len(text), 3))

    assert emitted == text[:-1], "exactly the last character stays buffered"
    assert emitted + red.flush() == text


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
