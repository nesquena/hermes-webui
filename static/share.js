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

// ── Show a message as plain text when the page would hide part of it ─────────
// renderMd keeps a few HTML tags but drops the attributes it doesn't generate,
// leaves HTML comments in the DOM (where the browser doesn't display them), and
// in some combinations drops text outright. A public share could therefore
// publish text its readers never see. After rendering, each message's words are
// compared with the words the page actually shows. If any word of the published
// text is missing, the whole message is shown as plain text instead.
//
// Words are counted, not just looked up, so a hidden copy of a word that also
// appears elsewhere is still caught. The check uses the real renderer's output,
// so it follows every renderer rule without restating them. A false alarm fails
// safe: that one message is shown as plain text.
//
// What counts as shown: the text of the rendered message, plus link and image
// addresses, image alt text, and the attribute values the renderer itself
// writes from a fixed set (classes other than language-*, loading, target, rel,
// the media-chip tooltip). Addresses and alt text are content readers can reach;
// the fixed attribute values can only cover their own fixed words.

// Elements the browser lays out inline: no word break at their edges. Every
// other element starts and ends a word.
const _SHARE_INLINE_TAGS=new Set(['abbr','b','bdi','bdo','cite','code','data','del','dfn','em','i','ins','kbd','mark','q','s','samp','small','span','strong','sub','sup','time','tt','u','var']);
// Standard HTML element names. Writing one of these as markup in a message does
// not add a word the reader is expected to see; an unknown name does.
const _SHARE_TAG_NAMES=new Set(['a','abbr','address','area','article','aside','audio','b','bdi','bdo','big','blockquote','body','br','button','canvas','caption','center','cite','code','col','colgroup','data','datalist','dd','del','details','dfn','dialog','div','dl','dt','em','embed','fieldset','figcaption','figure','font','footer','form','h1','h2','h3','h4','h5','h6','head','header','hr','html','i','iframe','img','input','ins','kbd','label','legend','li','link','main','mark','meta','meter','nav','noscript','object','ol','optgroup','option','output','p','picture','pre','progress','q','rp','rt','ruby','s','samp','script','section','select','small','source','span','strike','strong','style','sub','summary','sup','svg','table','tbody','td','template','textarea','tfoot','th','thead','time','title','tr','track','tt','u','ul','var','video','wbr']);
// Attribute names that are markup, not words. Values always count. Other data-*
// names stay words: the part after "data-" is free text.
const _SHARE_ATTR_NAMES=new Set(['align','alt','class','colspan','data-katex','data-mermaid-id','dir','download','height','href','id','lang','loading','name','rel','rowspan','span','src','start','style','target','title','type','value','width']);
// A tag written in the message. Names and unquoted values never contain "<",
// so a scan from one "<" stops at the next one and the pass stays linear.
const _SHARE_TAG_RE=/<(\/?)([A-Za-z][\w:-]*)((?:\s+[^\s"'>\/=<]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*\/?>/g;
const _SHARE_ATTR_RE=/([^\s"'>\/=<]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

// Add the words of text to counts. Emphasis and code marks (* _ ~ `) are
// dropped without breaking a word, as the rendered page joins the text around
// them; every other character that isn't a letter, digit or mark breaks a word.
function _shareAddWords(text,counts){
  const words=String(text==null?'':text).normalize('NFC').toLowerCase()
    .replace(/[*_~`]/g,'').split(/[^\p{L}\p{N}\p{M}]+/u);
  for(const w of words){ if(w) counts.set(w,(counts.get(w)||0)+1); }
  return counts;
}

// The words a reader is expected to see for a published message: its text with
// the markup that never shows as words removed (the entities renderMd decodes,
// standard tag and attribute names, the MEDIA: marker, task-list boxes, and a
// one-word code-fence language).
function _shareSourceWords(source){
  const decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
  let s=decode(decode(String(source==null?'':source).replace(/\r\n?/g,'\n')));
  s=s.replace(/MEDIA:/g,' ')
    .replace(/^(\s*[-*+]\s+)\[[ xX]\](?=\s)/gm,'$1')
    .replace(/^( {0,3}`{3,})[ \t]*[\w+-]+[ \t]*$/gm,'$1');
  s=s.replace(_SHARE_TAG_RE,(tag,_close,name,attrs)=>{
    const n=name.toLowerCase();
    if(!_SHARE_TAG_NAMES.has(n)) return tag;
    let kept='';
    String(attrs||'').replace(_SHARE_ATTR_RE,(_m,attr,dq,sq,bare)=>{
      const value=dq!==undefined?dq:(sq!==undefined?sq:(bare!==undefined?bare:''));
      kept+=' '+(_SHARE_ATTR_NAMES.has(attr.toLowerCase())?'':attr)+' '+value+' ';
      return '';
    });
    const edge=_SHARE_INLINE_TAGS.has(n)?'':' ';
    return edge+kept+edge;
  });
  return _shareAddWords(s,new Map());
}

// Words of one attribute value, counted in its written and percent-decoded
// forms (whichever has more of each word), since the renderer may encode an
// address the message wrote plainly, or the other way round.
function _shareAttrWords(value){
  const raw=_shareAddWords(value,new Map());
  let decoded=raw;
  try{ decoded=_shareAddWords(decodeURIComponent(value),new Map()); }catch(_){}
  for(const [w,c] of decoded) if(c>(raw.get(w)||0)) raw.set(w,c);
  return raw;
}

// The words the rendered message shows (see the note above).
function _shareVisibleWords(root){
  const counts=new Map();
  const text=[];
  const add=map=>{ for(const [w,c] of map) counts.set(w,(counts.get(w)||0)+c); };
  const walk=node=>{
    for(const child of Array.from(node.childNodes||[])){
      if(child.nodeType===3){ text.push(child.data); continue; }
      if(child.nodeType!==1) continue;  // comments are never shown
      const tag=String(child.tagName||'').toLowerCase();
      const inline=_SHARE_INLINE_TAGS.has(tag);
      if(!inline) text.push(' ');
      const start=text.length;
      walk(child);
      if(!inline) text.push(' ');
      const ownText=text.slice(start).join('');
      for(const attr of Array.from(child.attributes||[])){
        const name=String(attr.name||'').toLowerCase();
        let value=String(attr.value||'');
        if(name.startsWith('data-')||name==='download'||name==='style') continue;
        if(name==='class') value=value.split(/\s+/).filter(c=>!/^language-/i.test(c)).join(' ');
        // A bare link shows its own address: count it once, not twice.
        if(name==='href'&&_shareSameWords(value,ownText)) continue;
        add(_shareAttrWords(value));
      }
    }
  };
  walk(root);
  add(_shareAddWords(text.join(''),new Map()));
  return counts;
}

function _shareSameWords(a,b){
  const x=_shareAttrWords(a), y=_shareAddWords(b,new Map());
  if(x.size!==y.size) return false;
  for(const [w,c] of x) if(y.get(w)!==c) return false;
  return true;
}

// Words of the published text the page doesn't show (empty when all are shown).
function _shareMissingWords(source,root){
  const shown=_shareVisibleWords(root);
  const missing=[];
  for(const [w,c] of _shareSourceWords(source)) if(c>(shown.get(w)||0)) missing.push(w);
  return missing;
}

function _shareShowPlainText(body,source){
  body.textContent='';
  body.classList.add('share-plain');
  const note=document.createElement('div');
  note.className='share-plain-note';
  note.textContent='Shown as plain text: part of this message would not display with formatting.';
  const pre=document.createElement('pre');
  pre.className='share-plain-text';
  pre.textContent=String(source==null?'':source);
  body.appendChild(note);
  body.appendChild(pre);
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
    const source=String(msg.content||'');
    const bodyHtml=(typeof renderMd==='function')
      ? renderMd(source)
      : `<p>${_shareEscapeHtml(source)}</p>`;
    row.innerHTML=
      `<div class="share-role"><span class="share-role-badge">${_shareEscapeHtml(_shareRoleLabel(msg.role))}</span></div>`+
      `<div class="msg-body share-message-body">${bodyHtml}</div>`;
    // Before highlighting or any other pass changes the rendered message.
    const body=row.querySelector('.share-message-body');
    if(body&&_shareMissingWords(source,body).length) _shareShowPlainText(body,source);
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
