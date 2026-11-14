// Personal organization is independent of session ownership and project membership.
(function(root){
  'use strict';
  let state=null, loading=null, retryAt=0;
  const tx=k=>t('sidebar_org_'+k);
  const job=s=>!!(s&&s.cron_job&&s.cron_job.job_id&&s.cron_job.kind!=='scout');
  function repaint(){ if(typeof renderSessionListFromCache==='function') renderSessionListFromCache(); }
  async function ensure(){
    if(state||loading||Date.now()<retryAt)return loading;
    loading=(async()=>{try{state=await api('/api/me/sidebar-organization',{retries:0,timeoutToast:false});}
      catch(_){retryAt=Date.now()+30000;}finally{loading=null;}repaint();})();
    return loading;
  }
  async function save(body){
    await ensure();if(!state)throw new Error(tx('unavailable'));
    try{state=await api('/api/me/sidebar-organization',{method:'POST',body:JSON.stringify({revision:state.revision,...body})});}
    catch(e){state=null;retryAt=0;await ensure();throw e;}
    repaint();
  }
  function orderedSections(data){
    const children=new Map();
    for(const s of data.sections||[]){const key=s.parent||'';if(!children.has(key))children.set(key,[]);children.get(key).push(s);}
    const result=[],seen=new Set(),stack=[...(children.get('')||[])].reverse().map(s=>({s,depth:0,ancestors:[],path:s.name}));
    while(stack.length){
      const row=stack.pop();if(seen.has(row.s.id))continue;seen.add(row.s.id);result.push(row);
      const kids=children.get(row.s.id)||[];
      for(let i=kids.length-1;i>=0;i--)stack.push({s:kids[i],depth:row.depth+1,ancestors:[...row.ancestors,'section:'+row.s.id],path:row.path+' / '+kids[i].name});
    }
    return result;
  }
  // The board reads the same personal organization as the sidebar. Rebuild
  // the flattened tree only when a successful read/write replaces the state.
  let indexedState, sectionIndex;
  function boardSections(){
    if(state!==indexedState){
      indexedState=state;
      const sections=state?orderedSections(state):[];
      sectionIndex={available:!!state, sections, paths:new Map(sections.map(row=>[row.s.id,row.path]))};
    }
    return sectionIndex;
  }
  function sectionFor(session){
    return (!job(session) && state?.assignments[session.session_id]) || '';
  }
  async function assignNewChat(sessionId,section){
    const edit={action:'assign',session_id:sessionId,section};
    try{await save(edit);}
    catch(error){
      // save() refreshed the revision. Never overwrite a concurrent move.
      if(state?.assignments[sessionId]===section)return;
      if(error.status!==409 || state?.assignments[sessionId] || !boardSections().paths.has(section))throw error;
      await save(edit);
    }
  }
  function taskCategory(s,data){
    if(data.task_categories[s.session_id])return data.task_categories[s.session_id];
    if(s.cron_job&&typeof s.cron_job.category==='string'&&s.cron_job.category.trim())return s.cron_job.category;
    const title=String(s.title||'').toLowerCase();
    if(/linkedin|content|marketing|sales|lead|prospect/.test(title))return tx('marketing');
    if(/invoice|factuur|finance|bunq|boekhoud|cashflow/.test(title))return tx('finance');
    if(/health|watchdog|cleanup|clean|token|sync|keepalive|quota|git|backup|vps/.test(title))return tx('maintenance');
    if(/research|onderzoek|briefing|recap|scout/.test(title))return tx('research');
    if(/mail|inbox|draft|calendar|agenda|whatsapp|support/.test(title))return tx('communication');
    return tx('other');
  }
  function taskGroups(rows,data){
    const mode=data.task_grouping||'category_bot', buckets=new Map();
    for(const s of rows){
      const bot=String(s.cron_job?.profile||s.profile||'default');
      const botLabel=bot==='default'?'SynthPulse':bot;
      const category=taskCategory(s,data),label=mode==='bot'?botLabel:category;
      if(!buckets.has(label))buckets.set(label,[]);
      buckets.get(label).push({s,bot,botLabel});
    }
    const result=[];
    for(const [label,items] of [...buckets].sort((a,b)=>a[0].localeCompare(b[0]))){
      const key='tasks:'+mode+':'+label;
      result.push({key,label:label+' ('+items.length+')',items:mode==='category_bot'?[]:items.map(x=>x.s),depth:0,ancestors:[],defaultCollapsed:true});
      if(mode==='category_bot'){
        const bots=new Map();for(const item of items){if(!bots.has(item.bot))bots.set(item.bot,[]);bots.get(item.bot).push(item.s);}
        for(const [bot,sessions] of bots)result.push({key:key+':bot:'+bot,label:(bot==='default'?'SynthPulse':bot)+' ('+sessions.length+')',items:sessions,depth:1,ancestors:[key]});
      }
    }
    return result;
  }
  function groups(original,rows,kind,query,collapsed,data=state){
    if(!data)return original;
    const tasks=rows.filter(job), chats=rows.filter(s=>!job(s));
    const result=[];
    if(kind!=='jobs'){
      const assigned=new Set(), sectionRows=orderedSections(data), bySection=new Map();
      for(const s of chats){
        const section=data.assignments[s.session_id];
        if(section){if(!bySection.has(section))bySection.set(section,[]);bySection.get(section).push(s);}
      }
      const matching=new Set();
      if(query){for(const row of sectionRows)if((bySection.get(row.s.id)||[]).length){matching.add(row.s.id);for(const a of row.ancestors)matching.add(a.slice(8));}}
      for(const row of sectionRows){
        const items=bySection.get(row.s.id)||[];for(const item of items)assigned.add(item.session_id);
        if(query&&!matching.has(row.s.id))continue;
        result.push({key:'section:'+row.s.id,label:row.s.name+' ('+items.length+')',items,depth:row.depth,ancestors:row.ancestors,section:row.s,path:row.path});
      }
      for(const g of original){const items=g.items.filter(s=>!job(s)&&!assigned.has(s.session_id));if(items.length)result.push({...g,items});}
    }else if(chats.length){result.push({key:'active-chat',label:tx('active_chat'),items:chats});}
    result.push(...taskGroups(tasks,data));
    return result.filter(g=>{
      if(g.defaultCollapsed&&collapsed[g.key]===undefined)collapsed[g.key]=true;
      // Search reveals matching descendants without altering saved collapse state.
      if(query){g.forceExpanded=true;return true;}
      return !(g.ancestors||[]).some(k=>collapsed[k]);
    });
  }
  function button(label,click){const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=click;return b;}
  function controls(list,kind){
    void ensure();if(!state)return;
    const bar=document.createElement('div');bar.className='sidebar-org-controls';
    if(kind!=='jobs')bar.appendChild(button('+ '+tx('new'),()=>edit(null,null)));
    if(kind!=='chats'){
      const label=document.createElement('label');label.textContent=tx('group_by');
      const select=document.createElement('select');select.setAttribute('aria-label',tx('group_by'));
      for(const key of ['category_bot','category','bot']){const o=document.createElement('option');o.value=key;o.textContent=tx(key);select.appendChild(o);}
      select.value=state.task_grouping;select.onchange=()=>save({action:'grouping',value:select.value}).catch(e=>showToast(e.message));label.appendChild(select);bar.appendChild(label);
    }
    list.appendChild(bar);
  }
  function decorate(header,group){
    header.style.paddingInlineStart=(8+Math.min(group.depth||0,8)*12)+'px';
    if(group.path)header.title=group.path;
    if(group.section){
      const add=button('+',e=>{e.stopPropagation();edit(null,group.section.id);});add.setAttribute('aria-label',tx('new_sub'));
      const more=button('⋯',e=>{e.stopPropagation();edit(group.section);});more.setAttribute('aria-label',tx('edit'));
      add.className=more.className='sidebar-org-section-action';header.append(add,more);
    }
  }
  function dialog(title){
    const el=document.createElement('dialog');el.className='sidebar-org-dialog';
    const form=document.createElement('form');form.method='dialog';
    const heading=document.createElement('h3');heading.textContent=title;form.appendChild(heading);
    el.appendChild(form);document.body.appendChild(el);
    el.addEventListener('close',()=>el.remove());el.addEventListener('click',e=>{if(e.target===el)el.close();});
    const error=document.createElement('p');error.setAttribute('role','alert');error.className='sidebar-org-error';
    const actions=document.createElement('div');actions.className='sidebar-org-dialog-actions';
    const cancel=button(tx('cancel'),()=>el.close());actions.appendChild(cancel);
    form.append(error,actions);form.onsubmit=e=>e.preventDefault();
    return {el,form,error,actions,add(node){form.insertBefore(node,error);},submit(label,fn){const b=button(label,async()=>{b.disabled=true;try{await fn();el.close();}catch(e){error.textContent=e.message;b.disabled=false;}});actions.appendChild(b);form.onsubmit=e=>{e.preventDefault();b.click();};}};
  }
  function field(title,input){const label=document.createElement('label');label.textContent=title;label.appendChild(input);return label;}
  function selectSection(value,excluded){
    const select=document.createElement('select');const blank=document.createElement('option');blank.value='';blank.textContent=tx('no_section');select.appendChild(blank);
    for(const row of orderedSections(state)){
      if(excluded&&(row.s.id===excluded||row.ancestors.includes('section:'+excluded)))continue;
      const o=document.createElement('option');o.value=row.s.id;o.textContent=row.path;select.appendChild(o);
    }
    select.value=value||'';return select;
  }
  async function edit(section,parent){
    await ensure();if(!state)return showToast(tx('unavailable'));
    const d=dialog(tx(section?'edit':'new')), input=document.createElement('input');input.maxLength=100;input.required=true;input.value=section?.name||'';
    const target=selectSection(section?.parent||parent,section?.id);
    d.add(field(tx('name'),input));d.add(field(tx('parent'),target));
    d.submit(tx('save'),()=>save({action:section?'edit':'create',id:section?.id,name:input.value,parent:target.value||null}));
    if(section){
      const note=document.createElement('p');note.textContent=tx('delete_help');d.add(note);
      d.actions.appendChild(button(tx('delete'),async()=>{try{await save({action:'delete',id:section.id});d.el.close();}catch(e){d.error.textContent=e.message;}}));
    }
    d.el.showModal();input.focus();
  }
  async function moveChat(session,preferredSection){
    await ensure();if(!state)return showToast(tx('unavailable'));
    const isTask=job(session),d=dialog(tx(isTask?'change_category':'move'));
    let input;
    if(isTask){input=document.createElement('input');input.maxLength=100;input.value=state.task_categories[session.session_id]||'';input.placeholder=taskCategory(session,{...state,task_categories:{}});}
    else input=selectSection(state.assignments[session.session_id] || (boardSections().paths.has(preferredSection) ? preferredSection : null));
    d.add(field(tx(isTask?'category':'section'),input));
    d.submit(tx('save'),()=>save(isTask?{action:'category',session_id:session.session_id,category:input.value}:{action:'assign',session_id:session.session_id,section:input.value||null}));
    d.el.showModal();input.focus();
  }
  root.SynthPulseSidebarOrg={ensure,groups,controls,decorate,moveChat,orderedSections,taskGroups,boardSections,sectionFor,assignNewChat,newSection:()=>edit(null,null)};
})(window);
