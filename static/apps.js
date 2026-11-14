/* The server owns app availability, support requests and trusted destinations. */
(function () {
  'use strict';
  let allowed=false, generation=0, rows=null, loading=false, failed=false, language='', query='', category='all';
  let msp={connected:false,manage_apps:false},mutation=false,feedback='',draft=null,requestBusy=false,requestError='',placement='all';
  const $=id=>document.getElementById(id);
  const copy={
    nl:{intro:'Je apps op één plek. Open een app in een nieuw tabblad; sommige apps vragen om een eigen login.',search:'Zoek een app',category:'Categorie',all:'Alle apps',creative:'Ontwerp & video',marketing:'Marketing en SEO',sales:'Sales en CRM',automation:'Automatisering',productivity:'Documenten en kennis',platform:'Beheer en platform',open:'Open app',help:'Uitleg',admin:'Beheer',unconfigured:'Nog geen browseradres gekoppeld',loading:'Apps laden…',error:'Apps konden niet worden geladen.',retry:'Opnieuw proberen',empty:'Er zijn nog geen apps voor jouw account gekoppeld.',noResults:'Geen apps gevonden. Pas je zoekterm of categorie aan.',count:'apps',language:'Taal',guide:'Interactieve gids',sidebar:'Kies een categorie of zoek op naam. Je chat blijft bewaard.'},
    en:{intro:'Your apps in one place. Open an app in a new tab; some apps require their own login.',search:'Search apps',category:'Category',all:'All apps',creative:'Design & video',marketing:'Marketing and SEO',sales:'Sales and CRM',automation:'Automation',productivity:'Documents and knowledge',platform:'Management and platform',open:'Open app',help:'Guide',admin:'Admin',unconfigured:'No browser address linked yet',loading:'Loading apps…',error:'Apps could not be loaded.',retry:'Try again',empty:'No apps have been linked for your account yet.',noResults:'No apps found. Change your search or category.',count:'apps',language:'Language',guide:'Interactive guide',sidebar:'Choose a category or search by name. Your chat is preserved.'}
  };
  function lang(){if(language)return language;try{const v=localStorage.getItem('sp-apps-language')||localStorage.getItem('sp-feature-guide-language');if(copy[v])return v;}catch(_){}return document.documentElement.lang.startsWith('nl')?'nl':'en';}
  function el(tag,text,cls){const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;}
  function btn(text,fn){const n=el('button',text,'app-button');n.type='button';n.addEventListener('click',fn);return n;}
  function localized(value){return typeof value==='string'?value:value?.[lang()]||value?.en||'';}
  function appId(row){return row.id||row.module;}
  function validUrl(value){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&!u.search&&!u.hash?u.href:null;}catch(_){return null;}}
  function guide(id,trigger){window.SynthPulseHelp?.startFeatureTour(id==='overview'&&msp.connected?['webui.apps.overview','webui.apps.availability','webui.apps.request','webui.apps.support']:['webui.apps.'+id],{locale:lang(),trigger,fallbackId:'webui.modules.apps'});}
  const categories=['creative','marketing','sales','automation','productivity','platform'];
  function startRequest(id=''){
    if(requestBusy||mutation||draft||!msp.request_apps)return;
    draft={app_id:id,scope:'organisation',departments:[],note:''};requestError='';render();$('appRequestApp')?.focus();
  }
  function renderRequest(root){
    if(!draft||!msp.request_apps||loading||failed)return;
    const nl=lang()==='nl',form=el('form',null,'apps-request'),fields=el('fieldset');fields.disabled=requestBusy;
    form.append(el('h2',nl?'App aanvragen':'Request app'));
    form.append(el('p',nl?'Eén gedeelde inrichting voor de organisatie of meerdere afdelingen, of een uitbreiding van deze AI-omgeving.':'One shared setup for your organisation or departments, or an addition to this AI environment.'));
    const appLabel=el('label',nl?'Welke app wil je gebruiken?':'Which app would you like to use?'),appSelect=el('select');appSelect.id='appRequestApp';appSelect.required=true;
    const placeholder=el('option',nl?'Kies een app':'Choose an app');placeholder.value='';appSelect.append(placeholder);
    for(const app of msp.requestable_apps||[]){const option=el('option',localized(app.name));option.value=app.id;appSelect.append(option);}
    appSelect.value=draft.app_id;appSelect.addEventListener('change',()=>{draft.app_id=appSelect.value;requestError='';render();$('appRequestApp')?.focus();});appLabel.append(appSelect);fields.append(appLabel);
    const pending=(msp.app_intakes||[]).filter(item=>item.app_id===draft.app_id);
    if(pending.length)fields.append(el('p',(nl?'Deze app heeft al openstaande aanvragen: ':'This app already has open requests: ')+pending.map(item=>'#'+item.number).join(', ')+(nl?'. Voor dezelfde groep voegen we je toelichting toe aan de bestaande aanvraag.':'. For the same group, we add your note to the existing request.')));
    const scopeLabel=el('label',nl?'Wie gaan dit gebruiken?':'Who will use this?'),select=el('select');select.id='appRequestScope';
    for(const [value,label] of [['organisation',nl?'Hele organisatie':'Whole organisation'],['departments',nl?'Eén of meer afdelingen':'One or more departments'],['environment',nl?'Deze AI-omgeving':'This AI environment']]){const o=el('option',label);o.value=value;select.append(o);}
    select.value=draft.scope;select.addEventListener('change',()=>{draft.scope=select.value;draft.departments=[];render();$('appRequestScope')?.focus();});scopeLabel.append(select);fields.append(scopeLabel);
    if(draft.scope==='departments'){
      const group=el('fieldset',null,'app-request-departments');group.append(el('legend',nl?'Selecteer afdelingen':'Select departments'));
      for(const d of msp.departments||[]){const label=el('label'),check=el('input');check.type='checkbox';check.checked=draft.departments.includes(d.id);check.addEventListener('change',()=>{draft.departments=(check.checked?[...draft.departments,d.id]:draft.departments.filter(id=>id!==d.id)).sort();});label.append(check,el('span',d.name));group.append(label);}
      if(!(msp.departments||[]).length)group.append(el('p',nl?'Er zijn nog geen afdelingen ingericht. Kies de hele organisatie of deze omgeving.':'No departments are configured yet. Choose the whole organisation or this environment.'));
      fields.append(group);
    }
    const noteLabel=el('label',nl?'Wat wil je samen kunnen doen?':'What would you like to do together?'),note=el('textarea');note.required=true;note.minLength=12;note.maxLength=2000;note.rows=4;note.value=draft.note;note.addEventListener('input',()=>{draft.note=note.value;});noteLabel.append(note);fields.append(noteLabel);
    fields.append(el('p',nl?'Je ontvangt eerst een voorstel voor inrichting, toegangsrechten en kosten. Aanvragen activeert nog geen app of betaling.':'You receive a proposal for setup, access and costs first. A request does not activate an app or payment.','app-request-note'));
    if(requestError){const error=el('p',requestError);error.setAttribute('role','alert');fields.append(error);}
    const actions=el('div',null,'app-actions'),submit=el('button',requestBusy?(nl?'Aanvraag opslaan…':'Saving request…'):(nl?'Vraag een voorstel aan':'Request a proposal'),'app-button app-open');submit.type='submit';
    actions.append(submit,btn(nl?'Annuleren':'Cancel',()=>{draft=null;requestError='';render();$('appsRequest')?.focus();}));fields.append(actions);form.append(fields);
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(requestBusy||!draft)return;
      if(!(msp.requestable_apps||[]).some(app=>app.id===draft.app_id)){requestError=nl?'Kies een app uit de lijst.':'Choose an app from the list.';render();return;}
      if(draft.scope==='departments'&&!draft.departments.length){requestError=nl?'Kies minimaal één afdeling.':'Choose at least one department.';render();return;}
      const payload={app_id:draft.app_id,scope:draft.scope,departments:draft.departments,note:draft.note.trim()},fingerprint=JSON.stringify(payload);
      if(fingerprint!==draft.fingerprint){draft.key=crypto.randomUUID();draft.fingerprint=fingerprint;}
      const mine=generation;requestBusy=true;requestError='';render();
      try{const result=await api('/api/msp/app-intake',{method:'POST',body:JSON.stringify({...payload,request_key:draft.key})});if(mine!==generation||!allowed)return;
        feedback=(result.existing?(nl?'Toegevoegd aan de gezamenlijke aanvraag #':'Added to shared request #'):(nl?'Aanvraag ontvangen #':'Request received #'))+result.number+(nl?'. Je volgt het voorstel bij Support.':'. Follow the proposal in Support.');draft=null;requestBusy=false;await load();
      }catch(_){if(mine!==generation||!allowed)return;requestBusy=false;requestError=nl?'Je aanvraag is niet bevestigd. Je tekst is bewaard; probeer opnieuw.':'Your request was not confirmed. Your text is preserved; please retry.';render();}
    });root.append(form);
  }
  function renderList(){
    const root=$('appsList'),count=$('appsCount');if(!root)return;root.replaceChildren();const c=copy[lang()];
    if(feedback){const message=el('p',feedback,'msp-notice');message.setAttribute('role','status');root.append(message);}
    if(loading){root.append(el('p',c.loading));count.textContent='';return;}
    if(failed){root.append(el('p',c.error),btn(c.retry,load));count.textContent='';return;}
    const all=rows||[],term=query.trim().toLocaleLowerCase();
    const matches=all.filter(r=>(placement==='all'||(r.placement==='shared'?'shared':'environment')===placement)&&(category==='all'||(r.category||'creative')===category)&&[appId(r),localized(r.name),localized(r.description),appId(r)==='omniroute'?'omnirouter':''].join(' ').toLocaleLowerCase().includes(term));
    count.textContent=matches.length+' / '+all.length+' '+c.count;
    if(!matches.length){root.append(el('p',all.length?c.noResults:c.empty));return;}
    for(const group of categories){
      const items=matches.filter(r=>(r.category||'creative')===group);if(!items.length)continue;
      const section=el('section',null,'apps-group');section.append(el('h2',c[group]));const grid=el('div',null,'apps-grid');section.append(grid);root.append(section);
      for(const r of items){
        const id=appId(r),name=localized(r.name),card=el('article',null,'app-card');card.dataset.appId=id;card.tabIndex=-1;
        const heading=el('div',null,'app-card-heading');heading.append(el('h3',name));if(r.access==='platform')heading.append(el('span',c.admin,'app-badge'));
        card.append(heading,el('p',localized(r.description)||c.intro,'app-description'));
        if(r.placement==='shared')card.append(el('p',r.shared_scope==='organisation'?(lang()==='nl'?'Gedeelde organisatieruimte':'Shared organisation space'):(lang()==='nl'?'Gedeeld met: ':'Shared with: ')+(r.shared_departments||[]).join(', '),'app-shared-label'));
        const actions=el('div',null,'app-actions'),url=validUrl(r.url);
        if(url){const action=c.open;const link=el('a',action+' ↗','app-button app-open');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.setAttribute('aria-label',action+': '+name);actions.append(link);}
        else card.append(el('p',r.available&&r.enabled===false?(lang()==='nl'?'Uitgeschakeld in SynthPulse':'Disabled in SynthPulse'):c.unconfigured,'app-unconfigured'));
        if(r.id){const help=btn(c.help,()=>guide(id,help));help.setAttribute('aria-label',c.help+': '+name);actions.append(help);}
        card.append(actions);
        if(msp.connected && r.support_levels){
          const nl=lang()==='nl';
          const change=(path,payload)=>mutate(path,{app_id:id,...payload});
          if(msp.manage_apps && r.available){
            const toggle=btn((r.enabled?(nl?'Aan':'On'):(nl?'Uit':'Off'))+' · SynthPulse',()=>change('/api/msp/app-state',{enabled:!r.enabled}));
            toggle.classList.add('app-state-toggle');toggle.setAttribute('role','switch');toggle.setAttribute('aria-checked',String(r.enabled));toggle.setAttribute('aria-label',(nl?'App beschikbaar: ':'App available: ')+name);toggle.disabled=mutation;card.append(toggle);

          }
          const support=el('div',null,'app-support'),sub=r.support;
          support.append(el('strong',nl?'Support & uitbreidingen':'Support & upgrades'));
          if(msp.request_apps&&(msp.requestable_apps||[]).some(app=>app.id===id)){
            const request=btn(nl?'App aanvragen':'Request app',()=>startRequest(id));request.disabled=requestBusy||mutation||!!draft;support.append(request);
            const pending=(msp.app_intakes||[]).filter(item=>item.app_id===id);
            if(pending.length)support.append(el('p',(nl?'Openstaande aanvragen: ':'Open requests: ')+pending.map(item=>'#'+item.number+' · '+(item.scope==='organisation'?(nl?'organisatiebreed':'organisation'):item.scope==='departments'?item.departments.join(', '):(nl?'AI-omgeving':'AI environment'))).join('; ')));
          }
          if(sub)support.append(el('p',(sub.active_level||sub.requested_level||'')+' · '+(sub.status==='active'?(nl?'Actief':'Active'):sub.status)));
          support.append(btn(nl?'Aanbod bekijken':'View offers',()=>window.SynthPulseMSP?.openOrders(id)));
          if(msp.manage_apps&&sub&&!['cancelled','rejected','cancel_requested'].includes(sub.status)){const cancel=btn(nl?'Support opzeggen':'Cancel support',()=>change('/api/msp/app-support',{level:'cancel'}));cancel.disabled=mutation;support.append(cancel);}
          card.append(support);
        }
        grid.append(card);
      }
    }
  }
  function render(){
    const root=$('mainApps');if(!root)return;root.replaceChildren();if(!allowed)return;const c=copy[lang()];
    const head=el('div',null,'apps-heading');head.append(el('h1','Apps'));
    const locale=el('select');locale.id='appsLanguage';locale.setAttribute('aria-label',c.language);
    for(const [value,text] of [['nl','Nederlands'],['en','English']]){const o=el('option',text);o.value=value;locale.append(o);}locale.value=lang();locale.addEventListener('change',()=>{language=locale.value;feedback='';try{localStorage.setItem('sp-apps-language',language);}catch(_){}render();$('appsLanguage')?.focus();});
    const guideButton=btn(c.guide,()=>guide('overview',guideButton));head.append(guideButton,locale);root.append(head,el('p',c.intro,'apps-intro'));
    if(!loading&&!failed&&msp.request_apps&&(msp.requestable_apps||[]).length){const request=btn(lang()==='nl'?'App aanvragen':'Request app',()=>startRequest());request.id='appsRequest';request.disabled=requestBusy||mutation||!!draft;root.append(request);}
    if(!loading&&!failed&&!msp.connected){const requestLink=el('a',lang()==='nl'?'Functionaliteit aanvragen':'Request functionality','app-button');requestLink.href='https://www.synthwave.solutions/synthpulse/account?tab=packages';requestLink.target='_blank';requestLink.rel='noopener noreferrer';root.append(requestLink);}
    renderRequest(root);
    const toolbar=el('div',null,'apps-toolbar'),searchLabel=el('label',c.search),search=el('input');search.id='appsSearch';search.type='search';search.placeholder=c.search;search.value=query;search.addEventListener('input',()=>{query=search.value;renderList();});searchLabel.append(search);
    const filterLabel=el('label',c.category),filter=el('select');filter.id='appsCategory';
    for(const key of ['all',...categories]){if(key!=='all'&&!(rows||[]).some(r=>(r.category||'creative')===key))continue;const option=el('option',c[key]);option.value=key;filter.append(option);}if(![...filter.options].some(o=>o.value===category))category='all';filter.value=category;filter.addEventListener('change',()=>{category=filter.value;renderList();});filterLabel.append(filter);toolbar.append(searchLabel,filterLabel);root.append(toolbar);
    if((rows||[]).some(r=>r.placement==='shared')){const label=el('label',lang()==='nl'?'Werkruimte':'Workspace'),select=el('select');select.id='appsPlacement';for(const [value,title] of [['all',lang()==='nl'?'Alle werkruimtes':'All workspaces'],['shared',lang()==='nl'?'Gedeelde organisatieruimte':'Shared organisation space'],['environment',lang()==='nl'?'Deze AI-omgeving':'This AI environment']]){const option=el('option',title);option.value=value;select.append(option);}select.value=placement;select.addEventListener('change',()=>{placement=select.value;renderList();});label.append(select);toolbar.append(label);}else placement='all';
    const count=el('p',null,'apps-count');count.id='appsCount';count.setAttribute('role','status');const list=el('div');list.id='appsList';root.append(count,list);
    if($('appsSidebar'))$('appsSidebar').textContent=c.sidebar;
    renderList();
  }
  function applyVisible(value){allowed=value===true;msp={connected:false,manage_apps:false};mutation=false;feedback='';draft=null;requestBusy=false;requestError='';placement='all';generation++;rows=null;loading=false;failed=false;query='';category='all';if(!allowed){$('mainApps')?.replaceChildren();$('appsSidebar')?.replaceChildren();}}
  async function load(){
    if(!allowed||requestBusy)return false;const mine=++generation;loading=true;failed=false;render();
    try{const result=await api('/api/apps');if(mine!==generation||!allowed)return false;rows=Array.isArray(result.apps)?result.apps:[];msp=result.msp||{connected:false,manage_apps:false};loading=false;render();return true;}
    catch(_){if(mine===generation&&allowed){rows=null;loading=false;failed=true;render();}return false;}
  }
  async function mutate(path,payload){
    if(mutation||requestBusy)return;const mine=generation;mutation=true;feedback='';renderList();
    try{await api(path,{method:'POST',body:JSON.stringify(payload)});if(mine!==generation||!allowed)return;
      feedback=lang()==='nl'?'Opgeslagen. Aanvragen vind je terug bij Support en je tickets.':'Saved. Requests appear in Support and your tickets.';
      mutation=false;await load();
    }catch(_){if(mine===generation&&allowed){mutation=false;feedback=lang()==='nl'?'Wijziging niet bevestigd. Vernieuw om de actuele status te controleren.':'Change not confirmed. Refresh to check the current status.';renderList();}}
  }
  function reveal(id){
    if(!allowed)return;const previous={query,category,placement};const changed=query!==''||category!=='all'||placement!=='all';
    function filters(){if($('appsSearch'))$('appsSearch').value=query;if($('appsCategory'))$('appsCategory').value=category;if($('appsPlacement'))$('appsPlacement').value=placement;renderList();}
    query='';category='all';placement='all';if(changed)filters();
    const card=[...document.querySelectorAll('#mainApps [data-app-id]')].find(n=>n.dataset.appId===id);card?.scrollIntoView({block:'nearest'});
    return ()=>{if(allowed&&changed){query=previous.query;category=previous.category;placement=previous.placement;filters();}};
  }
  window.SynthPulseApps={applyVisible,load,reveal};
})();
