/* Bounded, authority-scoped persistent cache for immutable snapshot videos. */
(function(){
'use strict';

const SCHEMA=1;
const CACHE_FAMILY='hermes-snapshot-video-v';
const CACHE_PREFIX='hermes-snapshot-video-v'+SCHEMA+'-';
const META_PATH='/__hermes_snapshot_video_cache_meta__';
const DEFAULT_PER_FILE_BYTES=16*1024*1024;
const DEFAULT_TOTAL_BYTES=96*1024*1024;
const MAX_ENTRIES=256;
const testCfg=(typeof window!=='undefined'&&window.__HERMES_VIDEO_CACHE_TEST__)||{};
const PER_FILE_BYTES=Math.max(1,Number(testCfg.perFileBytes)||DEFAULT_PER_FILE_BYTES);
const TOTAL_BYTES=Math.max(PER_FILE_BYTES,Number(testCfg.totalBytes)||DEFAULT_TOTAL_BYTES);
const forceCacheUnavailable=!!testCfg.forceCacheUnavailable;

let scope='';
let scopeContext='';
let scopePromise=null;
let scopePromiseContext='';
let scopeGeneration=0;
const scopeWaiters=new Set();
let cacheOps=Promise.resolve();
let visibilityObserver=null;
let domObserver=null;
let debugMeta={entries:{}};
let authorityChannel=null;
const tasks=new Map();
const consumers=new Map();

class MediaCacheLimitError extends Error{
  constructor(message){super(message);this.name='MediaCacheLimitError';}
}

function _cacheStorage(){
  // CacheStorage is origin-global, so a per-tab promise is not enough to keep
  // byte accounting sound. Fail back to native media on browsers without the
  // origin-wide Web Locks primitive rather than claim a quota we cannot enforce.
  if(forceCacheUnavailable||typeof caches==='undefined'||typeof BroadcastChannel==='undefined'||
    typeof navigator==='undefined'||!navigator.locks||typeof navigator.locks.request!=='function') return null;
  return caches;
}
function _cacheName(value){return CACHE_PREFIX+value;}
function _metaUrl(){return new URL(META_PATH,location.origin).href;}
function _sourceUrl(video){
  const stored=String((video&&video.dataset&&video.dataset.mediaSource)||'');
  const current=String((video&&video.getAttribute&&video.getAttribute('src'))||'');
  // A Blob URL is owned by this module, so the stored native source remains
  // authoritative. Any other changed src is an external same-node replacement
  // and must supersede stale data-media-source state.
  if(current&&!current.startsWith('blob:')&&current!==stored) return current;
  return stored||current;
}
function _eligibleUrl(value){
  if(!value||typeof URL==='undefined') return '';
  try{
    const url=new URL(value,document.baseURI||location.href);
    if(url.origin!==location.origin) return '';
    if(!/(?:^|\/)api\/media$/.test(url.pathname)) return '';
    const sessionId=String(url.searchParams.get('session_id')||'');
    if(!/^[0-9A-Za-z._-]{1,128}$/.test(sessionId)) return '';
    if(!/^[0-9a-f]{64}$/i.test(url.searchParams.get('snap')||'')) return '';
    return url.href;
  }catch(_){return '';}
}
function _sessionContext(value){
  try{return String(new URL(value,document.baseURI||location.href).searchParams.get('session_id')||'');}
  catch(_){return '';}
}
function _scopeRequestContext(value){
  try{
    const url=new URL(value,document.baseURI||location.href);
    return String(url.searchParams.get('session_id')||'')+'\n'+String(url.searchParams.get('path')||'');
  }catch(_){return '';}
}
function _progress(record,received,total){
  if(!record||!record.video) return;
  const video=record.video;
  const numeric=total>0?Math.min(100,Math.floor(received*100/total)):received;
  video.dataset.cacheProgress=String(numeric);
  const host=video.closest&&video.closest('.msg-media-editor');
  const label=host&&host.querySelector&&host.querySelector('.msg-media-cache-progress');
  if(!label) return;
  label.hidden=false;
  label.textContent=total>0?numeric+'%':Math.max(1,Math.ceil(received/1024))+' KB';
}
function _clearProgress(record){
  if(!record||!record.video) return;
  delete record.video.dataset.cacheProgress;
  const host=record.video.closest&&record.video.closest('.msg-media-editor');
  const label=host&&host.querySelector&&host.querySelector('.msg-media-cache-progress');
  if(label){label.textContent='';label.hidden=true;}
}
function _broadcast(task,received,total){
  task.received=received;
  task.total=total;
  for(const record of task.consumers) _progress(record,received,total);
}
function _isQuotaError(error){
  return !!error&&(error.name==='QuotaExceededError'||error.code===22||/quota/i.test(String(error.message||'')));
}
function _normalizeMeta(value){
  const entries={};
  const raw=value&&typeof value==='object'&&value.entries&&typeof value.entries==='object'?value.entries:{};
  for(const [url,item] of Object.entries(raw)){
    const size=Number(item&&item.size);
    const at=Number(item&&item.at);
    if(size>=0&&Number.isFinite(size)&&Number.isFinite(at)) entries[url]={size,at};
  }
  return {entries};
}
function _metaBytes(meta){
  const text=JSON.stringify(meta);
  return typeof TextEncoder!=='undefined'?new TextEncoder().encode(text).byteLength:text.length*2;
}
function _total(meta){return Object.values(meta.entries).reduce((sum,item)=>sum+item.size,0)+_metaBytes(meta);}
function _queueCacheOp(fn){
  const locked=()=>navigator.locks.request(CACHE_FAMILY+'quota-lock',{mode:'exclusive'},fn);
  const run=cacheOps.then(locked,locked);
  cacheOps=run.catch(()=>{});
  return run;
}
async function _readMeta(cache){
  let meta={entries:{}};
  try{
    const response=await cache.match(_metaUrl());
    if(response) meta=_normalizeMeta(await response.json());
  }catch(_){}
  // Reconcile metadata with actual CacheStorage keys after a tab crash or an
  // interrupted body/meta pair. Unknown or oversized orphan bodies fail closed.
  const requests=await cache.keys();
  const actual=new Set(requests.map(request=>request.url).filter(url=>url!==_metaUrl()));
  let changed=false;
  for(const url of Object.keys(meta.entries)){
    if(!actual.has(url)){delete meta.entries[url];changed=true;}
  }
  for(const url of actual){
    const response=await cache.match(url);
    const lengthHeader=response&&response.headers.get('Content-Length');
    const size=lengthHeader===null||lengthHeader===undefined?NaN:Number(lengthHeader);
    if(!Number.isFinite(size)||size<0||size>PER_FILE_BYTES){
      await cache.delete(url);
      if(meta.entries[url]) delete meta.entries[url];
      changed=true;
      continue;
    }
    const prior=meta.entries[url];
    if(!prior){
      meta.entries[url]={size,at:0};
      changed=true;
    }else if(prior.size!==size){
      meta.entries[url]={size,at:prior.at};
      changed=true;
    }
  }
  while(_total(meta)>TOTAL_BYTES){
    if(!await _evictOldest(cache,meta)) break;
    changed=true;
  }
  if(changed) await _writeMeta(cache,meta);
  return meta;
}
async function _writeMeta(cache,meta){
  await cache.put(_metaUrl(),new Response(JSON.stringify(meta),{
    headers:{'Content-Type':'application/json','Cache-Control':'no-store'},
  }));
}
async function _deleteOldCaches(keepName=''){
  const storage=_cacheStorage();
  if(!storage||typeof storage.keys!=='function') return;
  await navigator.locks.request(CACHE_FAMILY+'quota-lock',{mode:'exclusive'},async()=>{
    const names=await storage.keys();
    await Promise.all(names.filter(name=>name.startsWith(CACHE_FAMILY)&&name!==keepName).map(name=>storage.delete(name)));
  });
}
async function _cachedBlob(sourceUrl,currentScope){
  const storage=_cacheStorage();
  if(!storage||!currentScope) return null;
  return _queueCacheOp(async()=>{
    if(scope!==currentScope) return null;
    const cache=await storage.open(_cacheName(currentScope));
    const meta=await _readMeta(cache);
    const response=await cache.match(sourceUrl);
    if(!response) return null;
    const declared=Number(response.headers.get('Content-Length'));
    const size=Number(meta.entries[sourceUrl]&&meta.entries[sourceUrl].size)||
      (Number.isFinite(declared)&&declared>=0?declared:0);
    meta.entries[sourceUrl]={size,at:Date.now()};
    await _writeMeta(cache,meta);
    debugMeta=meta;
    return response.blob();
  });
}
async function _evictOldest(cache,meta,exclude=''){
  const candidates=Object.entries(meta.entries)
    .filter(([url])=>url!==exclude)
    .sort((a,b)=>a[1].at-b[1].at);
  if(!candidates.length) return false;
  const [url]=candidates[0];
  await cache.delete(url);
  delete meta.entries[url];
  return true;
}
async function _storeBlob(sourceUrl,blob,currentScope){
  const storage=_cacheStorage();
  if(!storage||!currentScope||blob.size>PER_FILE_BYTES||blob.size>TOTAL_BYTES) return false;
  return _queueCacheOp(async()=>{
    if(scope!==currentScope) return false;
    const cache=await storage.open(_cacheName(currentScope));
    const meta=await _readMeta(cache);
    if(meta.entries[sourceUrl]){
      await cache.delete(sourceUrl);
      delete meta.entries[sourceUrl];
    }
    meta.entries[sourceUrl]={size:blob.size,at:Date.now()};
    const abandon=async()=>{
      delete meta.entries[sourceUrl];
      try{await _writeMeta(cache,meta);}catch(_){}
      debugMeta=meta;
      return false;
    };
    while(_total(meta)>TOTAL_BYTES||Object.keys(meta.entries).length>MAX_ENTRIES){
      if(!await _evictOldest(cache,meta,sourceUrl)) return abandon();
    }
    const response=()=>new Response(blob,{headers:{
      'Content-Type':blob.type||'video/mp4',
      'Content-Length':String(blob.size),
      'Cache-Control':'private, max-age=31536000, immutable',
    }});
    try{
      await cache.put(sourceUrl,response());
    }catch(error){
      if(!_isQuotaError(error)) return abandon();
      while(await _evictOldest(cache,meta,sourceUrl)){}
      try{await cache.put(sourceUrl,response());}
      catch(_retryError){return abandon();}
    }
    try{await _writeMeta(cache,meta);}
    catch(_){await cache.delete(sourceUrl);return abandon();}
    debugMeta=meta;
    return true;
  });
}
async function _requestScope(sourceUrl){
  const generation=scopeGeneration;
  const sessionContext=_sessionContext(sourceUrl);
  if(!sessionContext) throw new Error('media cache session context unavailable');
  const endpoint=new URL('api/media-cache/scope',document.baseURI||location.href);
  endpoint.searchParams.set('session_id',sessionContext);
  const mediaUrl=new URL(sourceUrl,document.baseURI||location.href);
  endpoint.searchParams.set('path',String(mediaUrl.searchParams.get('path')||''));
  const response=await fetch(endpoint.href,{
    credentials:'include',cache:'no-store',headers:{'Accept':'application/json'},
  });
  if(!response.ok){
    await clearAll();
    throw new Error('media cache scope unavailable');
  }
  const payload=await response.json();
  const value=String(payload&&payload.scope||'');
  if(payload.schema!==SCHEMA||!/^[0-9a-z-]{6,128}$/i.test(value)) throw new Error('invalid media cache scope');
  if(generation!==scopeGeneration) throw new DOMException('Superseded','AbortError');
  if(scope&&scope!==value){
    _teardownActive(scopeWaiters);
    debugMeta={entries:{}};
  }
  scope=value;
  scopeContext=sessionContext;
  await _deleteOldCaches(_cacheName(value));
  return value;
}
function _ensureScope(validate=false,preserveVideo=null,sourceUrl=''){
  const sessionContext=_sessionContext(sourceUrl);
  const requestContext=_scopeRequestContext(sourceUrl);
  if(!sessionContext||!requestContext) return Promise.reject(new Error('media cache session context unavailable'));
  if(preserveVideo) scopeWaiters.add(preserveVideo);
  if(scope&&scopeContext===sessionContext&&!validate) return Promise.resolve(scope);
  if(scopePromise){
    if(scopePromiseContext===requestContext) return scopePromise;
    return scopePromise.catch(()=>{}).then(()=>_ensureScope(validate,preserveVideo,sourceUrl));
  }
  scopePromiseContext=requestContext;
  scopePromise=_requestScope(sourceUrl).finally(()=>{
    scopePromise=null;
    scopePromiseContext='';
    scopeWaiters.clear();
  });
  return scopePromise;
}
function _taskKey(currentScope,sourceUrl){return currentScope+'\n'+sourceUrl;}
async function _rejectResponse(task,response,error){
  // A rejected response is still a live network stream. Cancel the body before
  // falling back so header-only validation failures cannot keep downloading in
  // the background after the task promise has settled.
  try{
    if(response&&response.body&&typeof response.body.cancel==='function'){
      await response.body.cancel(error);
    }
  }catch(_){}
  try{task.controller.abort();}catch(_){}
  throw error;
}
async function _download(task){
  const cached=await _cachedBlob(task.sourceUrl,task.scope);
  if(cached) return cached;
  const response=await fetch(task.sourceUrl,{
    credentials:'include',cache:'no-store',signal:task.controller.signal,
    headers:{'Accept':'video/*','X-Hermes-Video-Cache':'1'},
  });
  if(!response.ok) return _rejectResponse(task,response,new Error('media fetch failed: '+response.status));
  const requestedDigest=new URL(task.sourceUrl).searchParams.get('snap')||'';
  const servedDigest=String(response.headers.get('X-Hermes-Media-Snapshot')||'').toLowerCase();
  if(!requestedDigest||servedDigest!==requestedDigest.toLowerCase()){
    return _rejectResponse(task,response,new Error('media response was not an attested snapshot'));
  }
  const contentType=String(response.headers.get('Content-Type')||'').split(';')[0].trim().toLowerCase();
  if(contentType&&!contentType.startsWith('video/')){
    return _rejectResponse(task,response,new Error('not a video response'));
  }
  const lengthHeader=response.headers.get('Content-Length');
  const declared=lengthHeader===null?0:Number(lengthHeader);
  if(lengthHeader!==null&&(!Number.isFinite(declared)||declared<0)){
    return _rejectResponse(task,response,new MediaCacheLimitError('invalid content length'));
  }
  if(declared>PER_FILE_BYTES){
    return _rejectResponse(task,response,new MediaCacheLimitError('video exceeds persistent cache limit'));
  }
  if(!response.body||typeof TransformStream==='undefined'||typeof Response==='undefined'){
    return _rejectResponse(task,response,new Error('bounded streaming unavailable'));
  }
  let received=0;
  const counted=response.body.pipeThrough(new TransformStream({
    transform(chunk,controller){
      const size=chunk&&Number(chunk.byteLength)||0;
      received+=size;
      _broadcast(task,received,declared);
      if(received>PER_FILE_BYTES){
        controller.error(new MediaCacheLimitError('video exceeds persistent cache limit'));
        task.controller.abort();
        return;
      }
      controller.enqueue(chunk);
    },
  }));
  const blob=await new Response(counted,{headers:{'Content-Type':contentType||'video/mp4'}}).blob();
  if(blob.size>PER_FILE_BYTES) throw new MediaCacheLimitError('video exceeds persistent cache limit');
  await _storeBlob(task.sourceUrl,blob,task.scope);
  return blob;
}
function _getTask(currentScope,sourceUrl){
  const key=_taskKey(currentScope,sourceUrl);
  let task=tasks.get(key);
  if(task) return task;
  task={key,scope:currentScope,sourceUrl,controller:new AbortController(),consumers:new Set(),settled:false,promise:null,received:0,total:0};
  task.promise=_download(task).finally(()=>{
    task.settled=true;
    if(tasks.get(key)===task) tasks.delete(key);
  });
  tasks.set(key,task);
  return task;
}
function _removeListeners(record){
  if(!record||!record.listeners) return;
  for(const [name,fn] of record.listeners) record.video.removeEventListener(name,fn);
  record.listeners=[];
}
function _revoke(record){
  if(record&&record.blobUrl){
    try{URL.revokeObjectURL(record.blobUrl);}catch(_){}
    record.blobUrl='';
    if(record.video&&record.video.dataset) delete record.video.dataset.cacheBlobUrl;
  }
}
function _release(video,{keepState=false}={}){
  const record=consumers.get(video);
  if(!record) return;
  if(visibilityObserver) try{visibilityObserver.unobserve(video);}catch(_){}
  _removeListeners(record);
  _revoke(record);
  if(record.task){
    record.task.consumers.delete(record);
    if(!record.task.settled&&record.task.consumers.size===0) record.task.controller.abort();
    record.task=null;
  }
  _clearProgress(record);
  consumers.delete(video);
  if(!keepState&&video.dataset) delete video.dataset.persistentVideoState;
}
function _fallback(record){
  if(!record||consumers.get(record.video)!==record) return;
  const video=record.video;
  const source=record.sourceUrl;
  _release(video,{keepState:true});
  video.dataset.mediaSource=source;
  video.dataset.persistentVideoFallback=source;
  video.dataset.persistentVideoState='fallback';
  video.preload='metadata';
  video.src=source;
}
async function _activate(video){
  let record=consumers.get(video);
  if(!record){_observe(video);record=consumers.get(video);}
  if(!record||record.activated) return;
  record.activated=true;
  if(visibilityObserver) try{visibilityObserver.unobserve(video);}catch(_){}
  if(!_cacheStorage()){
    _fallback(record);
    return;
  }
  video.dataset.persistentVideoState='loading';
  try{
    // Revalidate authority before every new consumption. Otherwise an expired
    // auth cookie could keep reading an in-memory old scope without contacting
    // the server that would now deny the same media URL.
    const currentScope=await _ensureScope(true,video,record.sourceUrl);
    if(consumers.get(video)!==record) return;
    const task=_getTask(currentScope,record.sourceUrl);
    record.task=task;
    task.consumers.add(record);
    if(task.received>0) _progress(record,task.received,task.total);
    const blob=await task.promise;
    if(consumers.get(video)!==record||record.task!==task) return;
    const blobUrl=URL.createObjectURL(blob);
    record.blobUrl=blobUrl;
    video.dataset.cacheBlobUrl=blobUrl;
    video.src=blobUrl;
    video.preload='auto';
    video.dataset.persistentVideoState='ready';
    _clearProgress(record);
  }catch(error){
    if(consumers.get(video)!==record) return;
    if(error&&error.name==='AbortError'&&record.task&&record.task.consumers.size===0) return;
    _fallback(record);
  }
}
function _observe(video){
  if(!video||!video.matches||!video.matches('.msg-media-video')) return false;
  const sourceUrl=_eligibleUrl(_sourceUrl(video));
  const existing=consumers.get(video);
  if(!sourceUrl){
    if(existing) _release(video);
    if(video.dataset) delete video.dataset.mediaSource;
    return false;
  }
  const fallbackSource=String((video.dataset&&video.dataset.persistentVideoFallback)||'');
  if(fallbackSource===sourceUrl) return false;
  if(fallbackSource&&video.dataset) delete video.dataset.persistentVideoFallback;
  if(existing&&existing.sourceUrl===sourceUrl) return true;
  if(existing) _release(video);
  const record={video,sourceUrl,task:null,blobUrl:'',activated:false,listeners:[]};
  consumers.set(video,record);
  video.dataset.mediaSource=sourceUrl;
  video.dataset.persistentVideoState='observed';
  video.preload='none';
  video.removeAttribute('src');
  const onPlay=()=>{void _activate(video);};
  const onError=()=>{
    const current=consumers.get(video);
    if(current&&current.blobUrl) _fallback(current);
  };
  record.listeners=[['play',onPlay],['error',onError]];
  video.addEventListener('play',onPlay);
  video.addEventListener('error',onError);
  if(visibilityObserver) visibilityObserver.observe(video);
  return true;
}
function _videosIn(node){
  const found=[];
  if(!node||node.nodeType!==1) return found;
  if(node.matches&&node.matches('.msg-media-video')) found.push(node);
  if(node.querySelectorAll) found.push(...node.querySelectorAll('.msg-media-video'));
  return found;
}
function _teardownActive(preserveVideos=null){
  for(const video of Array.from(consumers.keys())){
    if(preserveVideos&&preserveVideos.has(video)) continue;
    _release(video);
  }
  for(const task of tasks.values()) if(!task.settled) task.controller.abort();
  tasks.clear();
}
async function clearAll(broadcast=true){
  if(broadcast&&authorityChannel){
    try{authorityChannel.postMessage({type:'authority-change'});}catch(_){}
  }
  scopeGeneration++;
  scope='';
  scopeContext='';
  scopePromise=null;
  scopePromiseContext='';
  _teardownActive();
  debugMeta={entries:{}};
  const storage=_cacheStorage();
  if(!storage) return;
  await _deleteOldCaches('');
}
async function prepareAuthorityChange(){
  // Cache cleanup is best-effort plumbing, never the authority mutation itself.
  // clearAll() invalidates this tab's scope/tasks/Blob URLs before its first
  // await; if persistent deletion then fails, the new server-issued scope still
  // makes the old partition unreadable and a later reconciliation can remove it.
  try{await clearAll();}catch(_){}
}
async function authorityChanged(){
  await clearAll();
  return Promise.resolve('');
}
async function refreshAuthority(){
  // This is the post-mutation half of profile/workspace transitions. A second
  // broadcast is required because another tab may have started old-scope work
  // after the pre-clear but before the server committed the authority change.
  return authorityChanged();
}
function debugSnapshot(){
  return {
    scope,
    scopeContext,
    tasks:tasks.size,
    consumers:consumers.size,
    entries:Object.keys(debugMeta.entries).sort(),
    totalBytes:_total(debugMeta),
  };
}
function _init(){
  if(typeof BroadcastChannel!=='undefined'){
    authorityChannel=new BroadcastChannel(CACHE_FAMILY+'authority');
    authorityChannel.addEventListener('message',event=>{
      if(event&&event.data&&event.data.type==='authority-change') void clearAll(false);
    });
  }
  if(typeof IntersectionObserver!=='undefined'){
    visibilityObserver=new IntersectionObserver(entries=>{
      for(const entry of entries){
        if(!entry.isIntersecting) continue;
        visibilityObserver.unobserve(entry.target);
        void _activate(entry.target);
      }
    },{root:null,rootMargin:'0px',threshold:0.01});
  }
  const start=()=>{
    document.querySelectorAll('.msg-media-video').forEach(_observe);
    if(document.body&&typeof MutationObserver!=='undefined'){
      domObserver=new MutationObserver(records=>{
        for(const mutation of records){
          if(mutation.type==='attributes'){
            _observe(mutation.target);
            continue;
          }
          for(const node of mutation.removedNodes||[]) _videosIn(node).forEach(video=>_release(video));
          for(const node of mutation.addedNodes||[]) _videosIn(node).forEach(_observe);
        }
      });
      domObserver.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['src','data-media-source']});
    }
  };
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',start,{once:true});
  else start();
  window.addEventListener('pagehide',()=>_teardownActive());
  window.addEventListener('pageshow',event=>{
    if(!event||!event.persisted) return;
    document.querySelectorAll('.msg-media-video').forEach(_observe);
  });
}

window.HermesPersistentVideoCache={
  ready:true,
  observe:_observe,
  detach:_release,
  clearAll,
  prepareAuthorityChange,
  authorityChanged,
  refreshAuthority,
  debugSnapshot,
  eligibleUrl:_eligibleUrl,
};
_init();
})();
