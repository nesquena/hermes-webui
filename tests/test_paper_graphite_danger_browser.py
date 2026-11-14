"""Native-browser coverage: a destructive confirmation stays semantically red.

The skin styles ordinary confirmation buttons with the interaction accent
(``!important``). The batch-delete path passes ``danger:true`` and
``showConfirmDialog()`` adds ``.confirm.danger``, so the skin must also ship a
``.confirm.danger`` pair that outranks its own ``.confirm`` rule — otherwise the
red warning turns blue.

This is asserted in Chromium against the stylesheets that ``index.html``
actually links, in that order:

    static/style.css  →  static/skins/*.css (any order)  →  static/scaffold.css

Expected colours are read from the page's own resolved custom properties
(``--error`` / ``--accent`` / ``--accent-hover``) rather than hard-coded, so the
test follows the palette and cannot go stale against a token tweak.

Deliberately *not* a CSS-text assertion: removing only ``!important`` from the
two ``.confirm.danger`` declarations leaves every declaration in place and every
substring intact, while the rendered button turns blue. Only a computed-style
check catches that.
"""

import glob
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIN = "paper-graphite"

DANGER = "#danger-confirm"
CONFIRM = "#ordinary-confirm"
PLAIN = "#plain-button"

SCENE = """
<div class="app-dialog">
  <div class="app-dialog-btn confirm danger" id="danger-confirm">Delete</div>
  <div class="app-dialog-btn confirm" id="ordinary-confirm">Save</div>
  <div class="app-dialog-btn" id="plain-button">Cancel</div>
</div>
"""

PROBE = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el);
  return {bg: cs.backgroundColor, border: cs.borderTopColor, color: cs.color};
}"""

# Resolve a custom property to the rgb()/rgba() string the browser would paint.
VAR_RGB = """(name) => {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return null;
  const probe = document.createElement('div');
  probe.style.color = raw;
  document.body.appendChild(probe);
  const out = getComputedStyle(probe).color;
  probe.remove();
  return out;
}"""

# style.css transitions colour and background on nearly every element; sampling
# getComputedStyle mid-transition returns an interpolated value (e.g. rgb(9,8,6)
# on the way to rgb(26,22,16)).
NO_TRANSITIONS = "*,*::before,*::after{transition:none!important;animation:none!important;}"


def _layers():
    """The stylesheets index.html links, in load order."""
    paths = [ROOT / "static" / "style.css"]
    paths += [Path(p) for p in sorted(glob.glob(str(ROOT / "static" / "skins" / "*.css")))]
    paths.append(ROOT / "static" / "scaffold.css")
    return paths


def _load(page, dark):
    page.set_content(SCENE)
    for path in _layers():
        assert path.is_file(), f"missing stylesheet: {path}"
        page.add_style_tag(path=str(path))
    page.add_style_tag(content=NO_TRANSITIONS)
    page.evaluate(
        """(dark) => {
          document.documentElement.setAttribute('data-skin', %s);
          document.documentElement.className = dark ? 'dark' : '';
        }""" % repr(SKIN).replace("'", '"'),
        dark,
    )
    # Two frames so the (now transition-less) styles are committed.
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def _sample(page, selector, hover):
    page.mouse.move(0, 0)
    if hover:
        page.hover(selector)
    return page.evaluate(PROBE, selector)


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("hover", [False, True], ids=["normal", "hover"])
def test_danger_confirmation_renders_the_semantic_error_colour(page, dark, hover):
    _load(page, dark)
    danger = _sample(page, DANGER, hover)
    error = page.evaluate(VAR_RGB, "--error")

    assert danger is not None, "danger button not rendered"
    assert error is not None, "--error is not defined for this palette"
    assert danger["border"] == error, (
        f"{'dark' if dark else 'light'}/{'hover' if hover else 'normal'}: "
        f"danger border is {danger['border']}, expected --error {error}"
    )
    assert danger["color"] == error, (
        f"danger text is {danger['color']}, expected --error {error}"
    )


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("hover", [False, True], ids=["normal", "hover"])
def test_ordinary_confirmation_stays_on_the_interaction_accent(page, dark, hover):
    """The control: a non-destructive confirmation must keep the accent."""
    _load(page, dark)
    confirm = _sample(page, CONFIRM, hover)
    accent = page.evaluate(VAR_RGB, "--accent-hover" if hover else "--accent")

    assert confirm is not None, "ordinary confirm button not rendered"
    assert accent is not None, "--accent is not defined for this palette"
    assert confirm["border"] == accent, (
        f"{'dark' if dark else 'light'}/{'hover' if hover else 'normal'}: "
        f"ordinary confirm border is {confirm['border']}, expected {accent}"
    )


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("hover", [False, True], ids=["normal", "hover"])
def test_destructive_and_ordinary_confirmations_are_visually_distinct(page, dark, hover):
    """A danger button must never look like an ordinary confirmation."""
    _load(page, dark)
    danger = _sample(page, DANGER, hover)
    confirm = _sample(page, CONFIRM, hover)
    assert danger["border"] != confirm["border"]
    assert danger["bg"] != confirm["bg"]
    assert danger["color"] != confirm["color"]


def test_hover_really_changes_the_destructive_button(page):
    """Guards the probe itself: if hover stopped registering, the cases above
    would silently compare two resting states."""
    _load(page, False)
    resting = _sample(page, DANGER, False)
    hovered = _sample(page, DANGER, True)
    assert resting["bg"] != hovered["bg"], (
        "hover produced no background change on the danger button — the probe "
        f"is not exercising :hover (both {resting['bg']})"
    )


def test_skin_layer_is_linked_after_style_and_before_scaffold():
    """The browser only sees this order if index.html links it that way."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    base_at = html.index('href="static/style.css')
    skin_at = html.index(f'href="static/skins/{SKIN}.css')
    scaffold_at = html.index('href="static/scaffold.css')
    assert base_at < skin_at < scaffold_at
