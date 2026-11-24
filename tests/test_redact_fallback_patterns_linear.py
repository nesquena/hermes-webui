"""Two fallback redaction patterns stay linear on long input, with unchanged results.

* ``_SENSITIVE_TELEGRAM_MARKER_RE`` (the prefilter's Telegram bot-token marker)
  retried a long digit run from every digit, and each try scanned to the end
  of the run, so ``"Result:" + 100_000 digits + ":"`` took seconds. The digit
  run now starts only at its first digit.
* The fallback private-key pattern retried every later BEGIN line once a
  BEGIN line had no END after it, and each try scanned to the end of the
  text, so many unterminated BEGIN lines took seconds. A BEGIN line without an
  END now takes the rest of the text in one step and keeps it unchanged.

The differential tests compare the new patterns with literal copies of the
previous ones. The long-input tests assert results under a generous pytest
timeout instead of timing anything: the previous patterns need many minutes
on these inputs, the new ones a few milliseconds.
"""
import itertools
import random
import re
import sys
import types
from unittest import mock

import pytest

from api import helpers

# Literal copies of the previous patterns: the oracles.
_OLD_TELEGRAM_MARKER_RE = re.compile(r"(?:bot)?\d{8,}:[-A-Za-z0-9_]{30,}")
_OLD_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----")
_REDACTED = "[REDACTED PRIVATE KEY]"

_BEGIN = "-----BEGIN RSA PRIVATE KEY-----\n"


def _old_private_key_sub(text):
    return _OLD_PRIVATE_KEY_RE.sub(_REDACTED, text)


def _spans(pattern, text):
    return [m.span() for m in pattern.finditer(text)]


def _assert_telegram_equivalent(text):
    old, new = _OLD_TELEGRAM_MARKER_RE.search(text), helpers._SENSITIVE_TELEGRAM_MARKER_RE.search(text)
    assert (old and old.span()) == (new and new.span()), repr(text)
    assert _spans(_OLD_TELEGRAM_MARKER_RE, text) == _spans(helpers._SENSITIVE_TELEGRAM_MARKER_RE, text), repr(text)


def _assert_private_key_equivalent(text):
    assert helpers._redact_private_key_blocks(text) == _old_private_key_sub(text), repr(text)


def _fallback_only_redactor():
    """The local fallback redactor, built as it is when hermes-agent is absent."""
    with mock.patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": None}):
        redact = helpers._build_redact_fn()
    assert redact.__name__ == "_fallback_redact"
    return redact


# ── Differential: Telegram marker ──────────────────────────────────────────


def _telegram_corpus():
    rnd = random.Random(20261124)
    token_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    corpus = [
        "1234567890:***",  # tests/test_sensitive_prefilter_equivalence.py
        "", ":", "bot", "bot:", "12345678:", "1234567:" + "A" * 30, "12345678:" + "A" * 29,
        "12345678:" + "A" * 30, "bot12345678:" + "A" * 30, "1bot12345678:" + "A" * 30,
        "x123456789012:" + "a1_-" * 10, "Result: 12:34:56", "+33612345678", "<@123456789012345678>",
        "\u0663" * 8 + ":" + "A" * 30,  # Arabic-Indic digits are \d too
        "\uff15" * 9 + ":" + "Z" * 31,  # fullwidth digits
    ]
    for _ in range(200):
        bot = str(rnd.randint(10**7, 10**10))
        tok = "".join(rnd.choice(token_chars) for _ in range(rnd.randint(28, 46)))
        corpus += [
            f"{bot}:{tok}", f"bot{bot}:{tok}", f"https://api.telegram.org/bot{bot}:{tok}/getMe",
            f"TELEGRAM_BOT_TOKEN={bot}:{tok}", f"id {bot}{bot}:{tok} ok", f"ref {bot}:{tok[:20]}",
        ]
    parts = ["bot", ":", "-", "_", " ", "\n", "a", "Z", "x9", "\u0663", "12345678", "1234567", "123456789012",
             "A" * 30, "a1_-" * 8, "0", "bo", "ot", "Result:", "://"]
    for _ in range(20000):
        corpus.append("".join(rnd.choice(parts) for _ in range(rnd.randint(1, 12))))
    for n in (8, 9, 50, 2000):
        for pre in ("", "bot", "1bot", "x"):
            corpus += [pre + "1" * n + ":" + "A" * 30, pre + "1" * n + ":" + "A" * 29,
                       pre + "1" * n + ":" + "A" * 29 + ":" + "1" * 8 + ":" + "A" * 30]
    return corpus


def test_telegram_marker_matches_like_the_previous_pattern():
    corpus = _telegram_corpus()
    matched = sum(bool(_OLD_TELEGRAM_MARKER_RE.search(text)) for text in corpus)
    assert 0 < matched < len(corpus), "the corpus must hold both matches and misses"
    for text in corpus:
        _assert_telegram_equivalent(text)


def test_prefilter_still_flags_telegram_tokens():
    token = "bot123456789:" + "AAEh" * 9
    assert helpers._might_contain_sensitive_text(f"use {token} here") is True
    assert helpers._might_contain_sensitive_text("1234567890:***") is False
    assert helpers._might_contain_sensitive_text("Result: 12345678:short") is False


# ── Differential: private-key blocks ───────────────────────────────────────


def _private_key_corpus():
    rnd = random.Random(20261124)
    b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

    def pem(kind):
        body = "\n".join("".join(rnd.choice(b64) for _ in range(64)) for _ in range(rnd.randint(1, 4)))
        return f"-----BEGIN {kind}PRIVATE KEY-----\n{body}\n-----END {kind}PRIVATE KEY-----"

    # tests/test_redaction_assignment_preconditions.py
    corpus = ["-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n-----END " + "PRIVATE KEY-----"]
    for kind in ("", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "):
        block = pem(kind)
        corpus += [block, f"key:\n{block}\nend", f"{block}\n\n{pem('RSA ')}", block.rsplit("-----END", 1)[0],
                   block.replace("\n", "\\n"), f"```\n{block}\n```"]
    tokens = [
        "-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----END PRIVATE KEY-----",
        "-----END RSA PRIVATE KEY-----", "-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----",
        "-----BEGIN PRIVATE KEY", "BEGIN PRIVATE KEY-----", "-----BEGINPRIVATE KEY-----",
        "-----BEGIN rsa PRIVATE KEY-----", "------BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY------",
        "-----BEGIN PRIVATE KEY------BEGIN PRIVATE KEY-----", "PRIVATE KEY-----", "-----", "-", "\n", " ", "x",
        "MIIEvQIBADANBg", "-----END", "-----BEGIN",
    ]
    for _ in range(20000):
        corpus.append("".join(rnd.choice(tokens) for _ in range(rnd.randint(1, 10))))
    small = ["-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
             "-----END RSA PRIVATE KEY-----", "x", "\n", "-----"]
    for n in range(1, 6):  # every interleaving of the small set up to 5 tokens
        corpus.extend("".join(combo) for combo in itertools.product(small, repeat=n))
    return corpus


def test_private_key_blocks_are_redacted_like_the_previous_pattern():
    corpus = _private_key_corpus()
    changed = sum(_old_private_key_sub(text) != text for text in corpus)
    assert 0 < changed < len(corpus), "the corpus must hold both blocks and non-blocks"
    for text in corpus:
        _assert_private_key_equivalent(text)


def test_fallback_redactor_keeps_its_private_key_results():
    redact = _fallback_only_redactor()
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEsynthetic\n-----END RSA PRIVATE KEY-----"
    assert redact(f"key:\n{block}\nend") == f"key:\n{_REDACTED}\nend"
    nested = "-----BEGIN PRIVATE KEY-----\na\n" + block + "\ntail"
    assert redact(nested) == _REDACTED + "\ntail"
    unterminated = "head\n-----BEGIN PRIVATE KEY-----\nMIIEsynthetic\n"
    assert redact(unterminated) == unterminated
    assert redact(block + "\n" + unterminated) == _REDACTED + "\n" + unterminated


# ── Long adversarial input: results, under a generous timeout ──────────────


@pytest.mark.timeout(60)
def test_long_digit_run_prefilter_completes():
    text = "Result:" + "1" * 1_000_000 + ":"
    assert helpers._SENSITIVE_TELEGRAM_MARKER_RE.search(text) is None
    assert helpers._might_contain_sensitive_text(text) is False
    assert helpers._redact_text(text, _enabled=True) == text
    token = "1" * 1_000_000 + ":" + "A" * 30
    assert helpers._SENSITIVE_TELEGRAM_MARKER_RE.search(token).span() == (0, len(token))


@pytest.mark.timeout(60)
def test_many_unterminated_private_key_headers_complete():
    text = _BEGIN * 100_000  # 3.2M characters, no END line
    assert _fallback_only_redactor()(text) == text
    assert helpers._redact_private_key_blocks(text) == text
    block = "-----END RSA PRIVATE KEY-----"
    assert helpers._redact_private_key_blocks(text + block) == _REDACTED
