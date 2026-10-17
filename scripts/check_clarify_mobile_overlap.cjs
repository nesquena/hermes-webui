// Use real clarification markup/CSS with an isolated synthetic composer.
// --baseline captures/asserts the original mobile overlap instead of the fix.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const baseline = process.argv.includes('--baseline');
const output = path.resolve(process.argv.slice(2).find(arg => arg !== '--baseline') || '/tmp/clarify-mobile-overlap');
fs.mkdirSync(output, {recursive:true});

(async () => {
  const css = fs.readFileSync(path.join(root, 'static/style.css'), 'utf8');
  const index = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  const start = index.indexOf('<div class="clarify-card"');
  const end = index.indexOf('<div class="composer-terminal-panel"', start);
  assert.ok(start >= 0 && end > start, 'Clarification markup must exist');
  const card = index.slice(start, end).replace('class="clarify-card"', 'class="clarify-card visible"')
    .replace('hidden aria-hidden="true" inert', 'aria-hidden="false"');
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    for (const [width,height] of [[1280,800],[720,800],[390,844],[390,440]]) {
      const mobile = width < 640;
      const page = await browser.newPage({viewport:{width,height},isMobile:mobile,hasTouch:mobile});
      await page.setContent(`<html class="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <style>${css}</style><style>.test-shell{position:fixed;bottom:0;left:0;right:0}
        .test-composer{height:110px;padding:16px;box-sizing:border-box}</style></head><body>
        <div class="test-shell"><div class="composer-wrap"><div class="composer-flyout">${card}</div>
        <div class="composer-box test-composer">Clarification needed<br><br>Attach　Microphone　Send</div>
        </div></div></body></html>`);
      await page.locator('#clarifyQuestion').evaluate(el => el.textContent='Which database should we use? Postgres or SQLite?');
      await page.waitForTimeout(500);
      const panel = await page.locator('.clarify-inner').boundingBox();
      const composer = await page.locator('.composer-box').boundingBox();
      const gap = composer.y-(panel.y+panel.height);
      const hintUncovered = await page.locator('#clarifyHint').evaluate(el => {
        const r = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
      });
      console.log(`${width}x${height}: panel gap=${gap}, hint uncovered=${hintUncovered}`);
      if (mobile) {
        assert.ok(baseline ? gap < 0 : gap >= 8, 'Expanded mobile card must clear the composer');
        assert.equal(hintUncovered, !baseline, 'Mobile hint must not be covered by composer');
      } else {
        assert.equal(gap, -24, 'Desktop flyout geometry must stay unchanged');
        assert.equal(hintUncovered, true, 'Desktop hint must not be covered by composer');
      }
      assert.ok(panel.y >= 0, 'Panel must fit the viewport');
      await page.locator('#clarifyInput').click();
      await page.locator('#clarifyInput').fill('Please use Postgres');
      assert.equal(await page.locator('#clarifyInput').inputValue(),'Please use Postgres');
      await page.screenshot({path:path.join(output,`${baseline ? 'before' : 'after'}-${width}x${height}.png`)});
      if (mobile && !baseline) {
        await page.locator('#clarifyQuestion').evaluate(el => el.textContent='Long question details. '.repeat(90));
        assert.ok(await page.locator('.clarify-inner').evaluate(el => el.scrollHeight>el.clientHeight));
        await page.locator('.clarify-inner').evaluate(el => el.scrollTop=el.scrollHeight);
        const send = await page.locator('#clarifySubmit').boundingBox();
        const hint = await page.locator('#clarifyHint').boundingBox();
        assert.ok(send.y >= 0 && hint.y+hint.height <= composer.y, 'Scroll must reveal Send and hint');
        await page.locator('#clarifyCard').evaluate(el => el.classList.add('collapsed'));
        await page.waitForTimeout(300);
        const collapsed = await page.locator('.clarify-inner').boundingBox();
        assert.ok(collapsed.height <= 48, 'Collapsed card must retain compact header');
        assert.equal(composer.y-(collapsed.y+collapsed.height),8);
      }
      await page.close();
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exit(1);});
