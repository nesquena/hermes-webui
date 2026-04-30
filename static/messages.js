function _markSessionViewed(sid, messageCount) {
  if(typeof _setSessionViewedCount!=='function' || !sid) return;
  const next = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  _setSessionViewedCount(sid, next);
}

function _isDocumentVisibleAndFocused() {
  if(typeof document!=='undefined' && document.visibilityState && document.visibilityState!=='visible') return false;
  if(typeof document!=='undefined' && typeof document.hasFocus==='function' && !document.hasFocus()) return false;
  return true;
}

function _isSessionCurrentPane(sid) {
  if(!sid || !S.session || S.session.session_id!==sid) return false;
  // During session switching, S.session still points at the previous row until
  // the next metadata request resolves. Do not let a just-finished old stream
  // update the chat pane while the user is moving to another session.
  if(typeof _loadingSessionId!=='undefined' && _loadingSessionId && _loadingSessionId!==sid) return false;
  return true;
}

function _isSessionActivelyViewed(sid) {
  if(!_isSessionCurrentPane(sid)) return false;
  if(!_isDocumentVisibleAndFocused()) return false;
  return true;
}

function _markActiveSessionViewedOnReturn() {
  if(!_isDocumentVisibleAndFocused() || !S.session || !S.session.session_id) return;
  _markSessionViewed(S.session.session_id, S.session.message_count || (S.messages&&S.messages.length) || 0);
  if(typeof _clearSessionCompletionUnread==='function') _clearSessionCompletionUnread(S.session.session_id);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
}

document.addEventListener('visibilitychange', _markActiveSessionViewedOnReturn);
window.addEventListener('focus', _markActiveSessionViewedOnReturn);
// TTS: pause speech synthesis when user focuses the composer (#499)
const _msgEl=document.getElementById('msg');
if(_msgEl) _msgEl.addEventListener('focus', ()=>{ if('speechSynthesis' in window && speechSynthesis.speaking) speechSynthesis.pause(); });
if(_msgEl) _msgEl.addEventListener('blur', ()=>{ if('speechSynthesis' in window && speechSynthesis.paused) speechSynthesis.resume(); });

async function send(){
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area'))return;
  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  // If busy or a manual compression is still running, handle based on busy_input_mode
  if(S.busy||compressionRunning){
    if(text){
      if(!S.session){await newSession();await renderSessionList();}
      // Busy-control slash commands must be intercepted HERE, before the
      // busyMode routing block, so the user can always type /steer, /interrupt,
      // or /queue while the agent is running and have them execute immediately.
      // Without this intercept they fall through to the queue and execute after
      // the current turn ends — by which point there is no active stream and
      // cmdSteer / cmdInterrupt say "No active task to stop."
      if(text.startsWith('/')){
        const _pc=typeof parseCommand==='function'&&parseCommand(text);
        if(_pc&&['steer','interrupt','queue','terminal'].includes(_pc.name)){
          const _bc=COMMANDS.find(c=>c.name===_pc.name);
          if(_bc){
            $('msg').value='';autoResize();
            await _bc.fn(_pc.args);
            return;
          }
        }
      }
      const busyMode=window._busyInputMode||'queue';
      if(busyMode==='steer'&&S.activeStreamId&&typeof _trySteer==='function'){
        // Real steer: clear the input first so the user gets immediate
        // feedback, then ship the steer payload via /api/chat/steer.
        // _trySteer falls back to queue+cancel internally if the agent
        // isn't running / cached / steer-capable.
        $('msg').value='';autoResize();
        // Do NOT clear pendingFiles yet — _trySteer may fall back to
        // interrupt+queue and needs the files for queueSessionMessage.
        // _trySteer clears pendingFiles itself in the fallback path, and
        // the server returns accepted:true (no files sent) on success.
        await _trySteer(text, /*explicitSteer=*/false);
        // After _trySteer: clear any remaining files (success path).
        S.pendingFiles=[];renderTray();
      } else if(busyMode==='interrupt'){
        // Queue the message, then cancel so drain re-sends it.
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
        updateQueueBadge(S.session.session_id);
        $('msg').value='';autoResize();
        S.pendingFiles=[];renderTray();
        if(S.activeStreamId&&typeof cancelStream==='function'){
          showToast(t('busy_interrupt_confirm'),2000);
          await cancelStream();
        } else {
          showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
        }
      } else {
        // Default: queue mode (current behavior). Also the fallback for
        // 'steer' mode when no stream is active or _trySteer is unavailable.
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
        $('msg').value='';autoResize();
        S.pendingFiles=[];renderTray();
        updateQueueBadge(S.session.session_id);
        showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
      }
    }
    return;
  }
  // Slash command intercept -- local commands handled without agent round-trip.
  // We push the user message BEFORE running the handler for echo-worthy
  // commands so chat order is correct: some handlers (e.g. cmdHelp) push
  // their assistant response synchronously.  If we pushed AFTER, S.messages
  // would be [assistant, user] and the chat would show the response above
  // the user's own input — reverse chronological order (#840 ordering bug).
  if(text.startsWith('/')&&!S.pendingFiles.length){
    const _parsedCmd=parseCommand(text);
    const _cmd=_parsedCmd?COMMANDS.find(c=>c.name===_parsedCmd.name):null;
    if(_cmd){
      let _pushedUser=false;
      if(!_cmd.noEcho){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        _pushedUser=true;
        renderMessages();
      }
      // Run the handler directly (we already looked it up).  If it returns
      // false it's opting out — e.g. /reasoning <level> falls through so the
      // agent sees the raw text.  Roll back the echo push in that case so
      // the normal send path doesn't duplicate it.
      if(_cmd.fn(_parsedCmd.args)===false){
        if(_pushedUser){S.messages.pop();renderMessages();}
        // Fall through to normal send path
      } else {
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
    }
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;

  setComposerStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setComposerStatus(`Upload error: ${e.message}`);return;}}

  const uploadedNames=uploaded.map(u=>u.name||u);
  const uploadedPaths=uploaded.map(u=>u&&u.is_image?(u.name||u.filename||u):(u.path||u.name||u));
  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploadedPaths.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploadedPaths.join(', ')}]`;
  if(!msgText){setComposerStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploadedNames.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploadedNames:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded:uploadedNames,toolCalls:[]};
  if(typeof saveInflightState==='function'){
    saveInflightState(activeSid,{streamId:null,messages:INFLIGHT[activeSid].messages,uploaded:uploadedNames,toolCalls:[]});
  }
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
  startApprovalPolling(activeSid);
  startClarifyPolling(activeSid);
  _fetchYoloState(activeSid);  // sync YOLO pill with backend state
  S.activeStreamId = null;  // will be set after stream starts

  // Set provisional title from user message immediately so session appears
  // in the sidebar right away with a meaningful name (server may refine later)
  if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
    const provisionalTitle=displayText.slice(0,64);
    S.session.title=provisionalTitle;
    syncTopbar();
    // Persist it and refresh the sidebar now -- don't wait for done
    api('/api/session/rename',{method:'POST',body:JSON.stringify({
      session_id:activeSid, title:provisionalTitle
    })}).catch(()=>{});  // fire-and-forget, server refines on done
    renderSessionList();  // session appears in sidebar immediately
  } else {
    renderSessionList();  // ensure it's visible even if already titled
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:S.session.model||$('modelSelect').value,workspace:S.session.workspace,
      attachments:uploaded.length?uploaded:undefined
    })});
    if(startData.effective_model && S.session){
      S.session.model=startData.effective_model;
      localStorage.setItem('hermes-webui-model', startData.effective_model);
      if($('modelSelect')) _applyModelToDropdown(startData.effective_model, $('modelSelect'));
      if(typeof syncTopbar==='function') syncTopbar();
    }
    streamId=startData.stream_id;
    S.activeStreamId = streamId;
    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id = streamId;
    }
    markInflight(activeSid, streamId);
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId,messages:INFLIGHT[activeSid].messages,uploaded:uploadedNames,toolCalls:INFLIGHT[activeSid].toolCalls||[]});
    }
    // Refresh session list so background streaming indicators appear immediately for the
    // session that was just started and any others that may already be running.
    if(typeof renderSessionList === 'function') {
      void renderSessionList();
    }
  }catch(e){
    const errMsg=String((e&&e.message)||'');
    const conflictActiveStream=/session already has an active stream/i.test(errMsg);
    if(conflictActiveStream){
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      // Keep the user's attempted turn by queueing it for after the current run.
      queueSessionMessage(activeSid,{text:msgText,files:[],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
      updateQueueBadge(activeSid);
      showToast('Current session is still running. Reconnected and queued your message.',2600);
      try{
        await loadSession(activeSid);
        setComposerStatus('');
        return;
      }catch(_){
        // Fall through to standard error handling if session reload fails.
      }
    }

    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    stopClarifyPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);removeThinking();
    if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
    S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});
    _queueDrainSid=activeSid;renderMessages();setBusy(false);setComposerStatus(`Error: ${errMsg}`);
    return;
  }

  // Open SSE stream and render tokens live
  attachLiveStream(activeSid, streamId, uploadedNames);

}

const LIVE_STREAMS={};

function closeLiveStream(sessionId, streamId){
  const live=LIVE_STREAMS[sessionId];
  if(!live) return;
  if(streamId&&live.streamId!==streamId) return;
  try{live.source.close();}catch(_){ }
  delete LIVE_STREAMS[sessionId];
}

function attachLiveStream(activeSid, streamId, uploaded=[], options={}){
  if(!activeSid||!streamId) return;
  const reconnecting=!!options.reconnecting;
  closeLiveStream(activeSid);
  if(!INFLIGHT[activeSid]) INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[...uploaded],toolCalls:[]};
  else {
    if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded];
    if(!Array.isArray(INFLIGHT[activeSid].toolCalls)) INFLIGHT[activeSid].toolCalls=[];
  }

  let assistantText='';
  let reasoningText='';
  let liveReasoningText='';
  let assistantRow=null;
  let assistantBody=null;
  let segmentStart=0;      // char offset in assistantText where current segment begins
  let _freshSegment=false; // true after a tool call — forces a new DOM segment
  // streaming-markdown state: incremental DOM-building parser per segment
  let _smdParser=null;     // current smd parser instance (null until first content)
  let _smdWrittenLen=0;    // how many chars of displayText have been fed to smd parser
  let _smdWrittenText='';  // exact displayText snapshot used for prefix-alignment checks
  // On reconnect, the assistantBody already has partial smd-rendered content.
  // We clear it on first new token and restart the parser from the reconnect point.
  let _smdReconnect=reconnecting;
  // Thinking tag patterns for streaming display
  const _thinkPairs=[
    {open:'<think>',close:'</think>'},
    {open:'<|channel>thought\n',close:'<channel|>'},
    {open:'<|turn|>thinking\n',close:'<turn|>'}  // Gemma 4
  ];

  function _isActiveSession(){
    return !!(S.session&&S.session.session_id===activeSid);
  }
  function persistInflightState(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight||typeof saveInflightState!=='function') return;
    saveInflightState(activeSid,{
      streamId,
      messages:inflight.messages||[],
      uploaded:inflight.uploaded||[...uploaded],
      toolCalls:inflight.toolCalls||[],
    });
  }
  // Throttled variant for token-by-token updates. persistInflightState()
  // calls saveInflightState() which does JSON.parse + JSON.stringify + write
  // on the entire inflight map every call. On a fast model at 60 tok/s with
  // a 10KB messages array this is ~36MB of JSON churn per second — a major
  // GC pressure source that causes the renderer to crash under load.
  // State transitions (tool events, done, error) still call persistInflightState()
  // directly so no more than 2s of progress is lost on a crash.
  let _persistTimer=null;
  function _throttledPersist(){
    if(_persistTimer) return;
    _persistTimer=setTimeout(()=>{_persistTimer=null;persistInflightState();},2000);
  }
  function _closeSource(){
    closeLiveStream(activeSid, streamId);
  }
  function syncInflightAssistantMessage(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return;
    if(!Array.isArray(inflight.messages)) inflight.messages=[];
    let assistantIdx=-1;
    for(let i=inflight.messages.length-1;i>=0;i--){
      const msg=inflight.messages[i];
      if(msg&&msg.role==='assistant'&&msg._live){assistantIdx=i;break;}
    }
    const ts=Date.now()/1000;
    if(assistantIdx>=0){
      inflight.messages[assistantIdx].content=assistantText;
      inflight.messages[assistantIdx].reasoning=reasoningText||undefined;
      inflight.messages[assistantIdx]._ts=inflight.messages[assistantIdx]._ts||ts;
      _throttledPersist();
      return;
    }
    inflight.messages.push({role:'assistant',content:assistantText,reasoning:reasoningText||undefined,_live:true,_ts:ts});
    _throttledPersist();
  }
  function ensureAssistantRow(force=false){
    if(!_isActiveSession()) return;
    if(assistantRow&&!assistantRow.isConnected){assistantRow=null;assistantBody=null;}
    if(!force&&!assistantRow){
      const parsed=_parseStreamState();
      if(!String((parsed&&parsed.displayText)||'').trim()) return;
    }
    let turn=$('liveAssistantTurn');
    if(!turn){
      appendThinking();
      turn=$('liveAssistantTurn');
    }
    const blocks=(typeof _assistantTurnBlocks==='function')?_assistantTurnBlocks(turn):null;
    if(!blocks) return;
    if(!assistantRow){
      // Only reuse an existing segment on the very first creation (e.g. reconnect).
      // After a tool call _freshSegment=true, so we always create a new segment
      // below the tool card rather than re-attaching to the old one above it.
      if(!_freshSegment){
        const existing=blocks.querySelector('[data-live-assistant="1"]');
        if(existing){
          assistantRow=existing;
          assistantBody=existing.querySelector('.msg-body');
        }
      }
    }
    if(assistantRow){
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      return;
    }

    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');
    assistantRow.className='assistant-segment';
    assistantRow.setAttribute('data-live-assistant','1');
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    assistantRow.appendChild(assistantBody);
    blocks.appendChild(assistantRow);
    _freshSegment=false; // consumed — next reuse check is normal again
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;
  let _terminalStateReached=false;

  // Bug A fix (#631): track whether the stream has been finalized so any rAF
  // scheduled by a trailing 'token'/'reasoning' event that arrives in the same
  // microtask batch as 'done' does not fire after renderMessages() has already
  // settled the DOM — which was causing the thinking card to reappear below
  // the final answer or the response to render twice.
  let _streamFinalized=false;
  let _pendingRafHandle=null;

  // rAF-throttled rendering: buffer tokens, render at most once per frame
  let _renderPending=false;
  // Extract display text from assistantText, stripping completed thinking blocks
  // and hiding content still inside an open thinking block.
  function _stripXmlToolCalls(s){
    // Strip <function_calls>...</function_calls> blocks (DeepSeek XML tool syntax).
    // These are processed as tool calls server-side; showing them raw in the bubble
    // looks broken. Also handles orphaned opening tags mid-stream. (#702)
    // Also handles DSML-prefixed variants from DeepSeek/Bedrock, including
    // spacing variants like "<｜DSML |function_calls" and truncated prefixes.
    if(!s) return s;
    const lo=String(s).toLowerCase();
    if(lo.indexOf('function_calls')===-1 && lo.indexOf('dsml')===-1) return s;
    // Support both plain <function_calls> and DSML-prefixed variants.
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
    // Also remove truncated opening tags (missing closing ">" at stream tail).
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
    // Remove malformed DSML tag fragments like "<｜DSML |" that can leak in tokens.
    s=s.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
    return s.trim();
  }
  function _streamDisplay(){
    const raw=_stripXmlToolCalls(assistantText);
    // Always run think-block stripping even when reasoningText is populated.
    // Some providers emit reasoning content via on_reasoning AND wrap it in
    // <think> tags in the token stream — the early-return caused the thinking
    // card and main response to show identical content (closes #852).
    for(const {open,close} of _thinkPairs){
      // Trim leading whitespace before checking for the open tag — some models
      // (e.g. MiniMax) emit newlines before <think>.
      const trimmed=raw.trimStart();
      if(trimmed.startsWith(open)){
        const ci=trimmed.indexOf(close,open.length);
        if(ci!==-1){
          // Thinking block complete — strip it, show the rest
          return trimmed.slice(ci+close.length).replace(/^\s+/,'');
        }
        // Still inside thinking block — show placeholder
        return '';
      }
      // Hide partial tag prefixes while streaming so users don't see
      // `<thi`, `<think`, etc. before the model finishes the token.
      if(open.startsWith(trimmed)) return '';
    }
    return raw;
  }
  function _parseStreamState(){
    const raw=_stripXmlToolCalls(assistantText);
    if(reasoningText){
      return {thinkingText:liveReasoningText, displayText:_streamDisplay(), inThinking:false};
    }
    for(const {open,close} of _thinkPairs){
      const trimmed=raw.trimStart();
      if(trimmed.startsWith(open)){
        const ci=trimmed.indexOf(close,open.length);
        if(ci!==-1){
          return {
            thinkingText: trimmed.slice(open.length, ci).trim(),
            displayText: trimmed.slice(ci+close.length).replace(/^\s+/,''),
            inThinking:false,
          };
        }
        return {
          thinkingText: trimmed.slice(open.length).trim(),
          displayText:'',
          inThinking:true,
        };
      }
      if(open.startsWith(trimmed)){
        return {thinkingText:'', displayText:'', inThinking:true};
      }
    }
    return {thinkingText:'', displayText:raw, inThinking:false};
  }
  function _renderLiveThinking(parsed){
    if(window._showThinking===false){removeThinking();return;}
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      if(typeof updateThinking==='function') updateThinking(text||'Thinking…');
      else appendThinking();
      return;
    }
    // Only remove thinking if we're not in an active reasoning phase.
    // When reasoningText is set but liveReasoningText was just reset (post-tool),
    // don't wipe the finalized thinking card — it has no id anymore so
    // removeThinking() won't find it anyway, but guard explicitly.
    if(!reasoningText) removeThinking();
  }
  // Helper: create (or recreate) the smd parser bound to a given DOM element.
  // Called when assistantBody is first created and after each tool-call segment reset.
  function _smdNewParser(el){
    _smdWrittenLen=0;
    _smdWrittenText='';
    if(!window.smd){_smdParser=null;return;}
    const renderer=window.smd.default_renderer(el);
    _smdParser=window.smd.parser(renderer);
  }
  // Helper: end the current smd parser (flushes remaining state) and null it out.
  function _smdEndParser(){
    if(_smdParser&&window.smd){
      try{window.smd.parser_end(_smdParser);}catch(_){}
      // parser_end may flush remaining markdown that creates new links/images —
      // re-sanitize the body before the DOM is handed off to highlightCode / renderMessages.
      if(assistantBody){_sanitizeSmdLinks(assistantBody);}
    }
    _smdParser=null;
    _smdWrittenLen=0;
    _smdWrittenText='';
  }
  // Helper: feed new displayText delta to the smd parser.
  // Only feeds chars beyond what has already been written (_smdWrittenLen).
  function _smdWrite(displayText){
    if(!_smdParser||!window.smd) return;
    displayText=String(displayText||'');
    // Self-heal desyncs: if displayText no longer starts with what we've already
    // written (e.g. due to stream sanitization/tag stripping), incremental slicing
    // can skip characters. Rebuild parser from the full current displayText.
    if(_smdWrittenText && !displayText.startsWith(_smdWrittenText)){
      _smdParser=null;
      _smdWrittenLen=0;
      _smdWrittenText='';
      if(assistantBody) assistantBody.innerHTML='';
      _smdNewParser(assistantBody);
      if(!_smdParser) return;
    }
    const delta=displayText.slice(_smdWrittenText.length);
    if(!delta) return;
    try{window.smd.parser_write(_smdParser,delta);}catch(_){}
    _smdWrittenLen=displayText.length;
    _smdWrittenText=displayText;
    // streaming-markdown does NOT sanitize URL schemes — `[click](javascript:...)`
    // and `![alt](javascript:...)` survive as href/src.  Strip any unsafe schemes
    // from anchors/images that were just added to the live DOM.  The existing
    // renderMd() path filters these via its http(s)-only regex; we need a matching
    // guard here so the live-stream path isn't an XSS vector for agent-echoed
    // prompt-injection content.  The final renderMessages() call at `done` uses
    // renderMd which is already safe, but during streaming the user could click
    // a malicious link before that replacement happens.
    if(assistantBody){_sanitizeSmdLinks(assistantBody);}
  }
  // Allowed URL schemes for anchors and images rendered from agent-streamed markdown.
  // Matches the effective allowlist of renderMd() (http/https via regex + relative).
  const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|\/|#|\?|\.)/i;
  function _sanitizeSmdLinks(root){
    if(!root||!root.querySelectorAll) return;
    const _a=root.querySelectorAll('a[href]');
    for(let i=0;i<_a.length;i++){
      const n=_a[i],v=n.getAttribute('href')||'';
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('href');n.setAttribute('data-blocked-scheme','1');}
    }
    const _im=root.querySelectorAll('img[src]');
    for(let i=0;i<_im.length;i++){
      const n=_im[i],v=n.getAttribute('src')||'';
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('src');n.setAttribute('data-blocked-scheme','1');}
    }
  }
  let _lastRenderMs=0;
  function _scheduleRender(){
    if(_renderPending) return;
    if(_streamFinalized) return; // Bug A: don't schedule new rAF after stream finalized
    _renderPending=true;
    // Cap render rate to ~15fps. The browser's rAF fires at 60fps, but each DOM
    // update takes 50-150ms on large sessions. During GC pauses, rAF callbacks
    // accumulate and then execute all at once, blocking the main thread for
    // multi-second stretches and crashing the renderer (Chrome error code 4/5).
    // Throttling to 66ms intervals prevents this pileup without noticeable
    // visual degradation — streaming text updates still feel immediate.
    // performance.now() is monotonic so tab suspend/resume and NTP adjustments
    // can't produce negative or enormous deltas.
    const sinceLastMs=performance.now()-_lastRenderMs;
    const _doRender=()=>{
      _pendingRafHandle=null;
      _renderPending=false;
      // Guard: a pending setTimeout+rAF can outlive stream finalization.
      if(_streamFinalized) return;
      _lastRenderMs=performance.now();
      const parsed=_parseStreamState();
      _renderLiveThinking(parsed);
      if(assistantBody){
        const displayText = segmentStart===0
          ? parsed.displayText                          // first segment: uses think-tag stripping
          : _stripXmlToolCalls(assistantText.slice(segmentStart));
        if(!_smdParser&&window.smd){
          // On reconnect: prior content in assistantBody came from a different smd parser run.
          // Clear it and start fresh — renderMessages() on done will restore the full content.
          if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
          _smdNewParser(assistantBody);
        }
        if(_smdParser){
          _smdWrite(displayText);
        } else {
          // Fallback: smd not loaded yet, reconnect session, or smd unavailable — use renderMd
          assistantBody.innerHTML = (segmentStart===0
            ? parsed.displayText
            : renderMd ? renderMd(assistantText.slice(segmentStart)) : assistantText.slice(segmentStart)) || '';
        }
      }
      scrollIfPinned();
    };
    if(sinceLastMs>=66){
      _pendingRafHandle=requestAnimationFrame(_doRender);
    } else {
      _pendingRafHandle=setTimeout(()=>requestAnimationFrame(_doRender), 66-sinceLastMs);
    }
  }

  function _wireSSE(source){
    // Note on #631 Bug B: the original PR description stated the server
    // "replays buffered token events" on reconnect, and proposed resetting
    // the accumulators here so the re-sent tokens wouldn't double the prefix.
    // That is NOT how the server actually works — api/routes._handle_sse_stream
    // reads a one-shot queue.Queue() that delivers each event to exactly one
    // consumer; a reconnect picks up from the current queue position and gets
    // only events produced during the outage.  Resetting the accumulators here
    // would wipe the already-displayed content and restart the response from
    // the first post-reconnect token — a real data-loss regression.
    //
    // The "doubled response" / "stuck cursor" symptom is fully explained by
    // Bug A (trailing rAF after `done` inserting a new live-turn wrapper) —
    // the fixes below (_streamFinalized guard + cancelAnimationFrame in the
    // terminal handlers) address it without needing a reset here.

    source.addEventListener('token',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      const parsed=_parseStreamState();
      if(String((parsed&&parsed.displayText)||'').trim()||assistantRow) ensureAssistantRow();
      _scheduleRender();
    });

    source.addEventListener('reasoning',e=>{
      const d=JSON.parse(e.data);
      reasoningText += d.text || '';
      liveReasoningText += d.text || '';
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      // Render thinking card synchronously — not via rAF — so the DOM is
      // up-to-date before a 'tool' event in the same microtask batch calls
      // finalizeThinkingCard(). The old rAF-only path caused a race where
      // the thinking row was still a spinner when finalized.
      if(window._showThinking!==false){
        if(typeof updateThinking==='function') updateThinking(liveReasoningText||'Thinking…');
        else appendThinking(liveReasoningText);
      }
      _scheduleRender();
    });

    source.addEventListener('tool',e=>{
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      const tc={name:d.name, preview:d.preview||'', args:d.args||{}, snippet:'', done:false, tid:d.tid||`live-${Date.now()}-${Math.random().toString(36).slice(2,8)}`};
      const inflight = INFLIGHT[activeSid] || (INFLIGHT[activeSid] = {
        messages:[...S.messages],
        uploaded:[],
        toolCalls:[]
      });
      if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
      INFLIGHT[activeSid].toolCalls.push(tc);
      S.toolCalls=INFLIGHT[activeSid].toolCalls;
      persistInflightState();

      if(!S.session||S.session.session_id!==activeSid) return;
      // NOTE: don't removeThinking() here — keep the thinking card visible
      // above the tool card so the turn reads top-to-bottom as:
      // user → thinking → tool cards → response. Removing it caused the card
      // to be re-created below everything when reasoning resumed post-tool.
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      liveReasoningText='';
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      appendLiveToolCard(tc);
      // Reset the live assistant row reference so that any text tokens arriving
      // after this tool call create a NEW segment appended below the tool card,
      // rather than updating the old segment that sits above it in the DOM.
      assistantRow=null;
      assistantBody=null;
      segmentStart=assistantText.length; // new segment starts at current text length
      _freshSegment=true;                // prevent reuse of old DOM node
      _smdEndParser();                   // finalize current smd parser; new one created on next token
      scrollIfPinned();
    });

    source.addEventListener('tool_complete',e=>{
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      const inflight=INFLIGHT[activeSid];
      if(!inflight) return;
      if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
      let tc=null;
      for(let i=inflight.toolCalls.length-1;i>=0;i--){
        const cur=inflight.toolCalls[i];
        if(cur&&cur.done===false&&(!d.name||cur.name===d.name)){
          tc=cur;
          break;
        }
      }
      if(!tc){
        tc={name:d.name||'tool', preview:d.preview||'', args:d.args||{}, snippet:'', done:true};
        inflight.toolCalls.push(tc);
      }
      tc.preview=d.preview||tc.preview||'';
      tc.args=d.args||tc.args||{};
      tc.done=true;
      tc.is_error=!!d.is_error;
      if(d.duration!==undefined) tc.duration=d.duration;
      S.toolCalls=inflight.toolCalls;
      persistInflightState();
      if(!S.session||S.session.session_id!==activeSid) return;
      appendLiveToolCard(tc);
      scrollIfPinned();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      d._session_id=activeSid;
      showApprovalCard(d, 1);
      playNotificationSound();
      sendBrowserNotification('Approval required',d.description||'Tool approval needed');
    });

    source.addEventListener('clarify',e=>{
      const d=JSON.parse(e.data);
      d._session_id=activeSid;
      showClarifyCard(d);
      playNotificationSound();
      sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed');
    });

    source.addEventListener('title',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      const newTitle=String(d.title||'').trim();
      if(!newTitle) return;
      if(S.session&&S.session.session_id===activeSid){
        S.session.title=newTitle;
        syncTopbar();
      }
      if(typeof _allSessions!=='undefined'&&Array.isArray(_allSessions)){
        const row=_allSessions.find(s=>s&&s.session_id===activeSid);
        if(row) row.title=newTitle;
      }
      if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
      else if(typeof renderSessionList==='function') renderSessionList();
    });

    source.addEventListener('title_status',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      try{
        console.info('[title]', {
          status:String(d.status||''),
          reason:String(d.reason||''),
          title:String(d.title||''),
          raw_preview:String(d.raw_preview||''),
          session_id:String(d.session_id||activeSid)
        });
      }catch(_){}
    });

    source.addEventListener('done',e=>{
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      // Bug A fix: cancel any pending rAF and mark stream finalized before
      // the DOM is settled by renderMessages, so no trailing token/reasoning rAF
      // can reintroduce a stale thinking card or duplicate content.
      _streamFinalized=true;
      if(_pendingRafHandle!==null){cancelAnimationFrame(_pendingRafHandle);clearTimeout(_pendingRafHandle);_pendingRafHandle=null;_renderPending=false;}
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      // Finalize smd parser — flushes any remaining buffered markdown state
      // and runs Prism + copy buttons on the live segment before the DOM is replaced
      if(assistantBody){
        const _finBody=assistantBody;
        _smdEndParser();
        requestAnimationFrame(()=>{
          if(typeof highlightCode==='function') highlightCode(_finBody);
          if(typeof addCopyButtons==='function') addCopyButtons(_finBody);
          if(typeof renderKatexBlocks==='function') renderKatexBlocks();
        });
      } else {
        _smdEndParser();
      }
      const d=JSON.parse(e.data);
      const isActiveSession=_isSessionCurrentPane(activeSid);
      const isSessionViewed=_isSessionActivelyViewed(activeSid);
      const completedSession=d.session||{session_id:activeSid};
      const completedSid=completedSession.session_id||activeSid;
      if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
        _markSessionCompletionUnread(completedSid, completedSession.message_count);
      }
      delete INFLIGHT[activeSid];
      clearInflight();clearInflightState(activeSid);
      if(typeof _markSessionCompletedInList==='function'){
        _markSessionCompletedInList(completedSession, activeSid);
      }
      stopApprovalPolling();
      stopClarifyPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
      if(isActiveSession){
        S.activeStreamId=null;
      }
      if(isActiveSession){
        // Capture previous session totals BEFORE overwriting S.session with the new
        // cumulative values from the done event. prevIn/prevOut are the totals as of
        // the start of this turn; curIn/curOut are the full post-turn totals — the
        // delta is the per-turn usage for #1159.
        const _prevIn=(S.session&&S.session.input_tokens)||0;
        const _prevOut=(S.session&&S.session.output_tokens)||0;
        const _prevCost=(S.session&&S.session.estimated_cost)||0;
        S.session=d.session;S.messages=d.session.messages||[];if(typeof _messagesTruncated!=='undefined')_messagesTruncated=!!d.session._messages_truncated;
        if(
          window._compressionUi&&window._compressionUi.automatic&&
          window._compressionUi.sessionId===activeSid&&
          d.session&&d.session.session_id
        ){
          window._compressionUi={...window._compressionUi, sessionId:d.session.session_id};
        }
        // Find the last assistant message once for both reasoning persistence and timestamp
        const lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
        // Persist reasoning trace so thinking card survives page reload
        if(reasoningText&&lastAsst&&!lastAsst.reasoning) lastAsst.reasoning=reasoningText;
        // Stamp _ts on the last assistant message if it has no timestamp
        if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp) lastAsst._ts=Date.now()/1000;
        if(d.usage){
          S.lastUsage=d.usage;_syncCtxIndicator(d.usage);
          // #503 — compute per-turn cost delta and attach to last assistant message
          if(lastAsst){
            const prevIn=_prevIn;
            const prevOut=_prevOut;
            const prevCost=_prevCost;
            const curIn=d.usage.input_tokens||0;
            const curOut=d.usage.output_tokens||0;
            const curCost=d.usage.estimated_cost||0;
            // Only set delta if values actually increased (skip no-op turns)
            if(curIn>prevIn||curOut>prevOut){
              lastAsst._turnUsage={
                input_tokens:Math.max(0,curIn-prevIn),
                output_tokens:Math.max(0,curOut-prevOut),
                estimated_cost:Math.max(0,curCost-prevCost),
              };
            }
          }
        }
        if(d.session.tool_calls&&d.session.tool_calls.length){
          S.toolCalls=d.session.tool_calls.map(tc=>({...tc,done:true}));
        } else {
          S.toolCalls=S.toolCalls.map(tc=>({...tc,done:true}));
        }
        if(uploaded.length){
          const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
          if(lastUser)lastUser.attachments=uploaded;
        }
        clearLiveToolCards();
        S.busy=false;
        // No-reply guard (#373): if agent returned nothing, show inline error
        if(!S.messages.some(m=>m.role==='assistant'&&String(m.content||'').trim())&&!assistantText){removeThinking();S.messages.push({role:'assistant',content:'**No response received.** Check your API key and model selection.'});}
        if(isSessionViewed) _markSessionViewed(completedSid, completedSession.message_count ?? S.messages.length);
        syncTopbar();renderMessages();loadDir('.');
        // TTS auto-read: speak the last assistant response if enabled (#499)
        if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);
      }
      _queueDrainSid=activeSid;renderSessionList();setBusy(false);setStatus('');
      setComposerStatus('');
      playNotificationSound();
      sendBrowserNotification('Response complete',assistantText?assistantText.slice(0,100):'Task finished');
    });

    source.addEventListener('stream_end',e=>{
      _terminalStateReached=true;
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
      }catch(_){}
      source.close();
    });

    source.addEventListener('pending_steer_leftover',e=>{
      // The agent finished its turn with steer text still stashed (no
      // tool-result boundary fired). Match the CLI's leftover-delivery
      // behaviour: queue the leftover text as a next-turn user message
      // so the existing drain in setBusy(false) ships it.
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const txt=String(d.text||'').trim();
        if(!txt||sid!==activeSid) return;
        if(typeof queueSessionMessage==='function'){
          queueSessionMessage(sid,{
            text:txt,files:[],
            model:S.session&&S.session.model||'',
            profile:S.activeProfile||'default',
          });
          if(typeof updateQueueBadge==='function') updateQueueBadge(sid);
          showToast(t('steer_leftover_queued'),3000);
        }
      }catch(_){}
    });

    source.addEventListener('compressed',e=>{
      // Context was auto-compressed during this turn. Render it through the
      // same transient compression-card path as manual /compress, without
      // inserting a fake assistant message into history or model context.
      if(!S.session||S.session.session_id!==activeSid) return;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      const message=String(d.message||'Context auto-compressed to continue the conversation').trim();
      if(typeof setCompressionUi==='function'){
        setCompressionUi({
          sessionId:activeSid,
          phase:'done',
          automatic:true,
          message,
          summary:{headline:message},
        });
      }
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(!S.busy&&typeof renderMessages==='function') renderMessages();
      showToast(message||'Context compressed');
    });

    source.addEventListener('metering',e=>{
      // TPS + HIGH/LOW stats for the header chip — emitted at 1 Hz during a stream,
      // silenced entirely when no sessions are active (ticker exits when idle).
      try{
        const d=JSON.parse(e.data||'{}');
        const el=$('tpsStat');
        if(!el) return;
        const tps=typeof d.tps==='number'?d.tps.toFixed(1):'0.0';
        const high=typeof d.high==='number' && d.high>=0?d.high.toFixed(1)+' high':'—';
        const low=typeof d.low==='number' && d.low>=0?d.low.toFixed(1)+' low':'';
        el.textContent=`${tps} t/s · ${high}${low?' · '+low:''}`;
      }catch(_){}
    });

    source.addEventListener('apperror',e=>{
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      if(_pendingRafHandle!==null){cancelAnimationFrame(_pendingRafHandle);clearTimeout(_pendingRafHandle);_pendingRafHandle=null;_renderPending=false;}
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      source.close();
      delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        clearLiveToolCards();if(!assistantText)removeThinking();
        try{
          const d=JSON.parse(e.data);
          const isRateLimit=d.type==='rate_limit';
          const isQuotaExhausted=d.type==='quota_exhausted';
          const isAuthMismatch=d.type==='auth_mismatch';
          const isModelNotFound=d.type==='model_not_found';
          const isNoResponse=d.type==='no_response';
          const label=isQuotaExhausted?'Out of credits':isRateLimit?'Rate limit reached':isAuthMismatch?(typeof t==='function'?t('provider_mismatch_label'):'Provider mismatch'):isModelNotFound?(typeof t==='function'?t('model_not_found_label'):'Model not found'):isNoResponse?'No response received':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          S.messages.push({role:'assistant',content:`**${label}:** ${d.message}${hint}`});
        }catch(_){
          S.messages.push({role:'assistant',content:'**Error:** An error occurred. Check server logs.'});
        }
        _markSessionViewed(activeSid, S.messages.length);
        renderMessages();
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        try{const d=JSON.parse(e.data);trackBackgroundError(activeSid,_errTitle,d.message||'Error');}
        catch(_){trackBackgroundError(activeSid,_errTitle,'Error');}
      }
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setComposerStatus('');}
      renderSessionList(); // clear streaming indicator immediately on apperror
    });

    source.addEventListener('warning',e=>{
      // Non-fatal warning from server (e.g. fallback activated, retrying)
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        // Show as a small inline notice, not a full error
        setComposerStatus(`${d.message||'Warning'}`);
        // If it's a fallback notice, show it briefly then clear
        if(d.type==='fallback') setTimeout(()=>setComposerStatus(''),4000);
      }catch(_){}
    });

    source.addEventListener('error',async e=>{
      source.close();
      if(_terminalStateReached || _streamFinalized){
        _closeSource();
        return;
      }
      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setComposerStatus('Reconnecting…');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setComposerStatus('Reconnected');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.href).href,{withCredentials:true}));
              return;
            }
          }catch(_){}
          if(await _restoreSettledSession()) return;
          _handleStreamError();
        },1500);
        return;
      }
      if(await _restoreSettledSession()) return;
      _handleStreamError();
    });

    source.addEventListener('cancel',e=>{
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      if(_pendingRafHandle!==null){cancelAnimationFrame(_pendingRafHandle);clearTimeout(_pendingRafHandle);_pendingRafHandle=null;_renderPending=false;}
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      source.close();
      delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true, 'cancelled');
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
      }
      // Fetch latest session from server to get accurate message list (includes cancel status)
      // This ensures messages stay in sync with server, fixing race condition where local
      // "*Task cancelled.*" message gets lost when done event overwrites S.messages
      (async()=>{
        try{
          const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
          if(data&&data.session&&S.session&&S.session.session_id===activeSid){
            S.session=data.session;
            S.messages=(data.session.messages||[]).filter(m=>m&&m.role);
            clearLiveToolCards();if(!assistantText)removeThinking();
            _markSessionViewed(activeSid, data.session.message_count ?? S.messages.length);
            renderMessages();
          }
        }catch(_){
          // Fallback to local cancel message if API fails
          if(S.session&&S.session.session_id===activeSid){
            clearLiveToolCards();if(!assistantText)removeThinking();
            S.messages.push({role:'assistant',content:'*Task cancelled.*'});renderMessages();
            _markSessionViewed(activeSid, S.messages.length);
          }
        }
      })();
      renderSessionList();
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setComposerStatus('');}
    });
  }

  async function _restoreSettledSession(){
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
      const session=data&&data.session;
      if(!session) return false;
      if(session.active_stream_id||session.pending_user_message) return false;
      delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
      _closeSource();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
      const isSessionViewed=_isSessionActivelyViewed(activeSid);
      const completedSid=session.session_id||activeSid;
      if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
        _markSessionCompletionUnread(completedSid, session.message_count);
      }
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.session=session;S.messages=(session.messages||[]).filter(m=>m&&m.role);
        const hasMessageToolMetadata=S.messages.some(m=>{
          if(!m||m.role!=='assistant') return false;
          const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
          const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
          return hasTc||hasTu;
        });
        if(!hasMessageToolMetadata&&session.tool_calls&&session.tool_calls.length){
          S.toolCalls=(session.tool_calls||[]).map(tc=>({...tc,done:true}));
        }else{
          S.toolCalls=[];
        }
        if(isSessionViewed) _markSessionViewed(completedSid, session.message_count ?? S.messages.length);
        syncTopbar();renderMessages();
      }
      _queueDrainSid=activeSid;renderSessionList();setBusy(false);setComposerStatus('');
      return true;
    }catch(_){
      return false;
    }
  }

  function _handleStreamError(){
    // Opus review Q1: mirror done/apperror/cancel finalization so any pending rAF
    // cannot fire after renderMessages() has settled the DOM with the error message.
    if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
    _streamFinalized=true;
    if(_pendingRafHandle!==null){cancelAnimationFrame(_pendingRafHandle);clearTimeout(_pendingRafHandle);_pendingRafHandle=null;_renderPending=false;}
    if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
    delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
    _closeSource();
    if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
    if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;
      clearLiveToolCards();if(!assistantText)removeThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost'});renderMessages();
      _markSessionViewed(activeSid, S.messages.length);
    }else{
      if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setComposerStatus('');}
  }

  (async()=>{
    // Reattach path can carry stale stream ids after server restart; preflight
    // status avoids opening a dead SSE URL that will 404 in the console.
    if(reconnecting){
      try{
        const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
        if(!st.active){
          delete INFLIGHT[activeSid];
          clearInflight();
          clearInflightState(activeSid);
          stopApprovalPolling();
          stopClarifyPolling();
          if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
          if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
          if(S.session&&S.session.session_id===activeSid){
            S.activeStreamId=null;
            clearLiveToolCards();
            removeThinking();
            _queueDrainSid=activeSid;setBusy(false);
            setComposerStatus('');
            renderMessages();
            renderSessionList();
          }
          return;
        }
      }catch(_){}
    }
    _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.href).href,{withCredentials:true}));
  })();

}

function transcript(){
  const lines=[`# Hermes session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    lines.push(`## ${m.role}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

function autoResize(){const el=$('msg');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px';updateSendBtn();}


// ── YOLO mode state ──
// Session-scoped; stored server-side in memory (tools/approval.py).
// Lifecycle:
//   • Page reload: state PERSISTS — _fetchYoloState() re-syncs from backend.
//   • Cross-tab: state is SHARED — enabling YOLO in Tab A affects Tab B for
//     the same session (both poll the same server-side flag).
//   • Server restart: state is LOST — in-memory only, not persisted to disk.
//   • Session switch: state resets — loadSession() clears _yoloEnabled and
//     fetches the new session's state.
let _yoloEnabled = false;

async function _fetchYoloState(sid) {
  try {
    const data = await api('/api/session/yolo?session_id=' + encodeURIComponent(sid));
    _yoloEnabled = !!data.yolo_enabled;
    _updateYoloPill();
  } catch (_) { /* ignore */ }
}

function _updateYoloPill() {
  const pill = $('yoloPill');
  if (!pill) return;
  pill.style.display = _yoloEnabled ? '' : 'none';
  if (_yoloEnabled) {
    pill.title = t('yolo_pill_title_active');
    pill.setAttribute('data-i18n-title', 'yolo_pill_title_active');
  }
  if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM();
}

async function toggleYoloFromApproval() {
  const sid = S.session && S.session.session_id;
  if (!sid) return;
  try {
    await api('/api/session/yolo', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, enabled: true }),
    });
    _yoloEnabled = true;
    _updateYoloPill();
    hideApprovalCard(true);
    showToast(t('yolo_enabled'));
  } catch (e) { showToast('YOLO: ' + e.message); }
}

// ── Approval polling ──
let _approvalPollTimer = null;
let _approvalHideTimer = null;
let _approvalVisibleSince = 0;
let _approvalSignature = '';
const APPROVAL_MIN_VISIBLE_MS = 30000;

// showApprovalCard moved above respondApproval

function _clearApprovalHideTimer() {
  if (_approvalHideTimer) {
    clearTimeout(_approvalHideTimer);
    _approvalHideTimer = null;
  }
}

function _resetApprovalCardState() {
  _clearApprovalHideTimer();
  _approvalVisibleSince = 0;
  _approvalSignature = '';
}

function hideApprovalCard(force=false) {
  const card = $("approvalCard");
  if (!card) return;
  if (!force && _approvalVisibleSince) {
    const remaining = APPROVAL_MIN_VISIBLE_MS - (Date.now() - _approvalVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _approvalSignature;
      _clearApprovalHideTimer();
      _approvalHideTimer = setTimeout(() => {
        _approvalHideTimer = null;
        if (_approvalSignature !== scheduledSignature) return;
        hideApprovalCard(true);
      }, remaining);
      return;
    }
  }
  _approvalSessionId = null;
  _resetApprovalCardState();
  card.classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
let _approvalSessionId = null;
let _approvalCurrentId = null;  // approval_id of the card currently shown

function showApprovalCard(pending, pendingCount) {
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  const desc = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  const cmd = pending.command || "";
  const sig = JSON.stringify({desc, cmd, sid: pending._session_id || (S.session && S.session.session_id) || null});
  const card = $("approvalCard");
  const sameApproval = card.classList.contains("visible") && _approvalSignature === sig;
  $("approvalDesc").textContent = desc;
  $("approvalCmd").textContent = cmd;
  _approvalSessionId = pending._session_id || (S.session && S.session.session_id) || null;
  _approvalCurrentId = pending.approval_id || null;
  _approvalSignature = sig;
  // Show "1 of N" counter when multiple approvals are queued
  const counter = $("approvalCounter");
  if (counter) {
    if (pendingCount && pendingCount > 1) {
      counter.textContent = "1 of " + pendingCount + " pending";
      counter.style.display = "";
    } else {
      counter.style.display = "none";
    }
  }
  if (!sameApproval) {
    _approvalVisibleSince = Date.now();
    _clearApprovalHideTimer();
  }
  // Re-enable buttons in case a previous approval disabled them
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id); if (b) { b.disabled = false; b.classList.remove("loading"); }
  });
  card.classList.add("visible");
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  const onceBtn = $("approvalBtnOnce");
  if (onceBtn) setTimeout(() => onceBtn.focus({preventScroll: true}), 50);
}

async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const approvalId = _approvalCurrentId;
  // Disable all buttons immediately to prevent double-submit
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id);
    if (b) { b.disabled = true; if (b.id === "approvalBtn" + choice.charAt(0).toUpperCase() + choice.slice(1)) b.classList.add("loading"); }
  });
  _approvalSessionId = null;
  _approvalCurrentId = null;
  hideApprovalCard(true);
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice, approval_id: approvalId })
    });
  } catch(e) { setStatus(t("approval_responding") + " " + e.message); }
}

function startApprovalPolling(sid) {
  stopApprovalPolling();
  // ── SSE (preferred): long-lived connection, server pushes instantly ──
  try {
    const es = new EventSource('/api/approval/stream?session_id=' + encodeURIComponent(sid));
    let _fallbackActive = false;

    es.addEventListener('initial', e => {
      const d = JSON.parse(e.data);
      if (d.pending) { d.pending._session_id = sid; showApprovalCard(d.pending, d.pending_count || 1); }
      else { hideApprovalCard(); }
    });

    es.addEventListener('approval', e => {
      const d = JSON.parse(e.data);
      if (d.pending) { d.pending._session_id = sid; showApprovalCard(d.pending, d.pending_count || 1); }
      else { hideApprovalCard(); }
    });

    es.onerror = () => {
      // SSE failed — fall back to HTTP polling (3s interval)
      if (_fallbackActive) return;
      _fallbackActive = true;
      try { es.close(); } catch(_){}
      _startApprovalFallbackPoll(sid);
    };

    // If the session changes or stops being busy, close the SSE.
    // We detect this via a periodic check (cheap — no network request).
    _approvalSSEHealthTimer = setInterval(() => {
      if (!S.busy || !S.session || S.session.session_id !== sid) {
        stopApprovalPolling(); hideApprovalCard(true);
      }
    }, 5000);

    _approvalEventSource = es;
  } catch(_e) {
    // EventSource constructor failed — use polling directly
    _startApprovalFallbackPoll(sid);
  }
}

let _approvalEventSource = null;
let _approvalSSEHealthTimer = null;

function _startApprovalFallbackPoll(sid) {
  _approvalPollTimer = setInterval(async () => {
    if (!S.busy || !S.session || S.session.session_id !== sid) {
      stopApprovalPolling(); hideApprovalCard(true); return;
    }
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showApprovalCard(data.pending, data.pending_count||1); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 3000);  // 3s fallback interval (was 1.5s when it was the primary path)
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
  if (_approvalEventSource) { try { _approvalEventSource.close(); } catch(_){} _approvalEventSource = null; }
  if (_approvalSSEHealthTimer) { clearInterval(_approvalSSEHealthTimer); _approvalSSEHealthTimer = null; }
}

// ── Clarify polling ──
let _clarifyPollTimer = null;
let _clarifyHideTimer = null;
let _clarifyVisibleSince = 0;
let _clarifySignature = '';
let _clarifySessionId = null;
let _clarifyMissingEndpointWarned = false;
let _clarifyCountdownTimer = null;
let _clarifyExpiresAt = 0;
const CLARIFY_MIN_VISIBLE_MS = 30000;

function _ensureClarifyCardDom() {
  let card = $("clarifyCard");
  if (card) return card;
  const host = $("msgInner") || $("messages");
  if (!host) return null;
  card = document.createElement("div");
  card.className = "clarify-card";
  card.id = "clarifyCard";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-labelledby", "clarifyHeading");
  card.setAttribute("aria-describedby", "clarifyQuestion clarifyHint");
  card.innerHTML = `
    <div class="clarify-inner">
      <div class="clarify-header">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 17h.01"/><path d="M9.09 9a3 3 0 1 1 5.82 1c0 2-3 2-3 4"/><circle cx="12" cy="12" r="10"/></svg>
        <span id="clarifyHeading" data-i18n="clarify_heading">Clarification needed</span>
        <span class="clarify-countdown" id="clarifyCountdown"></span>
      </div>
      <div class="clarify-question" id="clarifyQuestion"></div>
      <div class="clarify-choices" id="clarifyChoices"></div>
      <div class="clarify-response">
        <input class="clarify-input" id="clarifyInput" type="text" data-i18n-placeholder="clarify_input_placeholder" placeholder="Type your response…">
        <button class="clarify-submit" id="clarifySubmit" data-i18n="clarify_send">Send</button>
      </div>
      <div class="clarify-hint" id="clarifyHint" data-i18n="clarify_hint">Please choose one option, or type your own response below.</div>
    </div>
  `;
  host.appendChild(card);
  const submit = $("clarifySubmit");
  if (submit) submit.onclick = () => respondClarify();
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  return card;
}

function _clearClarifyHideTimer() {
  if (_clarifyHideTimer) {
    clearTimeout(_clarifyHideTimer);
    _clarifyHideTimer = null;
  }
}

function _clearClarifyCountdownTimer() {
  if (_clarifyCountdownTimer) {
    clearInterval(_clarifyCountdownTimer);
    _clarifyCountdownTimer = null;
  }
  _clarifyExpiresAt = 0;
  const countdown = $("clarifyCountdown");
  if (countdown) {
    countdown.textContent = "";
    countdown.classList.remove("urgent");
  }
}

function _clarifyExpiryMs(pending) {
  const expiresAt = Number(pending && pending.expires_at);
  if (Number.isFinite(expiresAt) && expiresAt > 0) return expiresAt * 1000;
  const requestedAt = Number(pending && pending.requested_at);
  const timeoutSeconds = Number(pending && pending.timeout_seconds);
  if (Number.isFinite(requestedAt) && Number.isFinite(timeoutSeconds)) {
    return (requestedAt + timeoutSeconds) * 1000;
  }
  return 0;
}

function _updateClarifyCountdown() {
  const countdown = $("clarifyCountdown");
  if (!countdown || !_clarifyExpiresAt) return;
  const remaining = Math.max(0, Math.ceil((_clarifyExpiresAt - Date.now()) / 1000));
  countdown.textContent = `${remaining}s`;
  countdown.classList.toggle("urgent", remaining <= 10);
}

function _startClarifyCountdown(pending) {
  const expiresAt = _clarifyExpiryMs(pending);
  if (_clarifyCountdownTimer && _clarifyExpiresAt === expiresAt) return;
  _clearClarifyCountdownTimer();
  _clarifyExpiresAt = expiresAt;
  if (!_clarifyExpiresAt) return;
  _updateClarifyCountdown();
  _clarifyCountdownTimer = setInterval(_updateClarifyCountdown, 1000);
}

function _stashClarifyDraft(reason) {
  if (reason !== "expired" && reason !== "terminal") return false;
  const input = $("clarifyInput");
  const draft = String((input && input.value) || "").trim();
  if (!draft) return false;
  const sid = _clarifySessionId || (S.session && S.session.session_id) || "unknown";
  const key = `hermes-clarify-draft-${sid}-${_clarifySignature || "unknown"}`;
  try {
    sessionStorage.setItem(key, JSON.stringify({
      draft,
      reason,
      saved_at: Date.now(),
    }));
  } catch (_) {}
  const composer = $('msg');
  if (composer) {
    const current = String(composer.value || "");
    composer.value = current.trim() ? `${current.replace(/\s+$/, "")}\n\n${draft}` : draft;
    if (typeof autoResize === "function") autoResize();
    if (typeof updateSendBtn === "function") updateSendBtn();
  }
  const notice = reason === "expired"
    ? "Clarification timed out. Your draft was kept in the composer."
    : "Clarification closed. Your draft was kept in the composer.";
  if (typeof setComposerStatus === "function") setComposerStatus(notice);
  else if (typeof setStatus === "function") setStatus(notice);
  if (typeof showToast === "function") showToast(notice, 5000);
  return true;
}

function _resetClarifyCardState() {
  _clearClarifyHideTimer();
  _clearClarifyCountdownTimer();
  _clarifyVisibleSince = 0;
  _clarifySignature = '';
}

function hideClarifyCard(force=false, reason="dismissed") {
  const card = $("clarifyCard");
  if (!card) {
    _clarifySessionId = null;
    _resetClarifyCardState();
    if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
    return;
  }
  if (!force && reason !== "expired" && _clarifyVisibleSince) {
    const remaining = CLARIFY_MIN_VISIBLE_MS - (Date.now() - _clarifyVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _clarifySignature;
      _clearClarifyHideTimer();
      _clarifyHideTimer = setTimeout(() => {
        _clarifyHideTimer = null;
        if (_clarifySignature !== scheduledSignature) return;
        hideClarifyCard(true, reason);
      }, remaining);
      return;
    }
  }
  _stashClarifyDraft(reason);
  _clarifySessionId = null;
  _resetClarifyCardState();
  card.classList.remove("visible");
  if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
  $("clarifyQuestion").textContent = "";
  $("clarifyChoices").innerHTML = "";
  $("clarifyInput").value = "";
  $("clarifyInput").disabled = false;
  $("clarifyInput").onkeydown = null;
  const submit = $("clarifySubmit");
  if (submit) { submit.disabled = false; submit.classList.remove("loading"); }
}

function _clarifySetControlsDisabled(disabled, loading=false) {
  const input = $("clarifyInput");
  const submit = $("clarifySubmit");
  if (input) input.disabled = disabled;
  if (submit) {
    submit.disabled = disabled;
    submit.classList.toggle("loading", !!loading);
  }
  const choices = $("clarifyChoices");
  if (choices) {
    choices.querySelectorAll("button").forEach(btn => {
      btn.disabled = disabled;
      if (loading && btn.dataset && btn.dataset.choice === "other") {
        btn.classList.toggle("loading", false);
      }
    });
  }
}

function showClarifyCard(pending) {
  const question = pending.question || pending.description || '';
  const choices = Array.isArray(pending.choices_offered)
    ? pending.choices_offered
    : (Array.isArray(pending.choices) ? pending.choices : []);
  const sig = JSON.stringify({
    question,
    choices,
    sid: pending._session_id || (S.session && S.session.session_id) || null,
  });
  const card = _ensureClarifyCardDom();
  if (!card) return;
  const questionEl = $("clarifyQuestion");
  const choicesEl = $("clarifyChoices");
  const input = $("clarifyInput");
  const sameClarify = card.classList.contains("visible") && _clarifySignature === sig;
  _clarifySessionId = pending._session_id || (S.session && S.session.session_id) || null;
  _clarifySignature = sig;
  _startClarifyCountdown(pending);
  if (!sameClarify) {
    _clarifyVisibleSince = Date.now();
    _clearClarifyHideTimer();
  }
  if (questionEl) questionEl.textContent = question;
  if (choicesEl) {
    choicesEl.innerHTML = '';
    choicesEl.style.display = choices.length ? '' : 'none';
    if (choices.length) {
      choices.forEach((choice, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'clarify-choice';
        btn.dataset.choice = choice;
        btn.onclick = () => respondClarify(choice);
        const badge = document.createElement('span');
        badge.className = 'clarify-choice-badge';
        badge.textContent = String(idx + 1);
        const text = document.createElement('span');
        text.className = 'clarify-choice-text';
        text.textContent = choice;
        btn.appendChild(badge);
        btn.appendChild(text);
        choicesEl.appendChild(btn);
      });
      const other = document.createElement('button');
      other.type = 'button';
      other.className = 'clarify-choice other';
      other.dataset.choice = 'other';
      other.setAttribute('data-i18n', 'clarify_other');
      const otherBadge = document.createElement('span');
      otherBadge.className = 'clarify-choice-badge other';
      otherBadge.textContent = '•';
      const otherText = document.createElement('span');
      otherText.className = 'clarify-choice-text';
      otherText.textContent = t('clarify_other') || 'Other';
      other.appendChild(otherBadge);
      other.appendChild(otherText);
      other.onclick = () => {
        const el = $("clarifyInput");
        if (el) {
          el.focus();
          if (typeof el.select === 'function') el.select();
        }
      };
      choicesEl.appendChild(other);
    }
  }
  if (input) {
    if (!sameClarify) input.value = '';
    input.disabled = false;
    input.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        respondClarify();
      }
    };
  }
  if (typeof lockComposerForClarify === "function") {
    lockComposerForClarify(question ? `Clarification needed: ${question}` : "Clarification needed");
  }
  _clarifySetControlsDisabled(false, false);
  card.classList.add("visible");
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  if (input && !sameClarify) setTimeout(() => input.focus({preventScroll: true}), 50);
}

async function respondClarify(response) {
  const sid = _clarifySessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const input = $("clarifyInput");
  let value = typeof response === 'string' ? response : (input ? input.value : '');
  value = String(value || '').trim();
  if (!value) {
    if (input) input.focus();
    return;
  }
  _clarifySessionId = null;
  _clarifySetControlsDisabled(true, true);
  hideClarifyCard(true, 'sent');
  try {
    await api("/api/clarify/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, response: value })
    });
  } catch(e) { setStatus(t("clarify_responding") + " " + e.message); }
}

function startClarifyPolling(sid) {
  stopClarifyPolling();
  _clarifyMissingEndpointWarned = false;
  _clarifyPollTimer = setInterval(async () => {
    if (!S.session || S.session.session_id !== sid) {
      stopClarifyPolling(); hideClarifyCard(true, 'session'); return;
    }
    try {
      const data = await api("/api/clarify/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showClarifyCard(data.pending); }
      else { hideClarifyCard(false, 'expired'); }
    } catch(e) {
      const msg = String((e && e.message) || "");
      if (!_clarifyMissingEndpointWarned && /(^|\b)(404|not found)(\b|$)/i.test(msg)) {
        _clarifyMissingEndpointWarned = true;
        setComposerStatus("Clarify unavailable on current server build. Restart server.");
        if (typeof showToast === "function") {
          showToast("Clarify endpoint unavailable. Please restart server.", 5000);
        }
        stopClarifyPolling();
      }
      // Ignore transient poll errors; SSE clarify event still provides a fast path.
    }
  }, 1500);
}

function stopClarifyPolling() {
  if (_clarifyPollTimer) { clearInterval(_clarifyPollTimer); _clarifyPollTimer = null; }
}

// ── Notifications and Sound ──────────────────────────────────────────────────

function playNotificationSound(){
  if(!window._soundEnabled) return;
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.type='sine';osc.frequency.setValueAtTime(660,ctx.currentTime);
    osc.frequency.setValueAtTime(880,ctx.currentTime+0.1);
    gain.gain.setValueAtTime(0.3,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+0.3);
    osc.start(ctx.currentTime);osc.stop(ctx.currentTime+0.3);
    osc.onended=()=>ctx.close();
  }catch(e){console.warn('Notification sound failed:',e);}
}

function sendBrowserNotification(title,body){
  if(!window._notificationsEnabled||!document.hidden) return;
  if(!('Notification' in window)) return;
  const botName=window._botName||'Hermes';
  if(Notification.permission==='granted'){
    new Notification(title||botName,{body:body});
  }else if(Notification.permission!=='denied'){
    Notification.requestPermission().then(p=>{
      if(p==='granted') new Notification(title||botName,{body:body});
    });
  }
}

// ── /btw ephemeral stream ────────────────────────────────────────────────────
// Connects to the ephemeral SSE stream from /api/btw and renders the answer
// in a visually distinct bubble that is NOT persisted to session history.

function attachBtwStream(parentSid, streamId, question){
  if(!parentSid||!streamId) return;
  const src=new EventSource('/api/chat/stream?stream_id='+encodeURIComponent(streamId));
  let answer='';
  let btwRow=null;
  let _streamDone=false;
  function _ensureBtwRow(){
    if(btwRow&&btwRow.isConnected) return;
    const inner=$('msgInner');
    if(!inner) return;
    btwRow=document.createElement('div');
    btwRow.className='msg-row msg-row-btw';
    btwRow.dataset.role='assistant';
    btwRow.dataset.btw='1';
    const labelEl=document.createElement('div');
    labelEl.className='msg-btw-label';
    labelEl.textContent=t('btw_label');
    const qEl=document.createElement('div');
    qEl.className='msg-body';
    qEl.textContent=question;
    const ansEl=document.createElement('div');
    ansEl.className='msg-body msg-btw-answer';
    ansEl.textContent='...';
    btwRow.appendChild(labelEl);
    btwRow.appendChild(qEl);
    btwRow.appendChild(ansEl);
    inner.appendChild(btwRow);
    btwRow.scrollIntoView({behavior:'smooth',block:'end'});
  }
  src.addEventListener('token',e=>{
    try{answer+=JSON.parse(e.data).text||'';}catch(_){}
    _ensureBtwRow();
    const ansEl=btwRow&&btwRow.querySelector('.msg-btw-answer');
    if(ansEl) ansEl.innerHTML=renderMd(answer);
  });
  src.addEventListener('done',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      if(d.answer&&!answer) answer=d.answer;
    }catch(_){}
    if(S.session&&S.session.session_id===parentSid) _ensureBtwRow();
    if(btwRow&&btwRow.isConnected){
      const ansEl=btwRow.querySelector('.msg-btw-answer');
      if(ansEl) ansEl.innerHTML=renderMd(answer||t('btw_no_answer'));
    }
    showToast(t('btw_done'));
  });
  src.addEventListener('apperror',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      showToast(t('btw_failed')+(d.message||''));
    }catch(_){showToast(t('btw_failed'));}
    if(btwRow&&btwRow.isConnected) btwRow.remove();
  });
  src.addEventListener('stream_end',()=>{_streamDone=true;src.close();});
  src.onerror=()=>{src.close();if(!_streamDone&&btwRow&&btwRow.isConnected) btwRow.remove();};
}

// ── /background task tracking ────────────────────────────────────────────────

let _bgPollTimers={};
let _bgActiveTasks=new Set();

function showBackgroundBadge(taskId){
  _bgActiveTasks.add(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function hideBackgroundBadge(taskId){
  _bgActiveTasks.delete(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function startBackgroundPolling(parentSid, taskId, prompt){
  if(_bgPollTimers[taskId]) return;
  async function _poll(){
    try{
      const r=await api('/api/background/status?session_id='+encodeURIComponent(parentSid));
      if(r&&r.results){
        for(const res of r.results){
          if(res.task_id===taskId){
            hideBackgroundBadge(taskId);
            delete _bgPollTimers[taskId];
            const msg={role:'assistant',content:`**${t('bg_label')}** ${prompt.slice(0,80)}\n\n${res.answer||t('bg_no_answer')}`,'_background':true,_ts:Date.now()/1000};
            S.messages.push(msg);
            renderMessages();
            showToast(t('bg_complete'));
            return;
          }
        }
      }
    }catch(_){}
    _bgPollTimers[taskId]=setTimeout(_poll,3000);
  }
  _poll();
}

// ── Panel navigation (Chat / Tasks / Skills / Memory) ──
