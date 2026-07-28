/* Custom wallpaper: authoritative saved state + Appearance-local explicit-save draft. */
(function(global){
'use strict';

const CACHE_KEY='hermes-wallpaper-meta';
const MAX_BYTES=10*1024*1024;
const VERSION_RE=/^[0-9a-f]{64}$/;
const ALLOWED_MIME=new Set(['image/jpeg','image/png','image/webp']);
const DEFAULT_INFO=Object.freeze({has_wallpaper:false,opacity:0.8,scope:'chat',mime_type:null,image_version:null});
let saved={...DEFAULT_INFO};
let hasAuthoritativeState=false;
let reconcileGeneration=0;
let mutationStarted=0;
let authoritativeGeneration=0;
let mutationTail=Promise.resolve();
let appearanceActive=false;
let paneGeneration=0;
let draftRevision=0;
let draftEdited=false;
let draftFile=null;
let draftUrl=null;
let draftOpacity=0.8;
let draftScope='chat';
let requestRunning=false;
let statusKey=null;
let statusIsError=false;
let bound=false;

function el(id){return document.getElementById(id)}
function text(key,fallback){return typeof global.t==='function'?global.t(key):fallback}
function normalizeInfo(value){
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Invalid wallpaper response');
  const keys=Object.keys(value).sort().join(',');
  if(keys!=='has_wallpaper,image_version,mime_type,opacity,scope')throw new Error('Invalid wallpaper response');
  if(typeof value.has_wallpaper!=='boolean'||typeof value.opacity!=='number'||!Number.isFinite(value.opacity)||value.opacity<0||value.opacity>1)throw new Error('Invalid wallpaper response');
  if(value.scope!=='chat'&&value.scope!=='app')throw new Error('Invalid wallpaper response');
  if(value.has_wallpaper){
    if(!VERSION_RE.test(value.image_version||'')||!ALLOWED_MIME.has(value.mime_type))throw new Error('Invalid wallpaper response');
  }else if(value.image_version!==null||value.mime_type!==null){throw new Error('Invalid wallpaper response')}
  return {has_wallpaper:value.has_wallpaper,opacity:value.opacity,scope:value.scope,mime_type:value.mime_type,image_version:value.image_version};
}
function imageUrl(version){return new URL('api/wallpaper/image?v='+encodeURIComponent(version),document.baseURI||location.href).href}
function cache(info){
  try{if(info.has_wallpaper)localStorage.setItem(CACHE_KEY,JSON.stringify(info));else localStorage.removeItem(CACHE_KEY)}catch(_){}
}
function clearRender(){
  const root=document.documentElement;
  delete root.dataset.wallpaper;delete root.dataset.wallpaperScope;
  root.style.removeProperty('--wallpaper-image');root.style.removeProperty('--wallpaper-opacity');
}
function renderSource(source,opacity,scope){
  if(!source){clearRender();return}
  const root=document.documentElement;
  root.dataset.wallpaper='active';root.dataset.wallpaperScope=scope;
  root.style.setProperty('--wallpaper-image','url('+JSON.stringify(source)+')');
  root.style.setProperty('--wallpaper-opacity',String(opacity));
}
function render(info){renderSource(info.has_wallpaper?imageUrl(info.image_version):'',info.opacity,info.scope)}
function renderDraft(){
  const source=draftUrl||(saved.has_wallpaper?imageUrl(saved.image_version):'');
  renderSource(source,draftOpacity,draftScope);
}
function probe(info){
  if(!info.has_wallpaper)return Promise.resolve();
  return new Promise((resolve,reject)=>{const image=new Image();image.onload=resolve;image.onerror=()=>reject(new Error('Wallpaper image unavailable'));image.src=imageUrl(info.image_version)});
}
async function applyAuthoritativeInfo(raw,guards){
  const info=normalizeInfo(raw);
  await probe(info);
  if(guards&&(guards.reconcile!==reconcileGeneration||guards.mutation!==mutationStarted||guards.authoritative!==authoritativeGeneration))return false;
  saved=info;hasAuthoritativeState=true;authoritativeGeneration++;cache(info);render(info);syncControls();return true;
}
function setStatus(message,isError){const status=el('wallpaperStatus');if(status){status.textContent=message||'';status.classList.toggle('is-error',!!isError)}}
function setStatusKey(key,isError){statusKey=key||null;statusIsError=!!isError;setStatus(key?text(key,key):'',statusIsError)}
function refreshLocalizedWallpaperText(){if(statusKey)setStatus(text(statusKey,statusKey),statusIsError);syncControls()}
function wallpaperErrorKey(error){
  const message=String(error&&error.message||'');
  if(message==='Invalid wallpaper response')return 'settings_wallpaper_invalid_response';
  if(message==='Wallpaper image unavailable')return 'settings_wallpaper_image_unavailable';
  if(message==='Invalid wallpaper upload')return 'settings_wallpaper_invalid_upload';
  if(message==='Invalid wallpaper metadata')return 'settings_wallpaper_invalid_metadata';
  if(message==='Wallpaper not found'||error&&error.status===404)return 'settings_wallpaper_not_found';
  if(message==='Wallpaper exceeds encoded size limit'||error&&error.status===413)return 'settings_wallpaper_too_large';
  if(message==='Wallpaper storage operation failed')return 'settings_wallpaper_unavailable';
  if(message==='Request timed out. Please try again.')return 'settings_wallpaper_timeout';
  if(message==='Failed to fetch'||error instanceof TypeError)return 'settings_wallpaper_network_failed';
  if(error&&Number.isFinite(error.status)&&error.status>=500)return 'settings_wallpaper_storage_failed';
  return 'settings_wallpaper_failed';
}
function ownsReconcile(guards){return guards.reconcile===reconcileGeneration&&guards.mutation===mutationStarted&&guards.authoritative===authoritativeGeneration}
async function reconcileWallpaperInfo(){
  const guards={reconcile:++reconcileGeneration,mutation:mutationStarted,authoritative:authoritativeGeneration};
  try{
    const raw=await global.api('/api/wallpaper/info',{retries:0});
    const applied=await applyAuthoritativeInfo(raw,guards);
    if(applied&&statusKey==='settings_wallpaper_reconciliation')setStatusKey(null);
  }catch(error){
    if(ownsReconcile(guards)){
      if(!hasAuthoritativeState){saved={...DEFAULT_INFO};try{localStorage.removeItem(CACHE_KEY)}catch(_){}clearRender();if(appearanceActive&&draftEdited)renderDraft()}
      setStatusKey('settings_wallpaper_reconciliation',true);
    }
    throw error;
  }
  return saved;
}
function _releaseWallpaperDraftUrl(expectedUrl){
  if(!draftUrl||(expectedUrl&&expectedUrl!==draftUrl))return;
  const url=draftUrl;draftUrl=null;
  URL.revokeObjectURL(url);
}
function currentScope(){const checked=document.querySelector('input[name="wallpaperScope"]:checked');return checked?checked.value:draftScope}
function dirty(){
  if(draftFile)return true;
  if(!saved.has_wallpaper)return false;
  return draftOpacity!==saved.opacity||draftScope!==saved.scope;
}
function syncControls(){
  if(!appearanceActive)return;
  const opacity=el('wallpaperOpacity');const output=el('wallpaperOpacityValue');
  if(opacity)opacity.value=String(Math.round(draftOpacity*100));if(output)output.value=output.textContent=Math.round(draftOpacity*100)+'%';
  const radio=el(draftScope==='app'?'wallpaperScopeApp':'wallpaperScopeChat');if(radio)radio.checked=true;
  const preview=el('wallpaperPreview');
  if(preview){const src=draftUrl||(saved.has_wallpaper?imageUrl(saved.image_version):'');preview.hidden=!src;if(src)preview.src=src;preview.style.opacity=String(draftOpacity)}
  const name=el('wallpaperFileName');if(name)name.textContent=draftFile?draftFile.name:(saved.has_wallpaper?text('settings_wallpaper_saved_file','Saved wallpaper'):'');
  const save=el('wallpaperSaveBtn');if(save)save.disabled=requestRunning||!dirty();
  const clear=el('wallpaperClearBtn');if(clear)clear.disabled=requestRunning||(!saved.has_wallpaper&&!draftFile);
  const field=el('wallpaperSettingsField');if(field)field.setAttribute('aria-busy',requestRunning?'true':'false');
}
function installDraftFile(file){
  const extension=(file.name||'').toLowerCase().split('.').pop();
  if(!ALLOWED_MIME.has(file.type)&&!['jpg','jpeg','png','webp'].includes(extension)){setStatusKey('settings_wallpaper_invalid_type',true);return false}
  if(!file.size||file.size>MAX_BYTES){setStatusKey('settings_wallpaper_invalid_size',true);return false}
  _releaseWallpaperDraftUrl();draftFile=file;draftUrl=URL.createObjectURL(file);draftRevision++;draftEdited=true;setStatusKey(null);renderDraft();syncControls();return true;
}
function discardDraft(){_releaseWallpaperDraftUrl();draftFile=null;draftOpacity=saved.opacity;draftScope=saved.scope;draftRevision++;draftEdited=false;render(saved);syncControls()}
function beginWallpaperSettingsSession(){
  if(appearanceActive)return;
  appearanceActive=true;paneGeneration++;draftRevision=0;draftEdited=false;draftFile=null;draftOpacity=saved.opacity;draftScope=saved.scope;bindControls();syncControls();
  const generation=paneGeneration,revision=draftRevision;
  reconcileWallpaperInfo().then(()=>{if(!appearanceActive||generation!==paneGeneration)return;if(revision===draftRevision){draftOpacity=saved.opacity;draftScope=saved.scope;syncControls()}else renderDraft()}).catch(()=>{if(appearanceActive&&generation===paneGeneration&&revision!==draftRevision)renderDraft()});
}
function endWallpaperSettingsSession(){if(!appearanceActive)return;appearanceActive=false;paneGeneration++;render(saved);_releaseWallpaperDraftUrl();draftFile=null;draftRevision=0;draftEdited=false}
async function _requestForTest(kind,file,opacity,scope){
  if(kind==='post')return global.api('/api/wallpaper?opacity='+encodeURIComponent(opacity)+'&scope='+encodeURIComponent(scope),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file,retries:0});
  if(kind==='patch')return global.api('/api/wallpaper',{method:'PATCH',body:JSON.stringify({opacity,scope}),retries:0});
  return global.api('/api/wallpaper',{method:'DELETE',retries:0});
}
function enqueueMutation(kind,file,opacity,scope,owner,revision,url){
  mutationStarted++;
  const run=async()=>{
    requestRunning=true;if(appearanceActive&&owner===paneGeneration){setStatusKey('settings_wallpaper_saving');syncControls()}
    try{
      const raw=await _requestForTest(kind,file,opacity,scope);
      await applyAuthoritativeInfo(raw,null);
      const ownsDraft=appearanceActive&&owner===paneGeneration&&revision===draftRevision&&(kind!=='post'||url===draftUrl);
      if(ownsDraft){if(kind==='post')_releaseWallpaperDraftUrl(url);draftFile=null;draftOpacity=saved.opacity;draftScope=saved.scope;draftEdited=false}
      else if(appearanceActive&&draftEdited)renderDraft();
      else if(appearanceActive){draftOpacity=saved.opacity;draftScope=saved.scope;render(saved)}
      if(ownsDraft)setStatusKey(kind==='delete'?'settings_wallpaper_cleared':'settings_wallpaper_saved');
    }catch(error){
      if(!error||!Number.isFinite(error.status)||error.status>=500){try{await reconcileWallpaperInfo()}catch(_){}finally{if(appearanceActive&&(draftUrl||saved.has_wallpaper))renderDraft()}}
      if(appearanceActive&&owner===paneGeneration)setStatusKey(wallpaperErrorKey(error),true);
    }finally{requestRunning=false;if(appearanceActive)syncControls()}
  };
  mutationTail=mutationTail.then(run,run);return mutationTail;
}
function saveDraft(){if(requestRunning||!dirty())return;draftOpacity=Number(el('wallpaperOpacity').value)/100;draftScope=currentScope();enqueueMutation(draftFile?'post':'patch',draftFile,draftOpacity,draftScope,paneGeneration,draftRevision,draftUrl)}
async function clearDraft(){
  if(requestRunning)return;
  if(!saved.has_wallpaper){discardDraft();setStatusKey('settings_wallpaper_cleared');return}
  const owner=paneGeneration,target=saved.image_version,revision=draftRevision;
  const confirmed=await global.showConfirmDialog({title:text('settings_wallpaper_confirm_clear','Clear the saved wallpaper?'),message:'',confirmLabel:text('settings_wallpaper_clear','Clear'),danger:true,focusCancel:true});
  if(!confirmed||requestRunning||!appearanceActive||owner!==paneGeneration||target!==saved.image_version||revision!==draftRevision)return;
  enqueueMutation('delete',null,0.8,'chat',owner,revision,null);
}
function bindControls(){
  if(bound)return;bound=true;
  const input=el('wallpaperFileInput'),drop=el('wallpaperDropZone'),opacity=el('wallpaperOpacity');
  if(input)input.addEventListener('change',()=>{if(input.files&&input.files[0])installDraftFile(input.files[0]);input.value=''});
  if(drop){drop.addEventListener('click',()=>input&&input.click());drop.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();if(input)input.click()}});drop.addEventListener('dragover',event=>event.preventDefault());drop.addEventListener('drop',event=>{event.preventDefault();if(event.dataTransfer&&event.dataTransfer.files[0])installDraftFile(event.dataTransfer.files[0])})}
  if(opacity)opacity.addEventListener('input',()=>{draftOpacity=Number(opacity.value)/100;draftRevision++;draftEdited=true;renderDraft();syncControls()});
  document.querySelectorAll('input[name="wallpaperScope"]').forEach(radio=>radio.addEventListener('change',()=>{draftScope=currentScope();draftRevision++;draftEdited=true;renderDraft();syncControls()}));
  const save=el('wallpaperSaveBtn'),clear=el('wallpaperClearBtn');if(save)save.addEventListener('click',saveDraft);if(clear)clear.addEventListener('click',clearDraft);
}
function speculativeBoot(){
  try{const raw=localStorage.getItem(CACHE_KEY);if(!raw)return;const info=normalizeInfo(JSON.parse(raw));if(!info.has_wallpaper)throw new Error();saved=info;render(info)}catch(_){try{localStorage.removeItem(CACHE_KEY)}catch(__){}clearRender()}
}
function init(){bindControls();reconcileWallpaperInfo().catch(()=>{})}
speculativeBoot();
if(document.addEventListener){document.addEventListener('DOMContentLoaded',init,{once:true});document.addEventListener('hermes:locale-changed',refreshLocalizedWallpaperText)}
const apiSurface={normalizeInfo,imageUrl,reconcileWallpaperInfo,applyAuthoritativeInfo,_requestForTest,_setSavedForTest(value){saved=normalizeInfo(value)},_releaseWallpaperDraftUrl};
global.HermesWallpaper=apiSurface;global.beginWallpaperSettingsSession=beginWallpaperSettingsSession;global.endWallpaperSettingsSession=endWallpaperSettingsSession;
})(window);
