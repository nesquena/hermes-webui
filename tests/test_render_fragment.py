"""Task 7: renderMessages must batch DOM mounts via DocumentFragment.

Repeated `inner.appendChild(row)` inside the message-build loop forces
the browser to recompute layout / style on every iteration.  For long
sessions this dominates the cost of session-switch.  Batching all
appends into a detached DocumentFragment and committing once with
`inner.replaceChildren(frag)` collapses N reflows into 1.

These tests grep the actual ui.js source so a future refactor can't
silently regress to the per-iteration inner.appendChild pattern.
"""
from pathlib import Path

UI_JS = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")


def _slice_function(name: str) -> str:
    needle = f"function {name}("
    start = UI_JS.find(needle)
    assert start != -1
    i = UI_JS.index("{", start) + 1
    depth = 1
    while depth and i < len(UI_JS):
        c = UI_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return UI_JS[start:i]


class TestRenderMessagesFragment:
    def test_uses_createDocumentFragment(self):
        body = _slice_function("renderMessages")
        assert "createDocumentFragment" in body, (
            "renderMessages must build into a DocumentFragment to avoid "
            "per-iteration layout/style recompute (Task 7)."
        )

    def test_does_not_appendChild_to_inner_inside_main_loop(self):
        """The main message-build loop used to do `inner.appendChild(row)`
        on every iteration.  After Task 7 the only `inner.X` calls in
        renderMessages are post-loop (replaceChildren mount + querySelectorAll
        cleanup for tool cards / token usage).  This test fences that.
        """
        body = _slice_function("renderMessages")
        # `inner.appendChild(` calls inside renderMessages are forbidden —
        # the build path uses `_frag.appendChild()` and a single mount.
        # Tool-card insertion (post-mount) uses anchorParent.insertBefore
        # which is fine.
        assert "inner.appendChild" not in body, (
            "Found inner.appendChild() inside renderMessages — Task 7 "
            "requires building into a DocumentFragment and mounting once."
        )

    def test_mounts_via_replaceChildren_or_appendChild_of_fragment(self):
        body = _slice_function("renderMessages")
        # The atomic commit must use either `inner.replaceChildren(frag)` or
        # `inner.appendChild(frag)`.  We chose replaceChildren because it
        # also clears any stale children in the same call.
        assert (
            "inner.replaceChildren" in body
        ), "Expected inner.replaceChildren(_frag) as the atomic mount point."
