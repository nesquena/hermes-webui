/* The server projects authorized schedules. Calendar dates use the viewer's time zone. */
(() => {
  let view = 'week', selected = new Date(), generation = 0, visible = false;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const dateKey = d => [d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');
  const local = (d, shift=0) => new Date(d.getFullYear(),d.getMonth(),d.getDate()+shift,12);
  const format = (date, options) => new Intl.DateTimeFormat(document.documentElement.lang || undefined, options).format(date);
  const tx = key => t('agenda_' + key);
  const button = (label, act) => {
    const b=document.createElement('button'); b.type='button'; b.className='sm-btn'; b.textContent=label; b.onclick=act; return b;
  };
  function period() {
    const first = view === 'month' ? new Date(selected.getFullYear(),selected.getMonth(),1,12) :
      local(selected, view === 'week' ? -((selected.getDay()+6)%7) : 0);
    const size = view === 'month' ? new Date(first.getFullYear(),first.getMonth()+1,0).getDate() : view === 'week' ? 7 : 1;
    return {first,size};
  }
  function move(n) {
    if (view === 'month') selected=new Date(selected.getFullYear(),selected.getMonth()+n,1,12);
    else selected=local(selected,n*(view === 'week' ? 7 : 1));
    reload(n < 0 ? 'prev' : 'next');
  }
  function controls(root, first, size) {
    const header=document.createElement('header'), title=document.createElement('h2'), meta=document.createElement('p');
    title.textContent=tx('title'); meta.textContent=tx('scope')+' · '+zone; header.append(title,meta);
    const tools=document.createElement('div'); tools.className='agenda-toolbar';
    const modes=document.createElement('div'); modes.className='om-seg'; modes.setAttribute('role','group'); modes.setAttribute('aria-label',tx('view'));
    for (const key of ['day','week','month']) {
      const b=button(tx(key),()=>{view=key;reload(key);});
      b.dataset.agendaView=key; b.dataset.control=key; b.setAttribute('aria-pressed',String(view===key)); modes.append(b);
    }
    const nav=document.createElement('div'); nav.className='agenda-nav';
    const prev=button('‹',()=>move(-1)), next=button('›',()=>move(1)), today=button(tx('today'),()=>{selected=new Date();reload('today');});
    [[prev,'prev'],[next,'next'],[today,'today']].forEach(([b,k])=>{b.dataset.control=k;b.setAttribute('aria-label',tx(k));});
    nav.append(prev,today,next);
    const heading=document.createElement('h3'), end=local(first,size-1);
    heading.textContent=view==='month' ? format(first,{month:'long',year:'numeric'}) : view==='day' ?
      format(first,{weekday:'long',day:'numeric',month:'long',year:'numeric'}) :
      new Intl.DateTimeFormat(document.documentElement.lang,{day:'numeric',month:'short',year:'numeric'}).formatRange(first,end);
    tools.append(modes,nav); root.append(header,tools,heading);
  }
  function draw(host, first, size, data) {
    const jobs=new Map(data.jobs.map(j=>[JSON.stringify([j.owner_profile,j.id]),j]));
    const byDay=new Map();
    for (const entry of data.days) {
      const job=jobs.get(JSON.stringify([entry.owner,entry.id]));
      if (!job) continue;
      if (!byDay.has(entry.day)) byDay.set(entry.day,[]);
      byDay.get(entry.day).push({...entry,job});
    }
    const today=dateKey(new Date());
    const grid=document.createElement('div'); grid.className='agenda-grid agenda-'+view;
    if (view==='month') {
      const monday=local(first,-((first.getDay()+6)%7));
      for(let n=0;n<7;n++) { const label=document.createElement('span'); label.className='agenda-weekday'; label.textContent=format(local(monday,n),{weekday:'short'}); grid.append(label); }
    }
    for(let i=0;i<size;i++) {
      const date=local(first,i), day=dateKey(date), rows=(byDay.get(day)||[]).sort((a,b)=>a.first-b.first || a.job.name.localeCompare(b.job.name));
      const count=rows.reduce((sum,r)=>sum+r.count,0);
      if(view==='month') {
        const b=button('',()=>{selected=date;view='day';reload('day');}); b.className='agenda-date';
        b.dataset.agendaDay=day;b.dataset.populated=String(count>0);
        if(i===0) b.style.gridColumnStart=String((date.getDay()+6)%7+1);
        const num=document.createElement('span');num.textContent=String(date.getDate()); b.append(num);
        if(count) {const badge=document.createElement('small');badge.textContent=String(count);b.append(badge);}
        b.setAttribute('aria-label',format(date,{dateStyle:'full'})+' · '+count+' '+tx('runs'));
        if(day===today)b.setAttribute('aria-current','date');
        grid.append(b);continue;
      }
      const group=document.createElement('section'); group.className='agenda-group';
      if(view==='week') {
        const b=button(format(date,{weekday:'long',day:'numeric',month:'short'}),()=>{selected=date;view='day';reload('day');});
        b.dataset.agendaDay=day;b.className='agenda-day-heading';group.append(b);
        if(day===today)b.setAttribute('aria-current','date');
      }
      if(!rows.length){
        const p=document.createElement('p');p.className='agenda-quiet';p.textContent=day<today?tx('past'):tx('empty');group.append(p);
      }
      for(const r of rows) {
        const b=button('',()=>openCronFromBot(r.id,r.owner,r.job.assigned_bot || r.owner)); b.className='agenda-job';
        const at=document.createElement('time');at.dateTime=new Date(r.first).toISOString();at.textContent=format(r.first,{hour:'2-digit',minute:'2-digit'});
        const label=document.createElement('strong');label.textContent=r.job.name || t('untitled');
        const sub=document.createElement('small');sub.textContent=[_cronBotTitle(r.job.assigned_bot || r.owner),r.count>1 ? r.count+' '+tx('runs') : ''].filter(Boolean).join(' · ');
        b.append(at,label,sub);group.append(b);
        if(view==='day' && r.count>1) {
          const detail=document.createElement('details'), summary=document.createElement('summary'), times=document.createElement('div');
          summary.textContent=tx('times')+' ('+r.count+')'; detail.className='agenda-times';
          detail.append(summary,times);group.append(detail);
          detail.ontoggle=()=>{
            if(!detail.open || times.childElementCount)return;
            for(let i=0;i<r.count;i++) {
              const instant=r.times?.[i] ?? r.first+i*r.step_ms;
              const tag=document.createElement('time');tag.dateTime=new Date(instant).toISOString();
              tag.textContent=format(instant,{hour:'2-digit',minute:'2-digit',timeZoneName:'short'});times.append(tag);
            }
          };
        }
      }
      grid.append(group);
    }
    host.replaceChildren(grid);
    if(view==='month') {
      const legend=document.createElement('p');legend.className='agenda-quiet';legend.textContent=tx('month_hint');host.prepend(legend);
    }
    if(data.partial){const p=document.createElement('p');p.className='agenda-quiet';p.textContent=tx('partial');host.prepend(p);}
    if(!data.jobs.length && typeof _cronCanCreate==='function' && _cronCanCreate()) {
      host.prepend(button(t('tasks_first_cta'),()=>openCronCreate()));
    }
  }
  async function reload(focus='') {
    if(!visible || _currentPanel !== 'tasks')return;
    const ticket=++generation, root=$('cronAgenda'), {first,size}=period(), wrap=document.createElement('div');
    wrap.className='agenda-sheet'; controls(wrap,first,size);
    const host=document.createElement('div');host.setAttribute('aria-busy','true');host.textContent=t('loading');
    wrap.append(host);root.replaceChildren(wrap);
    if(focus) wrap.querySelector('[data-control="'+focus+'"]')?.focus({preventScroll:true});
    try{
      const query=new URLSearchParams({all_profiles:'1',agenda_start:dateKey(first),agenda_days:String(size),agenda_zone:zone});
      const data=await api('/api/crons?'+query,{timeoutToast:false});
      if(ticket!==generation || !visible)return;
      if(!Array.isArray(data.days) || !Array.isArray(data.jobs))throw new Error();
      draw(host,first,size,data);
    } catch(_) {
      if(ticket!==generation || !visible)return;
      host.textContent=tx('failure')+' '; host.append(button(t('retry'),()=>reload()));
    } finally {host.removeAttribute('aria-busy');}
  }
  function leave() { visible=false; ++generation; $('cronAgenda')?.replaceChildren(); }
  function mode(mode) {
    const root=$('cronAgenda'); if(!root)return;
    $('cronAgendaBack').hidden=mode!=='read';
    const show=mode==='empty' && _currentPanel==='tasks';
    root.hidden=!show;
    if(!show){leave();return;}
    $('taskDetailEmpty').style.display='none';
    if(!visible){visible=true;reload();}
  }
  function open() {
    if(!['read','empty'].includes(_cronMode))return;
    document.querySelectorAll('.cron-item.active').forEach(el=>el.classList.remove('active'));
    _clearCronDetail();closeMobileSidebar();$('cronAgenda')?.querySelector('[aria-pressed="true"]')?.focus();
  }
  window.CronAgenda={reload,open,mode,leave};
})();
