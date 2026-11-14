import fs from 'node:fs';
import path from 'node:path';
import {test, expect, open, api, auth} from './fixtures';

test('SUPPLEMENT CRON SKILL PICKER an old blur cannot close a reopened query and a current outside click still closes it', async ({page}, info) => {
  await page.addInitScript(() => {
    const w=window as any, nativeTimeout=window.setTimeout.bind(window);
    w.__qaSkillBlurCallbacks=[];
    w.__qaSkillBlurSchedulers=[];
    window.setTimeout=((callback:TimerHandler,delay?:number,...args:any[])=>{
      const stack=new Error().stack||'';
      if(typeof callback==='function'&&delay===150&&/at search\.onblur.*panels\.js/.test(stack)){
        w.__qaSkillBlurCallbacks.push(()=>callback(...args));
        w.__qaSkillBlurSchedulers.push(stack);
        return nativeTimeout(()=>{},delay);
      }
      return nativeTimeout(callback,delay,...args);
    }) as typeof window.setTimeout;
    // Run even callbacks canceled after they were queued: the live focus/query
    // guard must independently keep a newer interaction intact.
    w.__qaReleaseSkillBlurs=()=>{const pending=w.__qaSkillBlurCallbacks.splice(0);for(const cb of pending)cb();return pending.length;};
  });
  const skill='qa-picker-'+Date.now(),name='QA picker '+Date.now();
  const directory=path.join(path.dirname(auth.workspace),'home','skills',skill);
  fs.mkdirSync(directory,{recursive:true});fs.writeFileSync(path.join(directory,'SKILL.md'),'---\nname: '+skill+'\ndescription: Synthetic picker fixture\n---\nUse only this synthetic fixture.\n');
  let id='';
  const ordering:any[]=[];
  const releaseBlurs=async(stage:string)=>{
    const evidence=await page.evaluate(()=>({
      released:(window as any).__qaReleaseSkillBlurs(),
      schedulers:[...(window as any).__qaSkillBlurSchedulers],
      dropdown:document.getElementById('cronFormSkillDropdown')?.style.display,
      query:(document.getElementById('cronFormSkillSearch') as HTMLInputElement|null)?.value,
    }));
    ordering.push({stage,...evidence});expect(evidence.released).toBeGreaterThan(0);
  };
  try{
    await open(page,'tasks');await page.getByRole('button',{name:'New job',exact:true}).click();
    await page.locator('#cronFormName').fill(name);await page.locator('#cronFormPrompt').fill('QA_PICKER_ONLY');
    await expect(page.locator('#cronFormDeliver')).toBeEnabled();await page.locator('#cronFormDeliver').selectOption('local');
    const search=page.locator('#cronFormSkillSearch'),dropdown=page.locator('#cronFormSkillDropdown');
    const option=dropdown.locator('.skill-opt').filter({hasText:skill});
    await search.fill(skill);await option.click();
    await expect(page.locator('#cronFormSkillTags .skill-tag')).toHaveCount(1);
    await page.locator('#cronFormSkillTags .remove-tag').click();
    await expect(page.locator('#cronFormSkillTags .skill-tag')).toHaveCount(0);
    await search.fill(skill);await expect(option).toBeVisible();
    await releaseBlurs('old blur after newer query');
    await option.click();await expect(page.locator('#cronFormSkillTags .skill-tag')).toHaveCount(1);
    await page.locator('#cronFormSkillTags .remove-tag').click();await search.fill(skill);await expect(option).toBeVisible();
    await page.locator('#cronFormName').click();
    await releaseBlurs('current outside blur');
    await expect(dropdown).toBeHidden();
    await search.click();await expect(option).toBeVisible();await option.click();
    await page.getByRole('checkbox',{name:'Not known yet',exact:true}).check();
    const saved=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/crons/create'&&r.request().method()==='POST');
    await page.locator('#btnSaveTaskDetail').click();const response=await saved;expect(response.status()).toBe(200);
    const created=(await response.json()).job;id=created.id;expect(created.skills).toEqual([skill]);
    await page.reload();await page.locator('.rail-btn[data-panel="tasks"]').click();
    await page.locator('.cron-item').filter({hasText:name}).click();await page.locator('#btnEditTaskDetail').click();
    await expect(search).toBeDisabled();await expect(page.locator('#cronFormSkillTags')).toContainText(skill);
    expect((await api(page,'/api/crons')).body.jobs.find((job:any)=>job.id===id).skills).toEqual([skill]);
  }finally{
    await info.attach('cron-picker-blur-ordering',{body:JSON.stringify({ordering}),contentType:'application/json'});
    if(id)expect((await api(page,'/api/crons/delete',{job_id:id})).status).toBe(200);
    fs.rmSync(directory,{recursive:true});
  }
});
