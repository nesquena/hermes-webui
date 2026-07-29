window.registerHermesTtsEngine = function registerHermesTtsEngineTombstone(){
  return false;
};

(function(){
  'use strict';

  const AUDIO_TYPES=new Set(['audio/mpeg','audio/ogg','audio/wav','audio/flac','audio/aac']);
  const AUDIO_MAX_BYTES=16*1024*1024;
  const AGENT_TIMEOUT_MS=65000;
  let generation=0;
  let active=null;
  let capabilityCache=null;
  let capabilityProfile='';
  let capabilityInflight=null;
  let capabilityInflightProfile='';
  let capabilityGeneration=0;
  let settingsGeneration=0;
  let migrationPending=false;
  let settingsSnapshot=null;

  function _profileKey(){
    try{
      if(window.S&&S.activeProfile) return String(S.activeProfile);
      if(window.S&&S.profile) return String(S.profile);
      if(window.S&&S.session&&S.session.profile) return String(S.session.profile);
      return localStorage.getItem('hermes-active-profile')||'default';
    }catch(_){return 'default';}
  }

  function _engine(){
    try{return String(localStorage.getItem('hermes-tts-engine')||'browser').trim().toLowerCase();}
    catch(_){return 'browser';}
  }

  function _safeCall(fn,arg){try{if(typeof fn==='function')fn(arg);}catch(_){}}

  function _abortError(reason){
    const error=new Error(`TTS playback was cancelled: ${reason||'stopped'}`);
    error.name='AbortError';
    return error;
  }

  function _cleanupAudio(state){
    if(!state)return;
    if(state.audio){
      state.audio.onended=null;
      state.audio.onerror=null;
      try{state.audio.pause();}catch(_){}
      try{state.audio.removeAttribute('src');state.audio.load();}catch(_){}
      state.audio=null;
    }
    if(state.url){try{URL.revokeObjectURL(state.url);}catch(_){}state.url=null;}
  }

  function _clearBrowser(state,cancel=false){
    if(!state)return;
    if(state.watchdog){clearTimeout(state.watchdog);state.watchdog=null;}
    if(state.keepAlive){clearInterval(state.keepAlive);state.keepAlive=null;}
    if(state.utterance){state.utterance.onend=null;state.utterance.onerror=null;state.utterance=null;}
    if(cancel){try{if('speechSynthesis' in window)window.speechSynthesis.cancel();}catch(_){}}
  }

  function stop(reason='stopped'){
    generation+=1;
    const state=active;
    active=null;
    if(!state)return false;
    state.stopped=true;
    if(state.upstreamSignal&&state.upstreamAbort){
      try{state.upstreamSignal.removeEventListener('abort',state.upstreamAbort);}catch(_){}
    }
    if(state.controller){try{state.controller.abort(reason);}catch(_){state.controller.abort();}}
    if(state.chunkReject){state.chunkReject(_abortError(reason));state.chunkReject=null;}
    _cleanupAudio(state);
    _clearBrowser(state,true);
    _safeCall(state.options&&state.options.onStop,reason);
    return true;
  }

  async function getCapability({refresh=false}={}){
    const profile=_profileKey();
    if(!refresh&&capabilityCache&&capabilityProfile===profile)return capabilityCache;
    if(!refresh&&capabilityInflight&&capabilityInflightProfile===profile)return capabilityInflight;
    const requestGeneration=++capabilityGeneration;
    const request=(async()=>{
      const result=await api('/api/tts/capability',{
        method:'GET',timeoutMs:15000,retries:0,retryTimeouts:false,timeoutToast:false
      });
      if(!result||typeof result!=='object')throw new Error('Invalid Agent TTS capability response.');
      if(requestGeneration!==capabilityGeneration||profile!==_profileKey()){
        const error=new Error('Stale Agent TTS capability response.');
        error.name='AbortError';
        throw error;
      }
      capabilityCache=result;
      capabilityProfile=profile;
      return result;
    })();
    capabilityInflight=request;
    capabilityInflightProfile=profile;
    try{return await request;}
    finally{
      if(capabilityInflight===request){capabilityInflight=null;capabilityInflightProfile='';}
    }
  }

  function invalidateCapability(){
    capabilityGeneration+=1;
    settingsGeneration+=1;
    capabilityCache=null;
    capabilityProfile='';
    capabilityInflight=null;
    capabilityInflightProfile='';
    stop('profile-changed');
  }

  function invalidateSettings(){settingsGeneration+=1;}

  function _captureSettings(settings){
    const present=new Set(Array.isArray(settings&&settings.persisted_speech_keys)?settings.persisted_speech_keys:[]);
    const values={};
    for(const key of ['tts_engine','tts_voice','tts_rate','tts_pitch']){
      if(present.has(key))values[key]=settings[key];
    }
    settingsSnapshot={
      revision:Number.isInteger(settings&&settings.speech_settings_revision)?settings.speech_settings_revision:0,
      present,
      values,
    };
    return settingsSnapshot;
  }

  function _persistedEngine(settings){
    const snapshot=_captureSettings(settings||{});
    if(snapshot.present.has('tts_engine'))return String(snapshot.values.tts_engine||'browser');
    try{return String(localStorage.getItem('hermes-tts-engine')||'browser');}catch(_){return 'browser';}
  }

  function isAutosaveSuppressed(settingKey){
    return migrationPending&&(settingKey==='tts_engine'||settingKey==='tts_voice');
  }

  function captureSettingsGeneration(){return settingsGeneration;}

  function isSettingsGenerationCurrent(generation){
    return generation===settingsGeneration;
  }

  function shouldMirrorSetting(settingKey,generation){
    return !isAutosaveSuppressed(settingKey)&&(generation===undefined||isSettingsGenerationCurrent(generation));
  }

  async function getSettingsState(settings,{refresh=false}={}){
    const requestGeneration=++settingsGeneration;
    const persistedEngine=_persistedEngine(settings);
    let capability=null;
    let capabilityError=null;
    try{capability=await getCapability({refresh});}catch(error){capabilityError=error;}
    if(requestGeneration!==settingsGeneration){
      const error=new Error('Stale TTS settings response.');
      error.name='AbortError';
      throw error;
    }
    const browserAvailable=_browserAvailable();
    let effectiveEngine=persistedEngine;
    let reason='';
    if(persistedEngine==='agent'){
      if(!capability||capability.synthesis_supported!==true||capability.active_provider_available!==true){
        effectiveEngine=browserAvailable?'browser':'unavailable';
        reason='agent_unavailable';
      }
    }else if(persistedEngine!=='browser'){
      effectiveEngine=browserAvailable?'browser':'unavailable';
      reason=['edge','elevenlabs','openai'].includes(persistedEngine)?'migration_required':'saved_engine_unavailable';
    }else if(!browserAvailable){
      effectiveEngine='unavailable';
      reason='browser_unavailable';
    }
    return {persistedEngine,effectiveEngine,reason,capability,capabilityError,snapshot:settingsSnapshot};
  }

  function _operationId(){
    if(window.crypto&&typeof window.crypto.randomUUID==='function')return window.crypto.randomUUID();
    const bytes=new Uint8Array(16);
    if(!window.crypto||typeof window.crypto.getRandomValues!=='function')throw new Error('Secure operation IDs are unavailable.');
    window.crypto.getRandomValues(bytes);
    bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
    const hex=Array.from(bytes,value=>value.toString(16).padStart(2,'0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }

  async function migrateLegacyEngine(settings,capability){
    const profile=_profileKey();
    const snapshot=_captureSettings(settings);
    const legacy=String(snapshot.values.tts_engine||'');
    if(!['edge','elevenlabs','openai'].includes(legacy))throw new Error('The saved engine is not migratable.');
    if(!capability||capability.provider_write_supported!==true)throw new Error('Hermes Agent provider changes are unavailable.');
    const builtinLabelKey=`tts_provider_${legacy}`;
    const row=(capability.providers||[]).find(candidate=>
      candidate.provider_id===legacy&&candidate.label_key===builtinLabelKey&&candidate.selectable===true
    );
    if(!row)throw new Error('The matching Hermes Agent provider is unavailable.');
    const operationId=_operationId();
    const migration={
      operation_id:operationId,
      legacy_engine:legacy,
      legacy_engine_was_persisted:snapshot.present.has('tts_engine'),
      legacy_edge_voice:legacy==='edge'&&snapshot.present.has('tts_voice')?String(snapshot.values.tts_voice||''):'',
      legacy_voice_was_persisted:snapshot.present.has('tts_voice'),
      expected_settings_revision:snapshot.revision,
    };
    migrationPending=true;
    const generation=++settingsGeneration;
    try{
      const result=await api('/api/tts/provider',{
        method:'POST',
        body:JSON.stringify({
          provider:row.name,
          expected_config_fingerprint:capability.config_fingerprint,
          migration,
        }),
        timeoutMs:15000,
        retries:0,
        retryTimeouts:false,
        timeoutToast:false,
      });
      if(generation!==settingsGeneration||profile!==_profileKey())throw _abortError('stale-migration');
      if(!result||!result.speech_settings||result.speech_settings.values.tts_engine!=='agent')throw new Error('Migration response was not authoritative.');
      localStorage.setItem('hermes-tts-engine','agent');
      invalidateCapability();
      return result;
    }catch(error){
      if(error&&error.name==='TimeoutError'){
        if(generation!==settingsGeneration||profile!==_profileKey())throw _abortError('stale-migration');
        const authoritative=await api('/api/settings',{method:'GET',retries:0,timeoutToast:false});
        if(generation!==settingsGeneration||profile!==_profileKey())throw _abortError('stale-migration');
        _captureSettings(authoritative);
        if(!settingsSnapshot.present.has('tts_engine')||settingsSnapshot.values.tts_engine!=='agent')throw error;
        if(settingsSnapshot.present.has('tts_engine'))localStorage.setItem('hermes-tts-engine',String(settingsSnapshot.values.tts_engine));
        return {reconciled:true,settings:authoritative};
      }
      throw error;
    }finally{
      migrationPending=false;
    }
  }

  async function selectProvider(providerName,capability){
    if(!capability||capability.provider_write_supported!==true)throw new Error('Hermes Agent provider changes are unavailable.');
    const profile=_profileKey();
    const generation=++settingsGeneration;
    const result=await api('/api/tts/provider',{
      method:'POST',
      body:JSON.stringify({provider:providerName,expected_config_fingerprint:capability.config_fingerprint}),
      timeoutMs:15000,retries:0,retryTimeouts:false,timeoutToast:false,
    });
    if(generation!==settingsGeneration||profile!==_profileKey()){
      const error=new Error('Stale Agent TTS provider response.');
      error.name='AbortError';
      throw error;
    }
    capabilityGeneration+=1;
    const activeName=String(result&&result.active_provider_name||providerName);
    const updated={
      ...capability,
      ...result,
      providers:Array.isArray(capability.providers)?capability.providers.map(row=>{
        const selected=row&&row.name===activeName;
        if(!selected)return {...row,active:false};
        const configured=result.configured===undefined?row.configured===true:result.configured===true;
        const available=result.active_provider_available===undefined?row.available===true:result.active_provider_available===true;
        return {...row,active:true,configured,available,selectable:configured&&available};
      }):[],
    };
    capabilityCache=updated;
    capabilityProfile=profile;
    return updated;
  }

  function splitText(text,maxChars){
    const points=Array.from(String(text||'').trim());
    const max=Math.max(1,Math.floor(Number(maxChars)||1));
    if(!points.length)return [];
    const chunks=[];
    let offset=0;
    while(offset<points.length){
      let end=Math.min(points.length,offset+max);
      if(end<points.length){
        const floor=offset+Math.max(1,Math.floor(max*0.6));
        for(let i=end;i>floor;i--){
          if(/[\s.!?;,:，。！？；：]/u.test(points[i-1])){end=i;break;}
        }
      }
      const chunk=points.slice(offset,end).join('').trim();
      if(chunk)chunks.push(chunk);
      offset=end;
    }
    return chunks;
  }

  function _assertCurrent(state){
    if(!state||state.stopped||active!==state||generation!==state.generation){
      const error=new Error('TTS playback was cancelled.');
      error.name='AbortError';
      throw error;
    }
  }

  async function _agentChunk(state,text){
    _assertCurrent(state);
    const response=await api('/api/tts',{
      method:'POST',
      body:JSON.stringify({engine:'agent',text}),
      responseType:'binary',
      timeoutMs:AGENT_TIMEOUT_MS,
      timeoutToast:false,
      retries:0,
      retryTimeouts:false,
      signal:state.controller.signal,
    });
    _assertCurrent(state);
    const mime=String(response&&response.contentType||'').split(';',1)[0].trim().toLowerCase();
    const length=response&&response.contentLength;
    const data=response&&response.data;
    const bytes=data instanceof ArrayBuffer?data.byteLength:-1;
    if(!AUDIO_TYPES.has(mime))throw new Error('Agent TTS returned an unsupported audio type.');
    if(!Number.isInteger(length)||length<=0||length>AUDIO_MAX_BYTES)throw new Error('Agent TTS returned an invalid audio length.');
    if(!(data instanceof ArrayBuffer)||bytes!==length)throw new Error('Agent TTS audio length did not match the response.');
    const blob=new Blob([data],{type:mime});
    state.url=URL.createObjectURL(blob);
    const audio=new Audio(state.url);
    state.audio=audio;
    await new Promise((resolve,reject)=>{
      let settled=false;
      const finish=error=>{
        if(settled)return;
        settled=true;
        if(state.watchdog){clearTimeout(state.watchdog);state.watchdog=null;}
        if(state.chunkReject===abort)state.chunkReject=null;
        audio.onended=null;audio.onerror=null;
        if(error)reject(error);else resolve();
      };
      const abort=error=>finish(error||_abortError('stopped'));
      state.chunkReject=abort;
      audio.onended=()=>finish();
      audio.onerror=()=>finish(new Error('Agent TTS audio could not be played.'));
      // Mirrors _browserChunk: estimate a conservative upper bound on playback
      // duration from the artifact size so a hung audio element cannot stall
      // the speak() promise indefinitely. 4 KB/s approximates the slowest
      // realistic compressed codec (32 kbps MP3); real audio finishes well
      // before this expires.
      const watchdogMs=Math.max(4000,Math.round(length/4)+10000);
      state.watchdog=setTimeout(()=>{
        if(settled)return;
        try{audio.pause();}catch(_){}
        finish(new Error('Agent TTS audio playback timed out.'));
      },watchdogMs);
      try{
        const pending=audio.play();
        if(pending&&typeof pending.catch==='function')pending.catch(finish);
      }catch(error){finish(error);}
    });
    _cleanupAudio(state);
    _assertCurrent(state);
  }

  async function _browserChunk(state,text){
    _assertCurrent(state);
    if(!('speechSynthesis' in window)||typeof SpeechSynthesisUtterance==='undefined'){
      throw new Error('Browser speech synthesis is unavailable.');
    }
    await new Promise((resolve,reject)=>{
      let settled=false;
      const utterance=new SpeechSynthesisUtterance(text);
      state.utterance=utterance;
      const finish=error=>{
        if(settled)return;
        settled=true;
        if(state.chunkReject===abort)state.chunkReject=null;
        utterance.onend=null;utterance.onerror=null;
        if(state.utterance===utterance)state.utterance=null;
        if(error)reject(error);else resolve();
      };
      const abort=error=>finish(error||_abortError('stopped'));
      state.chunkReject=abort;
      const voiceName=(()=>{try{return localStorage.getItem('hermes-tts-voice')||'';}catch(_){return '';}})();
      const voices=window.speechSynthesis.getVoices?window.speechSynthesis.getVoices():[];
      if(voiceName)utterance.voice=voices.find(voice=>voice.name===voiceName)||null;
      const savedRate=parseFloat((()=>{try{return localStorage.getItem('hermes-tts-rate');}catch(_){return '';}})());
      const savedPitch=parseFloat((()=>{try{return localStorage.getItem('hermes-tts-pitch');}catch(_){return '';}})());
      utterance.rate=Number.isFinite(savedRate)&&savedRate>0?savedRate:1;
      utterance.pitch=Number.isFinite(savedPitch)&&savedPitch>0?savedPitch:1;
      utterance.onend=()=>finish();
      utterance.onerror=event=>finish(new Error((event&&event.error)||'Browser speech synthesis failed.'));
      const watchdogMs=Math.max(4000,Math.round((Array.from(text).length/(12*utterance.rate))*1000)+10000);
      state.watchdog=setTimeout(()=>{
        if(active===state&&!state.stopped){try{window.speechSynthesis.cancel();}catch(_){} }
        finish(new Error('Browser speech synthesis timed out.'));
      },watchdogMs);
      state.keepAlive=setInterval(()=>{
        if(!window.speechSynthesis.speaking)return;
        try{window.speechSynthesis.pause();window.speechSynthesis.resume();}catch(_){}
      },10000);
      window.speechSynthesis.speak(utterance);
    }).finally(()=>_clearBrowser(state));
    _assertCurrent(state);
  }

  function _browserAvailable(){
    return 'speechSynthesis' in window&&typeof SpeechSynthesisUtterance!=='undefined';
  }

  async function _resolveEffectiveEngine(persisted,options){
    if(persisted==='browser'){
      if(!_browserAvailable())throw new Error('Browser speech synthesis is unavailable.');
      return {engine:'browser',persistedEngine:persisted,degraded:false,capability:null};
    }
    if(persisted==='agent'){
      let capability=null;
      try{capability=await getCapability({refresh:options.refreshCapability===true});}
      catch(error){
        if(error&&error.name==='AbortError')throw error;
        if(!_browserAvailable())throw error;
        _safeCall(options.onDegraded,{persistedEngine:persisted,effectiveEngine:'browser',reason:'agent_unavailable'});
        return {engine:'browser',persistedEngine:persisted,degraded:true,capability:null};
      }
      if(capability.synthesis_supported===true&&capability.active_provider_available===true){
        return {engine:'agent',persistedEngine:persisted,degraded:false,capability};
      }
      if(!_browserAvailable())throw new Error('Hermes Agent TTS is unavailable for the active profile.');
      _safeCall(options.onDegraded,{persistedEngine:persisted,effectiveEngine:'browser',reason:'agent_unavailable'});
      return {engine:'browser',persistedEngine:persisted,degraded:true,capability};
    }
    if(!_browserAvailable())throw new Error('The saved TTS engine is unavailable and browser speech synthesis is unsupported.');
    _safeCall(options.onDegraded,{persistedEngine:persisted,effectiveEngine:'browser',reason:'saved_engine_unavailable'});
    return {engine:'browser',persistedEngine:persisted,degraded:true,capability:null};
  }

  async function speak(text,options={}){
    stop('replaced');
    const clean=String(text||'').trim();
    if(!clean)throw new Error('TTS text is required.');
    const state={
      generation:++generation,
      controller:new AbortController(),
      options,
      audio:null,url:null,utterance:null,chunkReject:null,watchdog:null,keepAlive:null,stopped:false,
      upstreamSignal:options.signal||null,upstreamAbort:null,
    };
    active=state;
    if(state.upstreamSignal){
      state.upstreamAbort=()=>stop('aborted');
      if(state.upstreamSignal.aborted)state.upstreamAbort();
      else state.upstreamSignal.addEventListener('abort',state.upstreamAbort,{once:true});
    }
    const persistedEngine=String(options.engine||_engine()).toLowerCase();
    try{
      _assertCurrent(state);
      const resolved=await _resolveEffectiveEngine(persistedEngine,options);
      _assertCurrent(state);
      const engine=resolved.engine;
      _safeCall(options.onStart,{engine,persistedEngine:resolved.persistedEngine,degraded:resolved.degraded});
      let chunks;
      if(engine==='agent'){
        chunks=splitText(clean,resolved.capability.request_max_text_length);
      }else{
        chunks=[clean];
      }
      for(let index=0;index<chunks.length;index++){
        _assertCurrent(state);
        _safeCall(options.onChunkStart,{engine,index,total:chunks.length,text:chunks[index]});
        if(engine==='agent')await _agentChunk(state,chunks[index]);
        else await _browserChunk(state,chunks[index]);
      }
      _assertCurrent(state);
      _safeCall(options.onEnd,{engine,chunks:chunks.length});
      return {engine,chunks:chunks.length};
    }catch(error){
      if(error&&error.name!=='AbortError')_safeCall(options.onError,error);
      throw error;
    }finally{
      if(active===state){
        if(state.upstreamSignal&&state.upstreamAbort){
          try{state.upstreamSignal.removeEventListener('abort',state.upstreamAbort);}catch(_){}
        }
        _cleanupAudio(state);
        _clearBrowser(state);
        active=null;
      }
    }
  }

  function pause(){
    if(!active)return false;
    try{
      if(active.audio)active.audio.pause();
      else if('speechSynthesis' in window)window.speechSynthesis.pause();
      return true;
    }catch(_){return false;}
  }

  function resume(){
    if(!active)return false;
    try{
      if(active.audio){const pending=active.audio.play();if(pending&&pending.catch)pending.catch(()=>{});}
      else if('speechSynthesis' in window)window.speechSynthesis.resume();
      return true;
    }catch(_){return false;}
  }

  window.addEventListener('pagehide',()=>stop('pagehide'));

  window.HermesTTS=Object.freeze({
    captureSettingsGeneration,
    getCapability,
    getSettingsState,
    invalidateCapability,
    invalidateSettings,
    isSettingsGenerationCurrent,
    isAutosaveSuppressed,
    migrateLegacyEngine,
    pause,
    resume,
    selectProvider,
    shouldMirrorSetting,
    speak,
    stop,
    splitText,
  });
})();
