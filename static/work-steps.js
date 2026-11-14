/* A presentation of canonical activity_scene_v1 rows. No event subscription,
   timers, task storage, or inferred completion from elapsed time. */
(function(){
  'use strict';
  const text=(key,...args)=>t('work_steps_'+key,...args);
  function state(row,tc,settled){
    if(tc.is_error)return 'failed';
    const raw=String(row.status||'').toLowerCase();
    if(['cancelled','canceled','interrupted','stopped'].includes(raw))return 'stopped';
    if(['waiting_approval','pending_approval'].includes(raw))return 'waiting';
    if(raw==='queued')return 'queued';
    // A finished parent call does not imply its asynchronous helper finished.
    if(tc.name==='subagent_progress'&&window.SynPulseFriendlyTools){
      const status=window.SynPulseFriendlyTools.describe(tc).state;
      return status==='last_seen'?'unknown':status;
    }
    if(['completed','done','success'].includes(raw))return 'done';
    if(['running','pending'].includes(raw))return settled?'unknown':'running';
    if(row.tool?.done===true)return 'done';
    return 'unknown';
  }
  function label(summary){
    const counts=JSON.parse(summary.dataset.stepCounts||'{}');
    const total=Object.values(counts).reduce((sum,n)=>sum+n,0);
    const group=summary.closest('[data-anchor-scene-owner]');
    const elapsed=group?.querySelector('.tool-worklog-label');
    if(elapsed){
      elapsed.textContent=(group.hasAttribute('data-live-anchor-scene-owner')?_activityProcessedElapsedLabel(group):_activitySettledProcessedLabel(group))||t('processed_elapsed','');
      elapsed.dataset.sweepLabel=elapsed.textContent;
    }
    summary.textContent=[text(total===1?'one':'many',total),...['waiting','running','queued','failed','stopped','unknown','done'].filter(key=>counts[key]).map(key=>text(key,counts[key]))].join(' · ');
  }
  window.syncWorkStepOverview=function(group,rows,opts){
    const tools=rows.filter(row=>row.role==='tool');
    const header=group.querySelector('.tool-call-group-summary');
    if(!header)return;
    let summary=header.querySelector('.work-step-summary');
    if(!tools.length){summary?.remove();return;}
    const counts={};
    // Collapsed history keeps rows lazy. Reconcile result errors once per
    // summary instead of rescanning the transcript for every tool.
    const toolOpts=opts?.settled?{...opts,historyErrors:_toolResultErrorsByTid(S.messages)}:opts;
    for(const row of tools){
      const status=state(row,_anchorSceneToolCallFromRow(row,toolOpts),!!opts?.settled);
      counts[status]=(counts[status]||0)+1;
    }
    if(!summary){summary=document.createElement('span');summary.className='work-step-summary';header.insertBefore(summary,header.querySelector('.tool-call-group-chevron'));}
    summary.dataset.stepCounts=JSON.stringify(counts);label(summary);
    group.querySelectorAll('[data-anchor-row-role="tool"]').forEach((node,index)=>{
      const target=node.querySelector('.tool-card-header');if(!target)return;
      let number=target.querySelector('.work-step-number');
      if(!number){number=document.createElement('span');number.className='work-step-number';target.prepend(number);}
      number.textContent=String(index+1);number.setAttribute('aria-label',text('number',index+1));
      if(target.hasAttribute('onclick')){
        target.setAttribute('role','button');target.tabIndex=0;
        target.setAttribute('aria-expanded',String(target.closest('.tool-card').classList.contains('open')));
      }
    });
  };
  window.syncWorkStepsLocale=function(){
    document.querySelectorAll('.work-step-summary').forEach(label);
    document.querySelectorAll('.work-step-number').forEach(number=>number.setAttribute('aria-label',text('number',number.textContent)));
  };
  // Existing click toggles remain authoritative and survive cached HTML restore.
  document.addEventListener('keydown',event=>{
    // Transparent Stream already handles these keys on its own header.
    if(event.defaultPrevented)return;
    const header=event.target.closest?.('.tool-card-header[role="button"]');
    if(header===event.target&&(event.key==='Enter'||event.key===' ')){
      event.preventDefault();if(!event.repeat)header.click();
    }
  });
  document.addEventListener('click',event=>{
    const header=event.target.closest?.('.tool-card-header[role="button"]');
    if(header)header.setAttribute('aria-expanded',String(header.closest('.tool-card').classList.contains('open')));
  });
})();
