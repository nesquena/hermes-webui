// Playwright capture for PR #6810 evidence. Run as an async (page) => {...}
// body (e.g. via the Playwright MCP run-code tool) with two isolated WebUI
// servers: master on :8790 ("before") and the PR head on :8789 ("after").
// Cron list reads are browser fixtures; no real jobs or state are used.
async (page) => {
  // Absolute output dir: page.screenshot resolves relative paths against the
  // Playwright process cwd, not the repository.
  const OUT = (globalThis.PR_MEDIA_OUT || 'docs/pr-media/6810/');
  const JOBS = [
    { id: 'a1', name: 'daily-digest', enabled: true },
    { id: 'a2', name: 'weekly-report', enabled: true },
    { id: 'a3', name: 'backup-db', enabled: true },
    { id: 'a4', name: 'inbox-triage', enabled: true },
    { id: 'p1', name: 'paused-only-sync', enabled: true, state: 'paused' },
    { id: 'p2', name: 'daily-archive', enabled: true, state: 'paused' },
  ];
  const SERVERS = { before: 'http://127.0.0.1:8790/', after: 'http://127.0.0.1:8789/' };
  const VIEWPORTS = {
    1440: { viewport: { width: 1440, height: 900 } },
    720: { viewport: { width: 720, height: 900 } },
    390: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
  };
  const browser = page.context().browser();
  const results = {};

  async function open(which, width) {
    const ctx = await browser.newContext(VIEWPORTS[width]);
    const p = await ctx.newPage();
    const errors = [];
    p.on('pageerror', e => errors.push(String(e)));
    const cronReads = [];
    await p.route(/\/api\/crons(\?.*)?$/, r => { cronReads.push(r.request().url()); return r.fulfill({ json: { jobs: JOBS, other_profile_count: 0 } }); });
    await p.goto(SERVERS[which]);
    await p.waitForFunction(() => typeof switchPanel === 'function');
    await p.waitForTimeout(1500);
    // Fresh isolated state shows first-run onboarding; it is unrelated to this PR.
    await p.evaluate(() => { document.getElementById('onboardingOverlay')?.remove(); try { localStorage.removeItem('cron-paused-collapsed'); } catch (_) {} });
    await p.evaluate(() => switchPanel('tasks'));
    await p.waitForTimeout(800);
    if (!(await p.isVisible('#cronList'))) {
      await p.evaluate(() => toggleMobileSidebar());
      await p.waitForTimeout(500);
    }
    return { ctx, p, errors, cronReads };
  }
  const visibleRows = p => p.evaluate(() => [...document.querySelectorAll('#cronList .cron-item')]
    .filter(el => !el.closest('details:not([open])')).map(el => el.querySelector('.cron-name').textContent));
  const rect = (p, sel) => p.evaluate(s => { const r = document.querySelector(s)?.getBoundingClientRect(); return r ? [Math.round(r.width), Math.round(r.height)] : null; }, sel);

  for (const width of [1440, 720, 390]) {
    const b = await open('before', width);
    await b.p.screenshot({ path: `${OUT}before-${width}.png` });
    results[`before-${width}`] = { rows: await visibleRows(b.p), hasSearch: await b.p.isVisible('#cronSearch'), pageErrors: b.errors };
    await b.ctx.close();

    const a = await open('after', width);
    const readsAfterLoad = a.cronReads.length;
    await a.p.screenshot({ path: `${OUT}after-${width}.png` });
    const r = { rows: await visibleRows(a.p) };
    await a.p.fill('#cronSearch', 'daily');
    await a.p.waitForTimeout(200);
    await a.p.screenshot({ path: `${OUT}after-${width}-query-mixed.png` });
    r.mixed = await visibleRows(a.p);
    r.clearButton = await rect(a.p, '#cronSearchClear');
    r.clearGlyph = await rect(a.p, '#cronSearchClear svg');
    r.coarsePointer = await a.p.evaluate(() => matchMedia('(pointer:coarse)').matches);
    if (width === 1440) {
      await a.p.fill('#cronSearch', 'paused-only');
      await a.p.waitForTimeout(200);
      await a.p.screenshot({ path: `${OUT}after-1440-query-paused-only.png` });
      r.pausedOnly = await visibleRows(a.p);
      await a.p.fill('#cronSearch', 'zzz');
      await a.p.waitForTimeout(200);
      await a.p.screenshot({ path: `${OUT}after-1440-no-match.png` });
      r.noMatch = await a.p.evaluate(() => document.getElementById('cronList').innerText.trim());
      await a.p.evaluate(() => switchTasksSubtab('scripts'));
      await a.p.waitForTimeout(300);
      r.searchVisibleOnScripts = await a.p.isVisible('#cronSearch');
      await a.p.evaluate(() => switchTasksSubtab('jobs'));
    }
    await a.p.click('#cronSearchClear');
    await a.p.waitForTimeout(200);
    r.afterClear = await visibleRows(a.p);
    r.focusAfterClear = await a.p.evaluate(() => document.activeElement?.id || null);
    r.cronReadsDuringFiltering = a.cronReads.length - readsAfterLoad;
    r.pausedPreference = await a.p.evaluate(() => localStorage.getItem('cron-paused-collapsed'));
    r.pageErrors = a.errors;
    results[`after-${width}`] = r;
    await a.ctx.close();
  }
  return results;
}
