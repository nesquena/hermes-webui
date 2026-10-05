"""``_strip_compact_echo_suffix`` must find the cut point in one pass.

The reasoning/visible echo stripper is called up to four times per interim
assistant message. The original implementation probed every candidate cut
index and re-folded the whole remaining tail for each probe, so the cost grew
with ``window x suffix`` instead of with the data actually inspected. On a
6000-character conclusion block that is seconds of pure CPU, held under the
GIL, which stalls every other stream in the process.

The current implementation walks ``value`` and ``suffix`` backwards from the
end (skipping whitespace in ``value``) and has no fixed search window, so a
compact-equivalent echo is found regardless of how much interior whitespace
stretches its raw span.

These tests pin both halves of the contract:

* the fast path must return byte-identical results to a straightforward
  reference implementation, including the leftmost-cut tie-break; and
* it must not fold the whole buffer once per candidate cut index.
"""
import random
import re
import string

import pytest


def _reference_strip_compact_echo_suffix(value, suffix):
    """Deliberately naive oracle: probe every cut index, fold the tail.

    This mirrors the pre-window semantics (the original probing loop) with no
    search window, and exists only so the optimised backward walk can be
    proven equivalent to it.
    """
    def compact(v):
        return re.sub(r'\s+', '', str(v or ''))

    raw = str(value or '')
    candidate = compact(suffix)
    if not raw or not candidate:
        return raw, False
    for idx in range(len(raw) + 1):
        if compact(raw[idx:]) == candidate:
            return raw[:idx].rstrip(), True
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
        # Echo whose whitespace-padded span is wider than any fixed fold
        # window: the backward walk must still strip it.
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


def test_strip_compact_echo_suffix_finds_an_echo_wider_than_any_fixed_window():
    """An echo stretched past any fold window must still be stripped.

    The buffer ends with the echo, but interior whitespace stretches its raw
    span far beyond a fixed fold window (the retired windowed implementation
    exposed only the last word and left the buffer untouched). The backward
    walk has no window, so the cut point is found and the echo is removed.
    """
    import api.streaming as streaming

    buffer_text = "bla une" + " " * 5000 + "conclusion"
    suffix = "une conclusion"

    assert streaming._strip_compact_echo_suffix(buffer_text, suffix) == ("bla", True)
    assert _reference_strip_compact_echo_suffix(buffer_text, suffix) == ("bla", True)


def test_strip_compact_echo_suffix_does_not_refold_per_cut(monkeypatch):
    """One pass, not one fold per candidate cut index.

    Counting the folding-helper calls pins the algorithmic property directly,
    without depending on wall-clock timing: the backward walk folds the
    suffix once and then compares characters.
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
        "is re-folding once per candidate cut index"
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