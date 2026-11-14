/* Customer service: authenticated proxy, central ticket and billing records. */
(function(){
 'use strict';
 let allowed=false,generation=0,overview=null,tickets=[],detail=null,tab='overview',scope='mine',language='',busy=false,error='',notice='';
 const draft={title:'',body:'',need:'question',impact:'me',service:''};let consultantDays=null;let reply='';let orderFilter='';let invoiceFilter='all';
 const $=id=>document.getElementById(id);
 const words={nl:{title:'Support',intro:'Support en betalingen',overview:'Overzicht',tickets:'Tickets',billing:'Betalingen',refresh:'Vernieuwen',loading:'Laden...',error:'De MSP-verbinding is niet beschikbaar. Probeer opnieuw; je invoer blijft bewaard.',hours:'Supporturen',balance:'Beschikbaar',used:'Deze maand gebruikt',allowance:'Maandbundel',minutes:'minuten',noContract:'Er is nog geen actief supportcontract ingesteld. Je kunt wel een ticket of app-aanvraag indienen.',restricted:'Je beheerder beheert de urenbundels en betalingen.',subscriptions:'Support per app',none:'Urenstrippenkaarten per beheerde app horen bij je jaarlijkse supportafspraak. Kies je gewenste niveau bij Apps.',apps:'Naar Apps',newTicket:'Ticket aanmaken',titleLabel:'Onderwerp',body:'Waarmee kunnen we helpen?',need:'Soort vraag',question:'Vraag',broken:'Iets werkt niet',access:'Toegang nodig',change:'Wijziging of app aanvragen',billingNeed:'Factuurvraag',impact:'Wie heeft er last van?',me:'Alleen ik',team:'Mijn team',everyone:'Iedereen',app:'App (optioneel)',general:'Algemeen',send:'Verstuur naar MSP',mine:'Mijn tickets',org:'Organisatietickets',department:'Afdelingstickets',scope:'Zichtbaarheid',noTickets:'Geen tickets in dit overzicht.',open:'Openstaand',closed:'Afgehandeld',back:'Terug naar tickets',reply:'Reactie',sendReply:'Reactie versturen',created:'Ticket verzonden naar je MSP.',commented:'Reactie toegevoegd.',noInvoices:'Nog geen betaalverzoeken of facturen.',pay:'Betalen via Mollie',waitLink:'Je MSP maakt de betaallink beschikbaar zodra de factuur klaarstaat.',monthly:'per maand excl. btw',quote:'Tarief nog vast te stellen',pending:'Aanvraag in behandeling',active:'Actief',cancel_requested:'Opzegging in behandeling',cancelled:'Opgezegd',rejected:'Afgewezen',requested:'Aangevraagd',approved:'Goedgekeurd',invoiced:'Gefactureerd',paid:'Betaald',new:'Nieuw',triage:'In beoordeling',in_progress:'In behandeling',waiting_client:'Wacht op jouw reactie',needs_quote:'Offerte nodig',resolved:'Opgelost',language:'Taal',private:'Alleen zichtbaar voor mij en MSP',requestLevel:'Aangevraagd niveau',currentLevel:'Huidig niveau'},en:{title:'Support',intro:'Support and payments',overview:'Overview',tickets:'Tickets',billing:'Payments',refresh:'Refresh',loading:'Loading...',error:'The MSP connection is unavailable. Retry; your draft is preserved.',hours:'Support hours',balance:'Available',used:'Used this month',allowance:'Monthly allowance',minutes:'minutes',noContract:'No active support contract is configured yet. You can still submit a ticket or app request.',restricted:'Your administrator manages support allowances and payments.',subscriptions:'Support per app',none:'Managed-app hour bundles are part of your annual support agreement. Choose your preferred level in Apps.',apps:'Go to Apps',newTicket:'Create ticket',titleLabel:'Subject',body:'How can we help?',need:'Request type',question:'Question',broken:'Something is broken',access:'Access needed',change:'Change or app request',billingNeed:'Invoice question',impact:'Who is affected?',me:'Only me',team:'My team',everyone:'Everyone',app:'App (optional)',general:'General',send:'Send to MSP',mine:'My tickets',org:'Organisation tickets',department:'Department tickets',scope:'Visibility',noTickets:'No tickets in this view.',open:'Open',closed:'Closed',back:'Back to tickets',reply:'Reply',sendReply:'Send reply',created:'Ticket sent to your MSP.',commented:'Reply added.',noInvoices:'No payment requests or invoices yet.',pay:'Pay through Mollie',waitLink:'Your MSP will provide a payment link when the invoice is ready.',monthly:'per month excl. VAT',quote:'Price to be agreed',pending:'Request pending',active:'Active',cancel_requested:'Cancellation pending',cancelled:'Cancelled',rejected:'Declined',requested:'Requested',approved:'Approved',invoiced:'Invoiced',paid:'Paid',new:'New',triage:'Under review',in_progress:'In progress',waiting_client:'Waiting for your reply',needs_quote:'Quote needed',resolved:'Resolved',language:'Language',private:'Visible only to me and MSP',requestLevel:'Requested level',currentLevel:'Current level'}};
 function lang(){if(language)return language;try{const x=localStorage.getItem('sp-feature-guide-language');if(words[x])return x;}catch(_){}return document.documentElement.lang.startsWith('nl')?'nl':'en';}
 const c=()=>words[lang()];
 function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
 function button(text,fn){const n=el('button',text,'app-button');n.type='button';n.disabled=busy;n.addEventListener('click',fn);return n;}
 function card(title){const n=el('section',undefined,'msp-card');if(title)n.append(el('h2',title));return n;}
 function select(label,values,value,onChange){const l=el('label',label),n=el('select');n.setAttribute('aria-label',label);for(const [id,name] of values){const o=el('option',name);o.value=id;n.append(o);}n.value=value;n.onchange=()=>onChange(n.value);l.append(n);return l;}
 function field(form,key,label,multiline=false){const l=el('label',label),n=el(multiline?'textarea':'input');n.value=draft[key]||'';n.required=true;n.maxLength=multiline?20000:200;n.oninput=()=>draft[key]=n.value;l.append(n);form.append(l);}
 function amount(value){return new Intl.NumberFormat(lang()==='nl'?'nl-NL':'en-GB',{style:'currency',currency:'EUR'}).format(value/100);}
 function status(value){return c()[value]||value;}
 function failure(exc){let code='';try{code=JSON.parse(exc.body).error?.code||'';}catch(_){}const nl=lang()==='nl';if(code==='support_hours_required')return nl?'Je uren zijn op. Koop eerst uren bij.':'No hours available. Buy hours first.';if(code==='payment_required')return nl?'Betaal eerst en dien daarna je aanvraag in.':'Pay first, then submit your request.';if(code==='mollie_invoice_missing')return nl?'De factuur staat nog niet klaar in Mollie. Je MSP kan dit controleren.':'The invoice is not ready in Mollie yet. Your MSP can check this.';if(code==='mollie_invoice_unavailable')return nl?'De factuur kon niet worden opgehaald. Probeer opnieuw of neem contact op met je MSP.':'Could not retrieve the invoice. Retry or contact your MSP.';if(code==='mollie_config')return nl?'Betalen is nog niet beschikbaar. Je MSP moet de betaalkoppeling instellen.':'Payments are not available yet. Your MSP must configure the payment connection.';if(exc.status===409)return nl?'Controleer de actuele betaalstatus of neem contact op met je MSP.':'Check the current payment status or contact your MSP.';return c().error;}
 async function run(fn){if(busy)return;const mine=generation;busy=true;error='';render();try{await fn(()=>mine===generation);if(mine===generation)await refreshData();}catch(exc){if(mine===generation)error=failure(exc);}finally{if(mine===generation){busy=false;render();}}}
 async function refreshData(){const mine=generation;const [o,t]=await Promise.all([api('/api/msp/overview'),api('/api/msp/tickets?scope='+scope)]);if(mine!==generation)return;overview=o;tickets=t.tickets||[];}
 async function load(){if(!allowed)return false;const mine=++generation;busy=true;error='';render();try{await refreshData();}catch(exc){if(mine===generation)error=failure(exc);}finally{if(mine===generation){busy=false;render();}}return !error;}
 function applyVisible(v){allowed=v===true;generation++;overview=null;tickets=[];detail=null;scope='mine';busy=false;error='';notice='';reply='';consultantDays=null;invoiceFilter='all';for(const key of ['title','body','service'])draft[key]='';if(!allowed)$('mainMsp')?.replaceChildren();}
 function fold(title,id){const n=el('details',undefined,'msp-details');if(id)n.id=id;n.append(el('summary',title));return n;}
 function hours(n){return Number(((n||0)/60).toFixed(2));}
 function toOrders(filter=''){orderFilter=filter;tab='billing';detail=null;render();}
 function metric(grid,label,value){const n=el('div',label);n.append(el('strong',String(value),'msp-metric'));grid.append(n);}
 function renderAnnual(root,billing=false){
  const nl=lang()==='nl',rows=overview.annual_support||[],active=rows.find(r=>r.status==='active'&&r.start_period<=overview.period&&r.end_period>overview.period);
  const box=card(billing?(nl?'Afspraak & termijnen':'Agreement & installments'):(nl?'Supporturen':'Support hours'));box.id=billing?'mspAnnualBilling':'mspAnnualSupport';
  if(!billing){const grid=el('div',undefined,'msp-grid');metric(grid,nl?'Beschikbaar':'Available',hours(overview.support_access?.available_minutes));if(active){metric(grid,nl?'Gebruikt dit jaar':'Used this year',hours(active.used_minutes));metric(grid,nl?'Jaarcap':'Annual cap',hours(active.annual_minutes));}box.append(grid,el('p',nl?'Uren':'Hours','msp-muted'));
   const actions=el('div',undefined,'msp-actions');if(overview.support_access?.can_submit)actions.append(button(nl?'Supportticket maken':'Create support ticket',()=>{tab='tickets';draft.need='question';render();}));actions.append(button(nl?'Uren bijkopen':'Buy hours',()=>toOrders('hours')));box.append(actions);
   if(overview.support_commitment?.job_minutes){const total=overview.support_commitment.annual_cents;box.append(el('p',(nl?'Supporttotaal: ':'Support total: ')+(total==null?c().quote:amount(total)+(nl?' per jaar excl. btw, vóór jaarkorting':' per year excl. VAT, before annual discount'))));box.append(el('p',hours(overview.support_commitment.annual_minutes)+(nl?' supporturen per jaar, waarvan ':' annual support hours, including ')+hours(overview.support_commitment.job_minutes)+(nl?' voor geplande taken':' for scheduled jobs'),'msp-muted'));if(overview.support_commitment.pending_minutes)box.append(el('small',hours(overview.support_commitment.pending_minutes)+(nl?' job-uren wachten op opname en betaling.':' job hours await agreement and payment.')));}
   if(!overview.support_access?.can_submit)box.append(el('p',nl?'Je uren zijn op. Koop eerst uren bij voor een supportticket.':'No hours available. Buy hours before creating a support ticket.','msp-muted'));
  }
  if(overview.permissions?.billing){
   if(!rows.length&&!billing)box.append(el('p',nl?'Nog geen jaarafspraak.':'No annual agreement yet.','msp-muted'));
   for(const row of rows){const info=fold(row.start_period+' / '+(row.last_period||row.end_period)+' · '+(row.annual_cents==null?c().quote:amount(row.annual_cents))+(nl?' per jaar':' per year'));
    info.append(el('p',row.status==='active'?(nl?'Actief':'Active'):(nl?'Concept':'Draft')),el('p',row.payment_cadence==='yearly'?(nl?'Jaarbetaling':'Yearly upfront'):(nl?'Maandbetaling':'Monthly payments')));
    if(row.annual_discount_bp!=null)info.append(el('p',(row.annual_discount_bp/100)+'% '+(nl?'jaarkorting':'yearly discount')+' · '+(row.annual_prepayment_savings_cents==null?c().quote:amount(row.annual_prepayment_savings_cents))+(nl?' voordeel per jaar':' annual savings')));
    if(row.purchased_extra_minutes)info.append(el('p',hours(row.purchased_extra_minutes)+(nl?' uur bijgekocht':' hours purchased')));
    for(const l of row.lines)info.append(el('p',l.name+(l.level?' · '+l.level:'')+' · '+hours(l.annual_minutes)+(nl?' uur/jaar · ':' hours/year · ')+(l.payable_annual_cents==null?c().quote:amount(l.payable_annual_cents))));
    if(billing)for(const m of row.installments||[])info.append(el('p',m.period+' · '+(m.amount_ex_vat_cents==null?c().quote:amount(m.amount_ex_vat_cents))+' · '+(m.invoice?(m.invoice.invoice_ref||status(m.invoice.status)):(nl?'Nog geen factuur':'Not invoiced'))));
    info.append(el('small',nl?'Bedragen excl. btw. De uren gelden per contractjaar.':'Amounts exclude VAT. Hours cover the contract year.'));box.append(info);
   }
  }
  if(!billing||rows.length)root.append(box);
 }
 function renderPackages(root){
  const nl=lang()==='nl',rows=overview.service_packages||[],base=rows.find(p=>p.kind==='base'),consultant=rows.find(p=>p.kind==='consultant');
  const box=fold(nl?'Pakket, apps & consultant':'Package, apps & consultant','mspPackages');
  const baseBox=el('div');baseBox.id='mspBasePackage';baseBox.append(el('h3',nl?'Basispakket':'Base package'),el('p',base?status(base.status):overview.contract?.status==='active'?c().active:(nl?'Nog niet actief':'Not active')));box.append(baseBox);
  const consultantBox=el('div');consultantBox.id='mspConsultant';consultantBox.append(el('h3','Agentic Consultant'),el('p',consultant?.active_days_per_week!=null?consultant.active_days_per_week+(nl?' dagen/week':' days/week'):(nl?'Nog niet actief':'Not active')));
  if(consultant?.scope)consultantBox.append(el('p',consultant.scope));
  if(overview.permissions?.billing&&consultant&&!['cancelled','rejected','cancel_requested'].includes(consultant.status))consultantBox.append(button(nl?'Consultant opzeggen':'Cancel consultant',()=>run(async()=>api('/api/msp/package',{method:'POST',body:JSON.stringify({kind:'consultant',action:'cancel'})}))));
  box.append(consultantBox);
  for(const a of (overview.apps||[]).filter(a=>a.support))box.append(el('p',a.name+' · '+(a.support.active_level||a.support.requested_level||'')+' · '+status(a.support.status)));
  box.append(button(nl?'Pakket of uitbreiding kiezen':'Choose package or upgrade',()=>toOrders()));root.append(box);
 }
 function renderJobCosts(root){
  if(!overview.permissions?.billing)return;
  const nl=lang()==='nl',data=overview.scheduled_jobs||{jobs:[]},box=fold(nl?'Automations & supportvoorstellen':'Automations & support proposals','mspJobCosts');
  if(!data.jobs.length)box.append(el('p',nl?'Nog geen voorstellen.':'No proposals yet.'));
  for(const row of data.jobs)box.append(el('p',row.title+' · '+hours(row.annual_support_minutes)+(nl?' supporturen/jaar · ':' support hours/year · ')+(row.annual_support_cents==null?c().quote:amount(row.annual_support_cents)+(nl?' per jaar excl. btw':' per year excl. VAT'))+' · '+(row.support_enabled?(nl?'Ingeschakeld':'Enabled'):(nl?'Niet ingeschakeld':'Not enabled'))));
  box.append(el('small',nl?'Ingeschakelde job-uren tellen automatisch mee. Alleen je MSP kan support uitschakelen.':'Enabled job hours count automatically. Only your MSP can disable support.'));root.append(box);
 }
 function renderMaintenance(root){
  const cardData=overview.maintenance_card;
  if(!cardData || !overview.permissions?.billing)return;
  const nl=lang()==='nl', section=card(nl?'Onderhoudsuren':'Maintenance hours');
  section.append(el('p', hours(cardData.available_minutes)+(nl?' uur beschikbaar':' hours available')));
  if(cardData.reserved_minutes>0)section.append(el('p',hours(cardData.reserved_minutes)+(nl?' uur gereserveerd':' hours reserved')));
  for(const entry of cardData.automation_cards||[]){
   const credit=entry.ledger_credit;
   if(!credit && !entry.reversed_at)continue;
   const details=fold(entry.name);
   if(entry.reversed_at){
    details.append(el('p',nl?'Teruggeboekt':'Reversed'));
    section.append(details);continue;
   }
   details.append(el('p',hours(credit.minutes)+(nl?' uur bijgeschreven':' hours credited')));
   if(entry.source_ref)details.append(el('p',(nl?'Oplevering ':'Delivery ')+entry.source_ref));
   if(entry.installation_name)details.append(el('p',entry.installation_name));
   if(credit.expires_at)details.append(el('p',(nl?'Geldig tot ':'Valid until ')+String(credit.expires_at).slice(0,10)));
   section.append(details);
  }
  root.append(section);
 }
 function renderOverview(root){
  renderMaintenance(root);
  renderAnnual(root);const nl=lang()==='nl',open=tickets.filter(t=>!['resolved','closed'].includes(t.status)).length;
  const box=card(nl?'Tickets & aanvragen':'Tickets & requests');box.append(button(open+' '+(nl?'openstaande tickets':'open tickets'),()=>{tab='tickets';render();}));
  const offers=(overview.service_orders||[]).filter(o=>o.status==='offered'&&!o.expired);
  if(offers.length)box.append(button(offers.length+' '+(nl?'aanbod / te betalen':'offers / payments'),()=>toOrders()));root.append(box);
  renderPackages(root);renderJobCosts(root);
 }
 function safeCheckout(value){try{const u=new URL(value);return u.protocol==='https:'&&u.hostname==='www.mollie.com'&&!u.username&&!u.password?u.href:null;}catch(_){return null;}}
 function renderOrders(root){
  if(!overview.permissions?.billing)return;
  const nl=lang()==='nl',box=card(nl?'Kiezen → betalen → aanvragen':'Choose → pay → request');box.id='mspOrders';
  const rows=(overview.service_orders||[]).filter(o=>!orderFilter||(orderFilter==='hours'?o.kind==='hours':o.scope?.app_id===orderFilter));
  if(orderFilter)box.append(button(nl?'Toon alles':'Show all',()=>toOrders()));
  const history=fold(nl?'Afgerond & verlopen':'Completed & expired');let historyCount=0;
  if(!rows.some(o=>o.status==='offered'&&!o.expired))box.append(el('p',nl?'Nog geen geprijsd aanbod beschikbaar. Je MSP stelt dit voor je klaar.':'No priced offer available yet. Your MSP will prepare it.'));
  for(const o of rows){const item=el('article',undefined,'msp-order');item.dataset.orderId=o.id;
   item.append(el('h3',o.name),el('strong',amount(o.amount_incl_vat_cents)+(nl?' incl. btw':' incl. VAT')));
   const details=fold(nl?'Inhoud & voorwaarden':'Scope & terms');details.append(el('p',o.description));if(o.scope?.minutes)details.append(el('p',hours(o.scope.minutes)+(nl?' supporturen':' support hours')));if(o.scope?.days_per_week)details.append(el('p',o.scope.days_per_week+(nl?' dagen/week':' days/week')));if(o.scope?.level)details.append(el('p',o.scope.level));details.append(el('p',o.invoice_ref),el('small',(nl?'Geldig tot ':'Valid until ')+o.expires_at.slice(0,10)));item.append(details);
   if(o.status==='submitted')item.append(el('p',o.kind==='hours'?(nl?'Uren toegevoegd':'Hours added'):(nl?'Aangevraagd':'Requested')));
   else if(o.expired)item.append(el('p',nl?'Aanbod verlopen. Neem contact op met je MSP.':'Offer expired. Contact your MSP.'));
   else if(o.can_submit)item.append(button(o.kind==='hours'?(nl?'Uren toevoegen':'Add hours'):(nl?'Betaald · aanvraag indienen':'Paid · submit request'),()=>run(async valid=>{await api('/api/msp/order',{method:'POST',body:JSON.stringify({order_id:o.id,action:'submit'})});if(valid())notice=o.kind==='hours'?(nl?'Uren toegevoegd. Je kunt een supportticket maken.':'Hours added. You can create a support ticket.'):(nl?'Aanvraag ingediend.':'Request submitted.');})));
   else {const url=safeCheckout(o.checkout_url);if(url){const a=el('a',nl?'Betalen':'Pay','app-button');a.href=url;a.target='_blank';a.rel='noopener noreferrer';item.append(a);}else if(!o.paid)item.append(button(nl?'Betalen':'Pay',()=>run(async valid=>{const result=await api('/api/msp/order',{method:'POST',body:JSON.stringify({order_id:o.id,action:'checkout'})});const url=safeCheckout(result.checkout_url);if(valid()&&url)window.location.assign(url);})));
    item.append(button(nl?'Betaling controleren':'Check payment',()=>run(async()=>api('/api/msp/order',{method:'POST',body:JSON.stringify({order_id:o.id,action:'sync'})}))));}
   if(o.status==='submitted'||o.expired){history.append(item);historyCount++;}else box.append(item);
  }if(historyCount)box.append(history);root.append(box);
 }
 function downloadInvoice(doc){
  if(doc.content_type!=='application/pdf'||typeof doc.pdf_base64!=='string'||doc.pdf_base64.length>750000)throw new Error('Invalid invoice');
  const raw=atob(doc.pdf_base64);if(!raw.startsWith('%PDF-'))throw new Error('Invalid invoice');
  const bytes=Uint8Array.from(raw,c=>c.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:'application/pdf'}));
  const a=document.createElement('a');a.href=url;a.download=/^factuur-invoice_[A-Za-z0-9]+\.pdf$/.test(doc.filename)?doc.filename:'factuur.pdf';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 function paymentStatus(row){
  const nl=lang()==='nl',p=row.payment;
  if(row.status==='paid')return c().paid;
  if(p?.mode==='live'){
   const labels={open:nl?'Openstaand':'Outstanding',pending:nl?'In verwerking':'Processing',authorized:nl?'Geautoriseerd':'Authorized',failed:nl?'Mislukt':'Failed',canceled:nl?'Betaling geannuleerd':'Payment canceled',expired:nl?'Betaallink verlopen':'Payment link expired'};
   if(labels[p.status])return labels[p.status];
  }
  return row.status==='invoiced'?(nl?'Openstaand':'Outstanding'):status(row.status);
 }
 function renderBilling(root){
  const w=c(),nl=lang()==='nl';if(!overview.permissions?.billing){root.append(el('p',w.restricted));return;}
  const all=overview.billing_requests||[],rows=all.filter(r=>invoiceFilter==='all'||(invoiceFilter==='paid'?r.status==='paid':r.status==='invoiced'));
  const box=card(nl?'Betalingsoverzicht':'Payment overview');box.id='mspInvoices';
  if(all.length)box.append(select(nl?'Betaalstatus':'Payment status',[['all',nl?'Alles':'All'],['open',nl?'Openstaand':'Outstanding'],['paid',w.paid]],invoiceFilter,v=>{invoiceFilter=v;render();}));
  if(!rows.length)box.append(el('p',all.length?(nl?'Geen betalingen met deze status.':'No payments with this status.'):w.noInvoices,'msp-muted'));
  for(const r of [...rows].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))){
   const item=el('article',undefined,'msp-order');item.dataset.invoiceId=r.id;
   item.append(el('strong',r.invoice_ref||r.product_name||r.product_code));
   if(r.invoice_ref&&(r.product_name||r.product_code))item.append(el('p',r.product_name||r.product_code,'msp-muted'));
   item.append(el('p',amount(r.amount_incl_vat_cents)+(nl?' incl. btw · ':' incl. VAT · ')+paymentStatus(r)));
   if(r.created_at){const date=new Date(r.created_at);if(!Number.isNaN(date.getTime()))item.append(el('small',(nl?'Aangemaakt: ':'Created: ')+new Intl.DateTimeFormat(nl?'nl-NL':'en-GB',{dateStyle:'medium'}).format(date),'msp-muted'));}
   const p=r.payment,url=p?.mode==='live'&&r.status==='invoiced'&&['open','pending'].includes(p.status)?safeCheckout(p.checkout_url):null;
   if(url){const actions=el('div',undefined,'msp-actions'),a=el('a',w.pay,'app-button');a.href=url;a.target='_blank';a.rel='noopener noreferrer';actions.append(a);item.append(actions);}
   if(r.status==='paid'&&r.payment?.mode==='live'&&r.payment.status==='paid')item.append(button(nl?'Factuur (PDF)':'Invoice (PDF)',()=>run(async valid=>{const document=await api('/api/msp/invoice',{method:'POST',body:JSON.stringify({request_id:r.id})});if(valid())downloadInvoice(document);})));
   box.append(item);
  }
  root.append(box);renderOrders(root);renderAnnual(root,true);
 }
 async function openTicket(id){const mine=generation;busy=true;render();try{const result=await api('/api/msp/ticket?id='+encodeURIComponent(id));if(mine===generation){detail=result;reply='';}}catch(exc){if(mine===generation)error=failure(exc);}finally{if(mine===generation){busy=false;render();}}}
 function renderTickets(root){const w=c();if(detail){const box=card('#'+detail.number+' '+(detail.title||detail.summary||''));box.append(button(w.back,()=>{detail=null;render();}),el('p',status(detail.status)),el('p',detail.body||detail.summary,'msp-body'));for(const e of detail.events||[]){if(e.body)box.append(el('div',e.body,'msp-event'));}
 if(detail.kind==='request'){const form=el('form'),label=el('label',w.reply),input=el('textarea');input.value=reply;input.required=true;input.maxLength=20000;input.oninput=()=>reply=input.value;label.append(input);form.append(label);const submit=button(w.sendReply,()=>{});submit.type='submit';form.append(submit);form.onsubmit=e=>{e.preventDefault();const body=reply,id=detail.id;run(async(valid)=>{await api('/api/msp/comment',{method:'POST',body:JSON.stringify({ticket_id:id,body})});if(!valid())return;reply='';const updated=await api('/api/msp/ticket?id='+encodeURIComponent(id));if(!valid())return;detail=updated;notice=w.commented;});};box.append(form);}root.append(box);return;}
 const list=card(w.tickets);const options=[['mine',w.mine]];if(overview.permissions?.org_tickets)options.push(['org',w.org]);if(overview.permissions?.department_tickets)options.push(['department',w.department]);list.append(select(w.scope,options,scope,v=>{scope=v;load();}));
 for(const done of [false,true]){const rows=tickets.filter(t=>['resolved','closed'].includes(t.status)===done);list.append(el('h3',(done?w.closed:w.open)+' ('+rows.length+')'));for(const t of rows){const b=button('#'+t.number+' '+(t.title||t.summary||''),()=>openTicket(t.id));b.className='msp-ticket';b.append(el('span',status(t.status)));list.append(b);}}
 if(!tickets.length)list.append(el('p',w.noTickets));root.append(list);
 const box=card(w.newTicket),form=el('form');const canSupport=overview.support_access?.can_submit===true;if(!canSupport){box.append(el('p',lang()==='nl'?'Voor support heb je uren nodig. Een factuurvraag kun je wel direct stellen.':'Support requires available hours. You can still ask an invoice question.'),button(lang()==='nl'?'Uren bijkopen':'Buy hours',()=>toOrders('hours')));}field(form,'title',w.titleLabel);field(form,'body',w.body,true);form.append(select(w.need,[['question',w.question],['broken',w.broken],['access',w.access],['change',w.change],['billing',w.billingNeed]],draft.need,v=>{draft.need=v;render();}),select(w.impact,[['me',w.me],['team',w.team],['everyone',w.everyone]],draft.impact,v=>draft.impact=v),select(w.app,[['',w.general],...(overview.apps||[]).map(a=>[a.id,a.name])],draft.service,v=>draft.service=v));
 const submit=button(w.send,()=>{});submit.type='submit';submit.disabled=busy||(!canSupport&&draft.need!=='billing');form.append(submit);form.onsubmit=e=>{e.preventDefault();const payload={title:draft.title,body:draft.body,need:draft.need,impact:draft.impact,service:draft.service?'app:'+draft.service:null};run(async(valid)=>{await api('/api/msp/tickets',{method:'POST',body:JSON.stringify(payload)});if(!valid())return;draft.title='';draft.body='';notice=w.created;});};box.append(form);root.append(box);
 }
 function render(){const root=$('mainMsp');if(!root)return;root.replaceChildren();if(!allowed)return;const w=c(),head=el('div',undefined,'msp-heading');head.append(el('h1',w.title),button(lang()==='nl'?'Gids':'Guide',()=>window.SynthPulseHelp?.startFeatureTour(['webui.msp.annual-support','webui.msp.package','webui.msp.consultant','webui.msp.overview','webui.msp.job-costs','webui.msp.tickets','webui.msp.billing'],{locale:lang()})),button(w.refresh,load),select(w.language,[['nl','Nederlands'],['en','English']],lang(),v=>{language=v;notice='';render();}));root.append(head,el('p',overview?.org?.name||w.intro,'msp-muted'));
 const tabs=el('div',undefined,'msp-tabs');tabs.setAttribute('role','tablist');for(const name of ['overview','tickets','billing']){const b=button(w[name],()=>{tab=name;detail=null;render();});b.id={overview:'mspOverviewTab',tickets:'mspTicketsTab',billing:'mspBillingTab'}[name];b.setAttribute('role','tab');b.setAttribute('aria-selected',String(tab===name));tabs.append(b);}root.append(tabs);
 if(error){const n=el('p',error,'msp-error');n.setAttribute('role','alert');root.append(n);}if(notice){const n=el('p',notice,'msp-notice');n.setAttribute('role','status');root.append(n);}if(!overview){root.append(el('p',busy?w.loading:w.error));return;}if(tab==='overview')renderOverview(root);if(tab==='tickets')renderTickets(root);if(tab==='billing')renderBilling(root);if($('mspSidebar'))$('mspSidebar').textContent=w.intro;
 }
 window.SynthPulseMSP={load,applyVisible,openOrders(filter=''){orderFilter=filter;tab='billing';switchPanel('msp',{fromRailClick:true});}};
})();
