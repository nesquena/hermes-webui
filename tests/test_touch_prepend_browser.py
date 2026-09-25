"""Real layout oracle for repeated prepends; no overridden DOM geometry."""
from pathlib import Path

import pytest

from tests.test_ipad_sidebar_scroll_stuck import ROOT, SESSIONS_JS, _extract_fn


@pytest.mark.parametrize("width,height", [(1440, 900), (820, 1180), (390, 844)])
def test_repeated_prepend_preserves_real_viewport_anchor(width, height):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    functions = "\n".join(_extract_fn(SESSIONS_JS, name) for name in (
        "_prependTouchBatch", "_createTouchGroupWrapper",
        "_updateTouchGroupSpacers", "_updateTouchSentinel", "_touchIntervalState",
    ))
    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium is not installed")
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, has_touch=True)
            page.set_content('<div class="session-list" id="list"></div>')
            page.add_style_tag(content=(ROOT / "static/style.css").read_text())
            page.add_style_tag(content="#list {position:relative;width:300px;height:500px;flex:none;overflow-y:auto;overflow-anchor:none}")
            results = page.evaluate("""() => {
                const list = document.getElementById('list');
                const SESSION_TOUCH_BATCH_SIZE=40, SESSION_VIRTUAL_ROW_HEIGHT=52;
                let _sessionTouchGen=1, _sessionTouchListEl=list;
                let _sessionTouchStartIndex=80, _sessionTouchLoadedCount=160;
                let _touchBatchPending=false, _touchSentinelObserver=null;
                function _scheduleContinuousBatch() {}
                function _invalidateTouchRender() { throw Error('unexpected invalidation'); }
                function renderSessionListFromCache() { throw Error('unexpected rebuild'); }
                function t(key) { return key; }
                const labels=['Pre','Pre-2','Today','Later'];
                const flatRows=Array.from({length:160}, (_, i) => ({
                    group:{label:labels[Math.floor(i/40)]}, session:{session_id:'s'+i}
                }));
                function renderOneSession(s) {
                    const el=document.createElement('div');
                    el.className='session-item'; el.dataset.sid=s.session_id;
                    el.textContent='Conversation '+s.session_id;
                    return el;
                }
                const _touchRenderState={gen:1,list,flatRows,renderOneSession};
            """ + functions + """
                for (const label of labels.slice(2)) {
                    const wrapper=_createTouchGroupWrapper({label},_touchRenderState);
                    for (const row of flatRows.filter(r => r.group.label===label))
                        wrapper.querySelector('.session-date-body').append(renderOneSession(row.session));
                    list.append(wrapper);
                }
                const anchor=list.querySelector('[data-sid="s80"]');
                list.scrollTop=anchor.offsetTop;
                const results=[];
                for (const start of [40,0]) {
                    const before={top:anchor.getBoundingClientRect().top, offset:anchor.offsetTop, scroll:list.scrollTop};
                    _prependTouchBatch();
                    const rows=[...list.querySelectorAll('.session-item[data-sid]')];
                    results.push({start, before, top:anchor.getBoundingClientRect().top,
                        offset:anchor.offsetTop, scroll:list.scrollTop,
                        same:rows.find(r=>r.dataset.sid==='s80')===anchor,
                        sids:rows.map(r=>r.dataset.sid),
                        labels:[...list.querySelectorAll('[data-group-label]')].map(g=>g.dataset.groupLabel)});
                }
                return results;
            }""")
            for result in results:
                assert result["same"]
                assert result["sids"] == [f"s{i}" for i in range(result["start"], 160)]
                assert result["labels"] == (["Pre-2", "Today", "Later"] if result["start"] else ["Pre", "Pre-2", "Today", "Later"])
                assert result["offset"] > result["before"]["offset"]
                assert result["scroll"] - result["before"]["scroll"] == pytest.approx(result["offset"] - result["before"]["offset"], abs=1)
                assert result["top"] == pytest.approx(result["before"]["top"], abs=1)
        finally:
            browser.close()
