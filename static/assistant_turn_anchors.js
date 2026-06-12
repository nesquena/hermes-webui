// Stable Assistant Turn Anchors scaffold (#3926).
//
// This file is intentionally inert: it defines the current ownership inventory,
// event classifications, and small owner helpers, but it does not register
// anchors globally or change any renderer. Later phases can wire these helpers into
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

  const UNSAFE_OBJECT_KEYS=Object.freeze([
    '__proto__',
    'constructor',
    'prototype',
  ]);

  function _isUnsafeObjectKey(key){
    return UNSAFE_OBJECT_KEYS.indexOf(key)!==-1;
  }

  function _hasOwn(value, key){
    return !!value&&typeof value==='object'&&Object.prototype.hasOwnProperty.call(value,key);
  }

  function _own(value, key){
    return _hasOwn(value,key)?value[key]:undefined;
  }

  function _firstOwn(value, keys){
    if(!value||typeof value!=='object') return undefined;
    for(let i=0;i<keys.length;i+=1){
      const item=_own(value,keys[i]);
      if(item!==undefined&&item!==null&&item!=='') return item;
    }
    return undefined;
  }

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
    const out=Object.create(null);
    Object.keys(value).sort().forEach((key)=>{
      if(_isUnsafeObjectKey(key)) return;
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
    return _cleanString(_firstOwn(input,[
      'source_event_type',
      'sourceType',
      'source_type',
      'event_type',
      'type',
      'event',
    ])) || _cleanString(_firstOwn(payload,['source_event_type','type','event']));
  }

  function _sourceEventPayload(input){
    if(!input||typeof input!=='object') return {};
    if(_hasOwn(input,'payload')) return _coercePayload(_own(input,'payload'));
    if(_hasOwn(input,'data')) return _coercePayload(_own(input,'data'));
    const payload=Object.create(null);
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
      if(_isUnsafeObjectKey(key)) return;
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
      _own(context,'local_id')||
      _firstOwn(payload,['local_id','id','tid','tool_call_id','tool_use_id','call_id'])
    );
    if(explicit) return explicit;
    const sessionId=_cleanString(_own(context,'session_id'))||'session';
    const turnId=_cleanString(_own(context,'turn_id'))||'turn';
    const ctxSeq=_own(context,'seq');
    const seq=(ctxSeq!=null&&ctxSeq!=='')?String(ctxSeq):'pending';
    return [sessionId,turnId,sourceType||'event',seq].join(':');
  }

  function assistantTurnAnchorEventDedupeKey(event){
    if(!event||typeof event!=='object') return '';
    const eventId=_cleanString(_own(event,'event_id'));
    if(eventId) return 'event_id:'+JSON.stringify(eventId);
    const runId=_cleanString(_own(event,'run_id'));
    const eventSeq=_own(event,'seq');
    const seq=(eventSeq!=null&&eventSeq!=='')?String(eventSeq):'';
    if(runId&&seq) return 'run_seq:'+JSON.stringify([runId,seq]);
    const sid=_cleanString(_own(event,'session_id'));
    const localId=_cleanString(_own(event,'local_id'));
    if(sid&&localId) return 'local:'+JSON.stringify([sid,localId]);
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
    const eventId=_cleanString(_firstOwn(event,['event_id','lastEventId','last_event_id'])||_payloadEventId);
    const eventSeq=_own(event,'seq');
    const ctxSeq=_own(ctx,'seq');
    const seq=_coerceSeq(
      eventSeq!==undefined?eventSeq:
        _payloadSeq!==undefined?_payloadSeq:
          ctxSeq!==undefined?ctxSeq:
            _eventIdSeq(eventId)
    );
    const runId=_cleanString(_own(event,'run_id')||_payloadRunId||_own(ctx,'run_id'))||_eventIdRunId(eventId)||null;
    const sessionId=_cleanString(_own(event,'session_id')||_payloadSessionId||_own(ctx,'session_id'));
    const turnId=_cleanString(_own(event,'turn_id')||_payloadTurnId||_own(ctx,'turn_id'));
    const streamId=_cleanString(_own(event,'stream_id')||_payloadStreamId||_own(ctx,'stream_id'))||null;
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
      created_at:_own(event,'created_at')||_own(event,'timestamp')||_own(payload,'created_at')||_own(payload,'ts')||_own(ctx,'created_at')||null,
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

  function _copyObject(value){
    if(!value||typeof value!=='object'||Array.isArray(value)) return {};
    return {...value};
  }

  function _registryAnchor(registry){
    return registry&&typeof registry==='object'&&registry.anchor&&typeof registry.anchor==='object'
      ?registry.anchor
      :null;
  }

  function _registryContext(registry, context){
    const anchor=_registryAnchor(registry);
    const identity=anchor&&anchor.identity?anchor.identity:{};
    return {
      ..._copyObject(context),
      session_id:identity.session_id||_cleanString(context&&context.session_id),
      turn_id:identity.turn_id||_cleanString(context&&context.turn_id),
      run_id:identity.run_id||_cleanString(context&&context.run_id),
      stream_id:identity.stream_id||_cleanString(context&&context.stream_id),
    };
  }

  function _eventBelongsToAnchor(anchor, event){
    const identity=anchor.identity||{};
    const sessionId=_cleanString(event.session_id);
    if(sessionId&&identity.session_id&&sessionId!==identity.session_id) return false;
    const turnId=_cleanString(event.turn_id);
    if(turnId&&identity.turn_id&&turnId!==identity.turn_id) return false;
    return true;
  }

  function _ensureRegistryShape(registry){
    const anchor=_registryAnchor(registry);
    if(!anchor) throw new Error('assistant turn anchor registry requires anchor');
    registry.event_index=registry.event_index&&typeof registry.event_index==='object'
      ?registry.event_index
      :{};
    if(!Array.isArray(registry.event_index.dedupe_keys)) registry.event_index.dedupe_keys=[];
    registry.stats=registry.stats&&typeof registry.stats==='object'?registry.stats:{};
    registry.stats.applied=Number(registry.stats.applied)||0;
    registry.stats.skipped_duplicate=Number(registry.stats.skipped_duplicate)||0;
    registry.stats.skipped_excluded=Number(registry.stats.skipped_excluded)||0;
    registry.stats.skipped_mismatched=Number(registry.stats.skipped_mismatched)||0;
    if(!Array.isArray(anchor.metadata_events)) anchor.metadata_events=[];
    if(!Array.isArray(anchor.transport_events)) anchor.transport_events=[];
    return anchor;
  }

  function _syncAnchorIdentity(anchor, event){
    const identity=anchor.identity||{};
    if(!identity.run_id&&_cleanString(event.run_id)) identity.run_id=_cleanString(event.run_id);
    if(!identity.stream_id&&_cleanString(event.stream_id)) identity.stream_id=_cleanString(event.stream_id);
  }

  function _firstTextValue(){
    for(let i=0;i<arguments.length;i+=1){
      const value=arguments[i];
      if(typeof value==='string'&&value.length>0) return value;
    }
    return '';
  }

  function _messageRefFromPayload(payload, event){
    return _firstTextValue(
      payload&&payload.message_id,
      payload&&payload.id,
      payload&&payload.local_id,
      event&&event.local_id,
      event&&event.event_id
    )||null;
  }

  function _updateLifecycleFromEvent(anchor, event){
    const lifecycle=anchor.lifecycle||{};
    if(!lifecycle.started_at&&event.created_at) lifecycle.started_at=event.created_at;
    if((!lifecycle.status||lifecycle.status==='created')&&event.status==='running'){
      lifecycle.status='running';
    }
    if(event.kind==='terminal_status'){
      const terminal=_cleanString(event.status)||'completed';
      lifecycle.status=terminal;
      lifecycle.terminal_state=terminal;
      lifecycle.completed_at=event.created_at||lifecycle.completed_at||null;
    }
    anchor.lifecycle=lifecycle;
  }

  function _updateContentFromMetadata(anchor, event){
    const payload=event.payload||{};
    if(event.source_event_type==='usage'){
      anchor.usage=_copyObject(payload);
      return;
    }
    if(event.source_event_type!=='settled_message') return;
    if(payload.role&&payload.role!=='assistant') return;
    const finalAnswer=_firstTextValue(payload.content,payload.text,payload.final_answer,payload.answer);
    if(finalAnswer){
      anchor.content=anchor.content||{};
      anchor.content.final_answer=finalAnswer;
      anchor.content.final_message_ref=_messageRefFromPayload(payload,event);
    }
    if(payload._turnUsage&&typeof payload._turnUsage==='object') anchor.usage=_copyObject(payload._turnUsage);
    if(payload.usage&&typeof payload.usage==='object') anchor.usage=_copyObject(payload.usage);
  }

  function _routeAnchorEvent(anchor, normalized){
    const event=normalized.anchor_event;
    if(normalized.classification==='activity'){
      anchor.activity_events.push(event);
      _updateLifecycleFromEvent(anchor,event);
    }else if(normalized.classification==='artifact'){
      anchor.artifacts.push(event);
    }else if(normalized.classification==='side_effect'){
      anchor.side_effects.push(event);
    }else if(normalized.classification==='metadata'){
      anchor.metadata_events.push(event);
      _updateContentFromMetadata(anchor,event);
    }else if(normalized.classification==='transport'){
      anchor.transport_events.push(event);
    }
  }

  function applyAssistantTurnAnchorNormalizedEvent(registry, normalized){
    const anchor=_ensureRegistryShape(registry);
    const item=(normalized&&typeof normalized==='object')?normalized:{};
    const event=item.anchor_event;
    if(!event){
      registry.stats.skipped_excluded+=1;
      return Object.freeze({applied:false,reason:'excluded',normalized:item});
    }
    if(!_eventBelongsToAnchor(anchor,event)){
      registry.stats.skipped_mismatched+=1;
      return Object.freeze({applied:false,reason:'mismatched_anchor',normalized:item});
    }
    const dedupeKey=_cleanString(item.dedupe_key)||assistantTurnAnchorEventDedupeKey(event);
    if(dedupeKey&&registry.event_index.dedupe_keys.indexOf(dedupeKey)!==-1){
      registry.stats.skipped_duplicate+=1;
      return Object.freeze({applied:false,reason:'duplicate',normalized:item});
    }
    if(dedupeKey) registry.event_index.dedupe_keys.push(dedupeKey);
    _syncAnchorIdentity(anchor,event);
    _routeAnchorEvent(anchor,item);
    registry.stats.applied+=1;
    return Object.freeze({applied:true,reason:null,normalized:item});
  }

  function applyAssistantTurnAnchorSourceEvent(registry, input, context){
    const normalized=normalizeAssistantTurnAnchorSourceEvent(input,_registryContext(registry,context));
    return applyAssistantTurnAnchorNormalizedEvent(registry,normalized);
  }

  function applyAssistantTurnAnchorSourceEvents(registry, events, context){
    const list=Array.isArray(events)?events:[];
    return list.map((event)=>applyAssistantTurnAnchorSourceEvent(registry,event,context));
  }

  function _eventsForShadowSource(sources, primaryKey, fallbackKey){
    if(!sources||typeof sources!=='object') return [];
    const primary=sources[primaryKey];
    if(Array.isArray(primary)) return primary;
    const fallback=fallbackKey?sources[fallbackKey]:null;
    return Array.isArray(fallback)?fallback:[];
  }

  function _shadowSourceContext(context, sourceLayer){
    return {
      ..._copyObject(context),
      source_layer:sourceLayer,
    };
  }

  function createAssistantTurnAnchorShadowSnapshot(input){
    const opts=(input&&typeof input==='object')?input:{};
    const anchorInput=(opts.anchor&&typeof opts.anchor==='object')?opts.anchor:opts;
    const sources=(opts.sources&&typeof opts.sources==='object')?opts.sources:opts;
    const context=(opts.context&&typeof opts.context==='object')?opts.context:{};
    const registry=createAssistantTurnAnchorRegistry(anchorInput);
    const results={
      live:applyAssistantTurnAnchorSourceEvents(
        registry,
        _eventsForShadowSource(sources,'live_events'),
        _shadowSourceContext(context,'live')
      ),
      replay:applyAssistantTurnAnchorSourceEvents(
        registry,
        _eventsForShadowSource(sources,'replay_events','run_journal_events'),
        _shadowSourceContext(context,'replay')
      ),
      settled:applyAssistantTurnAnchorSourceEvents(
        registry,
        _eventsForShadowSource(sources,'settled_events'),
        _shadowSourceContext(context,'settled')
      ),
      inflight:applyAssistantTurnAnchorSourceEvents(
        registry,
        _eventsForShadowSource(sources,'inflight_events'),
        _shadowSourceContext(context,'inflight')
      ),
    };
    return Object.freeze({
      registry,
      results:Object.freeze(results),
    });
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
      metadata_events:[],
      transport_events:[],
      usage:null,
      presentation_state:{
        compact_worklog:{expanded:false},
        transparent_stream:{expanded:false},
        scroll:{follow:true},
      },
    };
  }

  function createAssistantTurnAnchorRegistry(input){
    const anchor=createAssistantTurnAnchorSeed(input);
    return {
      identity:anchor.identity,
      anchor,
      event_index:{
        dedupe_keys:[],
      },
      stats:{
        applied:0,
        skipped_duplicate:0,
        skipped_excluded:0,
        skipped_mismatched:0,
      },
    };
  }

  ROOT.HermesAssistantTurnAnchors=Object.freeze({
    version:'slice3-registry-shadow',
    activityEventKinds:ACTIVITY_EVENT_KINDS,
    stateLayers:STATE_LAYERS,
    sourceEventClassification:SOURCE_EVENT_CLASSIFICATION,
    classificationOrder:CLASSIFICATION_ORDER,
    createAssistantTurnAnchorSeed,
    assistantTurnAnchorEventDedupeKey,
    classifyAssistantTurnAnchorSourceEvent,
    normalizeAssistantTurnAnchorSourceEvent,
    normalizeAssistantTurnAnchorSourceEvents,
    createAssistantTurnAnchorRegistry,
    applyAssistantTurnAnchorNormalizedEvent,
    applyAssistantTurnAnchorSourceEvent,
    applyAssistantTurnAnchorSourceEvents,
    createAssistantTurnAnchorShadowSnapshot,
    isAssistantTurnAnchorActivityKind,
  });
})();
