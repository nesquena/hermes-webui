"""Browser behavior of mount scheduling while follow interpretation is guarded."""
from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_compensation_guard_does_not_block_required_mount(page):  # noqa: F811 - pytest injects the imported fixture
    result = page.evaluate("""() => {
      S.busy=false;S.activeStreamId=null;delete INFLIGHT.fixture;
      S.messages=Array.from({length:240},(_,i)=>({role:i%2?'assistant':'user',
        content:'Row '+i+'\\n\\n'+'Public text. '.repeat(50)}));
      renderMessages();
      const c=$('messages');
      // Isolate this scroll dispatch from any rAF queued by initial rendering.
      cancelAnimationFrame(_messageVirtualScrollRaf);_messageVirtualScrollRaf=0;
      c.scrollTop=0;
      _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
      const pinned=_scrollPinned;
      c.dispatchEvent(new Event('scroll'));
      return {scheduled:_messageVirtualScrollRaf!==0,pinned,
        afterPinned:_scrollPinned,fresh:_freshProgrammaticScrollActive()};
    }""")
    assert result['fresh'] and result['scheduled'], result
    assert result['afterPinned'] == result['pinned'], result
    page.wait_for_timeout(600)
    before = page.evaluate('_messageWindowRevision')
    page.wait_for_timeout(600)
    assert page.evaluate('_messageWindowRevision') == before, 'idle render loop'
