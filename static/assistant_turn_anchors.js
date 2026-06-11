// Stable Assistant Turn Anchors scaffold (#3926).
//
// This file is intentionally inert: it defines the current ownership inventory,
// event classifications, and small pure helpers, but it does not register
// anchors or change any renderer. Later phases can wire these helpers into
// send(), attachLiveStream(), replay hydration, and renderMessages().
(function(){
  const ROOT=(typeof window!=='undefined')?window:globalThis;

  const ACTIVITY_EVENT_KINDS=Object.freeze([
    'process_prose',
    'reasoning',
    'tool_started',
    'tool_updated',
    'tool_completed',
    'lifecycle_status',
    'control_boundary',
    'terminal_status',
  ]);

  const STATE_LAYERS=Object.freeze([
    Object.freeze({
      id:'event_envelope',
      label:'RuntimeAdapter / run-journal Event Envelope',
      currentSurface:'event_id, run_id, seq, Last-Event-ID / after_seq',
      role:'durable_identity',
      authorityRank:1,
      anchorPolicy:'Anchor identity and replay dedupe must consume this first.',
    }),
    Object.freeze({
      id:'run_journal',
      label:'Run journal replay events',
      currentSurface:'read_run_events(), _replay_run_journal, runtime_journal_snapshot',
      role:'durable_replay',
      authorityRank:2,
      anchorPolicy:'Replay hydration should rebuild activity events from this before caches.',
    }),
    Object.freeze({
      id:'settled_transcript',
      label:'Server settled transcript messages',
      currentSurface:'/api/session messages and message metadata',
      role:'durable_settlement',
      authorityRank:3,
      anchorPolicy:'Settlement updates the existing anchor final answer and terminal state.',
    }),
    Object.freeze({
      id:'S.messages',
      label:'Browser transcript projection',
      currentSurface:'S.messages consumed by renderMessages()',
      role:'projection_cache',
      authorityRank:4,
      anchorPolicy:'Projection input/output, not a second owner for one assistant turn.',
    }),
    Object.freeze({
      id:'INFLIGHT',
      label:'Browser in-flight recovery cache',
      currentSurface:'INFLIGHT[session_id], localStorage persisted in-flight state',
      role:'recovery_cache',
      authorityRank:5,
      anchorPolicy:'Recovery fallback only; must not outrank journal or settled transcript.',
    }),
    Object.freeze({
      id:'stream_closure',
      label:'attachLiveStream closure-local state',
      currentSurface:'assistantText, reasoningText, parser targets, live tool state',
      role:'hot_path_cache',
      authorityRank:6,
      anchorPolicy:'Hot-path write buffer; normalize into anchor events as the stream advances.',
    }),
    Object.freeze({
      id:'live_dom',
      label:'Live DOM / Worklog nodes',
      currentSurface:'#liveAssistantTurn, tool-card rows, Thinking cards',
      role:'renderer_output',
      authorityRank:7,
      anchorPolicy:'DOM continuity is useful, but DOM is never semantic truth.',
    }),
  ]);

  const SOURCE_EVENT_CLASSIFICATION=Object.freeze({
    token:Object.freeze({classification:'activity',kind:'process_prose',source:'sse'}),
    interim_assistant:Object.freeze({classification:'activity',kind:'process_prose',source:'sse'}),
    reasoning:Object.freeze({classification:'activity',kind:'reasoning',source:'sse'}),
    tool:Object.freeze({classification:'activity',kind:'tool_started',source:'sse'}),
    tool_complete:Object.freeze({classification:'activity',kind:'tool_completed',source:'sse'}),
    tool_update:Object.freeze({classification:'activity',kind:'tool_updated',source:'future_sse'}),
    compressing:Object.freeze({classification:'activity',kind:'lifecycle_status',source:'sse'}),
    compressed:Object.freeze({classification:'activity',kind:'lifecycle_status',source:'sse'}),
    approval:Object.freeze({classification:'activity',kind:'control_boundary',source:'sse'}),
    clarify:Object.freeze({classification:'activity',kind:'control_boundary',source:'sse'}),
    pending_steer_leftover:Object.freeze({classification:'activity',kind:'control_boundary',source:'sse'}),
    goal_continue:Object.freeze({classification:'activity',kind:'control_boundary',source:'sse'}),
    artifact_reference:Object.freeze({classification:'artifact',kind:'artifact_reference',source:'derived'}),
    state_saved:Object.freeze({classification:'side_effect',kind:null,source:'sse'}),
    usage:Object.freeze({classification:'metadata',kind:null,source:'settlement'}),
    title:Object.freeze({classification:'metadata',kind:null,source:'settlement'}),
    done:Object.freeze({classification:'activity',kind:'terminal_status',source:'sse'}),
    cancel:Object.freeze({classification:'activity',kind:'terminal_status',source:'sse'}),
    error:Object.freeze({classification:'activity',kind:'terminal_status',source:'sse'}),
    apperror:Object.freeze({classification:'activity',kind:'terminal_status',source:'sse'}),
    stream_end:Object.freeze({classification:'transport',kind:null,source:'sse'}),
    runtime_journal_snapshot:Object.freeze({classification:'metadata',kind:null,source:'session_payload'}),
    inflight_snapshot:Object.freeze({classification:'metadata',kind:null,source:'browser_storage'}),
    settled_message:Object.freeze({classification:'metadata',kind:null,source:'session_payload'}),
  });

  const CLASSIFICATION_ORDER=Object.freeze([
    'activity',
    'artifact',
    'side_effect',
    'metadata',
    'transport',
    'excluded',
  ]);

  function _cleanString(value){
    return typeof value==='string'?value.trim():'';
  }

  function _coercePayload(value){
    if(value==null) return {};
    if(typeof value==='string'){
      const raw=value.trim();
      if(!raw) return {};
      try{
        const parsed=JSON.parse(raw);
        return parsed&&typeof parsed==='object'?parsed:{value:parsed};
      }catch(_){
        return {text:value};
      }
    }
    if(typeof value==='object') return value;
    return {value};
  }

  function _sanitizePayload(value, depth=0){
    if(value==null) return value;
    const type=typeof value;
    if(type==='string'||type==='number'||type==='boolean') return value;
    if(type==='bigint') return String(value);
    if(type!=='object') return undefined;
    if(depth>=6) return '[MaxDepth]';
    if(Array.isArray(value)){
      return value.map((item)=>_sanitizePayload(item,depth+1)).filter((item)=>item!==undefined);
    }
    const proto=Object.getPrototypeOf(value);
    if(proto!==null&&Object.prototype.toString.call(value)!=='[object Object]') return '[Object]';
    const out={};
    Object.keys(value).sort().forEach((key)=>{
      const safe=_sanitizePayload(value[key],depth+1);
      if(safe!==undefined) out[key]=safe;
    });
    return out;
  }

  function _coerceSeq(value){
    if(value==null||value==='') return null;
    const str=String(value);
    const numeric=Number(str);
    return Number.isFinite(numeric)?numeric:str;
  }

  function _eventIdSeq(eventId){
    const raw=_cleanString(eventId);
    if(!raw||!raw.includes(':')) return null;
    return _coerceSeq(raw.slice(raw.lastIndexOf(':')+1));
  }

  function _eventIdRunId(eventId){
    const raw=_cleanString(eventId);
    if(!raw||!raw.includes(':')) return '';
    return raw.slice(0,raw.lastIndexOf(':'));
  }

  function _sourceEventType(input, payload){
    return _cleanString(input&&(
      input.source_event_type||
      input.sourceType||
      input.source_type||
      input.event_type||
      input.type||
      input.event
    )) || _cleanString(payload&&(payload.source_event_type||payload.type||payload.event));
  }

  function _sourceEventPayload(input){
    if(!input||typeof input!=='object') return {};
    if(Object.prototype.hasOwnProperty.call(input,'payload')) return _coercePayload(input.payload);
    if(Object.prototype.hasOwnProperty.call(input,'data')) return _coercePayload(input.data);
    const payload={};
    const reserved=new Set([
      'source_event_type',
      'sourceType',
      'source_type',
      'event_type',
      'type',
      'event',
      'event_id',
      'lastEventId',
      'last_event_id',
      'seq',
      'session_id',
      'turn_id',
      'run_id',
      'stream_id',
      'created_at',
      'timestamp',
    ]);
    Object.keys(input).forEach((key)=>{
      if(!reserved.has(key)) payload[key]=input[key];
    });
    return payload;
  }

  function _statusForSourceEvent(sourceType, kind, payload){
    const explicit=_cleanString(payload&&(payload.status||payload.state||payload.phase));
    if(explicit) return explicit;
    if(kind==='tool_started') return 'running';
    if(kind==='tool_completed') return payload&&payload.is_error?'error':'completed';
    if(kind==='terminal_status'){
      if(sourceType==='done') return 'completed';
      if(sourceType==='cancel') return 'cancelled';
      return 'error';
    }
    if(kind==='lifecycle_status') return 'running';
    if(kind==='control_boundary') return 'pending';
    if(sourceType==='stream_end') return 'transport_closed';
    return null;
  }

  function _localIdForSourceEvent(sourceType, context, payload){
    const explicit=_cleanString(
      (context&&context.local_id)||
      (payload&&(payload.local_id||payload.id||payload.tid||payload.tool_call_id||payload.tool_use_id||payload.call_id))
    );
    if(explicit) return explicit;
    const sessionId=_cleanString(context&&context.session_id)||'session';
    const turnId=_cleanString(context&&context.turn_id)||'turn';
    const seq=(context&&context.seq!=null&&context.seq!=='')?String(context.seq):'pending';
    return [sessionId,turnId,sourceType||'event',seq].join(':');
  }

  function assistantTurnAnchorEventDedupeKey(event){
    if(!event||typeof event!=='object') return '';
    const eventId=_cleanString(event.event_id);
    if(eventId) return 'event_id:'+eventId;
    const runId=_cleanString(event.run_id);
    const seq=(event.seq!=null&&event.seq!=='')?String(event.seq):'';
    if(runId&&seq) return 'run_seq:'+runId+':'+seq;
    const sid=_cleanString(event.session_id);
    const localId=_cleanString(event.local_id);
    if(sid&&localId) return 'local:'+sid+':'+localId;
    return '';
  }

  function classifyAssistantTurnAnchorSourceEvent(sourceType){
    const key=_cleanString(sourceType);
    return SOURCE_EVENT_CLASSIFICATION[key]||Object.freeze({
      classification:'excluded',
      kind:null,
      source:key||'unknown',
    });
  }

  function isAssistantTurnAnchorActivityKind(kind){
    return ACTIVITY_EVENT_KINDS.indexOf(kind)!==-1;
  }

  function normalizeAssistantTurnAnchorSourceEvent(input, context){
    const event=(input&&typeof input==='object')?input:{};
    const ctx=(context&&typeof context==='object')?context:{};
    const sanitizedPayload=_sanitizePayload(_sourceEventPayload(event));
    const rawPayload=(sanitizedPayload&&typeof sanitizedPayload==='object'&&!Array.isArray(sanitizedPayload))?sanitizedPayload:{};
    const {
      session_id:_payloadSessionId,
      turn_id:_payloadTurnId,
      run_id:_payloadRunId,
      stream_id:_payloadStreamId,
      event_id:_payloadEventId,
      seq:_payloadSeq,
      ...payload
    }=rawPayload;
    const sourceType=_sourceEventType(event,payload);
    const meta=classifyAssistantTurnAnchorSourceEvent(sourceType);
    const classification=meta.classification;
    if(classification==='excluded'){
      return Object.freeze({
        classification,
        source_event_type:sourceType||'unknown',
        anchor_event:null,
        dedupe_key:'',
      });
    }
    const eventId=_cleanString(event.event_id||event.lastEventId||event.last_event_id||rawPayload.event_id);
    const seq=_coerceSeq(
      event.seq!==undefined?event.seq:
        rawPayload.seq!==undefined?rawPayload.seq:
          ctx.seq!==undefined?ctx.seq:
            _eventIdSeq(eventId)
    );
    const runId=_cleanString(event.run_id||rawPayload.run_id||ctx.run_id)||_eventIdRunId(eventId)||null;
    const sessionId=_cleanString(event.session_id||rawPayload.session_id||ctx.session_id);
    const turnId=_cleanString(event.turn_id||rawPayload.turn_id||ctx.turn_id);
    const streamId=_cleanString(event.stream_id||rawPayload.stream_id||ctx.stream_id)||null;
    const localId=_localIdForSourceEvent(sourceType, {...ctx,seq}, payload);
    const anchorEvent={
      event_id:eventId||null,
      local_id:localId,
      session_id:sessionId||null,
      turn_id:turnId||null,
      run_id:runId,
      stream_id:streamId,
      seq,
      kind:meta.kind,
      source_event_type:sourceType,
      created_at:event.created_at||event.timestamp||payload.created_at||payload.ts||ctx.created_at||null,
      status:_statusForSourceEvent(sourceType,meta.kind,payload),
      payload,
    };
    const dedupeKey=assistantTurnAnchorEventDedupeKey(anchorEvent);
    return Object.freeze({
      classification,
      source_event_type:sourceType,
      anchor_event:Object.freeze(anchorEvent),
      dedupe_key:dedupeKey,
    });
  }

  function normalizeAssistantTurnAnchorSourceEvents(events, context){
    const list=Array.isArray(events)?events:[];
    const out=[];
    const seen=new Set();
    list.forEach((event)=>{
      const normalized=normalizeAssistantTurnAnchorSourceEvent(event,context);
      if(!normalized.anchor_event) return;
      const key=normalized.dedupe_key;
      if(key&&seen.has(key)) return;
      if(key) seen.add(key);
      out.push(normalized);
    });
    return out;
  }

  function createAssistantTurnAnchorSeed(input){
    const opts=(input&&typeof input==='object')?input:{};
    const sessionId=_cleanString(opts.session_id);
    if(!sessionId) throw new Error('assistant turn anchor requires session_id');
    const streamId=_cleanString(opts.stream_id);
    const runId=_cleanString(opts.run_id);
    const turnId=_cleanString(opts.turn_id)||[
      'local',
      sessionId,
      runId||streamId||'pending',
      _cleanString(opts.local_id)||'assistant',
    ].join(':');
    return {
      identity:{
        session_id:sessionId,
        turn_id:turnId,
        run_id:runId||null,
        stream_id:streamId||null,
        source_message_refs:Array.isArray(opts.source_message_refs)?opts.source_message_refs.slice():[],
      },
      lifecycle:{
        status:_cleanString(opts.status)||'created',
        terminal_state:null,
        started_at:opts.started_at||null,
        completed_at:null,
      },
      content:{
        final_answer:'',
        final_message_ref:null,
      },
      activity_events:[],
      artifacts:[],
      side_effects:[],
      usage:null,
      presentation_state:{
        compact_worklog:{expanded:false},
        transparent_stream:{expanded:false},
        scroll:{follow:true},
      },
    };
  }

  ROOT.HermesAssistantTurnAnchors=Object.freeze({
    version:'slice2-normalizer',
    activityEventKinds:ACTIVITY_EVENT_KINDS,
    stateLayers:STATE_LAYERS,
    sourceEventClassification:SOURCE_EVENT_CLASSIFICATION,
    classificationOrder:CLASSIFICATION_ORDER,
    createAssistantTurnAnchorSeed,
    assistantTurnAnchorEventDedupeKey,
    classifyAssistantTurnAnchorSourceEvent,
    normalizeAssistantTurnAnchorSourceEvent,
    normalizeAssistantTurnAnchorSourceEvents,
    isAssistantTurnAnchorActivityKind,
  });
})();
