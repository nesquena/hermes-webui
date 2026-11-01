#!/usr/bin/env python3
"""Actual stylesheet: the compact running-dot ring stays inside its card.

#7495 UX gate, round 4: the standalone live running card (not yet inside a
worklog group) must give the dot's ::after ring the same ~5px of left room the
grouped rows got in round 3 — the ring scales to 2.4x of the 7px dot (~4.9px
past the dot each side), and the card's overflow:hidden clips anything past the
left edge into a D shape. Because the reserve now applies to EVERY compact
worklog card header, the completed→grouped move must not shift the row either:
standalone and grouped headers are asserted to share one left inset.

Real stylesheet, synthetic compact-worklog DOM mirroring the renderer's
structure (ui.js worklog shell → .tool-worklog-list; tool group →
.tool-group-body.tool-worklog-tool-group-body → .tg-rows). Measured in
chromium at desktop and phone widths.
"""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

# Mirrors the renderer's compact-worklog nesting: a standalone live running
# card as a direct list row, plus a tool group body holding a card (the home a
# completed card moves into).
SCENE = """
<div class="tool-worklog-list">
  <div class="tool-card-row">
    <div class="tool-card tool-card-running">
      <div class="tool-card-header">
        <span class="tool-card-running-dot"></span>
        <span class="tool-card-name"><span class="tool-card-name-label">terminal</span></span>
      </div>
    </div>
  </div>
  <div class="tool-worklog-tool-group">
    <button type="button" class="tool-group-head tool-worklog-tool-group-head"></button>
    <div class="tool-group-body tool-worklog-tool-group-body">
      <div class="tg-rows tool-worklog-tool-group-rows">
        <div class="tool-card-row">
          <div class="tool-card">
            <div class="tool-card-header">
              <span class="tool-card-running-dot"></span>
              <span class="tool-card-name"><span class="tool-card-name-label">shell</span></span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
"""

MEASURE = """() => {
  const out={};
  for (const [key, sel] of [['standalone','.tool-worklog-list > .tool-card-row .tool-card'],
                            ['grouped','.tg-rows .tool-card']]) {
    const card=document.querySelector(sel);
    const dot=card.querySelector('.tool-card-running-dot');
    const c=card.getBoundingClientRect(), d=dot.getBoundingClientRect();
    out[key]={inset:d.left-c.left, cardLeft:c.left, dotLeft:d.left,
              overflow: getComputedStyle(card).overflow};
  }
  return out;
}"""


def main():
    with sync_playwright() as pw:
        for engine in os.environ.get('BROWSERS', 'chromium').split(','):
            browser = getattr(pw, engine).launch(headless=True)
            try:
                for label, width, height in (('desktop', 1440, 900), ('phone', 390, 844)):
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    page.set_content(f'<div style="width:100%"><div class="worklog">{SCENE}</div></div>')
                    page.add_style_tag(path=str(ROOT / 'static' / 'style.css'))
                    page.wait_for_timeout(150)
                    m = page.evaluate(MEASURE)
                    # The ring needs ~4.9px past the dot; the reserve must keep
                    # it at least 5px inside the card's overflow:hidden edge.
                    assert m['standalone']['overflow'] == 'hidden', m
                    assert m['standalone']['inset'] >= 5, (label, m)
                    assert m['grouped']['inset'] >= 5, (label, m)
                    # Running and completed rows share one left inset: the
                    # completed→grouped transition must not shift the row.
                    assert abs(m['standalone']['inset'] - m['grouped']['inset']) < 0.1, (label, m)
                    print(json.dumps({'engine': engine, 'width': label,
                                      'standalone_inset': round(m['standalone']['inset'], 2),
                                      'grouped_inset': round(m['grouped']['inset'], 2)}))
                    page.close()
            finally:
                browser.close()
        print('PASS: compact running-dot ring inset (standalone + grouped, desktop + phone)')


if __name__ == '__main__':
    main()
