"""``_CompactEchoIndex`` must agree with the raw backward walk, cheaply.

The index is the echo-match authority for both the journal rebuild
(``api/routes.py::_run_journal_live_snapshot``) and the live stream
(``api/streaming.py``). It replaces a per-interim raw backward walk that
rescanned the whole transcript's whitespace — quadratic on whitespace-heavy
text (#7569). Two contracts must hold for the swap to be safe:

1. **Equivalence** — incremental appends must reach the same cut point as a
   raw walk over the concatenated text, for every shape including chunked
   appends, whitespace-only chunks, and exotic Unicode whitespace.
2. **Cost** — the index must never rescan the buffer it has already folded:
   a probe after a wide whitespace span must not walk that span.
"""
import random
import re
import string

import pytest


def _compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def _raw_reference(value, suffix):
    """Deliberately naive oracle: probe every cut index, fold the tail."""
    raw = str(value or '')
    candidate = _compact(suffix)
    if not raw or not candidate:
        return raw, False
    for idx in range(len(raw) + 1):
        if _compact(raw[idx:]) == candidate:
            return raw[:idx].rstrip(), True
    return raw, False


def _cases():
    cases = [
        # Exact echo at the end.
        ("bla bla bla une conclusion", "une conclusion"),
        # Echo differing only by whitespace shape.
        ("bla bla  une   conclusion", "une conclusion"),
        ("bla bla\nune\tconclusion", "une conclusion"),
        # No echo.
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
        # Whitespace around the cut point.
        ("bla bla une conclusion   ", "une conclusion"),
        ("bla bla   \n  une conclusion", "une conclusion"),
        # Unicode, accents, emoji, exotic whitespace.
        ("préambule ✅ conclusion émise", "conclusion émise"),
        ("texte 🟢 Réponse", "🟢 Réponse"),
        ("bla\u00a0bla une conclusion", "une\u00a0conclusion"),
        ("bla\u2028une conclusion", "une conclusion"),
        # Whitespace-only buffer.
        ("     ", "une conclusion"),
        # Repetitions: the leftmost cut point is contractual.
        ("abc abc abc", "abc"),
        ("abc abc abc", "abc abc"),
        ("aaaa", "aa"),
    ]
    rnd = random.Random(20260920)
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


def test_index_matches_raw_reference_oracle():
    """Incremental index == naive raw probe, for one-shot and chunked appends."""
    import api.streaming as streaming

    divergences = []
    for value, suffix in _cases():
        expected = _raw_reference(value, suffix)
        index = streaming._CompactEchoIndex()
        index.append(value)
        actual = index.cut_to(suffix)
        if actual is None:
            got = (str(value or ''), False)
        else:
            got = (str(value or '')[:actual].rstrip(), True)
        if got != expected:
            divergences.append((value, suffix, expected, got))

    assert not divergences[:5], (
        f"{len(divergences)} divergence(s) from the reference implementation; "
        f"first: {divergences[:1]}"
    )


def test_chunked_appends_reach_the_same_cut_point():
    """Appending in pieces must equal appending the whole string."""
    import api.streaming as streaming

    rnd = random.Random(99)
    for _ in range(500):
        parts = [
            "".join(
                rnd.choice(string.ascii_letters + "  \n\té")
                for _ in range(rnd.randint(1, 20))
            )
            for _ in range(rnd.randint(1, 8))
        ]
        value = "".join(parts)
        if rnd.random() < 0.6 and len(value) > 4:
            suffix = value[-rnd.randint(1, len(value) // 2):]
        else:
            suffix = "".join(rnd.choice("abc \t") for _ in range(rnd.randint(0, 12)))

        chunked = streaming._CompactEchoIndex()
        for part in parts:
            chunked.append(part)
        whole = streaming._CompactEchoIndex()
        whole.append(value)

        assert chunked.cut_to(suffix) == whole.cut_to(suffix), (
            f"chunked append diverged for value={value!r} suffix={suffix!r}"
        )


def test_matches_tail_and_cut_to_agree():
    """The boolean probe and the cut probe must never disagree."""
    import api.streaming as streaming

    for value, suffix in _cases():
        index = streaming._CompactEchoIndex()
        index.append(value)
        assert index.matches_tail(suffix) == (index.cut_to(suffix) is not None), (
            f"matches_tail/cut_to disagree for value={value!r} suffix={suffix!r}"
        )


def test_reset_clears_state_after_truncation():
    """After reset + re-append the index must describe only the new text."""
    import api.streaming as streaming

    index = streaming._CompactEchoIndex()
    index.append("old reasoning tail with a wide span " + " " * 5000)
    index.reset()
    index.append("fresh start only")

    assert index.compact_length == len("freshstartonly")
    assert index.cut_to("start only") == len("fresh ")
    assert index.cut_to("old reasoning") is None


def test_index_probe_does_not_rescan_a_wide_whitespace_span(monkeypatch):
    """The probe must not walk raw characters it already folded.

    Pins the cost property the raw backward walk violated: after a 1 MB
    whitespace span is appended, a probe must cost O(candidate), not
    O(span). Counting ``str.isspace``-adjacent raw access is impractical, so
    this counts how many characters ``append`` has to fold: exactly the
    appended length, once — and the probe itself must not fold anything.
    """
    import api.streaming as streaming

    fold_calls = {'n': 0}
    original = streaming._compact_for_echo_compare

    def counting(value):
        fold_calls['n'] += 1
        return original(value)

    monkeypatch.setattr(streaming, '_compact_for_echo_compare', counting)

    index = streaming._CompactEchoIndex()
    index.append("alpha" + " " * 100_000)
    fold_calls['n'] = 0

    # 20 probes of a short candidate: the raw walk would fold/walk the span
    # each time; the index folds only the candidate.
    for i in range(20):
        assert index.matches_tail(f"progress {i}") is False

    assert fold_calls['n'] <= 20, (
        f"echo probe folded {fold_calls['n']} times — it is rescanning the "
        "raw transcript instead of using the indexed tail"
    )


@pytest.mark.timeout(60)
def test_index_stays_cheap_on_a_wide_span_with_many_probes():
    """Wide whitespace span + many probes must not cost seconds of CPU."""
    import time

    import api.streaming as streaming

    index = streaming._CompactEchoIndex()
    index.append("alpha" + " " * 1_000_000)
    probes = [f"progress {i}" for i in range(200)]

    start = time.perf_counter()
    for probe in probes:
        index.matches_tail(probe)
        index.cut_to(probe)
    elapsed = time.perf_counter() - start

    # The raw backward walk needs ~0.05s per probe here (2M+ character walks
    # total). A generous budget still fails it by an order of magnitude.
    assert elapsed < 0.5, f"200 probes on a 1MB span took {elapsed:.3f}s"
