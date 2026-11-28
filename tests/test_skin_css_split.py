"""Static/skins/ split contract: one file per skin, linked, scoped, and load-ordered.

Load order (mirrors ``static/index.html``):

    style.css  →  skins/<key>.css (any order)  →  scaffold.css

``scaffold.css`` carries the shared ``:root[data-skin]`` resets. They must load
*after* the skins: they used to sit after the skin block inside ``style.css``,
and moving them in front of the skins flips equal-specificity winners (dark Neon
worklog rows went from ``transparent`` to ``rgba(179,71,255,.1)``).
"""

import re
from pathlib import Path

from tests.skin_css import (
    BASE_CSS,
    SCAFFOLD_CSS,
    SKIN_DIR,
    read_skin_css,
    skin_keys,
)

REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
SW_JS = (REPO / "static" / "sw.js").read_text(encoding="utf-8")

SKIN_ATTR = re.compile(r'\[data-skin="([a-z0-9-]+)"\]')
ENTRY = re.compile(r"\{([^{}]*)\}")


def _registered_skin_entries():
    """[(name, key)] for every ``_SKINS`` entry, exactly as boot.js reads them.

    ``_VALID_SKINS`` is built from ``(s.value || s.name).toLowerCase()`` with no
    slugification, so a name is only slugified when no explicit value is given —
    and an explicit value always wins.
    """
    block = re.search(r"_SKINS=\s*\[(.*?)\];", BOOT_JS, re.S)
    assert block, "_SKINS not found in boot.js"
    out = []
    for body in ENTRY.findall(block.group(1)):
        name = re.search(r"name\s*:\s*'([^']*)'", body)
        value = re.search(r"value\s*:\s*'([^']*)'", body)
        assert name, f"_SKINS entry without a name: {body[:60]}"
        key = (value.group(1) if value else name.group(1)).lower()
        out.append((name.group(1), key))
    return out


def _registered_skin_keys():
    # `default` is the base palette in style.css (:root / :root.dark, no
    # data-skin attribute), so it intentionally has no file in static/skins/.
    return sorted({k for _, k in _registered_skin_entries() if k != "default"})


def _selector_branches(head):
    """Split a selector list, ignoring commas inside :is()/:not()/attribute values."""
    out, depth, cur = [], 0, []
    for ch in head:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [s for s in out if s]


def _rules(css):
    """(selector_head, body) for every top-level or @media-nested rule."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"([^{}@]+)\{([^{}]*)\}", css)


def test_every_registered_skin_has_a_stylesheet():
    missing = [k for k in _registered_skin_keys() if not (SKIN_DIR / f"{k}.css").is_file()]
    assert not missing, f"skins registered in boot.js but missing a file: {missing}"


def test_every_stylesheet_belongs_to_a_registered_skin():
    registered = set(_registered_skin_keys())
    orphans = [k for k in skin_keys() if k not in registered]
    assert not orphans, f"static/skins files not registered in boot.js: {orphans}"


def test_registered_keys_use_the_browser_fallback():
    """An explicit `value` wins; a bare `name` is lowercased, not slugified."""
    entries = dict(_registered_skin_entries())
    assert entries["Paper Graphite"] == "paper-graphite"
    assert entries["Ares"] == "ares"
    assert entries["Neon Soft"] == "neon-soft"
    # Guard the exact fallback expression boot.js uses.
    assert "(s.value||s.name).toLowerCase()" in BOOT_JS.replace(" ", "")


def test_every_stylesheet_is_linked_from_index_html():
    for k in skin_keys():
        href = f'href="static/skins/{k}.css'
        assert INDEX_HTML.count(href) == 1, f"{k}.css must be linked exactly once"


def test_style_css_loads_before_every_skin():
    base_at = INDEX_HTML.index('href="static/style.css')
    for k in skin_keys():
        assert INDEX_HTML.index(f'href="static/skins/{k}.css') > base_at, \
            f"{k}.css must load after style.css"


def test_scaffold_loads_after_every_skin():
    """The shared scaffold must come last, as it did after the block in style.css."""
    scaffold_at = INDEX_HTML.index('href="static/scaffold.css')
    for k in skin_keys():
        skin_at = INDEX_HTML.index(f'href="static/skins/{k}.css')
        assert skin_at < scaffold_at, f"scaffold.css must load after {k}.css"


def test_skin_files_are_scoped_to_their_own_key():
    """A skin file must never carry a selector branch for a different skin."""
    for k in skin_keys():
        css = read_skin_css(k)
        others = set()
        for head, _ in _rules(css):
            for branch in _selector_branches(head):
                others |= {m for m in SKIN_ATTR.findall(branch) if m != k}
        assert not others, f"{k}.css contains selectors for other skins: {sorted(others)}"


def test_each_skin_file_targets_only_skinned_selectors():
    """No unscoped selector *branch* may live in a skin file, or it leaks globally.

    Checked per comma-separated branch: ``:root[data-skin="ares"], body {…}``
    must fail even though the first branch carries the attribute.
    """
    for k in skin_keys():
        for head, _ in _rules(read_skin_css(k)):
            for branch in _selector_branches(head):
                assert f'[data-skin="{k}"]' in branch, \
                    f"{k}.css has an unscoped selector branch: {branch[:80]}"


def test_style_css_keeps_no_skin_palette():
    """The skins moved out; style.css must not still carry a skin block."""
    leftovers = SKIN_ATTR.findall(BASE_CSS)
    assert not leftovers, f"style.css still contains skin-scoped selectors: {sorted(set(leftovers))}"


def test_style_css_keeps_no_bare_scaffold():
    """The valueless scaffold moved to scaffold.css so it can load after the skins."""
    assert ":root[data-skin]" not in BASE_CSS, \
        "style.css still carries a valueless :root[data-skin] rule; it would load before the skins"


def test_scaffold_keeps_the_shared_resets():
    """The three reset blocks that must beat skin rules, in their original order."""
    order = [
        ":root[data-skin] .tool-worklog-list .tool-card,",
        ":root[data-skin] .tool-worklog-list > .agent-activity-thinking .thinking-card,",
        ":root[data-skin] .transparent-event-row .tool-card,",
    ]
    flat = re.sub(r"\s+", "", SCAFFOLD_CSS)
    positions = [flat.find(re.sub(r"\s+", "", sel)) for sel in order]
    assert all(p >= 0 for p in positions), f"scaffold reset missing: {positions}"
    assert positions == sorted(positions), "scaffold resets must keep their source order"
    # Key-agnostic by contract: no skin may be named here.
    assert not SKIN_ATTR.search(SCAFFOLD_CSS), "scaffold.css must not name a specific skin"


def test_service_worker_shell_covers_every_stylesheet():
    """Every stylesheet index.html loads must be in the SW shell, same spelling+query.

    Without this, the explicit ``SHELL_ASSETS.includes`` gate at sw.js skips both
    caching and the offline fallback, so an offline reload restores the page and
    the base CSS but loses the selected skin.
    """
    linked = re.findall(r'href="(static/[^"]*?\.css)\?v=__WEBUI_VERSION__"', INDEX_HTML)
    assert "static/style.css" in linked and "static/scaffold.css" in linked
    for path in linked:
        entry = f"'./{path}' + VQ,"
        assert entry in SW_JS, f"{path} is linked by index.html but missing from SHELL_ASSETS"
    # The version query must be the same token in both places.
    assert "'?v=__WEBUI_VERSION__'" in SW_JS
