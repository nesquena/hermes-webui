(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root) root.PluginMentions=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function activeToken(text,cursor){
    const value=String(text||'');
    const raw=Number(cursor);
    const pos=Number.isFinite(raw)?Math.max(0,Math.min(raw,value.length)):value.length;
    let start=pos;
    while(start>0&&!/\s/.test(value[start-1])) start--;
    if(value[start]!=='@') return null;
    if(start>0&&!/\s/.test(value[start-1])) return null;
    const query=value.slice(start+1,pos);
    if(/\s/.test(query)) return null;
    let end=pos;
    while(end<value.length&&!/\s/.test(value[end])) end++;
    return {start,end,query};
  }

  function words(value){
    return String(value||'').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  }

  function rankPlugins(plugins,query){
    const needle=String(query||'').toLowerCase();
    if(!needle) return [];
    return (Array.isArray(plugins)?plugins:[]).map((plugin,index)=>{
      const name=String(plugin&&plugin.name||'');
      const nameLower=name.toLowerCase();
      const keywords=Array.isArray(plugin&&plugin.keywords)?plugin.keywords.map(String):[];
      let tier=-1;
      if(nameLower.startsWith(needle)||words(name).some(word=>word.startsWith(needle))) tier=0;
      else if(keywords.some(keyword=>keyword.toLowerCase().includes(needle))) tier=1;
      else if(String(plugin&&plugin.description||'').toLowerCase().includes(needle)) tier=2;
      return {plugin,tier,index,name:nameLower,path:String(plugin&&plugin.path||'')};
    }).filter(row=>row.tier>=0).sort((a,b)=>
      a.tier-b.tier||a.name.localeCompare(b.name)||a.path.localeCompare(b.path)||a.index-b.index
    ).map(row=>row.plugin);
  }

  function addMention(mentions,plugin){
    const current=Array.isArray(mentions)?mentions:[];
    const mention={name:String(plugin&&plugin.name||''),path:String(plugin&&plugin.path||'')};
    if(!mention.name||!mention.path||current.some(item=>item.path===mention.path)) return current.slice();
    return current.concat(mention);
  }

  function removeToken(text,token){
    const value=String(text||'');
    if(!token) return value;
    return value.slice(0,token.start)+value.slice(token.end);
  }

  function withPayload(payload,mentions){
    const next={...(payload||{})};
    if(Array.isArray(mentions)&&mentions.length){
      next.plugin_mentions=mentions.map(item=>({name:item.name,path:item.path}));
    }
    return next;
  }

  function afterSend(current,submitted,accepted){
    const items=Array.isArray(current)?current:[];
    if(!accepted) return items.slice();
    const paths=new Set((Array.isArray(submitted)?submitted:[]).map(item=>item.path));
    return items.filter(item=>!paths.has(item.path));
  }

  return {activeToken,rankPlugins,addMention,removeToken,withPayload,afterSend};
});

(function(root){
  'use strict';
  if(!root||!root.document||!root.PluginMentions) return;
  let mentions=[];
  let inventory=[];
  let loadPromise=null;
  let matches=[];
  let selected=0;
  let active=null;

  function elements(){
    return {input:document.getElementById('msg'),dropdown:document.getElementById('pluginMentionDropdown')};
  }

  function loadInventory(){
    if(!loadPromise){
      loadPromise=(typeof root.api==='function'?root.api('/api/codex/plugins'):Promise.resolve({plugins:[]}))
        .then(data=>{inventory=Array.isArray(data&&data.plugins)?data.plugins:[];return inventory;})
        .catch(()=>{inventory=[];return inventory;});
    }
    return loadPromise;
  }

  function close(){
    const {input,dropdown}=elements();
    matches=[];active=null;selected=0;
    if(dropdown){dropdown.hidden=true;dropdown.innerHTML='';}
    if(input){input.removeAttribute('aria-activedescendant');input.setAttribute('aria-expanded','false');}
  }

  function renderChips(){
    const input=document.getElementById('msg');
    if(!input)return;
    let wrap=document.getElementById('pluginMentionChips');
    if(!wrap){
      wrap=document.createElement('div');
      wrap.id='pluginMentionChips';wrap.className='plugin-mention-chips';wrap.setAttribute('aria-label','Selected plugins');
      input.parentNode.insertBefore(wrap,input);
    }
    wrap.innerHTML='';wrap.hidden=!mentions.length;
    mentions.forEach(mention=>{
      const chip=document.createElement('span');chip.className='plugin-mention-chip';
      const name=document.createElement('span');name.className='plugin-mention-chip-name';name.textContent='@'+mention.name;
      const remove=document.createElement('button');remove.type='button';remove.className='plugin-mention-chip-remove';remove.textContent='×';remove.setAttribute('aria-label','Remove '+mention.name);
      remove.addEventListener('click',()=>{mentions=mentions.filter(item=>item.path!==mention.path);renderChips();input.focus();});
      chip.append(name,remove);wrap.appendChild(chip);
    });
  }

  function choose(index){
    const plugin=matches[index];
    const {input}=elements();
    if(!plugin||!input||!active)return;
    mentions=root.PluginMentions.addMention(mentions,plugin);
    const next=root.PluginMentions.removeToken(input.value,active);
    const caret=active.start;
    input.value=next;input.focus();input.setSelectionRange(caret,caret);
    renderChips();close();
    input.dispatchEvent(new Event('input',{bubbles:true}));
  }

  function render(){
    const {input,dropdown}=elements();
    if(!input||!dropdown||!matches.length){close();return;}
    dropdown.innerHTML='';
    matches.forEach((plugin,index)=>{
      const option=document.createElement('button');
      option.type='button';option.id='pluginMentionOption'+index;option.className='plugin-mention-option'+(index===selected?' selected':'');
      option.setAttribute('role','option');option.setAttribute('aria-selected',index===selected?'true':'false');
      const name=document.createElement('span');name.className='plugin-mention-option-name';name.textContent='@'+String(plugin.name||'');
      option.appendChild(name);
      if(plugin.description){const desc=document.createElement('span');desc.className='plugin-mention-option-desc';desc.textContent=String(plugin.description);option.appendChild(desc);}
      option.addEventListener('pointerdown',event=>{event.preventDefault();choose(index);});
      dropdown.appendChild(option);
    });
    dropdown.hidden=false;input.setAttribute('role','combobox');input.setAttribute('aria-autocomplete','list');input.setAttribute('aria-controls',dropdown.id);input.setAttribute('aria-expanded','true');input.setAttribute('aria-activedescendant','pluginMentionOption'+selected);
  }

  function update(event){
    const {input}=elements();
    if(!input)return;
    if(event&&(event.isComposing||event.inputType==='insertCompositionText')){close();return;}
    const token=root.PluginMentions.activeToken(input.value,input.selectionStart);
    if(!token||!token.query){close();return;}
    active=token;
    loadInventory().then(()=>{
      const live=root.PluginMentions.activeToken(input.value,input.selectionStart);
      if(!live||live.start!==token.start||live.query!==token.query){return;}
      active=live;matches=root.PluginMentions.rankPlugins(inventory,live.query);selected=0;
      if(matches.length){
        if(typeof root.hideCmdDropdown==='function') root.hideCmdDropdown();
        render();
      }else close();
    });
  }

  function move(delta){
    if(!matches.length)return;
    selected=(selected+delta+matches.length)%matches.length;render();
    const option=document.getElementById('pluginMentionOption'+selected);if(option)option.scrollIntoView({block:'nearest'});
  }

  root.updatePluginMentionAutocomplete=update;
  root.handlePluginMentionKeydown=function(event){
    const {dropdown}=elements();
    if(!dropdown||dropdown.hidden)return false;
    if(event.isComposing||event.keyCode===229)return false;
    if(event.key==='ArrowUp'||event.key==='ArrowDown'){event.preventDefault();move(event.key==='ArrowUp'?-1:1);return true;}
    if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();choose(selected);return true;}
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();return true;}
    return false;
  };
  root.getPluginMentions=()=>mentions.map(item=>({name:item.name,path:item.path}));
  root.clearPluginMentions=function(submitted){
    mentions=root.PluginMentions.afterSend(mentions,Array.isArray(submitted)?submitted:mentions,true);renderChips();
  };
})(typeof window!=='undefined'?window:null);
