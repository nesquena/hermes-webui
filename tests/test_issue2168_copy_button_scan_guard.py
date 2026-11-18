"""Tests for #2168 — session-switch post-render scan reduction.

#2168 tracks measured session-switch latency. #2166/#2170/#2171 removed
duplicate *calls* to the transcript post-render helpers, and every helper in
postProcessRenderedMessages() now carries a selector-level idempotence marker
(``:not([data-...])``) so a repeat pass matches zero nodes and returns early.

addCopyButtons() was the one remaining exception: it selects ``pre > code``
unfiltered, so every post-render pass re-walked every code block in the
transcript and then ran a nested querySelector() per block to filter out the
ones that already had a button. On a long session that is O(codelocks) node
visits on the *cached* path too, where the switch has nothing left to add.

The invariant asserted here is deterministic (node-match count), not wall-clock:
after the first pass, a repeat pass must select zero code blocks.
"""
import re


def _ui_src() -> str:
    with open("static/ui.js", encoding="utf-8") as f:
        return f.read()


def _fn(name: str) -> str:
    """Return the full source of a top-level function, brace-matched.

    A fixed character window silently truncates once the function grows, which
    turns unrelated assertion failures into false alarms.
    """
    src = _ui_src()
    m = re.search(rf"function {re.escape(name)}\b", src)
    assert m, f"{name}() must exist in static/ui.js"
    start = src.index("{", m.start())
    depth = 0
    for k in range(start, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():k + 1]
    raise AssertionError(f"{name}() has unbalanced braces")


class TestCopyButtonSelectorGuard:
    """addCopyButtons must skip processed blocks in the selector, not in JS."""

    def test_selects_only_unprocessed_code_blocks(self):
        """The selector must exclude already-processed blocks itself."""
        fn = _fn("addCopyButtons")
        m = re.search(r"querySelectorAll\(\s*'([^']*)'", fn)
        assert m, "addCopyButtons must use a querySelectorAll selector"
        selector = m.group(1)
        assert ":not(" in selector and "data-" in selector, (
            "addCopyButtons selector must exclude processed blocks at the "
            f"selector level (expected a :not([data-...]) guard), got: {selector!r}"
        )
        assert selector.startswith("pre > code"), (
            "selector must stay scoped to pre > code, got: " f"{selector!r}"
        )

    def test_selector_excludes_exactly_the_marker_that_is_written(self):
        """The excluded attribute and the written marker must be the SAME one.

        Both halves were previously asserted in isolation: the selector only had
        to contain *some* ``data-`` attribute and the stamp only had to be *some*
        ``dataset`` assignment. Renaming one side — selector
        ``:not([data-copy-button])`` while still writing ``dataset.copyBtn`` —
        satisfies every other assertion in this file, yet no block is ever
        excluded, so the repeat pass keeps matching the whole transcript and
        the O(code blocks) walk this PR removes comes straight back.

        ``dataset.copyBtn`` is what the DOM exposes as ``data-copy-btn``, so the
        two spellings have to be reconciled rather than eyeballed.
        """
        fn = _fn("addCopyButtons")
        m = re.search(r"querySelectorAll\(\s*'([^']*)'", fn)
        assert m, "addCopyButtons must use a querySelectorAll selector"
        selector = m.group(1)

        excluded = re.findall(r":not\(\[([^\]]+)\]\)", selector)
        assert excluded, f"selector must exclude the processed marker, got: {selector!r}"

        written = {m.group(1) for m in re.finditer(r"codeEl\.dataset\.(\w+)\s*=", fn)}
        assert written, "addCopyButtons must stamp a dataset marker"

        def _as_attr(dataset_key: str) -> str:
            # dataset.camelCase -> data-kebab-case, matching DOMTokenList.
            return "data-" + re.sub(r"(?<!^)(?=[A-Z])", "-", dataset_key).lower()

        written_attrs = {_as_attr(key) for key in written}
        assert set(excluded) == written_attrs, (
            "the selector must exclude exactly the attribute addCopyButtons "
            f"writes; selector excludes {excluded} but the function writes "
            f"{sorted(written_attrs)}"
        )

    def test_selector_uses_the_canonical_marker_attribute(self):
        """Pin the literal selector so a rename is a deliberate, visible change."""
        fn = _fn("addCopyButtons")
        m = re.search(r"querySelectorAll\(\s*'([^']*)'", fn)
        assert m, "addCopyButtons must use a querySelectorAll selector"
        selector = m.group(1)
        assert selector == "pre > code:not([data-copy-btn])", (
            "addCopyButtons selector changed unexpectedly; if the marker is "
            "being renamed, update this test in the same commit so the two can "
            f"never drift apart. Got: {selector!r}"
        )

    def test_marks_processed_code_blocks(self):
        """Processed blocks must be marked so the selector can skip them."""
        fn = _fn("addCopyButtons")
        assert re.search(r"codeEl\.dataset\.\w+\s*=", fn), (
            "addCopyButtons must stamp a data-* marker on the processed code "
            "element so later passes can skip it in the selector"
        )

    def test_marker_is_set_on_both_button_placements(self):
        """Header and in-<pre> placements must both mark the block."""
        fn = _fn("addCopyButtons")
        pre_append = fn.index("pre.appendChild(btn)")
        header_append = fn.index("header.appendChild(btn)")
        last_marker = max(m.start() for m in
                          re.finditer(r"codeEl\.dataset\.(\w+)\s*=", fn))
        assert last_marker > pre_append, "marker must be set after the in-<pre> append"
        assert last_marker > header_append, "marker must be set after the header append"

    def test_backstop_branch_also_marks_for_convergence(self):
        """The already-has-a-button branch must stamp too, or never converge.

        A block whose button already exists (cached HTML written by a build
        predating the marker) takes the early return. If that branch did not
        stamp, the selector would keep matching that block on every later
        post-render pass and a repeat pass would never reach zero matches.
        """
        fn = _fn("addCopyButtons")
        guard_at = fn.index("if(pre.querySelector(")
        return_at = fn.index("return;", guard_at)
        branch = fn[guard_at:return_at]
        assert re.search(r"codeEl\.dataset\.\w+\s*=\s*'[^']*'", branch), (
            "the already-has-a-button branch must stamp the marker so legacy "
            "blocks converge to zero matches on the next pass"
        )

    def test_keeps_js_duplicate_guard_for_header_blocks(self):
        """The JS-level guard stays as the correctness backstop (#1096).

        The marker is a fast path; the nested querySelector guard must remain so
        a block whose button lives in the sibling .pre-header is still detected
        as processed when the marker is absent (e.g. HTML swapped in by a
        cached-transcript path that predates the marker).
        """
        fn = _fn("addCopyButtons")
        assert "const header=pre.previousElementSibling;" in fn
        assert "header.querySelector('.code-copy-btn')" in fn
        assert fn.index("header.querySelector('.code-copy-btn')") < fn.index(
            "document.createElement('button')"
        )


class TestPostRenderHelpersRemainGuarded:
    """Guards must stay on every helper so a repeat pass matches nothing."""

    _GUARDED = [
        ("highlightCode", "data-highlighted"),
        ("loadDiffInline", "data-loaded"),
        ("loadCsvInline", "data-loaded"),
        ("loadExcalidrawInline", "data-loaded"),
        ("loadPdfInline", "data-loaded"),
        ("loadHtmlInline", "data-loaded"),
        ("renderMermaidBlocks", "data-rendered"),
        ("renderKatexBlocks", "data-rendered"),
        ("initTreeViews", "data-tree-init"),
    ]

    def test_every_helper_has_a_selector_level_guard(self):
        for name, attr in self._GUARDED:
            fn = _fn(name)
            assert ":not(" in fn, f"{name}() lost its selector-level :not() guard"
            assert attr in fn, f"{name}() must keep its {attr} idempotence marker"

    def test_addCopyButtons_is_now_guarded_like_the_rest(self):
        fn = _fn("addCopyButtons")
        assert ":not(" in fn, (
            "addCopyButtons must carry a selector-level guard like every other "
            "postProcessRenderedMessages helper (#2168)"
        )