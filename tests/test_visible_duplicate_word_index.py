"""The word index that narrows deduplication candidates must not change the result.

``_matching_visible_duplicate`` decides whether a row from state.db is a repeat
of what the WebUI already shows. It scanned every key of the same role, which is
quadratic: profiling a real transcript (14,779 messages) showed 5.67M candidates
walked and 97.5% of the time spent proving a duplicate does NOT exist (1327
probes, 905k candidates, 2.2s).

The word index narrows that list. It is purely a FILTER - every candidate it
returns goes through exactly the same comparisons as before. Hence the contract:
the result MUST be identical, and the only permitted change is fewer iterations.

The tests guard both properties at once, because either one alone can be
satisfied by a broken implementation: an index that returns everything is
correct but useless, and an index that returns nothing is fast and breaks
deduplication.

Two bugs from building this optimisation, pinned down by these tests:
1. Querying only the words of the text BEING CHECKED lost matches where the
   CANDIDATE was the shorter side ("1", "Start" against a long query) -
   containment is tested both ways, so the index must be bidirectional too.
2. Truncating the word set (the first version took the first 400) lost a match
   where the candidate's representative word sat further into the text.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.models as models


def _visible_key(role, content, sidecar=None):
    """A key in the shape used by _matching_visible_duplicate."""
    return (role, content, "", sidecar)


def _lookup(keys):
    return models._build_visible_duplicate_lookup(set(keys))


def _full_scan(visible_key, keys):
    """Result with the index DISABLED - the reference baseline."""
    stary = models._WORD_INDEX_MIN_CANDIDATES
    models._WORD_INDEX_MIN_CANDIDATES = 10 ** 9
    try:
        return models._matching_visible_duplicate(visible_key, set(keys), _lookup(keys))
    finally:
        models._WORD_INDEX_MIN_CANDIDATES = stary


def _z_indeksem(visible_key, keys):
    """Result with the index ENABLED from the first candidate."""
    stary = models._WORD_INDEX_MIN_CANDIDATES
    models._WORD_INDEX_MIN_CANDIDATES = 1
    try:
        return models._matching_visible_duplicate(visible_key, set(keys), _lookup(keys))
    finally:
        models._WORD_INDEX_MIN_CANDIDATES = stary


def _zawezone_i_wynik(visible_key, keys):
    """(result, how many candidates were actually inspected) with the index ENABLED.

    The number of walked candidates matters here, because without it the test
    also passes on code WITHOUT the index - and then it guards nothing. We count
    candidate-list reads by substituting a list that reports every iteration.
    """
    stary = models._WORD_INDEX_MIN_CANDIDATES
    models._WORD_INDEX_MIN_CANDIDATES = 1
    counter = {"n": 0}
    oryg_narrow = models._narrowed_visible_duplicate_candidates

    def licz(index, content):
        result = oryg_narrow(index, content)
        counter["n"] += len(result)
        return result

    models._narrowed_visible_duplicate_candidates = licz
    try:
        result = models._matching_visible_duplicate(visible_key, set(keys), _lookup(keys))
    finally:
        models._narrowed_visible_duplicate_candidates = oryg_narrow
        models._WORD_INDEX_MIN_CANDIDATES = stary
    return result, counter["n"]


def _assert_equivalence(visible_key, keys, *, require_narrowing=True):
    """Same result as the full scan AND the index genuinely narrowed the list.

    Both conditions together, because each alone is satisfied by a broken
    implementation: an index returning everything is correct but useless, while
    one returning nothing is fast and breaks deduplication.
    """
    expected = _full_scan(visible_key, keys)
    result, walked = _zawezone_i_wynik(visible_key, keys)
    assert result == expected, (
        f"the index changed the result: {result!r} instead of {expected!r}"
    )
    if require_narrowing:
        assert walked > 0, (
            "the index was not used - the test is not checking what it was meant to check"
        )
        assert walked < len(keys), (
            f"the index inspected {walked} of {len(keys)} candidates - no narrowing"
        )
    return expected


class TestIndexDoesNotChangeTheResult:
    """Every match shape must come out the same with and without the index."""

    def test_long_query_and_short_candidate_still_match(self):
        """Bug 1: containment also works when the candidate is shorter.

        The first version of the index queried only the words of the checked
        content and took candidates that had them. Candidate "Start" contains no
        rare word from the long query, so it dropped out of the list - and the
        match vanished. On real data, 37 of 187 matches were lost this way.
        """
        short_text = "Start"
        long_text = ("Start" + " installed a new service supervisor and "
                 "fixed the accessibility of the tabs in the settings panel " * 3)
        keys = [_visible_key("user", short_text)]
        keys += [_visible_key("user", f"completely different content number {i} "
                                      f"with many words to populate the index")
                 for i in range(40)]
        probe = _visible_key("user", long_text)

        assert _full_scan(probe, keys) is not None, "expected: the full scan matches"
        _assert_equivalence(probe, keys)

    def test_candidate_word_far_into_the_text_still_matches(self):
        """Bug 2: truncating the word set loses matches.

        The version with a 400-word limit lost a pair where the candidate's
        representative word (122 characters) appeared in the query (9653
        characters) only after the limit. Tokenisation must be full for
        everything that enters the index.
        """
        filler = " ".join(f"wyraz{i}" for i in range(1200))
        candidate = ("this ending is long enough to be admitted into the word index "
                    "instead of the always-checked key list")
        keys = [_visible_key("assistant", candidate)]
        keys += [_visible_key("assistant", f"another candidate {i} " + "text " * 20)
                 for i in range(40)]
        probe = _visible_key("assistant", filler + " " + candidate)

        assert _full_scan(probe, keys) is not None
        _assert_equivalence(probe, keys)

    def test_containment_mid_string_still_matches(self):
        """Matches are not prefix-only.

        In real sessions, 20 of 187 matches had the shorter text in the MIDDLE
        of the longer one, which is why the cheaper prefix index was rejected.
        """
        middle = ("a fragment that occurs in the middle of a longer turn of text "
                  "and has enough words to enter the index")
        keys = [_visible_key("assistant", middle)]
        keys += [_visible_key("assistant", f"filler {i} " + "word " * 25)
                 for i in range(40)]
        probe = _visible_key("assistant", "start of the turn " + middle + " and the ending")

        assert _full_scan(probe, keys) is not None
        _assert_equivalence(probe, keys)

    def test_short_texts_breaking_word_boundaries_still_match(self):
        """RAW containment does not respect word boundaries.

        "abc" is contained in "xabcy", but the word sets are disjoint - the
        index's necessary condition does not cover that. Therefore short texts
        must stay on the always-checked list, not in the index.
        """
        keys = [_visible_key("user", "abc")]
        keys += [_visible_key("user", f"filler numer {i} with a few words")
                 for i in range(40)]
        probe = _visible_key("user", "xabcy")

        assert _full_scan(probe, keys) is not None, "expected: the full scan catches this"
        result, walked = _zawezone_i_wynik(probe, keys)
        assert result == _full_scan(probe, keys), (
            "short contents must always be checked directly, not through the word index"
        )
        assert walked > 0, "the index was not used - the test guards nothing"

    def test_content_without_words_does_not_lose_matches(self):
        """Content made only of non-alphanumeric characters cannot be indexed."""
        keys = [_visible_key("assistant", "!!! ???")]
        keys += [_visible_key("assistant", f"candidate {i} " + "text " * 20)
                 for i in range(40)]
        probe = _visible_key("assistant", ">>> !!! ??? <<<")

        result, walked = _zawezone_i_wynik(probe, keys)
        assert result == _full_scan(probe, keys)
        assert walked > 0, "the index was not used - the test guards nothing"

    def test_no_duplicate_returns_none_in_both_modes(self):
        """The most common case (97.5% of the cost) must return the same result."""
        keys = [_visible_key("tool", f"tool result number {i} " + "data " * 30)
                for i in range(60)]
        probe = _visible_key("tool", "something entirely unrelated to anything above")

        assert _full_scan(probe, keys) is None
        result, walked = _zawezone_i_wynik(probe, keys)
        assert result is None
        assert walked < len(keys), (
            f"inspected {walked} of {len(keys)} - the index did not narrow "
            "the most common case (97.5% of the cost)"
        )

    def test_different_role_is_not_matched(self):
        """The index is per role - it must not leak across roles."""
        content = "identyczna content w dwoch roznych rolach " + "word " * 20
        keys = [_visible_key("assistant", content)]
        keys += [_visible_key("assistant", f"inne {i} " + "text " * 20) for i in range(40)]
        probe = _visible_key("user", content)

        _assert_equivalence(probe, keys, require_narrowing=False)

    def test_different_sidecar_is_not_matched(self):
        """The sidecar condition from the loop still applies after narrowing."""
        content = "ta sama content ale inny sidecar " + "word " * 20
        keys = [_visible_key("assistant", content, sidecar="A")]
        keys += [_visible_key("assistant", f"inne {i} " + "text " * 20) for i in range(40)]
        probe = _visible_key("assistant", content, sidecar="B")

        _assert_equivalence(probe, keys)


class TestIndexActuallyNarrows:
    """Without this, the index could be 'correct' by returning everything."""

    def test_index_filters_out_most_candidates(self):
        candidates = [_visible_key("tool", f"unique entry number {i} " +
                                          f"distinctive word{i} " + "data " * 25)
                     for i in range(400)]
        index = models._visible_duplicate_word_index(candidates)
        narrowed = models._narrowed_visible_duplicate_candidates(
            index, "completely different content with no words shared with the candidates")

        assert len(narrowed) < len(candidates) / 5, (
            f"the index returned {len(narrowed)} of {len(candidates)} candidates - "
            "narrowing is too weak, the performance gain would disappear"
        )

    def test_short_and_long_texts_are_always_checked(self):
        short_key = _visible_key("tool", "abc")
        long_key = _visible_key("tool", "x" * (models._WORD_INDEX_MAX_CHARS + 10))
        plain = _visible_key("tool", "plain candidate content " + "word " * 30)
        index = models._visible_duplicate_word_index([short_key, long_key, plain])

        assert short_key in index["always"], "short contents must always be checked"
        assert long_key in index["always"], "very long contents do not enter the index"
        assert plain not in index["always"], "regular content should go into the index"

    def test_query_without_words_gets_every_candidate(self):
        """Fail open: if we cannot narrow, we return everything."""
        candidates = [_visible_key("tool", f"candidate {i} " + "word " * 30)
                     for i in range(30)]
        index = models._visible_duplicate_word_index(candidates)
        narrowed = models._narrowed_visible_duplicate_candidates(index, "!!! ???")

        assert len(narrowed) >= len(candidates), (
            "a query without words must not narrow the candidate list"
        )


class TestIndexActivationThreshold:
    def test_small_roles_stay_on_the_old_path(self):
        """Below the threshold the index is not worth it and is absent from the lookup."""
        keys = [_visible_key("user", f"content {i} " + "word " * 20)
                for i in range(models._WORD_INDEX_MIN_CANDIDATES - 1)]
        lookup = _lookup(keys)
        models._matching_visible_duplicate(
            _visible_key("user", "cos nowego bez duplikatu"), set(keys), lookup)

        assert "word_indexes" not in lookup, (
            "with a small candidate count, the index should not be built"
        )

    def test_large_roles_build_the_index_once(self):
        """The index should be built once per role, not on every query."""
        keys = [_visible_key("tool", f"content {i} " + f"slowo{i} " * 20)
                for i in range(models._WORD_INDEX_MIN_CANDIDATES + 50)]
        lookup = _lookup(keys)
        calls = {"n": 0}
        oryg = models._visible_duplicate_word_index

        def licz(candidates):
            calls["n"] += 1
            return oryg(candidates)

        models._visible_duplicate_word_index = licz
        try:
            for i in range(5):
                models._matching_visible_duplicate(
                    _visible_key("tool", f"nowa content {i} bez duplikatu"),
                    set(keys), lookup)
        finally:
            models._visible_duplicate_word_index = oryg

        assert calls["n"] == 1, f"the index was built {calls['n']} times, it should be built once"


class TestStableConfiguration:
    def test_thresholds_have_sane_values(self):
        assert models._WORD_INDEX_MIN_CHARS >= 8, (
            "a length threshold set too low would let texts into the index for which "
            "tokenisation does not reflect raw containment"
        )
        assert models._WORD_INDEX_MAX_CHARS >= 1000
        assert models._WORD_INDEX_MIN_CANDIDATES >= 50, (
            "an index-enable threshold set too low slows small conversations with index-build overhead"
        )
        assert isinstance(models._WORD_INDEX_TOKEN.pattern, str)
