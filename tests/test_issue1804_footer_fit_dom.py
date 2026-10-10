"""#1804 re-gate 10-08 — footer-fit DOM sweep for the busy-mode pill.

The 09-24 rounds proved the label *renders* (a real ``<span>`` child,
non-zero offset width). The 10-06 review found the remaining
half of the contract was never exercised in a real DOM: the
**footer** geometry. ``_fitComposerFooter()`` (static/ui.js)
collapses ``.composer-footer`` through three stages —

- ``(none)``  full labels
- ``.cf-icons``  icon chips
- ``.cf-icons.cf-burger``  hamburger

— by probing whether ``.composer-left`` overflows. The busy-mode
pill widens ``#btnSend`` from 34 px to 74-108 px. Because the
button is a *sibling* of ``.composer-left`` inside
``.composer-footer`` (``.composer-right`` wraps it, see
static/index.html:699-727), a wider button steals horizontal room
from ``.composer-left`` and can push it into an earlier stage.

Two failure modes the review reproduced in Chromium, both
invisible to a source-level test:

1. **Mid-transition measurement.** While ``width`` was a
   transitioned property on ``.send-btn`` (fixed in this round),
   an idle pass running during the pill's 150 ms grow read the
   pre-transition width and reserved a too-narrow footer
   permanently.
2. **Stage oscillation.** If the measurement does not pin the
   label hidden, the footer resolves one stage with the pill and
   the opposite without it — the #4968 chip-label flicker, and
   the mobile config burger clipped under the pill at 390 px.

This harness loads a fixture that mirrors the real footer
structure (``.composer-footer > .composer-left + .composer-right >
#btnSend``) with the production ``.composer-left`` /
``.composer-footer`` / stage rules and the production
``_fitComposerFooter`` spliced in, then drives it from inside the
page. Playwright is required; CI runs this file explicitly.
"""
from __future__ import annotations

import re
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


# Viewport widths to sweep. The review checked the widths from its
# previous review plus 320 px; this covers the same set and the
# reported 1320 px workspace-panel case.
_VIEWPORT_WIDTHS = (320, 360, 390, 768, 1024, 1320)

_BUSY_ACTION = "interrupt"

_EN_LOCALE = {
    "composer_action_stop": "Stop",
    "composer_action_queue": "Queue",
    "composer_action_interrupt": "Interrupt",
    "composer_action_steer": "Steer",
}


def _extract_function(src: str, name: str) -> str:
    """Return the literal source of ``function <name>`` including its
    balanced body. Splicing the real function (rather than a
    re-typed copy) means a regression in the production helper
    trips this test directly."""
    i = src.find("function " + name)
    assert i != -1, f"{name} not found"
    brace_open = src.find("{", i)
    depth = 0
    end = brace_open
    for j, ch in enumerate(src[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return src[i:end]


def _extract_css_block(selector_regex: str) -> str:
    m = re.search(selector_regex, _REPO_STYLE_CSS, re.DOTALL)
    assert m, f"CSS rule not found: {selector_regex!r}"
    return m.group(0)


def _extract_all_cf_stage_rules() -> str:
    """Pull EVERY ``.composer-footer.cf-icons`` / ``.cf-burger`` rule from the
    real style.css. These are the rules that make the stage ladder actually
    shrink ``.composer-left`` (44px square profile chip at icons, model/
    reasoning/toolsets display:none at burger, the send button forced back to
    the 34px circle). Omitting any of them leaves the fixture overflowing at
    every stage, which is a fixture bug, not a product finding."""
    # A selector list may span lines before its `{`; capture up to the first
    # `{`, then the balanced-ish block (no nested braces in these rules).
    out = []
    for m in re.finditer(
        r"\.composer-footer\.cf-(?:icons|burger)[^{]*\{[^}]*\}", _REPO_STYLE_CSS
    ):
        out.append(m.group(0))
    assert out, "no .composer-footer.cf-* stage rules found in style.css"
    return "\n  ".join(out)


_FOOTER_HELPERS_JS = "\n".join(
    [
        _extract_function(_REPO_UI_JS, "_fitComposerFooter"),
        _extract_function(_REPO_UI_JS, "_scheduleComposerFit"),
        (
            "window._fitComposerFooter = _fitComposerFooter;\n"
            "window._scheduleComposerFit = _scheduleComposerFit;"
        ),
    ]
)

# The helper under test plus the label helper it depends on for the
# pill geometry.
_HELPER_JS = (
    _FOOTER_HELPERS_JS
    + "\n"
    + _extract_function(_REPO_UI_JS, "_setComposerPrimaryButtonIcon")
    + "\nwindow._setComposerPrimaryButtonIcon = _setComposerPrimaryButtonIcon;"
)


_FIXTURE_CSS = "\n  ".join(
    [
        # Layout rules — the footer is a flex row, .composer-left is the
        # flex:1 scroller, .composer-right is the fixed-width sibling
        # that carries the send button.
        _extract_css_block(r"\.composer-footer\s*\{[^}]+\}"),
        _extract_css_block(r"\.composer-left\s*\{[^}]+\}"),
        _extract_css_block(r"\.composer-left::-webkit-scrollbar\s*\{[^}]+\}"),
        # Send-button geometry — the base circle and the busy-mode pill.
        _extract_css_block(r"\.send-btn\s*\{[^}]+\}"),
        _extract_css_block(
            r"\.send-btn\[data-action=\"(?:stop|queue|interrupt|steer)\"\]\s*\{[^}]+\}"
        ),
        _extract_css_block(r"\.send-btn-label\s*\{[^}]+\}"),
        _extract_css_block(r"\.send-btn-label:empty\s*\{[^}]+\}"),
        # Every stage rule — without these the fixture cannot shed the chips
        # the way production does, and the ladder never bottoms out.
        _extract_all_cf_stage_rules(),
    ]
)


# Fixture mirrors static/index.html:597-727: .composer-footer wraps
# .composer-left (the chip group) and .composer-right (status,
# context ring, badge, #btnSend). Wide chips make .composer-left
# overflow at narrow viewports so the stage ladder is exercised.
_FIXTURE_HTML = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>{_FIXTURE_CSS}</style>
</head>
<body style="margin:0;padding:0;background:#0c0c1a;">
  <div class="composer-footer" id="composerFooter">
    <div class="composer-left" id="composerLeft">
      <button class="composer-profile-chip" type="button" style="flex:0 0 auto;">Profile</button>
      <button class="composer-ws-chip" type="button" style="flex:0 0 auto;">Workspace with a long label</button>
      <div class="composer-reasoning-wrap" style="flex:0 0 auto;">
        <button class="composer-reasoning-chip" type="button" style="display:inline-flex;">Reasoning effort</button>
      </div>
      <div class="composer-toolsets-wrap" style="flex:0 0 auto;">
        <button class="composer-toolsets-chip" type="button" style="display:inline-flex;">Toolsets</button>
      </div>
      <div class="composer-model-wrap" style="flex:0 0 auto;">
        <button class="composer-model-chip" type="button" style="display:inline-flex;">claude-sonnet-5</button>
      </div>
    </div>
    <div class="composer-right" style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
      <span class="composer-status" id="composerStatus" style="display:none;"></span>
      <button class="send-btn has-tooltip has-tooltip--left" id="btnSend" data-tooltip="Send message" data-action="send" disabled>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
      </button>
    </div>
  </div>
  <script>{_HELPER_JS}</script>
</body>
</html>
""".strip()


@pytest.fixture(scope="module")
def footer_page():
    """Module-scoped headless Chromium yielding a factory that opens the
    footer fixture at a given viewport width and applies a given
    send-button action."""
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")

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

        def _make(viewport_width: int, action: str = "send"):
            context = browser.new_context(
                viewport={"width": viewport_width, "height": 600}
            )
            page = context.new_page()
            page.set_content(_FIXTURE_HTML)
            page.evaluate(
                "(action) => {\n"
                "  window.t = (k) => ({\n"
                "    'composer_action_stop': 'Stop',\n"
                "    'composer_action_queue': 'Queue',\n"
                "    'composer_action_interrupt': 'Interrupt',\n"
                "    'composer_action_steer': 'Steer',\n"
                "  })[k] || k;\n"
                "  const btn = document.getElementById('btnSend');\n"
                "  btn.dataset.action = action;\n"
                "  window._setComposerPrimaryButtonIcon(btn, action);\n"
                "  const footer = document.getElementById('composerFooter');\n"
                "  const left = document.getElementById('composerLeft');\n"
                "  window._fitComposerFooter();\n"
                "  return {foot: footer.className, overflows: left.scrollWidth > left.clientWidth + 1};\n"
                "}",
                action,
            )
            return page, context

        try:
            yield _make
        finally:
            browser.close()


def _stage_of(page) -> str:
    return page.eval_on_selector(
        "#composerFooter", "el => Array.from(el.classList).filter(c => c.startsWith('cf-')).join(' ')"
    )


def _send_width(page) -> float:
    return page.evaluate(
        "() => document.getElementById('btnSend').getBoundingClientRect().width"
    )


def _left_overflow(page) -> bool:
    return page.evaluate(
        "() => { const l = document.getElementById('composerLeft');"
        " return l.scrollWidth > l.clientWidth + 1; }"
    )


# ── 1. No geometry is transitioned on .send-btn ───────────────────────


def test_send_btn_does_not_transition_geometry():
    """A width/padding/border-radius transition makes the footer fit
    time-dependent: an idle pass overlapping the pill's growth reads
    the pre-transition width and reserves a too-narrow footer.
    Colour/transform transitions are fine.

    Anchored on the rule that sets ``width:34px`` (the base circular
    button) rather than the first ``.send-btn`` block in the file —
    style.css carries more than one rule whose selector contains
    ``.send-btn``, and an unanchored regex grabs whichever comes first,
    which silently turns this into a no-op test."""
    m = re.search(r"\.send-btn\s*\{[^}]*width:34px[^}]*\}", _REPO_STYLE_CSS, re.DOTALL)
    assert m, "base .send-btn rule (width:34px) not found"
    decls = m.group(0)
    assert "transition:" in decls, (
        "base .send-btn rule carries no transition declaration — the "
        "colour/transform animation was dropped; this rule is what the "
        "regex must be matching"
    )
    for prop in ("width", "padding", "border-radius"):
        assert not re.search(rf"transition:[^;}}]*\b{prop}\b", decls), (
            f".send-btn transitions {prop}; the footer fit can measure "
            "mid-transition and pin the wrong stage. Only colour/transform "
            "may animate."
        )


# ── 2. The busy pill's behaviour is per-stage, not per-width ──────────


@pytest.mark.parametrize("width", _VIEWPORT_WIDTHS)
def test_busy_pill_is_wider_than_idle(footer_page, width):
    """The pill affordance must actually widen the button wherever the
    footer keeps the full-label or icons stage — otherwise the fixture
    never exercises the room-stealing the review reported.

    At the burger stage the label is intentionally ``display:none`` and
    the button is forced back to the 34 px circle (style.css
    ``.composer-footer.cf-burger .send-btn-label`` / ``.send-btn``), so
    the pill is correctly suppressed there. Which widths land on which
    stage is a function of the fixtures' chip widths, so assert the
    contract per resolved stage instead of assuming a width band."""
    page_busy, ctx = footer_page(width, _BUSY_ACTION)
    try:
        busy_w = _send_width(page_busy)
        stage = _stage_of(page_busy)
        page_busy.evaluate(
            "() => { const b = document.getElementById('btnSend');"
            " b.dataset.action = 'send';"
            " window._setComposerPrimaryButtonIcon(b, 'send'); }"
        )
        idle_w = _send_width(page_busy)
        if "cf-burger" in stage:
            # #7686 finding 2: the phone 44px touch-target contract outranks
            # the 34px circle. The top-level cf-burger rule still asks for
            # width:34px, but the 640px block now pins min-width/min-height:44px
            # (higher need, lower specificity — min-* is not overridden by a
            # plain width), so phone widths measure 44px, not 34px.
            expected = 44 if width <= 640 else 34
            assert busy_w <= expected + 1 and idle_w <= expected + 1, (
                f"viewport {width}px: burger stage must keep the {expected}px "
                f"target for both modes, got busy={busy_w}px idle={idle_w}px"
            )
        else:
            assert busy_w > idle_w + 1, (
                f"viewport {width}px (stage {stage!r}): busy button "
                f"({busy_w}px) is not wider than the idle button "
                f"({idle_w}px) — the pill did not render"
            )
    finally:
        ctx.close()


# ── 3. The footer stage is stable across a busy/idle cycle ────────────


@pytest.mark.parametrize("width", _VIEWPORT_WIDTHS)
def test_footer_stage_is_stable_across_a_busy_idle_cycle(footer_page, width):
    """Re-fit while busy and while idle at the same viewport must
    resolve to the same stage. A stage that flips means the pill's
    width leaks into the measurement — the #4968 chip-label flicker
    and the burger-clipped-under-the-pill bug."""
    page_busy, ctx = footer_page(width, _BUSY_ACTION)
    try:
        stage_busy = _stage_of(page_busy)
        # Switch back to idle and re-fit: the measurement pins the label
        # hidden, so the resolved stage must not change.
        page_busy.evaluate(
            "() => { const b = document.getElementById('btnSend');"
            " b.dataset.action = 'send';"
            " window._setComposerPrimaryButtonIcon(b, 'send');"
            " window._fitComposerFooter(); }"
        )
        stage_idle = _stage_of(page_busy)
        assert stage_busy == stage_idle, (
            f"viewport {width}px: footer stage flipped busy({stage_busy!r}) -> "
            f"idle({stage_idle!r}); the busy pill's width is leaking into "
            "the overflow measurement"
        )
        # At the burger stage the label is display:none and the button is forced
        # back to the 34px circle (style.css `.composer-footer.cf-burger
        # .send-btn-label` / `.send-btn`), so the pill must NOT widen the
        # button there. At any smaller stage, .composer-left is a horizontal
        # scroller by design (overflow-x:auto) — the collapsed chips are
        # display:none, not clipped, so scrollWidth may exceed clientWidth.
        # Neither is a defect; assert the burger-stage button shape instead.
        if "cf-burger" in stage_busy:
            # #7686 finding 2: 44px at phone widths (see the note above),
            # 34px from 641px up.
            expected = 44 if width <= 640 else 34
            assert _send_width(page_busy) <= expected + 1, (
                f"viewport {width}px: burger stage must force the {expected}px "
                f"target back, measured {_send_width(page_busy)}px"
            )
    finally:
        ctx.close()


# ── 4. The 1320 px workspace-panel case must not clip ────────────────


def test_wide_viewport_with_sibling_chrome_keeps_full_labels(footer_page):
    """The 10-06 review's headline repro: at 1320 px with the workspace
    panel, the context ring and a "Reconnecting... (1/3)" status
    visible, a footer fit that measures ``.composer-left`` against the
    wrong reference clips 119 px that master does not. With no chips
    overflowing, the footer must keep the full-label stage."""
    page, ctx = footer_page(1320, _BUSY_ACTION)
    try:
        # Surface sibling chrome the way a live run does.
        page.evaluate(
            "() => { const s = document.getElementById('composerStatus');"
            " s.style.display = ''; s.textContent = 'Reconnecting… (1/3)'; }"
        )
        page.evaluate("() => window._fitComposerFooter()")
        assert _stage_of(page) == "", (
            f"wide viewport collapsed to {_stage_of(page)!r} with no "
            "chip overflow — the fit is measuring against the wrong "
            "reference box"
        )
        assert not _left_overflow(page), (
            "wide viewport: .composer-left clips horizontally with no "
            "collapsed stage committed"
        )
    finally:
        ctx.close()
