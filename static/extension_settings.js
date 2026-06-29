(function(){
  'use strict';

  const SETTINGS_PREFIX='hermes.ext.settings.';
  const STORAGE_PREFIX='hermes.ext.storage.';
  const FIELD_TYPES=new Set(['boolean','string','number','integer','enum']);
  const schemas=new Map();
  const trustedExtensions=new Map();
  let trustedSeeded=false;

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

  function settingsForExtension(id){
    const clean=extensionId(id);
    const meta=schemas.get(clean)||{id:clean,name:clean,storage_owned:false,settings_schema:[]};
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
    return {
      extensionId:clean,
      trusted:schemas.has(clean),
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
  }

  function storageForExtension(id){
    const clean=extensionId(id);
    const meta=schemas.get(clean)||{id:clean,name:clean,storage_owned:false,settings_schema:[]};
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

  const api={
    normalizeSchemas,
    primeFromStatus,
    namespaceForExtension,
    settingsForExtension,
    storageForExtension,
    resetSettingsForExtension(id){return settingsForExtension(id).reset();},
    clearStorageForExtension(id){return storageForExtension(id).clear();},
  };

  window.HermesExtensionSettings=api;
  window.hermesExt=window.hermesExt||{};
  window.hermesExt.settings=window.hermesExt.settings||{};
  window.hermesExt.storage=window.hermesExt.storage||{};
  window.hermesExt.settings.forExtension=settingsForExtension;
  window.hermesExt.storage.forExtension=storageForExtension;
  primeFromStatus(window.__HERMES_EXTENSION_CONFIG__||{});
})();
