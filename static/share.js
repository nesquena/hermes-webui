function _shareTokenFromPath(){
  const path=window.location.pathname||'';
  const m=path.match(/\/share\/([^/?#]+)/);
  return m?decodeURIComponent(m[1]):'';
}

function _shareEscapeHtml(text){
  return String(text==null?'':text)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

function _shareRoleLabel(role){
  if(role==='user') return 'User';
  if(role==='assistant') return 'Assistant';
  return 'System';
}

function _shareRenderMessages(messages){
  const wrap=$('shareTranscript');
  if(!wrap) return;
  if(!Array.isArray(messages)||!messages.length){
    wrap.innerHTML='<div class="share-empty">This shared conversation has no visible messages.</div>';
    return;
  }
  wrap.innerHTML='';
  messages.forEach(msg=>{
    const row=document.createElement('article');
    row.className='share-message';
    row.dataset.role=String(msg.role||'assistant');
    const bodyHtml=(typeof renderMd==='function')
      ? renderMd(String(msg.content||''))
      : `<p>${_shareEscapeHtml(msg.content||'')}</p>`;
    row.innerHTML=
      `<div class="share-role"><span class="share-role-badge">${_shareEscapeHtml(_shareRoleLabel(msg.role))}</span></div>`+
      `<div class="msg-body share-message-body">${bodyHtml}</div>`;
    wrap.appendChild(row);
  });
  if(typeof highlightCode==='function') highlightCode(wrap);
}

function _shareSetError(message){
  const title=$('shareTitle');
  const meta=$('shareMeta');
  const wrap=$('shareTranscript');
  if(title) title.textContent='Share unavailable';
  if(meta) meta.textContent='This public snapshot could not be loaded.';
  if(wrap) wrap.innerHTML=`<div class="share-error"><strong>Could not open this share.</strong><div style="margin-top:8px">${_shareEscapeHtml(message||'The link may have expired or been revoked.')}</div></div>`;
}

async function _shareLoad(){
  const token=_shareTokenFromPath();
  if(!token){
    _shareSetError('Missing share token.');
    return;
  }
  try{
    const data=await fetch(new URL(`/api/share/${encodeURIComponent(token)}`,window.location.origin).href,{credentials:'same-origin',cache:'no-store'});
    if(!data.ok){
      let message='The link may have expired or been revoked.';
      try{
        const payload=await data.json();
        if(payload&&payload.error) message=payload.error;
      }catch(_){}
      _shareSetError(message);
      return;
    }
    const payload=await data.json();
    const share=payload&&payload.share;
    if(!share||!Array.isArray(share.messages)){
      _shareSetError('Malformed share payload.');
      return;
    }
    const title=$('shareTitle');
    const meta=$('shareMeta');
    if(title) title.textContent=share.title||'Untitled';
    if(meta){
      const count=Number(share.message_count||share.messages.length||0);
      meta.textContent=`${count} message${count===1?'':'s'} - public read-only snapshot`;
    }
    _shareRenderMessages(share.messages);
  }catch(err){
    _shareSetError(err&&err.message?err.message:String(err||'Failed to load share.'));
  }
}

async function _shareCopyLink(){
  const btn=$('shareCopyBtn');
  const text=window.location.href;
  const done=()=>{
    if(btn){
      const original=btn.textContent;
      btn.textContent='Copied!';
      setTimeout(()=>{btn.textContent=original;},1200);
    }
  };
  if(typeof _copyText==='function'){
    try{
      await _copyText(text);
      done();
      return;
    }catch(_){}
  }
  try{
    if(navigator&&navigator.clipboard&&navigator.clipboard.writeText){
      await navigator.clipboard.writeText(text);
      done();
    }
  }catch(_){}
}

document.addEventListener('DOMContentLoaded',()=>{
  const copyBtn=$('shareCopyBtn');
  if(copyBtn) copyBtn.addEventListener('click',_shareCopyLink);
  _shareLoad();
});
