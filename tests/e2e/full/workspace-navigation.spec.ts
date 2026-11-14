import fs from 'node:fs';
import path from 'node:path';
import {test, expect, open, api} from './fixtures';

async function ready(page:any){
  await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
}

test('WORKSPACE NAVIGATION bot library search stays focused during refresh and starts a conversation',async({page},info)=>{
  await ready(page);await page.locator('.rail [data-panel="profiles"]').click();
  await page.screenshot({path:info.outputPath('before-library.png')});
  await expect(page.locator('#botDirectorySearch')).toBeVisible({timeout:3000});
  await expect(page.locator('.sidebar')).toBeHidden();
  await page.locator('#botDirectorySearch').fill('Research');
  await expect(page.locator('.bot-directory-card')).toHaveCount(1);
  await page.evaluate(()=>filterProfilesPanel());
  await expect(page.locator('#botDirectorySearch')).toBeFocused();
  await expect(page.locator('#botDirectorySearch')).toHaveValue('Research');
  await expect(page.locator('#profileSearch')).toHaveValue('Research');
  await page.locator('#botDirectorySearch').fill('');
  await page.screenshot({path:info.outputPath('library-desktop.png')});
  const contrasts:any[]=[];
  for(const dark of [false,true]){
    await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
    await page.evaluate(()=>document.getAnimations().forEach(animation=>{try{animation.finish();}catch{}}));
    const result=await page.evaluate(()=>{
      const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const context=canvas.getContext('2d')!;
      const rgb=(value:string)=>{context.clearRect(0,0,1,1);context.fillStyle=value;context.fillRect(0,0,1,1);const color=[...context.getImageData(0,0,1,1).data];color[3]/=255;return color;};
      const luminance=(color:number[])=>color.slice(0,3).map(c=>c/255).map(c=>c<=.04045?c/12.92:Math.pow((c+.055)/1.055,2.4)).reduce((sum,c,i)=>sum+c*[.2126,.7152,.0722][i],0);
      return ['.bot-directory-head p','.bot-directory-create','.bot-start-action','#botDirectoryCount','#botDirectorySearch'].map(selector=>{
        const node=document.querySelector(selector)!,style=getComputedStyle(node,selector==='#botDirectorySearch'?'::placeholder':null);
        let bg:number[]=[255,255,255],opacity=1;const ancestors:Element[]=[];
        for(let p:Element|null=node;p;p=p.parentElement){opacity*=Number(getComputedStyle(p).opacity);ancestors.unshift(p);}
        for(const parent of ancestors){const color=rgb(getComputedStyle(parent).backgroundColor);bg=bg.map((c,i)=>color[i]*color[3]+c*(1-color[3]));}
        const fg=rgb(style.color).map((c,i)=>i<3?c*opacity+bg[i]*(1-opacity):c),a=luminance(fg),b=luminance(bg);
        return {selector,fg,bg,opacity,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      });
    });
    contrasts.push({dark,result});for(const item of result)expect(item.ratio,JSON.stringify({dark,...item})).toBeGreaterThanOrEqual(4.5);
    if(!dark)await page.screenshot({path:info.outputPath('library-light.png')});
  }
  await info.attach('workbench-contrast',{body:JSON.stringify(contrasts),contentType:'application/json'});
  await page.setViewportSize({width:900,height:900});
  await expect(page.locator('#botDirectorySearch')).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('library-narrow.png')});
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>closeMobileSidebar());
  await expect(page.locator('#botDirectorySearch')).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.locator('#botDirectorySearch').fill('Research');
  await expect(page.locator('[data-bot-start="qa-research"]')).toBeInViewport();
  await page.screenshot({path:info.outputPath('library-mobile.png')});
  await page.locator('[data-bot-start="qa-research"]').click();
  await expect(page.locator('#msg')).toBeVisible();
  await expect.poll(()=>page.evaluate(()=>S.activeProfile)).toBe('qa-research');
  await page.locator('#msg').fill('QA_LIBRARY_DIRECT_CHAT');await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_LIBRARY_DIRECT_CHAT',{timeout:30000});
});

test('WORKSPACE NAVIGATION keyboard search preserves drafts and unifies Projects and Spaces',async({page},info)=>{
  await ready(page);await page.locator('#msg').fill('Keep this navigation draft');
  await expect(page.locator('.rail [data-panel="kanban"], .rail [data-panel="workspaces"]')).toHaveCount(0);
  await page.keyboard.press('Control+Shift+P');
  await expect(page.locator('#workspaceNavigatorSearch')).toBeFocused();
  await page.locator('#workspaceNavigatorSearch').fill('space');
  await expect(page.locator('#workspaceDestinationList [role="option"]')).toHaveCount(1);
  await expect(page.locator('[data-destination="projects"]')).toBeVisible();
  await page.screenshot({path:info.outputPath('navigation-projects.png')});
  await page.evaluate(()=>document.documentElement.classList.remove('dark'));
  await page.screenshot({path:info.outputPath('navigation-light.png')});
  await page.evaluate(()=>document.documentElement.classList.add('dark'));
  await page.keyboard.press('Escape');await expect(page.locator('#msg')).toBeFocused();
  await expect(page.locator('#msg')).toHaveValue('Keep this navigation draft');
  await page.locator('#btnWorkspaceNavigator').click();
  await page.locator('#workspaceNavigatorSearch').fill('bots');await page.keyboard.press('Enter');
  await expect(page.locator('#botDirectorySearch')).toBeFocused();
  await page.keyboard.press('Control+Shift+P');await page.locator('#workspaceNavigatorSearch').fill('chat');
  await page.keyboard.press('ArrowDown');await page.keyboard.press('ArrowUp');await page.keyboard.press('Enter');
  await expect(page.locator('#msg')).toHaveValue('Keep this navigation draft');
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('#btnWorkspaceNavigator')).toBeInViewport();
  await page.locator('#btnWorkspaceNavigator').click();
  await page.screenshot({path:info.outputPath('navigation-mobile.png')});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.keyboard.press('Escape');await expect(page.locator('#btnWorkspaceNavigator')).toBeFocused();
});

test.describe('member navigation',()=>{
  test.use({user:'alice'});
  test('WORKSPACE NAVIGATION hides denied functions and handles an empty search without losing focus',async({page})=>{
    await ready(page);expect((await api(page,'/api/governance/users')).status).toBe(403);
    await page.locator('#btnWorkspaceNavigator').click();
    await expect(page.locator('[data-destination="governance"], [data-destination="logs"], [data-destination="kanban"], [data-destination="workspaces"]')).toHaveCount(0);
    await page.locator('#workspaceNavigatorSearch').fill('governance');
    await expect(page.locator('#workspaceDestinationList [role="option"]')).toHaveCount(0);
    await expect(page.locator('#workspaceNavigatorStatus')).toContainText('No matching');
    await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
    await expect(page.locator('#workspaceNavigatorSearch')).toBeFocused();
    await page.locator('#workspaceNavigatorSearch').fill('projects');await page.keyboard.press('Enter');
    await expect(page.locator('#mainProjects')).toBeVisible();
  });
});

test('WORKSPACE NAVIGATION merged project folders remain reachable through the single Projects destination',async({page},info)=>{
  const state=path.resolve(process.env.QA_STATE||'');
  expect(path.basename(state)).toBe('e2e-state');
  const marker=path.join(state,'state','projects_merge_state.json');
  const previous=fs.existsSync(marker)?fs.readFileSync(marker):null;
  // Exercise the existing post-migration contract on disposable fixture state.
  fs.writeFileSync(marker,JSON.stringify({fixture:true}));
  try{
    await ready(page);
    expect((await api(page,'/api/projects/folders/config')).body.merged).toBe(true);
    await page.locator('#btnWorkspaceNavigator').click();await page.locator('#workspaceNavigatorSearch').fill('folders');await page.keyboard.press('Enter');
    await expect(page.locator('#mainProjects')).toBeVisible();
    await page.locator('.proj-row-mywork').click();
    await expect(page.locator('#projSummary')).toContainText('My work');
    await expect(page.locator('#projSections .sp-folders-card')).toBeVisible();
    await expect(page.locator('#projSections .sp-folders-card')).toContainText('Files and folders');
    await page.screenshot({path:info.outputPath('projects-folders.png')});
    await page.setViewportSize({width:390,height:844});await page.evaluate(()=>closeMobileSidebar());
    await expect(page.locator('#projSections .sp-folders-card')).toBeVisible();
    await expect(page.locator('.sidebar-nav [data-panel="kanban"], .sidebar-nav [data-panel="workspaces"]')).toHaveCount(0);
  } finally { if(previous)fs.writeFileSync(marker,previous);else fs.unlinkSync(marker); }
});
