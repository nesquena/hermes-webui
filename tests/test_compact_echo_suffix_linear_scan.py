"""``_strip_compact_echo_suffix`` must find the cut point in one pass.

The reasoning/visible echo stripper is called up to four times per interim
assistant message. The original implementation probed every candidate cut
index and re-folded the whole remaining tail for each probe, so the cost grew
with ``window x suffix`` instead of with the data actually inspected. On a
6000-character conclusion block that is seconds of pure CPU, held under the
GIL, which stalls every other stream in the process.

These tests pin both halves of the contract:

* the fast path must return byte-identical results to a straightforward
  reference implementation, including the leftmost-cut tie-break; and
* it must not re-fold the search window once per candidate cut index.
"""
import random
import re
import string

import pytest


def _reference_strip_compact_echo_suffix(value, suffix, *, search_window: int = 4096):
    """Deliberately naive oracle: probe every cut index, fold the whole tail.

    This mirrors the original behaviour exactly and exists only so the
    optimised implementation can be proven equivalent to it.
    """
    def compact(v):
        return re.sub(r'\s+', '', str(v or ''))

    raw = str(value or '')
    candidate = compact(suffix)
    if not raw or not candidate:
        return raw, False
    tail = raw[-max(len(str(suffix or '')) * 3, search_window):]
    offset = len(raw) - len(tail)
    for idx in range(len(tail) + 1):
        if compact(tail[idx:]) == candidate:
            return raw[: offset + idx].rstrip(), True
    return raw, False


def _cases():
    cases = [
        # Exact echo at the end of the buffer.
        ("bla bla bla une conclusion", "une conclusion"),
        # Echo that differs only by whitespace shape.
        ("bla bla  une   conclusion", "une conclusion"),
        ("bla bla\nune\tconclusion", "une conclusion"),
        # No echo at all.
        ("texte quelconque sans rapport", "une conclusion"),
        # Partial echo must NOT match.
        ("bla une conclu", "une conclusion"),
        # The buffer is exactly the echo.
        ("une conclusion", "une conclusion"),
        # Empty / None inputs.
        ("", "une conclusion"),
        ("bla bla", ""),
        ("", ""),
        (None, "x"),
        ("x", None),
        # Trailing and interior whitespace around the cut point.
        ("bla bla une conclusion   ", "une conclusion"),
        ("bla bla   \n  une conclusion", "une conclusion"),
        # Unicode, accents, emoji.
        ("préambule ✅ conclusion émise", "conclusion émise"),
        ("texte 🟢 Réponse", "🟢 Réponse"),
        # Whitespace-only suffix folds to nothing.
        ("bla bla", "   \n  "),
        # Repetitions: the leftmost cut point is the contractual one.
        ("abc abc abc", "abc"),
        ("abc abc abc", "abc abc"),
        ("aaaa", "aa"),
        # Long buffer with the echo at the very end.
        ("remplissage " * 500 + "la vraie conclusion", "la vraie conclusion"),
        # Echo whose whitespace-padded span is wider than the search window:
        # the window only exposes ``conclusion``, so nothing may be stripped.
        ("bla une" + " " * 5000 + "conclusion", "une conclusion"),
        # Exotic whitespace that ``\s`` and ``str.isspace`` must treat alike.
        ("bla\u00a0bla une conclusion", "une\u00a0conclusion"),
        ("bla\u2028une conclusion", "une conclusion"),
    ]
    rnd = random.Random(20260825)
    alphabet = string.ascii_letters + "  \n\t" + "éà✅\u00a0"
    for _ in range(2000):
        base = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 120)))
        if rnd.random() < 0.5 and len(base) > 4:
            k = rnd.randint(1, max(1, len(base) // 2))
            suffix = base[-k:]
        else:
            suffix = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30)))
        cases.append((base, suffix))
    return cases


def test_strip_compact_echo_suffix_matches_reference_oracle():
    """The optimised scan must be indistinguishable from the naive probe."""
    import api.streaming as streaming

    divergences = []
    for value, suffix in _cases():
        expected = _reference_strip_compact_echo_suffix(value, suffix)
        actual = streaming._strip_compact_echo_suffix(value, suffix)
        if actual != expected:
            divergences.append((value, suffix, expected, actual))

    assert not divergences[:5], (
        f"{len(divergences)} divergence(s) from the reference implementation; "
        f"first: {divergences[:1]}"
    )


def test_strip_compact_echo_suffix_honours_a_custom_search_window():
    """The bounded window must keep its meaning on the fast path."""
    import api.streaming as streaming

    buffer_text = "z" * 400 + " la conclusion"
    for window in (16, 64, 512, 4096):
        assert streaming._strip_compact_echo_suffix(
            buffer_text, "la conclusion", search_window=window
        ) == _reference_strip_compact_echo_suffix(
            buffer_text, "la conclusion", search_window=window
        ), f"window={window}"


def test_strip_compact_echo_suffix_rejects_an_echo_wider_than_the_window():
    """An echo that only fits inside a wider window must not be found.

    The buffer ends with the echo, but interior whitespace stretches its raw
    span past the default window, so the folded window only exposes the last
    word. Widening the window to cover the whole span makes the same echo
    visible again, which proves the non-match is caused by the window and
    not by the text.
    """
    import api.streaming as streaming

    buffer_text = "bla une" + " " * 5000 + "conclusion"
    suffix = "une conclusion"

    for impl in (streaming._strip_compact_echo_suffix, _reference_strip_compact_echo_suffix):
        assert impl(buffer_text, suffix) == (buffer_text, False), impl.__name__
        assert impl(buffer_text, suffix, search_window=len(buffer_text)) == ("bla", True), impl.__name__


def test_strip_compact_echo_suffix_does_not_refold_the_window_per_cut(monkeypatch):
    """One pass, not one fold per candidate cut index.

    The original implementation called the whitespace-folding helper once per
    possible cut position — roughly 4097 times for a default window — and each
    of those calls re-scanned the remaining tail. Counting the calls pins the
    algorithmic property directly, without depending on wall-clock timing.
    """
    import api.streaming as streaming

    calls = {'n': 0}
    original = streaming._compact_for_echo_compare

    def counting(value):
        calls['n'] += 1
        return original(value)

    monkeypatch.setattr(streaming, '_compact_for_echo_compare', counting)

    reasoning_buffer = "Analyse de la situation en cours. " * 1500
    conclusion = "x" * 6000
    streaming._strip_compact_echo_suffix(reasoning_buffer, conclusion)

    assert calls['n'] <= 8, (
        f"whitespace folding ran {calls['n']} times for a single call; the scan "
        "is re-folding the window once per candidate cut index"
    )


@pytest.mark.timeout(60)
def test_strip_compact_echo_suffix_stays_cheap_on_a_long_conclusion():
    """A long final message must not cost seconds of GIL-held CPU."""
    import time

    import api.streaming as streaming

    reasoning_buffer = "Analyse de la situation en cours. " * 1500
    conclusion = "x" * 6000

    # Warm up so the first-call import/regex cache is not measured.
    streaming._strip_compact_echo_suffix(reasoning_buffer, conclusion)

    start = time.perf_counter()
    for _ in range(5):
        streaming._strip_compact_echo_suffix(reasoning_buffer, conclusion)
    elapsed = (time.perf_counter() - start) / 5

    # The original implementation needs ~7s here. A very loose budget keeps the
    # test meaningful on a loaded CI box while still failing the quadratic scan
    # by three orders of magnitude.
    assert elapsed < 0.5, f"single call took {elapsed:.3f}s"
