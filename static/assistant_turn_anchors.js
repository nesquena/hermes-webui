// Stable Assistant Turn Anchors Phase 0 scaffold (#3926).
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
    version:'phase0',
    activityEventKinds:ACTIVITY_EVENT_KINDS,
    stateLayers:STATE_LAYERS,
    sourceEventClassification:SOURCE_EVENT_CLASSIFICATION,
    classificationOrder:CLASSIFICATION_ORDER,
    createAssistantTurnAnchorSeed,
    assistantTurnAnchorEventDedupeKey,
    classifyAssistantTurnAnchorSourceEvent,
    isAssistantTurnAnchorActivityKind,
  });
})();
