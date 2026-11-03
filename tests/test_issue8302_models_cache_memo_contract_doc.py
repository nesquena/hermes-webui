"""Doc contract for the models-cache parse memo introduced by #8302.

`docs/architecture/models-cache-invalidation.md` is the runtime contract for the
`/api/models` cache layers. Before #8302 it documented exactly two: the in-memory
snapshot and the disk snapshot. #8302 added a THIRD layer -- the single-slot
`_models_disk_payload_memo` -- and that layer has its own file key, its own
consume-once ownership rule, and its own invalidation rule. An undocumented
cache layer is the failure mode this file exists to prevent: the next person to
touch cache invalidation has no written record that the memo survives a
`delete_disk=False` invalidation, or that two readers must never share its
payload.

These are documentation assertions, not behavior tests. The runtime invariants
(ownership, the same-size-rewrite window, invalidation) are covered in
`tests/test_issue8302_models_cache_memo_identity.py`.

Each assertion here names a symbol or phrase that MUST exist in the document,
and `test_documented_symbols_exist_in_config` re-checks the symbol names against
`api/config.py` so the contract cannot drift into documenting a rename that
never happened.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "architecture" / "models-cache-invalidation.md"
CONFIG = ROOT / "api" / "config.py"


def _contract_text() -> str:
    assert CONTRACT.exists(), "models cache invalidation contract must exist"
    return CONTRACT.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of a markdown section, up to the next same-level heading."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    except StopIteration:  # pragma: no cover - the assert below reports it
        assert heading in text, f"missing section heading: {heading!r}"
        raise
    body = []
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body)


def test_parse_memo_is_listed_as_a_cache_layer():
    """'What is cached' must name the memo, not just memory and disk."""
    cached = _section(_contract_text(), "## What is cached")
    assert "_models_disk_payload_memo" in cached, (
        "the parse memo is a third cache layer and must be listed under "
        f"'What is cached'; section was:\n{cached}"
    )


def test_parse_memo_file_key_is_documented():
    """The memo key must document the descriptor-derived stat identity.

    The key is taken from `os.fstat` of the descriptor `json.load` actually
    parsed -- NOT a separate pre-open `stat()` -- and includes st_ino/st_dev so
    a same-size, same-mtime `os.replace` rewrite is seen. Keying on
    (mtime_ns, size) alone is what the #8302 review caught, so the document has
    to name the descriptor source and the inode terms.
    """
    text = _contract_text()
    for term in ("os.fstat", "st_ino", "st_dev", "st_mtime_ns", "st_size"):
        assert term in text, f"the memo file key must document {term!r}"


def test_parse_memo_consume_once_ownership_is_documented():
    """The single-reader ownership rule must be written down, with its reason."""
    text = _contract_text()
    for term in ("consume-once", "exactly one reader", "_MODELS_DISK_PAYLOAD_UNSET"):
        assert term in text, f"the memo ownership rule must document {term!r}"

    flat = " ".join(text.split())
    assert "mutate" in flat, "the ownership rule must say callers mutate what they receive"


def test_parse_memo_generation_fence_is_documented():
    """The generation fence must be written down, with both of its sides.

    The stat identity says which bytes were parsed; it does not say which
    catalog generation they belonged to, because a rebuild is not guaranteed
    to change any part of that key. So the entry also carries the catalog
    generation, and the producer AND the consumer each refuse a superseded
    generation. Both sides are required: invalidation clears the slot before
    it advances the generation, so a producer can publish into that gap.
    """
    text = _contract_text()
    fence = _section(text, "### Generation fence")
    for term in (
        "_models_rebuild_seq",
        "generation",
        "producer",
        "consumer",
        "gap",
    ):
        assert term in fence, f"the generation fence must document {term!r}; section was:\n{fence}"


def test_parse_memo_invalidation_rules_are_documented():
    """Invalidation must state that the memo is a cache that gets dropped.

    The slot is keyed on stat identity, and a rebuild is NOT guaranteed to
    change any of it (same coarse-clock mtime tick, same size, a filesystem
    that reissues inodes). So `invalidate_models_cache()` has to clear the memo
    even when it keeps the disk snapshot.
    """
    invalidation = _section(_contract_text(), "## Invalidation paths: memory vs. disk")
    assert "_models_disk_payload_memo" in invalidation, (
        "invalidation must state the memo's own drop rule; section was:\n"
        f"{invalidation}"
    )


def test_parse_memo_lifetime_rule_is_documented():
    """The memo's lifetime must be stated: a visit may not leave it resident.

    A visit served from the in-memory cache never reaches the loader that
    would consume the precheck's parse, so it drops the slot instead. Without
    that, an abandoned entry keeps a whole parsed catalog resident and an
    unrelated later read can pick it up.
    """
    text = _contract_text()
    flat = " ".join(text.split())
    assert "never outlives a single session visit" in flat, (
        "the doc must state that the memo does not outlive a session visit"
    )
    assert "memory" in flat and "cache" in flat, (
        "the doc must name the memory-cache-hit case that abandons the parse"
    )


def test_documented_symbols_exist_in_config():
    """Every config symbol the contract names must still exist in api/config.py.

    Keeps the doc from documenting a rename that never happened, and keeps the
    rename honest in the other direction: if a symbol is renamed in config.py
    without updating this contract, the doc names a symbol that no longer
    exists and this test fails.
    """
    config_source = CONFIG.read_text(encoding="utf-8")
    for symbol in (
        "_models_disk_payload_memo",
        "_models_disk_payload_memo_lock",
        "_MODELS_DISK_PAYLOAD_UNSET",
        "invalidate_models_cache",
    ):
        assert symbol in config_source, (
            f"{symbol!r} is documented in the cache contract but does not exist "
            f"in api/config.py"
        )


def test_memo_reference_tests_are_indexed():
    """The Tests section must point at the memo's runtime invariant suite."""
    tests_section = _section(_contract_text(), "## Tests")
    assert "test_issue8302_models_cache_memo_identity.py" in tests_section, (
        "the contract must index the suite that proves the memo invariants; "
        f"section was:\n{tests_section}"
    )