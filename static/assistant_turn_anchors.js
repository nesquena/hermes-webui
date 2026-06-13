// Stable Assistant Turn Anchors scaffold (#3926).
//
// This file defines the current ownership inventory, event classifications, and
// small owner helpers. It does not register anchors globally. The only renderer
// wiring in this slice is settled assistant final-answer projection; live
// streaming, replay hydration, tools, and DOM ownership remain unwired.
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

  const TERMINAL_STATES=Object.freeze({
    completed:'completed',
    cancelled:'cancelled',
    interrupted:'interrupted',
    no_response:'no_response',
    tool_limit_reached:'tool_limit_reached',
    compression_exhausted:'compression_exhausted',
    connection_lost:'connection_lost',
    degraded:'degraded',
    error:'error',
  });

  const TERMINAL_STATE_ALIASES=Object.freeze({
    completed:TERMINAL_STATES.completed,
    complete:TERMINAL_STATES.completed,
    done:TERMINAL_STATES.completed,
    cancelled:TERMINAL_STATES.cancelled,
    canceled:TERMINAL_STATES.cancelled,
    cancel:TERMINAL_STATES.cancelled,
    interrupted:TERMINAL_STATES.interrupted,
    interrupted_by_user:TERMINAL_STATES.interrupted,
    no_response:TERMINAL_STATES.no_response,
    no_response_generated:TERMINAL_STATES.no_response,
    tool_limit_reached:TERMINAL_STATES.tool_limit_reached,
    max_iterations:TERMINAL_STATES.tool_limit_reached,
    compression_exhausted:TERMINAL_STATES.compression_exhausted,
    connection_lost:TERMINAL_STATES.connection_lost,
    lost_worker_bookkeeping:TERMINAL_STATES.connection_lost,
    degraded:TERMINAL_STATES.degraded,
    error:TERMINAL_STATES.error,
    failed:TERMINAL_STATES.error,
    apperror:TERMINAL_STATES.error,
  });

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

  function _terminalStateKey(value){
    return _cleanString(value).toLowerCase().replace(/[\s-]+/g,'_');
  }

  function normalizeAssistantTurnAnchorTerminalState(value, fallback){
    const key=_terminalStateKey(value);
    if(key&&_hasOwn(TERMINAL_STATE_ALIASES,key)) return TERMINAL_STATE_ALIASES[key];
    const fallbackKey=_terminalStateKey(fallback);
    if(fallbackKey&&_hasOwn(TERMINAL_STATE_ALIASES,fallbackKey)){
      return TERMINAL_STATE_ALIASES[fallbackKey];
    }
    return null;
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
    const explicit=_cleanString(_firstOwn(payload,['status','state','phase']));
    if(explicit){
      return kind==='terminal_status'
        ?normalizeAssistantTurnAnchorTerminalState(explicit,sourceType)||explicit
        :explicit;
    }
    if(kind==='tool_started') return 'running';
    if(kind==='tool_completed') return _own(payload,'is_error')?'error':'completed';
    if(kind==='terminal_status'){
      return normalizeAssistantTurnAnchorTerminalState(sourceType)||TERMINAL_STATES.error;
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
    const sourceType=_cleanString(_own(event,'source_event_type'))||'event';
    if(sid&&localId&&seq&&seq!=='pending') return 'local:'+JSON.stringify([sid,sourceType,localId,seq]);
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

  function _frozenIdentityCopy(identity){
    const refs=Array.isArray(identity&&identity.source_message_refs)
      ?identity.source_message_refs.slice()
      :[];
    return Object.freeze({
      ..._copyObject(identity),
      source_message_refs:Object.freeze(refs),
    });
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
      session_id:_cleanString(_own(identity,'session_id'))||_cleanString(_own(context,'session_id')),
      turn_id:_cleanString(_own(identity,'turn_id'))||_cleanString(_own(context,'turn_id')),
      run_id:_cleanString(_own(identity,'run_id'))||_cleanString(_own(context,'run_id')),
      stream_id:_cleanString(_own(identity,'stream_id'))||_cleanString(_own(context,'stream_id')),
    };
  }

  function _eventBelongsToAnchor(anchor, event){
    const identity=anchor.identity||{};
    const sessionId=_cleanString(_own(event,'session_id'));
    const identitySessionId=_cleanString(_own(identity,'session_id'));
    if(sessionId&&identitySessionId&&sessionId!==identitySessionId) return false;
    const turnId=_cleanString(_own(event,'turn_id'));
    const identityTurnId=_cleanString(_own(identity,'turn_id'));
    if(turnId&&identityTurnId&&turnId!==identityTurnId) return false;
    const runId=_cleanString(_own(event,'run_id'));
    const identityRunId=_cleanString(_own(identity,'run_id'));
    if(runId&&identityRunId&&runId!==identityRunId) return false;
    return true;
  }

  function _ensureDedupeKeySet(eventIndex){
    const existing=eventIndex.dedupe_key_set;
    if(existing instanceof Set) return existing;
    const set=new Set(Array.isArray(eventIndex.dedupe_keys)?eventIndex.dedupe_keys:[]);
    Object.defineProperty(eventIndex,'dedupe_key_set',{
      value:set,
      enumerable:false,
      configurable:true,
      writable:true,
    });
    return set;
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
    _ensureDedupeKeySet(registry.event_index);
    return anchor;
  }

  function _syncAnchorIdentity(anchor, event){
    const identity=anchor.identity||{};
    const runId=_cleanString(_own(event,'run_id'));
    const streamId=_cleanString(_own(event,'stream_id'));
    if(!_cleanString(_own(identity,'run_id'))&&runId) identity.run_id=runId;
    if(!_cleanString(_own(identity,'stream_id'))&&streamId) identity.stream_id=streamId;
  }

  function _textFromContentValue(value){
    if(typeof value==='string') return value;
    if(Array.isArray(value)){
      return value.map((item)=>{
        if(typeof item==='string') return item;
        if(!item||typeof item!=='object') return '';
        const text=_firstOwn(item,['text','content']);
        return typeof text==='string'?text:'';
      }).join('');
    }
    if(value&&typeof value==='object'){
      const text=_firstOwn(value,['text','content']);
      return typeof text==='string'?text:'';
    }
    return '';
  }

  function _firstTextValue(...values){
    for(let i=0;i<values.length;i+=1){
      const value=_textFromContentValue(values[i]);
      if(value.length>0) return value;
    }
    return '';
  }

  function _messageRefFromPayload(payload, event){
    return _firstTextValue(
      _firstOwn(payload,['message_id','id','local_id']),
      _firstOwn(event,['local_id','event_id'])
    )||null;
  }

  function _updateLifecycleFromEvent(anchor, event){
    const lifecycle=anchor.lifecycle||{};
    const createdAt=_own(event,'created_at');
    const status=_cleanString(_own(event,'status'));
    const kind=_cleanString(_own(event,'kind'));
    if(!lifecycle.started_at&&createdAt) lifecycle.started_at=createdAt;
    if((!lifecycle.status||lifecycle.status==='created')&&status==='running'){
      lifecycle.status='running';
    }
    if(kind==='terminal_status'){
      const terminal=normalizeAssistantTurnAnchorTerminalState(
        status,
        _own(event,'source_event_type')
      )||TERMINAL_STATES.completed;
      lifecycle.status=terminal;
      lifecycle.terminal_state=terminal;
      lifecycle.completed_at=createdAt||lifecycle.completed_at||null;
    }
    anchor.lifecycle=lifecycle;
  }

  function _updateContentFromMetadata(anchor, event){
    const payload=_own(event,'payload')||{};
    const sourceType=_cleanString(_own(event,'source_event_type'));
    if(sourceType==='usage'){
      anchor.usage=_copyObject(payload);
      return;
    }
    if(sourceType!=='settled_message') return;
    const role=_cleanString(_own(payload,'role'));
    if(role&&role!=='assistant') return;
    const finalAnswer=_firstTextValue(
      _own(payload,'content'),
      _own(payload,'text'),
      _own(payload,'final_answer'),
      _own(payload,'answer')
    );
    if(finalAnswer){
      anchor.content=anchor.content||{};
      anchor.content.final_answer=finalAnswer;
      anchor.content.final_message_ref=_messageRefFromPayload(payload,event);
    }
    const usage=_own(payload,'usage');
    const turnUsage=_own(payload,'_turnUsage');
    if(usage&&typeof usage==='object') anchor.usage=_copyObject(usage);
    if(turnUsage&&typeof turnUsage==='object') anchor.usage=_copyObject(turnUsage);
  }

  function _routeAnchorEvent(anchor, normalized){
    const event=_own(normalized,'anchor_event');
    const classification=_cleanString(_own(normalized,'classification'));
    if(classification==='activity'){
      anchor.activity_events.push(event);
      _updateLifecycleFromEvent(anchor,event);
    }else if(classification==='artifact'){
      anchor.artifacts.push(event);
    }else if(classification==='side_effect'){
      anchor.side_effects.push(event);
    }else if(classification==='metadata'){
      anchor.metadata_events.push(event);
      _updateContentFromMetadata(anchor,event);
    }else if(classification==='transport'){
      anchor.transport_events.push(event);
    }
  }

  function applyAssistantTurnAnchorNormalizedEvent(registry, normalized){
    const anchor=_ensureRegistryShape(registry);
    const item=(normalized&&typeof normalized==='object')?normalized:{};
    const event=_own(item,'anchor_event');
    if(!event){
      registry.stats.skipped_excluded+=1;
      return Object.freeze({applied:false,reason:'excluded',normalized:item});
    }
    if(!_eventBelongsToAnchor(anchor,event)){
      registry.stats.skipped_mismatched+=1;
      return Object.freeze({applied:false,reason:'mismatched_anchor',normalized:item});
    }
    const dedupeKey=_cleanString(_own(item,'dedupe_key'))||assistantTurnAnchorEventDedupeKey(event);
    const dedupeKeySet=_ensureDedupeKeySet(registry.event_index);
    if(dedupeKey&&dedupeKeySet.has(dedupeKey)){
      registry.stats.skipped_duplicate+=1;
      return Object.freeze({applied:false,reason:'duplicate',normalized:item});
    }
    if(dedupeKey){
      dedupeKeySet.add(dedupeKey);
      registry.event_index.dedupe_keys.push(dedupeKey);
    }
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

  function _contextValue(context, keys){
    return _firstOwn(context&&typeof context==='object'?context:{},keys);
  }

  function _messageValue(message, keys){
    return _firstOwn(message&&typeof message==='object'?message:{},keys);
  }

  function _rawIndexMessageRef(context){
    const rawIdx=_contextValue(context,['raw_idx','rawIdx']);
    if(rawIdx===undefined||rawIdx===null||rawIdx==='') return '';
    return 'raw_idx:'+String(rawIdx);
  }

  function projectAssistantTurnAnchorSettledMessageFinalAnswer(input, context){
    const message=(input&&typeof input==='object')?input:{};
    const ctx=(context&&typeof context==='object')?context:{};
    const sessionId=_cleanString(_contextValue(ctx,['session_id','sessionId']))
      ||_cleanString(_messageValue(message,['session_id','sessionId']));
    if(!sessionId){
      return Object.freeze({applied:false,reason:'missing_session',final_answer:'',final_message_ref:null,registry:null});
    }
    const role=_cleanString(_messageValue(message,['role']))||'assistant';
    if(role&&role!=='assistant'){
      return Object.freeze({applied:false,reason:'non_assistant',final_answer:'',final_message_ref:null,registry:null});
    }
    const runId=_cleanString(_contextValue(ctx,['run_id','runId']))
      ||_cleanString(_messageValue(message,['run_id','runId','_run_id','runtime_run_id']));
    const streamId=_cleanString(_contextValue(ctx,['stream_id','streamId']))
      ||_cleanString(_messageValue(message,['stream_id','streamId','_stream_id']));
    const messageRef=_firstTextValue(
      _messageValue(message,['message_id','id','local_id']),
      _contextValue(ctx,['message_id','messageId','local_id','localId'])
    )||_rawIndexMessageRef(ctx);
    const turnId=_cleanString(_contextValue(ctx,['turn_id','turnId']))
      ||_cleanString(_messageValue(message,['turn_id','turnId']))
      ||[
        'settled',
        sessionId,
        runId||streamId||messageRef||'assistant',
      ].join(':');
    const registry=createAssistantTurnAnchorRegistry({
      session_id:sessionId,
      turn_id:turnId,
      run_id:runId||null,
      stream_id:streamId||null,
      local_id:messageRef||null,
      source_message_refs:messageRef?[messageRef]:[],
    });
    const payload={
      role:'assistant',
      id:messageRef||null,
      content:_hasOwn(ctx,'content')?_own(ctx,'content'):_own(message,'content'),
    };
    const usage=_own(message,'usage');
    const turnUsage=_own(message,'_turnUsage');
    if(usage&&typeof usage==='object') payload.usage=usage;
    if(turnUsage&&typeof turnUsage==='object') payload._turnUsage=turnUsage;
    const result=applyAssistantTurnAnchorSourceEvent(registry,{
      source_type:'settled_message',
      payload,
      local_id:messageRef||null,
    },{
      session_id:sessionId,
      turn_id:turnId,
      run_id:runId||null,
      stream_id:streamId||null,
    });
    if(!result.applied){
      return Object.freeze({
        applied:false,
        reason:result.reason||null,
        final_answer:'',
        final_message_ref:null,
        registry,
      });
    }
    const rawFinalAnswer=registry.anchor&&registry.anchor.content&&registry.anchor.content.final_answer;
    const rawFinalMessageRef=registry.anchor&&registry.anchor.content&&registry.anchor.content.final_message_ref;
    const finalAnswer=typeof rawFinalAnswer==='string'?rawFinalAnswer:'';
    const finalMessageRef=typeof rawFinalMessageRef==='string'?rawFinalMessageRef:null;
    return Object.freeze({
      applied:!!result.applied,
      reason:result.reason||null,
      final_answer:finalAnswer,
      final_message_ref:finalMessageRef,
      registry,
    });
  }

  function _anchorFromProjectionInput(input){
    if(!input||typeof input!=='object') return null;
    if(input.anchor&&typeof input.anchor==='object') return input.anchor;
    if(input.identity&&typeof input.identity==='object') return input;
    return null;
  }

  function _activityRowId(event, index){
    const eventId=_cleanString(_own(event,'event_id'));
    if(eventId) return eventId;
    const runId=_cleanString(_own(event,'run_id'));
    const seq=_own(event,'seq');
    if(runId&&seq!==undefined&&seq!==null&&seq!=='') return [runId,String(seq)].join(':');
    const localId=_cleanString(_own(event,'local_id'));
    if(localId){
      const sourceType=_cleanString(_own(event,'source_event_type'))||_cleanString(_own(event,'kind'))||'event';
      return [localId,sourceType,String(index)].join(':');
    }
    return 'activity:'+String(index);
  }

  function _activityRowText(event){
    const payload=_own(event,'payload')||{};
    return _firstTextValue(
      _own(payload,'text'),
      _own(payload,'content'),
      _own(payload,'message'),
      _own(payload,'summary'),
      _own(payload,'result'),
      _own(payload,'output')
    );
  }

  function _isToolActivityKind(kind){
    return kind==='tool_started'||kind==='tool_updated'||kind==='tool_completed';
  }

  function _activityRowToolId(event, kind){
    if(!_isToolActivityKind(kind)) return null;
    const payload=_own(event,'payload')||{};
    return _firstTextValue(
      _own(payload,'tool_call_id'),
      _own(payload,'tool_use_id'),
      _own(payload,'call_id'),
      _own(payload,'tid'),
      _own(payload,'id')
    )||null;
  }

  function _activityPayloadFirst(payload, keys){
    return _firstOwn(payload||{},keys);
  }

  function _activityRowToolDone(kind, status, payload){
    if(payload&&typeof _own(payload,'done')==='boolean') return _own(payload,'done');
    if(kind==='tool_completed') return true;
    if(kind==='tool_started'||kind==='tool_updated') return false;
    if(status==='completed'||status==='error'||status==='failed') return true;
    if(status==='running'||status==='pending') return false;
    return null;
  }

  function _activityRowToolIsError(status, payload){
    if(payload&&typeof _own(payload,'is_error')==='boolean') return _own(payload,'is_error');
    const raw=_cleanString(status).toLowerCase();
    if(raw==='error'||raw==='failed'||raw==='failure') return true;
    return false;
  }

  function _activityRowGroup(event, payload, index){
    const activitySegmentSeq=_activityPayloadFirst(payload,['activitySegmentSeq','activity_segment_seq','segmentSeq','segment_seq']);
    const activityBurstId=_activityPayloadFirst(payload,['activityBurstId','activity_burst_id','burstId','burst_id']);
    const assistantMsgIdx=_activityPayloadFirst(payload,['assistant_msg_idx','assistantMessageIndex','assistant_msg_index']);
    const cleanSegment=activitySegmentSeq!==undefined&&activitySegmentSeq!==null&&String(activitySegmentSeq)!==''
      ? activitySegmentSeq
      : null;
    const cleanBurst=activityBurstId!==undefined&&activityBurstId!==null&&String(activityBurstId)!==''
      ? activityBurstId
      : null;
    const cleanAssistant=assistantMsgIdx!==undefined&&assistantMsgIdx!==null&&String(assistantMsgIdx)!==''
      ? assistantMsgIdx
      : null;
    const fallbackSeq=_own(event,'seq');
    const fallbackKey=fallbackSeq!==undefined&&fallbackSeq!==null&&fallbackSeq!==''?`event:${String(fallbackSeq)}`:`activity:${String(index)}`;
    const groupKey=cleanSegment!==null
      ? `segment:${String(cleanSegment)}`
      : cleanBurst!==null
        ? `burst:${String(cleanBurst)}`
        : cleanAssistant!==null
          ? `assistant:${String(cleanAssistant)}`
          : fallbackKey;
    return Object.freeze({
      group_key:groupKey,
      activity_burst_id:cleanBurst,
      activity_segment_seq:cleanSegment,
      assistant_msg_idx:cleanAssistant,
    });
  }

  function _activityRowThinking(event, kind, text){
    if(kind!=='reasoning') return null;
    const payload=_own(event,'payload')||{};
    const thinkingText=_firstTextValue(
      _own(payload,'thinking'),
      _own(payload,'reasoning'),
      _own(payload,'text'),
      text
    );
    const preview=thinkingText?String(thinkingText).replace(/\s+/g,' ').trim():'';
    return Object.freeze({
      text:thinkingText||'',
      preview:preview.length>180?`${preview.slice(0,177)}...`:preview,
      dedupe_key:preview?`thinking:${preview.toLowerCase()}`:'',
    });
  }

  function _activityRowTool(event, kind, status, text, toolCallId){
    if(!_isToolActivityKind(kind)) return null;
    const payload=_own(event,'payload')||{};
    const toolName=_cleanString(
      _activityPayloadFirst(payload,['name','tool_name','function_name'])||
      (_own(payload,'function')&&_own(_own(payload,'function'),'name'))
    )||'tool';
    const args=_activityPayloadFirst(payload,['args','arguments','input','params']);
    const preview=_firstTextValue(
      _own(payload,'preview'),
      _own(payload,'summary'),
      text
    );
    const snippet=_firstTextValue(
      _own(payload,'snippet'),
      _own(payload,'result'),
      _own(payload,'output')
    );
    const done=_activityRowToolDone(kind,status,payload);
    const isError=_activityRowToolIsError(status,payload);
    const signatureParts=[
      toolName,
      toolCallId||'',
      JSON.stringify(_sanitizePayload(args||{})),
    ];
    return Object.freeze({
      id:toolCallId,
      name:toolName,
      args:_sanitizePayload(args||{}),
      preview:preview||'',
      snippet:snippet||'',
      result:_sanitizePayload(_own(payload,'result'))??null,
      output:_sanitizePayload(_own(payload,'output'))??null,
      done,
      is_error:isError,
      duration:_activityPayloadFirst(payload,['duration','duration_seconds','elapsed'])??null,
      started_at:_activityPayloadFirst(payload,['started_at','startedAt'])??null,
      signature:signatureParts.join('|'),
    });
  }

  function _activityRowRole(kind){
    if(kind==='process_prose') return 'prose';
    if(kind==='reasoning') return 'thinking';
    if(_isToolActivityKind(kind)) return 'tool';
    if(kind==='lifecycle_status') return 'lifecycle';
    if(kind==='control_boundary') return 'control';
    if(kind==='terminal_status') return 'terminal';
    return 'activity';
  }

  function _activityRowDisplayHint(kind, mode){
    if(mode==='transparent_stream') return 'chronological_activity';
    if(kind==='process_prose') return 'main_prose';
    if(kind==='reasoning') return 'collapsed_thinking';
    if(_isToolActivityKind(kind)) return 'tool_row';
    if(kind==='lifecycle_status') return 'quiet_lifecycle_row';
    if(kind==='control_boundary') return 'control_boundary_row';
    if(kind==='terminal_status') return 'terminal_status_row';
    return 'activity_row';
  }

  function _activityRowDisplayHints(kind){
    return Object.freeze({
      compact_worklog:_activityRowDisplayHint(kind,'compact_worklog'),
      transparent_stream:_activityRowDisplayHint(kind,'transparent_stream'),
    });
  }

  function _activitySceneRow(event, index, mode){
    const payload=_own(event,'payload');
    const kind=_cleanString(_own(event,'kind'))||'activity';
    const status=_cleanString(_own(event,'status'))||null;
    const text=_activityRowText(event);
    const toolCallId=_activityRowToolId(event,kind);
    const sanitizedPayload=_sanitizePayload(payload);
    return Object.freeze({
      row_id:_activityRowId(event,index),
      order_index:index,
      kind,
      role:_activityRowRole(kind),
      display_hint:_activityRowDisplayHint(kind,mode),
      display_hints:_activityRowDisplayHints(kind),
      source_event_type:_cleanString(_own(event,'source_event_type'))||null,
      event_id:_cleanString(_own(event,'event_id'))||null,
      local_id:_cleanString(_own(event,'local_id'))||null,
      run_id:_cleanString(_own(event,'run_id'))||null,
      stream_id:_cleanString(_own(event,'stream_id'))||null,
      seq:_own(event,'seq')??null,
      status,
      created_at:_own(event,'created_at')??null,
      identity:Object.freeze({
        event_id:_cleanString(_own(event,'event_id'))||null,
        local_id:_cleanString(_own(event,'local_id'))||null,
        run_id:_cleanString(_own(event,'run_id'))||null,
        stream_id:_cleanString(_own(event,'stream_id'))||null,
        seq:_own(event,'seq')??null,
      }),
      group:_activityRowGroup(event,payload||{},index),
      text,
      thinking:_activityRowThinking(event,kind,text),
      tool_call_id:toolCallId,
      tool:_activityRowTool(event,kind,status,text,toolCallId),
      payload:sanitizedPayload,
    });
  }

  function projectAssistantTurnAnchorActivityScene(input, options){
    const anchor=_anchorFromProjectionInput(input);
    const opts=(options&&typeof options==='object')?options:{};
    const requestedMode=_cleanString(_own(opts,'mode'));
    const mode=requestedMode==='transparent_stream'?'transparent_stream':'compact_worklog';
    if(!anchor){
      return Object.freeze({
        version:'activity_scene_v1',
        mode,
        identity:Object.freeze({source_message_refs:Object.freeze([])}),
        lifecycle:Object.freeze({}),
        final_answer:'',
        final_message_ref:null,
        terminal_state:null,
        activity_rows:Object.freeze([]),
      });
    }
    const rows=(Array.isArray(anchor.activity_events)?anchor.activity_events:[])
      .map((event,index)=>_activitySceneRow(event,index,mode));
    const lifecycle=_copyObject(anchor.lifecycle);
    const content=anchor.content&&typeof anchor.content==='object'?anchor.content:{};
    return Object.freeze({
      version:'activity_scene_v1',
      mode,
      identity:_frozenIdentityCopy(anchor.identity||{}),
      lifecycle:Object.freeze(lifecycle),
      final_answer:typeof content.final_answer==='string'?content.final_answer:'',
      final_message_ref:typeof content.final_message_ref==='string'?content.final_message_ref:null,
      terminal_state:_cleanString(_own(lifecycle,'terminal_state'))||null,
      activity_rows:Object.freeze(rows),
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
    };
  }

  function createAssistantTurnAnchorRegistry(input){
    const anchor=createAssistantTurnAnchorSeed(input);
    const registry={
      identity:_frozenIdentityCopy(anchor.identity),
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
    _ensureDedupeKeySet(registry.event_index);
    return registry;
  }

  ROOT.HermesAssistantTurnAnchors=Object.freeze({
    version:'slice5-activity-scene',
    activityEventKinds:ACTIVITY_EVENT_KINDS,
    stateLayers:STATE_LAYERS,
    sourceEventClassification:SOURCE_EVENT_CLASSIFICATION,
    classificationOrder:CLASSIFICATION_ORDER,
    terminalStates:TERMINAL_STATES,
    createAssistantTurnAnchorSeed,
    normalizeAssistantTurnAnchorTerminalState,
    assistantTurnAnchorEventDedupeKey,
    classifyAssistantTurnAnchorSourceEvent,
    normalizeAssistantTurnAnchorSourceEvent,
    normalizeAssistantTurnAnchorSourceEvents,
    createAssistantTurnAnchorRegistry,
    applyAssistantTurnAnchorNormalizedEvent,
    applyAssistantTurnAnchorSourceEvent,
    applyAssistantTurnAnchorSourceEvents,
    createAssistantTurnAnchorShadowSnapshot,
    projectAssistantTurnAnchorSettledMessageFinalAnswer,
    projectAssistantTurnAnchorActivityScene,
    isAssistantTurnAnchorActivityKind,
  });
})();
