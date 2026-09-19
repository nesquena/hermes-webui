"""Bound same-frame opacity nodes without changing reveal or rewind semantics."""
import json

import pytest
from playwright.sync_api import sync_playwright
from tests.test_smooth_text_fade import function_block, MESSAGES_JS


@pytest.mark.parametrize('writer', ['append', 'renderer'])
def test_same_frame_fade_batches_preserve_text_and_rewind_tail(writer):
    names = ['_streamFadeAppendText', '_streamFadeRenderer', '_streamFadeBindCleanup',
             '_streamFadeSkipNode', '_streamFadeMuteRenderedPrefix']
    helpers = '\n'.join(function_block(MESSAGES_JS, name) for name in names)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="root" class="stream-fade-active"></div>')
        result = page.evaluate('''() => {
          let reduce=false,_streamFadeSilentPrefixChars=0,_streamFadeCurrentMs=620;
          let _streamFadeLatestAnimationEndAt=0;
          const _STREAM_FADE_MS=620,_SMD_MEDIA_TAIL=null;
          const _streamFadeReduceMotionEnabled=()=>reduce;
          const _smdParserKey=()=>null,_smdMediaPrefixTail=()=>'';
          window.smd={default_renderer:()=>({add_text(){},set_attr(){}})};
        ''' + helpers + '''
          const root=document.getElementById('root');
          _streamFadeBindCleanup(root);
          const renderer=_streamFadeRenderer(root);
          const write=text=>WRITER==='append'?_streamFadeAppendText(root,text):renderer.add_text({nodes:[root],index:0},text);
          const text='word '.repeat(100);
          write(text);
          const initial={text:root.textContent,nodes:root.childNodes.length,spans:root.querySelectorAll('.is-new').length};
          _streamFadeMuteRenderedPrefix(root,text.slice(0,52));
          const muted={text:root.textContent,prefix:root.firstChild.textContent,tail:root.querySelector('.is-new')?.textContent};
          root.querySelector('.is-new')?.dispatchEvent(new Event('animationend',{bubbles:true}));
          const settled={text:root.textContent,active:root.querySelectorAll('.is-new').length};
          root.replaceChildren();reduce=true;write(text);
          const reduced={text:root.textContent,active:root.querySelectorAll('.stream-fade-word').length};
          root.replaceChildren();reduce=false;_streamFadeSilentPrefixChars=10;write(text);
          return {initial,muted,settled,reduced,silentText:root.textContent,firstAnimated:root.querySelector('.is-new')?.textContent};
        }'''.replace('WRITER', json.dumps(writer)))
        browser.close()
    text = 'word ' * 100
    assert result['initial'] == {'text': text, 'nodes': 1, 'spans': 1}
    assert result['muted'] == {'text': text, 'prefix': text[:54], 'tail': text[54:]}
    assert result['settled'] == {'text': text, 'active': 0}
    assert result['reduced'] == {'text': text, 'active': 0}
    assert result['silentText'] == text
    assert result['firstAnimated'] == 'word'
