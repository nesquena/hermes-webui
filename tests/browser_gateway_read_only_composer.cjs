// Isolated composer check: real markup, styles and functions, with synthetic state.
// No server, credentials, model requests or saved conversations are used.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const output = process.env.COMPOSER_ARTIFACT_DIR;
if (!output) throw new Error('Set COMPOSER_ARTIFACT_DIR to a disposable output folder.');
fs.mkdirSync(output, {recursive:true});
const before = process.env.COMPOSER_BEFORE_REF;
const read = (name, ref) => ref
  ? execFileSync('git', ['show', `${ref}:${name}`], {cwd:root, encoding:'utf8', maxBuffer:8*1024*1024})
  : fs.readFileSync(path.join(root, name), 'utf8');
function extract(source, name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.ok(start >= 0, name);
  const rest = source.slice(start + 1);
  const end = rest.search(/^(?:async )?function \w+\(/m);
  return source.slice(start, end < 0 ? undefined : start + 1 + end);
}

(async () => {
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROMIUM_EXECUTABLE_PATH ? {executablePath:process.env.CHROMIUM_EXECUTABLE_PATH} : {})});
  const results = [];
  try {
    for (const [label, ref] of [...(before ? [['before', before]] : []), ['after', null]]) {
      const ui = read('static/ui.js', ref);
      const functions = ['getComposerPrimaryAction', '_applyBusyComposerPlaceholder',
        '_setComposerPrimaryButtonIcon', 'updateSendBtn', 'syncTopbar'].map(name => extract(ui, name)).join('\n')
        + '\n' + extract(read('static/messages.js', ref), 'send')
        + '\n' + extract(read('static/panels.js', ref), 'syncAppTitlebar');
      for (const width of [1440, 900, 390]) {
        for (const theme of ['light', 'dark']) {
        for (const scenario of [
          {name:'desktop',source:'Desktop',locale:'en',destination:'Desktop',help:true},
          {name:'desktop-es',source:'Desktop',locale:'es',destination:'Desktop',help:true},
          {name:'cron',source:'Cron',locale:'en',destination:'',help:false},
          {name:'subagent',source:'Subagent',locale:'en',destination:'',help:false},
          {name:'original-client',source:'Other client',locale:'en',destination:'the original client',help:true},
        ]) {
          const page = await browser.newPage({viewport:{width, height:700}, isMobile:width===390, hasTouch:width===390});
          await page.route('**/*', route => route.abort());
          await page.setContent('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body></body></html>');
          await page.evaluate(({html, theme}) => {
            const parsed = new DOMParser().parseFromString(html, 'text/html');
            document.documentElement.classList.toggle('dark', theme==='dark');
            document.body.innerHTML = '<main style="width:calc(100% - 32px);max-width:780px;margin:80px auto"><h2 id="appTitlebarTitle"></h2><p id="appTitlebarSub"></p><div hidden><span id="topbarTitle"></span><span id="topbarMeta"></span></div><p>Example conversation for a layout check.</p>'
              + parsed.getElementById('composerBox').outerHTML + '</main>';
          }, {html:read('static/index.html', ref), theme});
          await page.addStyleTag({content:read('static/style.css', ref)});
          await page.addScriptTag({content:read('static/i18n.js', ref)});
          await page.addScriptTag({content:`
            const S={session:{},messages:[],busy:false,activeStreamId:null,pendingFiles:[]};
            const $=id=>document.getElementById(id);
            const _currentPanel='chat';
            const _renamingAppTitlebar=false;
            const _topbarMessageMetaText=()=> '0';
            const _applyModelToDropdown=()=>true;
            let _compressionPlaceholderSaved=null;
            const assistantDisplayName=()=> 'Hermes';
            const _composerHasContent=()=>!!$('msg').value;
            const _getExplicitBusyCommandAction=()=>null;
            const showToast=(text,duration)=>window.lastToast={text,duration};
            ${functions}
          `});
          await page.evaluate(scenario => {
            setLocale(scenario.locale);applyLocaleToDOM();
            S.session={title:'Example conversation',model:'example',source_label:scenario.source,
              chat_read_only_reason:'Read-only in WebUI.'+(scenario.destination ? ` Continue in ${scenario.destination}.` : ''),
              chat_read_only_can_connect:scenario.help};
            updateSendBtn();syncTopbar();
          }, scenario);
          await page.locator('main').screenshot({path:path.join(output, `${label}-${scenario.name}-${width}-${theme}.png`)});
          const metrics = await page.evaluate(() => {
            const notice=$('composerChatReadOnlyNotice')||$('composerChatReadOnlyReason');
            const rect=notice.getBoundingClientRect();
            return {noticeHeight:rect.height, placeholder:$('msg').placeholder,
              readOnly:$('msg').readOnly,sendDisabled:$('btnSend').disabled,
              fits:rect.left>=0&&rect.right<=innerWidth,
              help:$('composerChatReadOnlyHelp')?.getAttribute('href'),
              helpLabel:$('composerChatReadOnlyHelp').textContent,
              text:$('composerChatReadOnlyReason').textContent,
              titleBadge:document.querySelector('#appTitlebarSub .topbar-source-badge').textContent,
              headerBadge:document.querySelector('#topbarMeta .topbar-source-badge').textContent};
          });
          assert.ok(metrics.fits);
          assert.ok(metrics.readOnly && metrics.sendDisabled);
          if(label==='after') {
            assert.equal(metrics.placeholder, scenario.locale==='es' ? 'Solo lectura en WebUI' : 'Read-only in WebUI');
            assert.equal(metrics.helpLabel, scenario.locale==='es' ? 'Cómo conectarse' : 'How to connect');
            assert.equal(metrics.text, scenario.destination ? `Continue in ${scenario.destination}.` : '');
            assert.equal(metrics.titleBadge, scenario.source+' · read-only');
            assert.equal(metrics.headerBadge, scenario.source+' · read-only');
            assert.equal(await page.locator('#composerChatReadOnlyNotice').isHidden(), !scenario.destination);
            assert.equal(await page.locator('#btnSend').getAttribute('title'),
              'Read-only in WebUI.'+(scenario.destination ? ` Continue in ${scenario.destination}.` : ''));
            assert.ok(metrics.noticeHeight<=44, JSON.stringify({width,theme,...metrics}));
            assert.ok(metrics.help.endsWith('#gateway-session-connection'));
            await page.locator('#msg').focus();
            await page.keyboard.type('must not be entered');
            assert.equal(await page.locator('#msg').inputValue(), '');
            await page.evaluate(async () => {
              $('msg').value='keep this draft';
              await send();
            });
            assert.equal(await page.locator('#msg').inputValue(), 'keep this draft');
            assert.equal(await page.evaluate(()=>window.lastToast),undefined);
            await page.evaluate(() => {
              S.busy=true;S.activeStreamId='test-stream';updateSendBtn();
            });
            assert.equal(await page.locator('#btnSend').getAttribute('data-action'),'stop');
            await page.evaluate(() => {
              S.busy=false;S.activeStreamId=null;
              S.session={chat_read_only_reason:'Read-only in WebUI.'};updateSendBtn();
            });
            assert.ok(await page.locator('#composerChatReadOnlyHelp').isHidden());
            assert.ok(await page.locator('#composerChatReadOnlyNotice').isHidden());
            await page.evaluate(() => {
              S.session={model:'example',source_label:'Desktop',read_only:true};syncTopbar();
            });
            assert.equal(await page.locator('#appTitlebarSub .topbar-source-badge').textContent(),'Desktop · read-only');
            assert.equal(await page.locator('#topbarMeta .topbar-source-badge').textContent(),'Desktop · read-only');
            await page.evaluate(() => {S.session={model:'example',source_label:'Desktop'};syncTopbar();updateSendBtn();});
            assert.equal(await page.locator('#appTitlebarSub .topbar-source-badge').textContent(),'Desktop');
            assert.equal(await page.locator('#topbarMeta .topbar-source-badge').textContent(),'Desktop');
            await page.evaluate(() => {S.session={model:'example',source_label:'WebUI'};syncTopbar();updateSendBtn();});
            assert.equal(await page.locator('.topbar-source-badge').count(),0);
            assert.equal(await page.locator('#msg').getAttribute('placeholder'),'Message Hermes…');
            assert.equal(await page.locator('#msg').inputValue(),'keep this draft');
            assert.equal(await page.locator('#msg').evaluate(el=>el.readOnly),false);
            assert.ok(await page.locator('#composerChatReadOnlyNotice').isHidden());
          }
          results.push({label,scenario:scenario.name,width,theme,...metrics});
          await page.close();
        }
        }
      }
    }
    fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify(results));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
