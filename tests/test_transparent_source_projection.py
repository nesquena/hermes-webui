"""Scene hydration must not relocate virtualized transparent source cards."""
import json

from tests.test_issue500_message_list_virtualization import UI_JS_PATH, _extract_func_script, _run_node
from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_only_browser_derived_scenes_use_source_window_ownership():
    script = _extract_func_script(UI_JS_PATH.read_text()) + """
const _sourceWindowHistoricalScenes=new WeakSet();
let mode=true;
const window={_virtualizeTranscript:true};
const isTransparentStream=()=>mode;
eval(extractFunc('_sourceWindowOwnsHistoricalScene'));
const scene={},derived={_anchor_activity_scene:scene},server={_anchor_activity_scene:{}};
_sourceWindowHistoricalScenes.add(scene);
const active=[_sourceWindowOwnsHistoricalScene(derived),_sourceWindowOwnsHistoricalScene(server)];
mode=false;const compact=_sourceWindowOwnsHistoricalScene(derived);
mode=true;window._virtualizeTranscript=false;const off=_sourceWindowOwnsHistoricalScene(derived);
console.log(JSON.stringify({active,compact,off}));
"""
    assert json.loads(_run_node(script)) == {'active': [True, False], 'compact': False, 'off': False}


def test_historical_cards_keep_declaring_source_when_summary_mounts(page):  # noqa: F811 - pytest fixture injection
    result = page.evaluate("""() => {
      S.busy=false;S.activeStreamId=null;delete INFLIGHT.fixture;
      window._chatActivityDisplayMode='transparent_stream';window._transparentStream=true;
      window._virtualizeTranscript=true;
      const data=[{role:'user',content:'Previous question'},
        {role:'assistant',content:'Previous answer'}, {role:'user',content:'Current question'}];
      for(let i=0;i<55;i++){
        data.push({role:'assistant',content:'',tool_calls:[{id:'projection-'+i,
          type:'function',function:{name:'web_search',arguments:'{}'}}]});
        data.push({role:'tool',tool_call_id:'projection-'+i,content:'Public result '+i});
      }
      data.push({role:'assistant',content:'Current final answer'});
      S.messages=data;S.toolCalls=[];
      _hydrateIdLinkedHistoricalToolScenes(S.messages,{sessionId:S.session.session_id,mode:'transparent_stream'});
      const generated=!!S.messages.at(-1)._anchor_activity_scene;
      renderMessages();
      const cards=Array.from($('msgInner').querySelectorAll('.tool-card-row[data-tool-disclosure-key]'));
      const owners=cards.map(card=>{
        let source=card.previousElementSibling;
        while(source&&!source.hasAttribute('data-msg-idx'))source=source.previousElementSibling;
        return {key:card.dataset.toolDisclosureKey,raw:source?Number(source.dataset.msgIdx):-1};
      });
      const declared=S.toolCalls.filter(t=>String(t.tid).startsWith('projection-'))
        .map(t=>({key:'id:'+t.tid,raw:t.assistant_msg_idx}));
      return {generated,owners,declared};
    }""")
    assert result['generated'], 'fixture must exercise actual historical scene synthesis'
    expected = [{'key': f'id:projection-{i}', 'raw': 3 + 2 * i} for i in range(55)]
    assert result['owners'] == expected
    assert result['declared'] == expected
