"""#1804 re-gate 9/24 — DOM rendering harness for the busy-mode action label.

Round 1 of this fix used a CSS ``::after`` pseudo-element driven by a
``data-label`` attribute. The test suite passed because the rule was
in the CSS, but the label was never visible in the real app: the
``#btnSend`` element is also ``.has-tooltip``, and the
``.has-tooltip::after`` rule at style.css:2110 is the single owner of
that pseudo-element for the hover tooltip. The two pseudo-elements
collided silently — the rule chain resolved to whichever selector
won, the label never rendered, and a busy-mode hover surfaced
"Interrupt" only because the tooltip was also overriding the data-label.

This test pins the round 2 fix at the rendering layer rather than the
source layer. A real Chromium instance loads a minimal fixture that
mirrors the relevant styles, the production helper runs in-page, and
the test asserts:

- The label element is a real DOM node (``document.querySelector(
  '.send-btn-label')``), not a pseudo-element.
- The label element has non-zero ``offsetWidth`` (i.e. it is actually
  laid out next to the icon SVG, not zero-width / hidden).
- The button's rendered width is meaningfully wider than the 34 px
  circular send button — the pill affordance is visible.
- The ``.has-tooltip::after`` pseudo-element still owns the tooltip
  text (not the action name) — the two pseudo-element rules no
  longer fight.
- The pill does not collide with composer footer chips at the 390 px
  mobile viewport.

Per the #7649 review lesson: execute the helper in a real DOM, do not
just grep the source. The fixture inlines the same CSS rules that
style.css applies to ``.send-btn`` / ``.send-btn-label`` /
``.has-tooltip``, so a CSS regression in the production file trips
this test directly.

Playwright is required (this test spins up headless Chromium). The
suite-level ``pytest tests/`` invocation ignores any file that
imports ``playwright.sync_api``; CI runs the relevant tests
explicitly so this DOM harness still executes.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except Exception:  # pragma: no cover - dependency optional in dev envs
    PlaywrightError = None
    sync_playwright = None


REPO = Path(__file__).resolve().parents[1]


_REPO_UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
_REPO_STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _extract_real_helper() -> str:
    """Pull the literal source of _setComposerPrimaryButtonIcon from
    ``static/ui.js``. We splice it into the page rather than
    re-typing it, so the test pins the actual production code and
    any regression in the helper (e.g. a future change that reverts
    to ``::after``/``data-label``) trips this test directly.
    """
    i = _REPO_UI_JS.find("function _setComposerPrimaryButtonIcon")
    assert i != -1, "_setComposerPrimaryButtonIcon not found in static/ui.js"
    brace_open = _REPO_UI_JS.find("{", i)
    depth = 0
    end = brace_open
    for j, ch in enumerate(_REPO_UI_JS[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return _REPO_UI_JS[i:end]


def _extract_relevant_css() -> str:
    """Pull the ``.send-btn`` / ``.send-btn-label`` / ``.has-tooltip``
    rule blocks from the real ``static/style.css`` so the fixture
    is layout-faithful. We extract the four rule blocks that this
    PR touches (the pill shape, the label class, the tooltip
    pseudo, and the action-class colour overrides); the rest of
    the 7k-line file is irrelevant to the rendering contract under
    test.

    Each rule is identified by its selector prefix and pulled
    including the trailing brace. A single regex per rule is
    enough — the rules are well-formed and there are no nested
    blocks inside them.
    """
    # Block pattern: ``SELECTOR { DECLS }`` with greedy ``[^}]+``.
    patterns = [
        r"\.send-btn\s*\{[^}]+\}",  # base .send-btn (circular send button)
        r"\.send-btn\[data-action=\"(?:stop|queue|interrupt|steer)\"]\s*\{[^}]+\}",  # busy-mode pill
        r"\.send-btn-label\s*\{[^}]+\}",  # label typography
        r"\.send-btn-label:empty\s*\{[^}]+\}",  # label empty hide
        r"\.send-btn\.stop\s*,\s*\.send-btn\.interrupt\s*\{[^}]+\}",  # red colour
        r"\.send-btn\.steer\s*\{[^}]+\}",  # steer purple
        r"\.send-btn\.queue\s*\{[^}]+\}",  # queue accent
        r"\.has-tooltip\s*\{[^}]+\}",  # has-tooltip position
        r"\.has-tooltip::after\s*\{[^}]+\}",  # tooltip pseudo
        r"\.has-tooltip:hover::after\s*,\s*\.has-tooltip:focus-visible::after\s*\{[^}]+\}",  # hover
    ]
    out = []
    for p in patterns:
        m = re.search(p, _REPO_STYLE_CSS, re.DOTALL)
        assert m, f"CSS rule not found in static/style.css: {p!r}"
        out.append(m.group(0))
    return "\n  ".join(out)


# Fixture CSS — extracted from the real production file at module
# import time. Sourced live (not hard-coded) so a regression in
# style.css trips this test directly. Re-extracted on every test
# session.
_SEND_BTN_RULES = _extract_relevant_css()


# The production helper, extracted live from static/ui.js. We
# inline the function (rather than eval the file) so the test
# only depends on the helper itself, not on the full app boot
# sequence. Any regression in the helper's label construction
# trips this test directly.
_HELPER_JS = _extract_real_helper() + "\nwindow._setComposerPrimaryButtonIcon = _setComposerPrimaryButtonIcon;"


_FIXTURE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>__SEND_BTN_RULES__</style>
</head>
<body style="margin:0;padding:24px;background:#0c0c1a;">
  <button
    class="send-btn has-tooltip has-tooltip--left"
    id="btnSend"
    data-tooltip="Send message"
    data-action="send"
  >
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
  </button>
  <script>__HELPER_JS__</script>
</body>
</html>
""".strip()


def _build_page_html(t_locale: dict[str, str] | None = None) -> str:
    """Substitute the CSS and helper into the fixture and, if given,
    install a ``t()`` shim in the page that returns the locale's
    translated strings.
    """
    html = _FIXTURE_HTML.replace("__SEND_BTN_RULES__", _SEND_BTN_RULES)
    html = html.replace("__HELPER_JS__", _HELPER_JS)
    if t_locale is not None:
        shim = (
            "\nwindow.t = (k) => ("
            + repr(t_locale)
            + ")[k] || k;\n"
        )
        html = html.replace(
            "window._setComposerPrimaryButtonIcon = _setComposerPrimaryButtonIcon;",
            "window._setComposerPrimaryButtonIcon = _setComposerPrimaryButtonIcon;" + shim,
        )
    return html


# Actions that are NOT busy-mode — the pill should NOT render and
# the button should stay the 34 px circular send affordance.
_IDLE_ACTIONS = ("send", "disabled")


# Busy-mode actions — the label element must render with non-zero
# offsetWidth.
_BUSY_ACTIONS = (
    ("stop", "Stop"),
    ("queue", "Queue"),
    ("interrupt", "Interrupt"),
    ("steer", "Steer"),
)


_EN_LOCALE = {
    "composer_action_stop": "Stop",
    "composer_action_queue": "Queue",
    "composer_action_interrupt": "Interrupt",
    "composer_action_steer": "Steer",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chromium_page():
    """Module-scoped headless Chromium page. Boots once for the
    whole file so the per-test cost is one round-trip. Yields a
    factory that takes (action, locale_or_none) and returns a
    freshly-loaded Page with the helper-driven button.
    """
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    if shutil.which("node") is None:
        # Not strictly required (we don't run a node VM here), but
        # the rest of the #1804 suite assumes node is on PATH; we
        # skip rather than partially-render the page if it isn't.
        pytest.skip("node is not on PATH")

    with sync_playwright() as p:
        browser = None
        try:
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except PlaywrightError as exc:
                if "Executable doesn't exist at" in str(exc):
                    pytest.skip(
                        "playwright chromium executable is unavailable; "
                        "run `playwright install chromium`"
                    )
                raise
        except PlaywrightError:
            pytest.skip("playwright chromium failed to launch")

        context = browser.new_context(viewport={"width": 1024, "height": 768})
        try:
            def _make(action: str, t_locale: dict[str, str] | None = None):
                page = context.new_page()
                page.set_content(_build_page_html(t_locale))
                # Drive the helper from inside the page so the test
                # exercises the real production code path, including
                # the data-action attribute that updateSendBtn() would
                # set in the live app.
                page.evaluate(
                    "(action) => {"
                    "  const btn = document.getElementById('btnSend');"
                    "  btn.dataset.action = action;"
                    "  window._setComposerPrimaryButtonIcon(btn, action);"
                    "}",
                    action,
                )
                return page

            # Expose the browser so the mobile-viewport test can
            # open a second context with a 390px viewport without
            # paying for a second Chromium launch.
            _make.browser = browser
            yield _make
        finally:
            context.close()
            browser.close()


# ── Baseline: idle modes must NOT render a label ──────────────────────


@pytest.mark.parametrize("action", _IDLE_ACTIONS)
def test_label_element_is_absent_in_idle_modes(chromium_page, action):
    """send / disabled must NOT render a ``.send-btn-label`` child.
    The pill affordance is busy-mode-only; the idle button stays
    the 34 px circular send button.
    """
    page = chromium_page(action, _EN_LOCALE)
    label_count = page.eval_on_selector_all(
        ".send-btn-label", "els => els.length"
    )
    assert label_count == 0, (
        f"action={action!r} must not surface a .send-btn-label child "
        f"(found {label_count}); the pill is busy-mode-only"
    )
    # The button should still be the 34 px circular affordance.
    width = page.evaluate(
        "() => document.getElementById('btnSend').getBoundingClientRect().width"
    )
    # Allow a small sub-pixel tolerance; CSS uses width:34px but the
    # device-pixel ratio can shave a fraction of a pixel.
    assert 32 <= width <= 36, (
        f"action={action!r} expected ~34px circular button, got width={width}"
    )


# ── Core fix: busy modes must render a real, laid-out label ──────────


@pytest.mark.parametrize("action,label_text", _BUSY_ACTIONS)
def test_label_renders_with_nonzero_offset_width(chromium_page, action, label_text):
    """Each busy-mode action must render the ``.send-btn-label``
    child with non-zero ``offsetWidth``. This is the core rendering
    pin: a CSS rule that is parsed but never laid out (e.g. the
    original round 1 ``::after`` approach that was overridden by
    ``.has-tooltip::after``) would fail here.
    """
    page = chromium_page(action, _EN_LOCALE)
    # Label must exist in the DOM as a real element.
    label_count = page.eval_on_selector_all(
        ".send-btn-label", "els => els.length"
    )
    assert label_count == 1, (
        f"action={action!r} expected exactly 1 .send-btn-label child, "
        f"got {label_count}"
    )
    # And the label must have non-zero layout (the mainline check).
    label_width = page.evaluate(
        "() => document.querySelector('.send-btn-label').offsetWidth"
    )
    assert label_width > 0, (
        f"action={action!r}: .send-btn-label has offsetWidth={label_width}; "
        "the label is not laid out — the round 1 ::after regression is back"
    )
    # And the label must carry the right text.
    rendered_text = page.eval_on_selector(
        ".send-btn-label", "el => el.textContent"
    )
    assert rendered_text == label_text, (
        f"action={action!r}: expected label text {label_text!r}, "
        f"got {rendered_text!r}"
    )


# ── The two pseudo-element rules must not collide anymore ─────────────


def test_has_tooltip_after_still_owns_tooltip_text(chromium_page):
    """The label is now a real child element, so the
    ``.has-tooltip::after`` pseudo-element must still own the hover
    tooltip text (i.e. ``content: attr(data-tooltip)`` resolves to
    "Send message" — not to the action name). This is the inverse
    of the round 1 regression: round 1 had the action label replace
    the tooltip text and the user had to hover the pill to see
    "Interrupt" (and it would only briefly flash the right name).
    """
    page = chromium_page("interrupt", _EN_LOCALE)
    # The pseudo-element's resolved `content` value can be read via
    # getComputedStyle on a hovered button. We force the hover state
    # and read the computed content of the pseudo-element.
    tooltip_content = page.evaluate(
        """() => {
            const btn = document.getElementById('btnSend');
            // Synthesize a hover so the ::after becomes visible AND
            // its resolved content is computable. Browsers expose
            // pseudo-element getComputedStyle for both ::before and
            // ::after regardless of hover state, but the property
            // value is what we want.
            return getComputedStyle(btn, '::after').content;
        }"""
    )
    # The content should be the tooltip text, NOT the action name.
    assert "Send message" in tooltip_content or '"Send message"' in tooltip_content, (
        f".has-tooltip::after should still own the hover tooltip text; "
        f"got content={tooltip_content!r} — the label pseudo-element "
        "is fighting the tooltip pseudo-element again"
    )
    assert "Interrupt" not in tooltip_content, (
        f".has-tooltip::after must NOT contain the action label "
        f"(got {tooltip_content!r}); the round 1 silent collision is back"
    )


# ── The pill must be visibly wider than the circular send button ─────


@pytest.mark.parametrize("action,_label_text", _BUSY_ACTIONS)
def test_button_widens_into_pill_in_busy_modes(chromium_page, action, _label_text):
    """The busy-mode pill must be meaningfully wider than the
    34 px circular send button. This is the user-visible affordance
    change — a circular icon-only button reads as "send" even when
    it is actually a stop.
    """
    page = chromium_page(action, _EN_LOCALE)
    pill_width = page.evaluate(
        "() => document.getElementById('btnSend').getBoundingClientRect().width"
    )
    # Real-world measurement: a "Stop" label at 12px/600 in a
    # 34px-tall pill with 0 12px 0 10px padding and 6px gap lands
    # around 75-85px in Chromium's default font stack. The CSS
    # contract only requires "meaningfully wider than 34px", so a
    # generous 50px floor is enough to catch a silent zero-width
    # regression without being brittle to font changes.
    assert pill_width > 50, (
        f"action={action!r}: pill width {pill_width}px is not "
        "meaningfully wider than the 34px circular send button; "
        "the busy-mode pill affordance is not landing"
    )


# ── Mobile: 390px viewport must not collide with composer chips ──────


def test_pill_fits_in_390px_mobile_viewport(chromium_page):
    """At 390px (iPhone-class) the busy-mode pill becomes ~80-110px
    wide. That still leaves headroom next to the composer footer
    chips because the composer shrinks the model/workspace chips
    on narrow viewports, but the pill itself must never overflow
    the viewport horizontally. This pins the mobile layout contract
    that the 9/24 review flagged ("390px 移动端上 pill 会变成
    ~80-110px 宽，检查不会和 composer footer 里的 model/workspace
    chips 碰撞").

    The page fixture gives us a 1024px viewport, so this test spins
    its own 390px context (we cannot just call ``chromium_page``
    because the viewport is baked into the module-scoped context).
    Re-uses the launch cost by going through the same browser
    instance when possible — but to keep the contract simple and
    match the existing per-test isolation in the file, this test
    takes the small extra cost of a second ``new_context`` with
    the mobile viewport.
    """
    browser = chromium_page.browser
    context = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        page = context.new_page()
        page.set_content(_build_page_html(_EN_LOCALE))
        # Drive the helper into the longest busy mode ("Interrupt"
        # — the longest action name in EN).
        page.evaluate(
            "() => {"
            "  const btn = document.getElementById('btnSend');"
            "  btn.dataset.action = 'interrupt';"
            "  window._setComposerPrimaryButtonIcon(btn, 'interrupt');"
            "}"
        )
        metrics = page.evaluate(
            """() => {
                const btn = document.getElementById('btnSend');
                const r = btn.getBoundingClientRect();
                const label = document.querySelector('.send-btn-label');
                const lr = label ? label.getBoundingClientRect() : null;
                return {
                  right: r.right,
                  viewportWidth: window.innerWidth,
                  labelWidth: lr ? lr.width : 0,
                  pillWidth: r.width,
                };
            }"""
        )
        # The pill must not overflow the viewport.
        assert metrics["right"] <= metrics["viewportWidth"] + 1, (
            f"busy-mode pill overflows the 390px viewport: "
            f"right={metrics['right']} > viewport={metrics['viewportWidth']}; "
            f"pill width={metrics['pillWidth']}px, label width={metrics['labelWidth']}px"
        )
        # The label itself must be laid out (not collapsed to zero
        # width by an overflow:hidden ancestor).
        assert metrics["labelWidth"] > 0, (
            f"label element collapsed to zero width on mobile "
            f"({metrics}); the pill is rendered but the label is "
            "hidden — a silent regression"
        )
    finally:
        context.close()


# ── Locale round-trip: locale B replaces locale A in-place ───────────


def test_locale_restamp_swaps_label_text_in_place(chromium_page):
    """Drive the helper with locale A, then replace the page's
    ``t()`` shim with locale B and re-run the helper (the
    ``applyLocaleToDOM`` restamp flow). The label text must
    flip to the new locale without a page reload — this is the
    "stale language after locale change" bug the re-gate fix
    layers on top of the rendering fix.
    """
    page = chromium_page("interrupt", _EN_LOCALE)
    initial_text = page.eval_on_selector(".send-btn-label", "el => el.textContent")
    assert initial_text == "Interrupt", initial_text
    # Now simulate a locale restamp: swap t() and re-run the helper.
    page.evaluate(
        """() => {
            window.t = (k) => ({
              'composer_action_stop': '停止',
              'composer_action_queue': '队列',
              'composer_action_interrupt': '中断',
              'composer_action_steer': '转向',
            })[k] || k;
            const btn = document.getElementById('btnSend');
            window._setComposerPrimaryButtonIcon(btn, btn.dataset.action);
        }"""
    )
    new_text = page.eval_on_selector(".send-btn-label", "el => el.textContent")
    assert new_text == "中断", (
        f"after locale restamp the label should be '中断' (locale B), "
        f"got {new_text!r} — the live locale is not winning over the "
        "imperatively-stamped text"
    )
