import assert from 'node:assert/strict';
import fs from 'node:fs';

const source=fs.readFileSync(new URL('../static/ui.js',import.meta.url),'utf8').replace(/\r\n/g,'\n');

function functionSource(name){
  const start=source.indexOf(`function ${name}`);
  assert.notEqual(start,-1,`${name} must exist`);
  const brace=source.indexOf('{',start);
  let depth=0;
  for(let i=brace;i<source.length;i+=1){
    if(source[i]==='{') depth+=1;
    else if(source[i]==='}'){
      depth-=1;
      if(depth===0) return source.slice(start,i+1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

const persistedCommentarySource=functionSource('_assistantPersistedCommentaryPayloadText');
const commentarySource=functionSource('_assistantCommentaryPayloadText');
const displaySource=functionSource('_assistantDisplayContentFromMessage');
const reasoningSource=functionSource('_assistantReasoningPayloadText');
const sanitizeSource=functionSource('_sanitizeThinkingDisplayText');
const normalizeSource=functionSource('_normalizeThinkingEchoCompare');
const stripEchoSource=functionSource('_stripVisibleAssistantEchoFromThinking');
const worklogSource=functionSource('_worklogReasoningTextFromMessage');
const belongsSource=functionSource('_assistantMessageBelongsInWorklog');
const settledRowsSource=functionSource('_anchorSceneRowsForSettledWorklog');
const deferredRowsSource=functionSource('_deferredWorklogRowsFromGroup');
const revealSource=functionSource('_revealTransparentEarlierSteps');
const runtime=new Function(`const window={_showCommentary:true};\nfunction _stripXmlToolCallsDisplay(text){return String(text||'');}\nfunction _isAssistantEmptyPlaceholderContent(){return false;}\n${persistedCommentarySource}\n${commentarySource}\n${displaySource}\n${reasoningSource}\n${sanitizeSource}\n${normalizeSource}\n${stripEchoSource}\n${worklogSource}\n${belongsSource}\nreturn {window,_assistantPersistedCommentaryPayloadText,_assistantCommentaryPayloadText,_assistantDisplayContentFromMessage,_worklogReasoningTextFromMessage,_assistantMessageBelongsInWorklog};`)();

const progress='Candidate identity verified; running the real restart now.';
const commentaryMessage={
  role:'assistant',
  content:'',
  reasoning:progress,
  reasoning_content:progress,
  codex_message_items:[{
    type:'message', role:'assistant', status:'completed', phase:'commentary',
    content:[{type:'output_text',text:progress}],
  }],
};
assert.equal(runtime._assistantCommentaryPayloadText(commentaryMessage),progress);
assert.equal(runtime._assistantDisplayContentFromMessage(commentaryMessage,''),progress,
  'settled commentary must become ordinary assistant display content');
assert.equal(runtime._worklogReasoningTextFromMessage(commentaryMessage,0,new Set(),progress,'',[progress]),'',
  'the matching reasoning_content echo must not also render as Thinking');
assert.equal(runtime._assistantMessageBelongsInWorklog({...commentaryMessage,_activityBurstId:9},0,new Set(),progress,{}),false,
  'visible commentary with activity metadata must remain ordinary information');
assert.equal(runtime._assistantMessageBelongsInWorklog({...commentaryMessage,_live:true},0,new Set(),progress,{}),false,
  'visible live commentary must remain ordinary information');
assert.equal(runtime._assistantMessageBelongsInWorklog({role:'assistant',content:'',tool_calls:[{id:'call-1'}]},0,new Set([0]),'',{}),true,
  'an empty tool activity message still belongs in Worklog');
runtime.window._showCommentary=false;
assert.equal(runtime._assistantCommentaryPayloadText(commentaryMessage),'',
  'profile-off commentary must not become ordinary assistant content');
assert.equal(runtime._assistantPersistedCommentaryPayloadText(commentaryMessage),progress,
  'presentation gating must not mutate or discard the persisted sidecar');
runtime.window._showCommentary=true;

const settledRowsRuntime=new Function('messages',`const window={_showCommentary:true,_showThinking:true};\nconst S={messages};\nfunction _anchorSceneRowsForRendering(scene){return scene.activity_rows||[];}\nfunction _normalizeThinkingEchoCompare(text){return String(text||'').replace(/\\s+/g,' ').trim();}\n${persistedCommentarySource}\n${reasoningSource}\n${settledRowsSource}\nreturn {window,run:_anchorSceneRowsForSettledWorklog};`)([commentaryMessage]);
const sceneRows=[
  {role:'prose',kind:'process_prose',text:progress},
  {role:'prose',kind:'process_prose',text:'Distinct process prose'},
  {role:'thinking',kind:'reasoning',text:'private reasoning'},
  {role:'tool',kind:'tool_call',text:'terminal'},
];
const visibleCommentaryNode={getAttribute:name=>name==='data-raw-text'?progress:null,textContent:progress};
const visibleBlocks={
  querySelectorAll:selector=>selector==='[data-visible-commentary="1"]'?[visibleCommentaryNode]:[],
  insertBefore(fragment){this.inserted=fragment.children.slice();},
  appendChild(fragment){this.inserted=fragment.children.slice();},
};
assert.deepEqual(settledRowsRuntime.run({activity_rows:sceneRows},visibleBlocks),sceneRows.slice(1),
  'settled Worklog must drop only prose already rendered as ordinary commentary');
assert.deepEqual(settledRowsRuntime.run({activity_rows:sceneRows},{querySelectorAll:()=>[]}),sceneRows,
  'legacy scenes without a visible commentary owner keep their prose fallback');

const deferredRowsRuntime=new Function('sceneRows','visibleBlocks',`
  const window={_showCommentary:true,_showThinking:true};
  const S={messages:[{_anchor_activity_scene:{activity_rows:sceneRows}}]};
  function _assistantTurnBlocks(){return visibleBlocks;}
  function _anchorSceneRowsForRendering(scene){return scene.activity_rows||[];}
  function _normalizeThinkingEchoCompare(text){return String(text||'').replace(/\\s+/g,' ').trim();}
  ${settledRowsSource}
  ${deferredRowsSource}
  return _deferredWorklogRowsFromGroup;
`)(sceneRows,visibleBlocks);
const deferredGroup={
  getAttribute:name=>name==='data-activity-disclosure-key'?'anchor-scene:0':null,
  closest:()=>({}),
};
assert.deepEqual(deferredRowsRuntime(deferredGroup),sceneRows.slice(1),
  'deferred/cache-restored Worklog rows must use the same commentary ownership filter');

function runReveal(revealImplementation){
  return new Function('sceneRows','visibleBlocks',`
    const seen=[];
    const _transparentRevealedTurns=new Set();
    const _sessionHtmlCache=new Map();
    const window={_showCommentary:true,_showThinking:true};
    const S={session:{session_id:'sid'},messages:[]};
    const messages={scrollTop:0,scrollHeight:100};
    function $(id){return id==='messages'?messages:null;}
    function _transparentRevealKey(){return 'sid:0';}
    function _assistantTurnBlocks(){return visibleBlocks;}
    function _anchorSceneRowsForRendering(scene){return scene.activity_rows||[];}
    function _normalizeThinkingEchoCompare(text){return String(text||'').replace(/\\s+/g,' ').trim();}
    ${settledRowsSource}
    function _anchorSceneLastNonTerminalWorkRowIndex(rows){return rows.length-1;}
    function _assistantAnchorSceneFinalAnswerText(){return '';}
    function msgContent(){return '';}
    function _computeTransparentHiddenPrefixCount(rows){return rows.length;}
    function _anchorSceneTransparentNodeForRow(row){if(!row)return null;seen.push(row);return {setAttribute(){}};}
    function _syncTransparentEventControls(){}
    const frag={children:[],appendChild(node){this.children.push(node);}};
    const document={createDocumentFragment(){return frag;}};
    const turnEl={setAttribute(){},removeAttribute(){}};
    const segment={closest(){return turnEl;}};
    const affordance={parentElement:visibleBlocks,getAttribute(){return String(sceneRows.length-1);},remove(){}};
    ${revealImplementation}
    _revealTransparentEarlierSteps({_anchor_activity_scene:{activity_rows:sceneRows}},segment,0,affordance);
    return seen;
  `)(sceneRows,visibleBlocks);
}
assert.deepEqual(runReveal(revealSource),sceneRows.slice(1),
  'transparent earlier-step reveal must not resurrect the visible commentary echo');
assert.equal(runtime._assistantDisplayContentFromMessage({...commentaryMessage,content:'final answer'},'final answer'),'final answer',
  'a real final answer must remain authoritative');
assert.equal(runtime._assistantCommentaryPayloadText({...commentaryMessage,codex_message_items:[{
  type:'message',phase:'analysis',content:[{type:'output_text',text:'private reasoning'}],
}]}),'','private reasoning is not visible commentary');
assert.equal(runtime._assistantCommentaryPayloadText({...commentaryMessage,codex_message_items:[{
  type:'reasoning',role:'assistant',phase:'commentary',content:[{type:'output_text',text:'private reasoning'}],
}]}),'','a reasoning item cannot become visible merely by carrying a commentary phase');
assert.equal(runtime._assistantCommentaryPayloadText({...commentaryMessage,codex_message_items:[{
  type:'message',role:'user',phase:'commentary',content:[{type:'output_text',text:'user data'}],
}]}),'','a user-role message item cannot be projected as assistant commentary');

const renderSource=functionSource('renderMessages');
const call='_assistantDisplayContentFromMessage(m, content)';
assert.equal(renderSource.split(call).length-1,1,'renderMessages must consume commentary display classification exactly once');

const phaseTarget="String(item.phase||'').toLowerCase()!=='commentary'";
assert.equal(persistedCommentarySource.split(phaseTarget).length-1,1,'commentary mutation target must be unique');
const phaseMutant=persistedCommentarySource.replace(phaseTarget,'true');
const phaseRuntime=new Function(`${phaseMutant}\nreturn _assistantPersistedCommentaryPayloadText;`)();
assert.notEqual(phaseRuntime(commentaryMessage),progress,'removing commentary classification must RED');

const displayTarget="if(existing) return existing;";
assert.equal(displaySource.split(displayTarget).length-1,1,'display mutation target must be unique');
const displayMutant=displaySource.replace(displayTarget,'return existing;');
const displayRuntime=new Function(`const window={_showCommentary:true};\n${persistedCommentarySource}\n${commentarySource}\n${displayMutant}\nreturn _assistantDisplayContentFromMessage;`)();
assert.notEqual(displayRuntime(commentaryMessage,''),progress,'disabling empty-content fallback must RED');

const belongsTarget='if(hasVisibleText) return false;';
assert.equal(belongsSource.split(belongsTarget).length-1,1,'visible prose ownership target must be unique');
const belongsMutant=belongsSource.replace(belongsTarget,'');
const belongsRuntime=new Function(`function _isAssistantEmptyPlaceholderContent(){return false;}\n${belongsMutant}\nreturn _assistantMessageBelongsInWorklog;`)();
assert.notEqual(belongsRuntime({...commentaryMessage,_activityBurstId:9},0,new Set(),progress,{}),false,
  'allowing activity metadata to override visible prose must RED');

const settledRowsTarget="if(!visibleCommentaryTexts.size) return rows;";
assert.equal(settledRowsSource.split(settledRowsTarget).length-1,1,'settled Worklog ownership target must be unique');
const settledRowsMutant=settledRowsSource.replace(settledRowsTarget,'return rows;');
const settledRowsMutantRuntime=new Function(`const window={_showCommentary:true,_showThinking:true};\nconst S={messages:[]};\nfunction _anchorSceneRowsForRendering(scene){return scene.activity_rows||[];}\nfunction _normalizeThinkingEchoCompare(text){return String(text||'').replace(/\\s+/g,' ').trim();}\n${persistedCommentarySource}\n${reasoningSource}\n${settledRowsMutant}\nreturn _anchorSceneRowsForSettledWorklog;`)();
assert.notDeepEqual(settledRowsMutantRuntime({activity_rows:sceneRows},visibleBlocks),sceneRows.slice(1),
  'duplicating visible commentary inside Worklog must RED');

const revealTarget="const rows=_anchorSceneRowsForSettledWorklog(scene,blocks)||[];";
assert.equal(revealSource.split(revealTarget).length-1,1,'transparent reveal ownership target must be unique');
const revealMutant=revealSource.replace(
  revealTarget,
  "const rows=_anchorSceneRowsForRendering(scene,{settled:true})||[];",
);
assert.notDeepEqual(runReveal(revealMutant),sceneRows.slice(1),
  'bypassing commentary ownership during earlier-step reveal must RED');

console.log('PASS commentary_is_visible_info');
