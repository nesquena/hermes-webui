(function(){
  'use strict';

  const SETTINGS_PREFIX='hermes.ext.settings.';
  const STORAGE_PREFIX='hermes.ext.storage.';
  const FIELD_TYPES=new Set(['boolean','string','number','integer','enum']);
  const TURN_LIFECYCLE_TYPES=new Set(['turn:start','turn:complete','turn:error','turn:cancel']);
  const TURN_LIFECYCLE_STATE_LIMIT=512;
  const schemas=new Map();
  const trustedExtensions=new Map();
  const registrations=new Map();
  const turnLifecycleListeners=new Map();
  const turnLifecycleStates=new Map();
  const configureRegistrations=new Map();
  const configureQuarantinedIds=new Set();
  const configureChangeListeners=new Set();
  const currentExtensionStatus=new Map();
  let trustedSeeded=false;
  let extensionStatusSeeded=false;

  function extensionId(value){
    return String(value||'').trim();
  }

  function namespaceForExtension(id){
    const clean=extensionId(id);
    if(!clean) throw new Error('extensionId is required');
    return encodeURIComponent(clean);
  }

  function settingsKey(id){
    return SETTINGS_PREFIX+namespaceForExtension(id);
  }

  function storageKey(id){
    return STORAGE_PREFIX+namespaceForExtension(id);
  }

  function text(value,fallback){
    const raw=typeof value==='string'?value.trim():'';
    return raw||fallback||'';
  }

  function enumOptions(options){
    if(!Array.isArray(options)||!options.length) return null;
    const out=[];
    const seen=new Set();
    for(const option of options){
      let value='';
      let label='';
      if(typeof option==='string'){
        value=option.trim();
        label=value;
      }else if(option&&typeof option==='object'&&typeof option.value==='string'){
        value=option.value.trim();
        label=text(option.label,value);
      }else{
        return null;
      }
      if(!value||seen.has(value)) return null;
      seen.add(value);
      out.push({value,label});
    }
    return out;
  }

  function defaultFor(type,rawDefault,options){
    if(type==='boolean'){
      if(rawDefault===undefined) return {ok:true,value:false};
      return typeof rawDefault==='boolean'?{ok:true,value:rawDefault}:{ok:false};
    }
    if(type==='string'){
      if(rawDefault===undefined) return {ok:true,value:''};
      return typeof rawDefault==='string'?{ok:true,value:rawDefault}:{ok:false};
    }
    if(type==='number'){
      if(rawDefault===undefined) return {ok:true,value:0};
      return typeof rawDefault==='number'&&Number.isFinite(rawDefault)?{ok:true,value:rawDefault}:{ok:false};
    }
    if(type==='integer'){
      if(rawDefault===undefined) return {ok:true,value:0};
      return Number.isInteger(rawDefault)?{ok:true,value:rawDefault}:{ok:false};
    }
    if(type==='enum'&&options){
      if(rawDefault===undefined) return {ok:true,value:options[0].value};
      return typeof rawDefault==='string'&&options.some(option=>option.value===rawDefault)?{ok:true,value:rawDefault}:{ok:false};
    }
    return {ok:false};
  }

  function normalizeSchema(rawSchema){
    const rawFields=Array.isArray(rawSchema)?rawSchema:(rawSchema&&Array.isArray(rawSchema.fields)?rawSchema.fields:[]);
    const fields=[];
    const seen=new Set();
    for(const raw of rawFields){
      if(!raw||typeof raw!=='object'||raw.sensitive===true) continue;
      const key=typeof raw.key==='string'?raw.key.trim():'';
      const type=typeof raw.type==='string'?raw.type.trim().toLowerCase():'';
      if(!/^[A-Za-z][A-Za-z0-9._-]{0,63}$/.test(key)||!FIELD_TYPES.has(type)||seen.has(key)) continue;
      const options=type==='enum'?enumOptions(raw.options):null;
      if(type==='enum'&&!options) continue;
      const normalizedDefault=defaultFor(type,raw.default,options);
      if(!normalizedDefault.ok) continue;
      seen.add(key);
      const field={key,type,label:text(raw.label,key),description:text(raw.description,''),default:normalizedDefault.value};
      if(options) field.options=options;
      fields.push(field);
    }
    return fields;
  }

  function normalizeSchemas(rawRows){
    const list=Array.isArray(rawRows)?rawRows:[];
    const entries=[];
    for(const entry of list){
      const id=extensionId(entry&&entry.id);
      if(!id) continue;
      const storageOwned=!!(entry&&entry.storage_owned);
      entries.push({
        id,
        name:text(entry&&entry.name,id),
        storage_owned:storageOwned,
        settings_schema:storageOwned?normalizeSchema(entry&&entry.settings_schema):[],
        effective_enabled:!(entry&&entry.effective_enabled===false),
      });
    }
    return entries;
  }

  function primeFromStatus(statusPayload){
    const entries=normalizeSchemas(statusPayload&&statusPayload.extensions);
    if(!trustedSeeded){
      trustedExtensions.clear();
      for(const entry of entries){
        trustedExtensions.set(entry.id,{
          id:entry.id,
          name:entry.name,
          storage_owned:entry.storage_owned,
          settings_schema:entry.settings_schema,
        });
      }
      trustedSeeded=true;
    }
    const previousStatus=new Map(currentExtensionStatus);
    const nextIds=new Set(entries.map(entry=>entry.id));
    if(extensionStatusSeeded){
      for(const id of previousStatus.keys()){
        if(nextIds.has(id)) continue;
        configureQuarantinedIds.add(id);
        notifyConfigureChange(id,'quarantine');
      }
    }
    currentExtensionStatus.clear();
    for(const entry of entries){
      currentExtensionStatus.set(entry.id,{effective_enabled:entry.effective_enabled===true});
    }
    extensionStatusSeeded=true;
    schemas.clear();
    for(const entry of entries){
      const trusted=trustedExtensions.get(entry.id);
      if(!trusted) continue;
      schemas.set(entry.id,{
        id:entry.id,
        name:entry.name,
        storage_owned:trusted.storage_owned===true,
        settings_schema:trusted.storage_owned===true?trusted.settings_schema:[],
      });
    }
    for(const [id,record] of configureRegistrations){
      if(!record||!record.active) continue;
      const before=previousStatus.get(id);
      const after=currentExtensionStatus.get(id);
      if(!before||!after||before.effective_enabled!==after.effective_enabled){
        notifyConfigureChange(id,'status');
      }
    }
  }

  function safeReadState(key){
    try{
      const raw=window.localStorage.getItem(key);
      if(!raw) return {value:{},malformed:false};
      const parsed=JSON.parse(raw);
      return parsed&&typeof parsed==='object'&&!Array.isArray(parsed)
        ?{value:parsed,malformed:false}
        :{value:{},malformed:true};
    }catch(_e){
      return {value:{},malformed:true};
    }
  }

  function safeRead(key){
    return safeReadState(key).value;
  }

  function safeWrite(key,value){
    const keys=Object.keys(value||{});
    try{
      if(!keys.length) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key,JSON.stringify(value));
      return true;
    }catch(_e){
      return false;
    }
  }

  function fieldMap(schema){
    const map=new Map();
    for(const field of schema) map.set(field.key,field);
    return map;
  }

  function validateValue(field,value){
    if(field.type==='boolean') return typeof value==='boolean'?{ok:true,value}:{ok:false};
    if(field.type==='string') return typeof value==='string'?{ok:true,value}:{ok:false};
    if(field.type==='number') return typeof value==='number'&&Number.isFinite(value)?{ok:true,value}:{ok:false};
    if(field.type==='integer') return Number.isInteger(value)?{ok:true,value}:{ok:false};
    if(field.type==='enum') return typeof value==='string'&&field.options.some(option=>option.value===value)?{ok:true,value}:{ok:false};
    return {ok:false};
  }

  function validate(schema,values){
    const input=values&&typeof values==='object'&&!Array.isArray(values)?values:{};
    const map=fieldMap(schema);
    const normalized={};
    const errors={};
    for(const field of schema) normalized[field.key]=field.default;
    for(const [key,value] of Object.entries(input)){
      const field=map.get(key);
      if(!field) continue;
      const checked=validateValue(field,value);
      if(checked.ok) normalized[key]=checked.value;
      else errors[key]='invalid';
    }
    return {ok:Object.keys(errors).length===0,values:normalized,errors};
  }

  function defaultsFor(schema){
    const defaults={};
    for(const field of schema) defaults[field.key]=field.default;
    return defaults;
  }

  function overridesFromValues(schema,values){
    const overrides={};
    for(const field of schema){
      if(values[field.key]!==field.default) overrides[field.key]=values[field.key];
    }
    return overrides;
  }

  function readSettingsState(schema,key){
    const stored=safeReadState(key);
    const checked=validate(schema,stored.value);
    const overrides=overridesFromValues(schema,checked.values);
    if(stored.malformed||JSON.stringify(stored.value)!==JSON.stringify(overrides)) safeWrite(key,overrides);
    return {values:checked.values,overrides};
  }

  function supportsSettings(meta){
    return !!(meta&&meta.storage_owned&&Array.isArray(meta.settings_schema)&&meta.settings_schema.length);
  }

  function settingsAccessor(clean,meta,isTrusted,allowConfigureRegistration){
    const schema=supportsSettings(meta)?meta.settings_schema:[];
    const key=settingsKey(clean);
    function current(){
      return supportsSettings(meta)?readSettingsState(schema,key).values:validate(schema,safeRead(key)).values;
    }
    function currentOverrides(){
      return supportsSettings(meta)?readSettingsState(schema,key).overrides:safeRead(key);
    }
    function setAll(values){
      if(!supportsSettings(meta)) return {ok:false,values:current(),errors:{extension:'unsupported'}};
      const checked=validate(schema,values);
      if(!checked.ok) return checked;
      const saved=safeWrite(key,overridesFromValues(schema,checked.values));
      return {ok:saved,values:checked.values,errors:saved?{}:{storage:'unavailable'}};
    }
    const accessor={
      extensionId:clean,
      trusted:isTrusted,
      storageOwned:!!meta.storage_owned,
      supported:supportsSettings(meta),
      schema,
      defaults:defaultsFor(schema),
      get values(){return current();},
      get overrides(){return currentOverrides();},
      get(name){return current()[name];},
      validate(values){return validate(schema,values);},
      set(name,value){
        if(name&&typeof name==='object') return setAll(name);
        const next=current();
        next[name]=value;
        return setAll(next);
      },
      setAll,
      reset(){
        if(!supportsSettings(meta)) return current();
        safeWrite(key,{});
        return current();
      },
      clear(){
        if(!supportsSettings(meta)) return false;
        safeWrite(key,{});
        return true;
      },
    };
    if(allowConfigureRegistration===true){
      accessor.registerConfigure=handler=>registerConfigureHandler(clean,handler);
    }
    return accessor;
  }

  function settingsForExtension(id){
    const clean=extensionId(id);
    const meta=schemas.get(clean)||{id:clean,name:clean,storage_owned:false,settings_schema:[]};
    return settingsAccessor(clean,meta,schemas.has(clean));
  }

  function storageAccessor(clean,meta){
    const allowed=!!meta.storage_owned;
    const key=storageKey(clean);
    return {
      getAll(){return allowed?safeRead(key):{};},
      get(name,defaultValue){
        if(!allowed) return defaultValue;
        const data=safeRead(key);
        return Object.prototype.hasOwnProperty.call(data,name)?data[name]:defaultValue;
      },
      set(name,value){
        if(!allowed) return false;
        const data=safeRead(key);
        data[name]=value;
        return safeWrite(key,data);
      },
      remove(name){
        if(!allowed) return false;
        const data=safeRead(key);
        delete data[name];
        return safeWrite(key,data);
      },
      clear(){
        if(!allowed) return false;
        safeWrite(key,{});
        return true;
      },
    };
  }

  function storageForExtension(id){
    const clean=extensionId(id);
    const meta=schemas.get(clean)||{id:clean,name:clean,storage_owned:false,settings_schema:[]};
    return storageAccessor(clean,meta);
  }

  function eventAccessor(clean){
    return Object.freeze({
      on(type,handler){
        if(!TURN_LIFECYCLE_TYPES.has(type)||typeof handler!=='function') return null;
        let listenersByExtension=turnLifecycleListeners.get(type);
        if(!listenersByExtension){
          listenersByExtension=new Map();
          turnLifecycleListeners.set(type,listenersByExtension);
        }
        let listeners=listenersByExtension.get(clean);
        if(!listeners){
          listeners=new Set();
          listenersByExtension.set(clean,listeners);
        }
        listeners.add(handler);
        let active=true;
        return function unsubscribe(){
          if(!active) return false;
          active=false;
          listeners.delete(handler);
          if(!listeners.size) listenersByExtension.delete(clean);
          if(!listenersByExtension.size) turnLifecycleListeners.delete(type);
          return true;
        };
      },
    });
  }

  function notifyConfigureChange(id,reason){
    const change=Object.freeze({id,reason});
    for(const listener of [...configureChangeListeners]){
      try{
        listener(change);
      }catch(error){
        if(typeof console!=='undefined'&&typeof console.error==='function'){
          try{console.error('[Hermes extensions] Configure change listener failed:',error);}catch(_loggingError){}
        }
      }
    }
  }

  function onConfigureChange(listener){
    if(typeof listener!=='function') return null;
    configureChangeListeners.add(listener);
    let active=true;
    return function unsubscribe(){
      if(!active) return false;
      active=false;
      configureChangeListeners.delete(listener);
      return true;
    };
  }

  function registerConfigureHandler(clean,handler){
    if(typeof handler!=='function'||!trustedExtensions.has(clean)||configureQuarantinedIds.has(clean)) return null;
    if(configureRegistrations.has(clean)) return null;
    const record={handler,pending:false,active:true};
    configureRegistrations.set(clean,record);
    notifyConfigureChange(clean,'registration');
    let active=true;
    return function unregister(){
      if(!active) return false;
      active=false;
      record.active=false;
      if(configureRegistrations.get(clean)===record) configureRegistrations.delete(clean);
      notifyConfigureChange(clean,'registration');
      return true;
    };
  }

  function configureStateForExtension(id){
    const clean=extensionId(id);
    const record=configureRegistrations.get(clean);
    const status=currentExtensionStatus.get(clean);
    const available=!!(
      clean&&record&&record.active&&!configureQuarantinedIds.has(clean)
      &&status&&status.effective_enabled===true
    );
    return {available,pending:available&&record.pending===true};
  }

  function focusableConfigureTarget(node){
    if(!node||typeof node.focus!=='function'||node.isConnected===false||node.hidden===true||node.disabled===true) return false;
    if(typeof node.getAttribute==='function'&&node.getAttribute('aria-disabled')==='true') return false;
    if(typeof node.closest==='function'&&node.closest('[hidden]')) return false;
    return true;
  }

  function focusConfigureTarget(clean,opener){
    const candidates=[];
    if(focusableConfigureTarget(opener)) candidates.push(opener);
    if(typeof document!=='undefined'&&document){
      if(typeof document.querySelectorAll==='function'){
        const buttons=document.querySelectorAll('[data-extension-configure-id]');
        for(const button of buttons){
          if(button&&button.dataset&&button.dataset.extensionConfigureId===clean&&focusableConfigureTarget(button)){
            candidates.push(button);
            break;
          }
        }
      }
      if(typeof document.querySelector==='function'){
        const installedTab=document.querySelector('[data-extensions-tab="installed"]');
        if(focusableConfigureTarget(installedTab)) candidates.push(installedTab);
      }
    }
    for(const candidate of candidates){
      try{
        candidate.focus({preventScroll:true});
        return true;
      }catch(_focusOptionsError){
        try{
          candidate.focus();
          return true;
        }catch(_focusError){}
      }
    }
    return false;
  }

  function reportConfigureFailure(clean,error,onError){
    if(typeof console!=='undefined'&&typeof console.error==='function'){
      try{console.error(`[Hermes extensions] ${clean} Configure handler failed:`,error);}catch(_loggingError){}
    }
    if(typeof onError==='function'){
      try{onError(error);}catch(callbackError){
        if(typeof console!=='undefined'&&typeof console.error==='function'){
          try{console.error(`[Hermes extensions] ${clean} Configure failure reporter failed:`,callbackError);}catch(_loggingError){}
        }
      }
    }
  }

  function invokeConfigure(id,options){
    const clean=extensionId(id);
    const state=configureStateForExtension(clean);
    const record=configureRegistrations.get(clean);
    if(!state.available||state.pending||!record) return false;
    const opener=options&&options.opener;
    const onError=options&&options.onError;
    let settled=false;
    let failureReported=false;
    record.pending=true;
    notifyConfigureChange(clean,'pending');

    function settle(){
      if(settled) return false;
      settled=true;
      record.pending=false;
      notifyConfigureChange(clean,'pending');
      focusConfigureTarget(clean,opener);
      return true;
    }

    function fail(error){
      if(!failureReported){
        failureReported=true;
        reportConfigureFailure(clean,error,onError);
      }
      settle();
    }

    let result;
    try{
      result=record.handler(Object.freeze({opener,restoreFocus:settle}));
    }catch(error){
      fail(error);
      return true;
    }

    let then;
    try{
      then=result!==null&&(typeof result==='object'||typeof result==='function')?result.then:null;
    }catch(error){
      fail(error);
      return true;
    }
    if(typeof then==='function'){
      try{
        then.call(result,settle,fail);
      }catch(error){
        fail(error);
      }
    }
    return true;
  }

  function turnLifecycleKey(sessionId,streamId){
    return `${sessionId}\u0000${streamId}`;
  }

  function rememberTurnLifecycleState(key,state){
    if(turnLifecycleStates.has(key)) turnLifecycleStates.delete(key);
    turnLifecycleStates.set(key,state);
    while(turnLifecycleStates.size>TURN_LIFECYCLE_STATE_LIMIT){
      turnLifecycleStates.delete(turnLifecycleStates.keys().next().value);
    }
  }

  function dispatchTurnLifecycle(type,raw){
    if(!TURN_LIFECYCLE_TYPES.has(type)||!raw||typeof raw!=='object') return false;
    const sessionId=typeof raw.sessionId==='string'?raw.sessionId.trim():'';
    const streamId=typeof raw.streamId==='string'?raw.streamId.trim():'';
    if(!sessionId||!streamId) return false;

    const key=turnLifecycleKey(sessionId,streamId);
    const previous=turnLifecycleStates.get(key)||{started:false,terminal:false};
    const terminal=type!=='turn:start';
    if((type==='turn:start'&&(previous.started||previous.terminal))||(terminal&&previous.terminal)) return false;
    rememberTurnLifecycleState(key,{
      started:previous.started||type==='turn:start',
      terminal:previous.terminal||terminal,
    });

    const now=Date.now()/1000;
    const event={
      type,
      sessionId,
      streamId,
      timestamp:Number.isFinite(raw.timestamp)?raw.timestamp:now,
    };
    if(type==='turn:start') event.startedAt=Number.isFinite(raw.startedAt)?raw.startedAt:event.timestamp;
    else event.endedAt=Number.isFinite(raw.endedAt)?raw.endedAt:event.timestamp;
    if(typeof raw.status==='string'&&raw.status.trim()) event.status=raw.status.trim();
    const frozenEvent=Object.freeze(event);
    const listenersByExtension=turnLifecycleListeners.get(type);
    if(!listenersByExtension) return true;
    for(const [extensionId,listeners] of listenersByExtension){
      for(const listener of [...listeners]){
        try{
          listener(frozenEvent);
        }catch(error){
          if(typeof console!=='undefined'&&typeof console.error==='function'){
            try{
              console.error(`[Hermes extensions] ${extensionId} ${type} listener failed:`,error);
            }catch(_loggingError){ }
          }
        }
      }
    }
    return true;
  }

  function registerExtension(id){
    if(typeof id!=='string') return null;
    const clean=extensionId(id);
    if(!clean) return null;
    const existing=registrations.get(clean);
    if(existing) return existing;
    const trusted=trustedExtensions.get(clean);
    if(!trusted) return null;
    const handle=Object.freeze({
      id:clean,
      settings:settingsAccessor(clean,trusted,true,true),
      storage:storageAccessor(clean,trusted),
      events:eventAccessor(clean),
    });
    registrations.set(clean,handle);
    return handle;
  }

  const api={
    normalizeSchemas,
    primeFromStatus,
    namespaceForExtension,
    settingsForExtension,
    storageForExtension,
    _dispatchTurnLifecycle:dispatchTurnLifecycle,
    _configureStateForExtension:configureStateForExtension,
    _invokeConfigure:invokeConfigure,
    _onConfigureChange:onConfigureChange,
    resetSettingsForExtension(id){return settingsForExtension(id).reset();},
    clearStorageForExtension(id){return storageForExtension(id).clear();},
  };

  window.HermesExtensionSettings=api;
  window.hermesExt=window.hermesExt||{};
  window.hermesExt.settings=window.hermesExt.settings||{};
  window.hermesExt.storage=window.hermesExt.storage||{};
  window.hermesExt.settings.forExtension=settingsForExtension;
  window.hermesExt.storage.forExtension=storageForExtension;
  window.hermesExt.register=registerExtension;
  primeFromStatus(window.__HERMES_EXTENSION_CONFIG__||{});
})();
