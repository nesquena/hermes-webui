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
// appears elsewhere is still caught (the fixed attribute words below are the
// one exception: each can cover a hidden copy of itself). The check uses the
// real renderer's output, so it follows the renderer's rules without restating
// them. A false alarm fails safe: that one message is shown as plain text.
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

// The five entities renderMd decodes itself, before it looks at tags.
function _shareDecode5(s){
  return String(s).replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
}

// One normalization, applied the same way to both sides: the published text
// (after its markup is handled) and everything the page shows. Entities and
// %-escapes are decoded. Applying the same transform to both keeps them
// comparable wherever the page shows text as written (an entity inside code)
// and wherever the browser decodes it (prose, link addresses), and it never
// gives the page an extra copy of a word.
function _shareNormalize(text){
  const s=_shareDecodeRefs(_shareDecode5(_shareDecode5(String(text==null?'':text))));
  return s.replace(/(?:%[0-9A-Fa-f]{2})+/g,run=>{ try{ return decodeURIComponent(run); }catch(_){ return run; } });
}

// The words a reader is expected to see for a published message: its text with
// the markup that never shows as words removed (standard tag and attribute
// names, the MEDIA: and file:// markers the renderer turns into media, and
// task-list boxes). Code spans and fences keep their text as written.
function _shareSourceWords(source){
  let s=_shareDecode5(_shareDecode5(String(source==null?'':source).replace(/\r\n?/g,'\n')));
  // Everything inside an HTML comment is hidden text, including tag syntax, code
  // marks, list boxes and markers: keep all its words. A browser comment never
  // ends later than the first "-->", so this can only keep more words than the
  // page shows (a false alarm), never fewer.
  s=s.replace(/<!--[\s\S]*?(?:-->|$)/g,c=>' '+c.replace(/[<>[\]`:]/g,' ')+' ');
  // Lift literal code out before anything else is stripped. Inside a code span or
  // fence the page shows the text as written, so the marker, task-box, link and
  // anchor rules below must not touch it: a word shown in code would otherwise
  // pay for the same word hidden elsewhere. The tag pass skips it too.
  // One exception is decided first: the renderer parses MEDIA:<ref> before it sees
  // code, so which tokens it consumes depends on the text around them as written
  // (a closing backtick right before MEDIA: counts). Mark those tokens now; outside
  // code the mark is removed below, inside code it goes back to MEDIA:.
  let mediaMark='\x00SHARE_MEDIA_';
  while(s.includes(mediaMark)) mediaMark+='_';
  mediaMark+='\x00';
  s=s.replace(/MEDIA:([^\s)\]]+)/g,(token,ref,offset,whole)=>
    (typeof _mediaTokenParts==='function'&&_mediaTokenParts(whole,offset,ref))?mediaMark+ref:token);
  const literalCode=[];
  let literalPrefix='\x00SHARE_LITERAL_';
  while(s.includes(literalPrefix)) literalPrefix+='_';
  const literalRe=new RegExp(literalPrefix+'(\\d+)\\x00','g');
  const unlit=t=>t.replace(literalRe,(_m,i)=>literalCode[+i]);
  s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)|`([^`\n]+)`/g,part=>{
    literalCode.push(part.split(mediaMark).join('MEDIA:'));
    return literalPrefix+(literalCode.length-1)+'\x00';
  });
  // Markers are removed only where the renderer consumes them; anywhere else the page shows
  // them as written. MEDIA:<ref> becomes an image or a link when the renderer's own parser
  // takes the token (marked above; MEDIA: with a space or nothing after it stays text). A bare
  // file:// address at a line start or after a space (outside raw <pre>) and a Markdown link or
  // image to one become media links without the scheme.
  s=s.split(mediaMark).join(' ');
  const preRanges=[];
  s.replace(/<pre\b[^>]*>[\s\S]*?<\/pre>/gi,(m,offset)=>{ preRanges.push([offset,offset+m.length]); return m; });
  s=s.replace(/(^|\s)file:\/\/(?=[^\s<>"')\]])|(\]\()file:\/\/(?=[^\s)]+\))/g,(m,bare,link,offset)=>
    preRanges.some(([a,b])=>offset>=a&&offset<b)?m:(bare!==undefined?bare+' ':link));
  s=s.replace(/^([ \t]*[-*+][ \t]+)\[[ xX]\](?=[ \t])/gm,'$1');
  // The source and DOM must use the same normalized equality for label/href. A
  // label may hold a code span: compare its shown text.
  s=s.replace(/\[([^[\]\n]+)\]\(((?:https?:\/\/|file:\/\/|workspace:\/\/|session:\/\/|mailto:|tel:|message:)[^\s)]+)\)/g,(all,label,href)=>
    _shareSameWords(_shareWordsOf(unlit(label)),_shareWordsOf(href))?label:all);
  s=s.replace(/<a\b([^<>]*)>([^<]*)<\/a\s*>/gi,(all,attrs,label)=>{
    let href,hrefCount=0;
    attrs.replace(_SHARE_ATTR_RE,(_m,name,dq,sq,bare)=>{
      if(name.toLowerCase()==='href'){ hrefCount++; href=dq!==undefined?dq:(sq!==undefined?sq:(bare!==undefined?bare:'')); }
      return '';
    });
    // The renderer keeps a raw anchor's address only for these schemes; with any other the
    // address is dropped, so the label alone must show its words.
    if(hrefCount!==1||href===undefined||!/^(?:https?:\/\/|mailto:|tel:|message:)/i.test(href)
      ||!_shareSameWords(_shareWordsOf(unlit(label)),_shareWordsOf(href))) return all;
    const rest=attrs.replace(_SHARE_ATTR_RE,(part,name)=>name.toLowerCase()==='href'?'':part);
    return '<a'+rest+'>'+label+'</a>';
  });
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
  s=unlit(s);
  return _shareAddWords(_shareNormalize(s),new Map());
}

// Decode HTML character references the way the browser does for text: numeric
// ones directly, named ones through a detached textarea (one reference at a
// time, so no markup is ever parsed). Without a DOM, a named reference stays as
// written, which can only cause a false alarm.
const _shareRefCache=new Map();
function _shareDecodeRefs(text){
  return String(text).replace(/&(?:#([0-9]{1,8})|#[xX]([0-9a-fA-F]{1,8})|[A-Za-z][A-Za-z0-9]{1,31});/g,(ref,dec,hex)=>{
    if(dec!==undefined||hex!==undefined){
      const cp=dec!==undefined?parseInt(dec,10):parseInt(hex,16);
      if(!cp||cp>0x10FFFF||(cp>=0xD800&&cp<=0xDFFF)) return '\uFFFD';
      return String.fromCodePoint(cp);
    }
    if(_shareRefCache.has(ref)) return _shareRefCache.get(ref);
    let out=ref;
    try{
      if(typeof document!=='undefined'&&typeof document.createElement==='function'){
        const ta=document.createElement('textarea');
        ta.innerHTML=ref;
        out=ta.value;
      }
    }catch(_){ out=ref; }
    _shareRefCache.set(ref,out);
    return out;
  });
}

function _shareWordsOf(text){ return _shareAddWords(_shareNormalize(text),new Map()); }

// The words the rendered message shows (see the note above).
function _shareVisibleWords(root){
  const counts=new Map();
  const text=[];
  const add=map=>{ for(const [w,c] of map) counts.set(w,(counts.get(w)||0)+c); };
  // `clipped`: inside a block that cuts off anything drawn outside it (code, math, diagram).
  // `literal`: inside code, which shows its text as written (the source side keeps it whole).
  const walk=(node,clipped,literal)=>{
    for(const child of Array.from(node.childNodes||[])){
      if(child.nodeType===3){ text.push(child.data); continue; }
      if(child.nodeType!==1) continue;  // comments are never shown
      const tag=String(child.tagName||'').toLowerCase();
      const attrs=Array.from(child.attributes||[]);
      // A math span stands where "$" or "\(" broke a word in the published text.
      const math=attrs.some(a=>a.name==='data-katex'||(a.name==='class'&&/\bkatex-(?:inline|block)\b/.test(a.value)));
      const inline=_SHARE_INLINE_TAGS.has(tag)&&!math;
      if(!inline) text.push(' ');
      const start=text.length;
      walk(child,clipped||tag==='pre'||attrs.some(a=>a.name==='class'&&/\b(?:katex|mermaid)-block\b/.test(a.value)),
        literal||tag==='code'||tag==='pre');
      if(!inline) text.push(' ');
      const ownText=text.slice(start).join('');
      for(const attr of attrs){
        const name=String(attr.name||'').toLowerCase();
        let value=String(attr.value||'');
        if(name.startsWith('data-')||name==='download'||name==='style') continue;
        if(name==='class') value=value.split(/\s+/).filter(c=>!/^language-/i.test(c)).join(' ');
        // A list item's number is drawn in the margin left of the item. It shows only inside a
        // numbered list, not inside a block that clips that margin, and in full only up to four
        // digits (longer numbers lose their leading digits on a phone). Otherwise it gives no credit.
        if(name==='value'&&tag==='li'&&!(String(node.tagName||'').toLowerCase()==='ol'&&!clipped&&/^(?:0|[1-9]\d{0,3})$/.test(value))) continue;
        // A link the renderer makes inside code (it autolinks addresses there) adds no words:
        // the code's text already shows them, and the source side counts that text once.
        if(literal&&(name==='href'||name==='src')) continue;
        // A bare link shows its own address: count it once, not twice.
        if(name==='href'&&_shareSameWords(_shareWordsOf(value),_shareWordsOf(ownText))) continue;
        add(_shareWordsOf(value));
      }
    }
  };
  walk(root,false,false);
  add(_shareWordsOf(text.join('')));
  return counts;
}

function _shareSameWords(x,y){
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

// An image the server embedded in a shared message, in exactly the form
// api/shares.py writes it. The plain-text view shows it as the image rather
// than as a wall of base64.
const _SHARE_EMBED_RE=/<img src="(data:image\/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+)" class="msg-media-img" alt="([^"<>]*)" loading="lazy">/g;

function _shareShowPlainText(body,source){
  body.textContent='';
  body.classList.add('share-plain');
  const note=document.createElement('div');
  note.className='share-plain-note';
  note.setAttribute('role','note');
  note.textContent='Shown as plain text because formatting could hide part of this message.';
  const pre=document.createElement('pre');
  pre.className='share-plain-text';
  const text=String(source==null?'':source);
  let last=0;
  for(const m of text.matchAll(_SHARE_EMBED_RE)){
    if(typeof _isSafeDataImageUri!=='function'||!_isSafeDataImageUri(m[1])) continue;
    pre.appendChild(document.createTextNode(text.slice(last,m.index)));
    const img=document.createElement('img');
    img.className='msg-media-img share-plain-image';
    img.alt=_shareDecode5(m[2]);
    img.src=m[1];
    pre.appendChild(img);
    last=m.index+m[0].length;
  }
  pre.appendChild(document.createTextNode(text.slice(last)));
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
