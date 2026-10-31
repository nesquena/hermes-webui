#!/usr/bin/env python3
"""Live language-switch regression test (#7698 review request), v3.

Boots server.py agent-free (like browser_smoke.py), loads the app in headless
Chromium, seeds cached dynamic surfaces, then switches locale EN -> RU -> EN
without reload and asserts every surface the repaint block owns.

All repaint+assert rounds run inside ONE page.evaluate so nothing (sidebar
SSE, usage polls) can repaint between the locale pass and the reads.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = int(os.getenv("SMOKE_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"

BENIGN = ["favicon", "manifest.json", "serviceworker", "sw.js",
          "the server responded with a status of 404"]

# Console errors that only mean "this sandbox has no network": the real app
# references a few external assets (fonts/CDN) that simply cannot resolve
# offline. Only NON-app-origin failures are forgiven — an offline error on the
# app's own origin, or any other console error, still fails the run.
OFFLINE_NET_MARKERS = (
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_ADDRESS_UNREACHABLE",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_PROXY_CONNECTION_FAILED",
    "net::ERR_NETWORK_CHANGED",
)


def _is_benign(text):
    t = text.lower()
    return any(p.lower() in t for p in BENIGN)


def _is_offline_external(text, url):
    """True for an offline network failure against a non-app origin."""
    if not any(m in text for m in OFFLINE_NET_MARKERS):
        return False
    return not str(url or "").startswith(BASE)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    if not os.path.exists(server_py):
        print(f"SETUP FAIL: server.py not found at {server_py}", file=sys.stderr)
        return 2

    state_dir = tempfile.mkdtemp(prefix="hermes-locale-switch-", dir=os.getenv("TMPDIR", "/tmp"))
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    # Hermetic env: an inherited HERMES_WEBUI_PASSWORD (or trusted-header/OIDC
    # config) would flip auth on and 302 the app to /login. Strip every WebUI
    # knob we do not set explicitly below.
    for k in list(env):
        if k.startswith("HERMES_WEBUI_"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )

    def _wait_for_health(timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                    if r.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        return False

    failures = []
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = browser.new_context(base_url=BASE)
            page = ctx.new_page()
            errors = []

            def _on_console(msg):
                if msg.type != "error":
                    return
                loc = getattr(msg, "location", None) or {}
                url = loc.get("url", "") if isinstance(loc, dict) else ""
                errors.append(("console", msg.text, url))

            page.on("console", _on_console)
            page.on("pageerror", lambda e: errors.append(("pageerror", str(e), "")))

            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_selector("#msg", timeout=15000)

            # One evaluate per round: seed + repaint + collect, atomically.
            # Rows need message_count>0 to survive _sidebarRowHasVisibleMessages;
            # S.lastUsage feeds _syncCtxIndicator; the chip needs a set effort.
            JS_ROUND = (
                """(lang) => {
                  const fail = [];
                  const want = {
                    en: {
                      placeholder: 'Message Hermes\\u2026',
                      chip: 'Medium',
                      menu_default: 'Default',
                      menu_max: 'Max',
                      ctx_usage: 'Context window: 12% used (88% left)',
                      aria_ctx: 'Context window: 12% used (88% left)',
                      archived: 'Show 2 archived',
                      tts_browser: 'Browser speech synthesis',
                    },
                    ru: {
                      placeholder: 'Сообщение для Hermes\\u2026',
                      chip: 'Средний',
                      menu_default: 'По умолчанию',
                      menu_max: 'Максимальный',
                      ctx_usage: 'Контекстное окно: использовано 12% (осталось 88%)',
                      aria_ctx: 'Контекстное окно: использовано 12% (осталось 88%)',
                      archived: 'Показать архив (2)',
                      tts_browser: 'Синтез речи браузера',
                    },
                  }[lang];

                  window._botName = 'Hermes';
                  if (typeof _allSessions !== 'undefined') {
                    _allSessions = [
                      {session_id: 'live-a', title: 'Live A', message_count: 2, archived: false},
                      {session_id: 'arch-1', title: 'Arch 1', message_count: 1, archived: true},
                      {session_id: 'arch-2', title: 'Arch 2', message_count: 1, archived: true},
                    ];
                  }
                  if (typeof S !== 'undefined') {
                    S.lastUsage = {
                      last_prompt_tokens: 120000,
                      context_length: 1000000,
                      input_tokens: 0,
                      output_tokens: 0,
                      cache_hit_percent: 38,
                      cache_read_tokens: 48100,
                      cache_write_tokens: 0,
                    };
                  }
                  if (typeof _applyReasoningChip === 'function') {
                    _applyReasoningChip('medium', {supported_efforts: ['low','medium','high']});
                  }

                  // Real usage events paint the meter first; the locale switch must
                  // then repaint it from that resolved snapshot (never from
                  // S.lastUsage, which a restore or a settings change leaves stale).
                  if (typeof _syncCtxIndicator === 'function' && typeof S !== 'undefined' && S.lastUsage) {
                    _syncCtxIndicator(S.lastUsage);
                  }

                  // The single code path under test: setLocale + applyLocaleToDOM
                  // (which must repaint every dynamic surface from cached state).
                  setLocale(lang);
                  applyLocaleToDOM();

                  const txt = el => (el ? (el.textContent || '').trim() : null);
                  const chipLabel = document.getElementById('composerReasoningLabel');
                  const opts = {};
                  document.querySelectorAll('#composerReasoningDropdown .reasoning-option').forEach(o => {
                    opts[o.dataset.effort === '' ? 'menu_default' : 'menu_' + o.dataset.effort] = (o.textContent || '').trim();
                  });
                  const usage = document.getElementById('ctxTooltipUsage');
                  const ind = document.getElementById('ctxIndicator');
                  const archivedToggle = [...document.querySelectorAll('#sessionList > div')].find(
                    d => /Показать архив|Скрыть архив|Show \\d+ archived|Hide archived/.test((d.textContent || '').trim()));
                  const tts = document.querySelector('#settingsTtsEngine option[value="browser"]');
                  const got = {
                    placeholder: document.getElementById('msg').placeholder,
                    chip: txt(chipLabel),
                    ctx_usage: txt(usage),
                    aria_ctx: ind ? ind.getAttribute('aria-label') : null,
                    archived: archivedToggle ? (archivedToggle.textContent || '').trim() : null,
                    tts_browser: tts ? (tts.textContent || '').trim() : null,
                    ...opts,
                  };
                  for (const k of Object.keys(want)) {
                    const gotVal = got[k] == null ? null : String(got[k]);
                    const okVal = gotVal != null && (gotVal === want[k] || gotVal.startsWith(want[k]));
                    if (!okVal) fail.push(`[${lang}] ${k}: expected ${JSON.stringify(want[k])}, got ${JSON.stringify(got[k])}`);
                  }
                  return {fail, got};
                }"""
            )

            # 1) English baseline
            r = page.evaluate(JS_ROUND, "en")
            failures.extend(r["fail"])

            # 2) Switch to Russian WITHOUT reload — every dynamic surface repaints
            r = page.evaluate(JS_ROUND, "ru")
            failures.extend(r["fail"])

            # 3) Switch back to English — stale RU strings would fail here
            r = page.evaluate(JS_ROUND, "en")
            failures.extend(r["fail"])

            # 4) Busy/locked placeholder must win over the idle repaint
            #    (lockComposerForClarify override, re-asserted by the busy pass).
            r = page.evaluate(
                """() => {
                  const fail = [];
                  setLocale('ru');
                  lockComposerForClarify('BUSY-OVERRIDE');
                  applyLocaleToDOM();
                  const got = document.getElementById('msg').placeholder;
                  if (got !== 'BUSY-OVERRIDE') fail.push(`[busy] placeholder: expected 'BUSY-OVERRIDE', got ${JSON.stringify(got)}`);
                  // and unlocking restores the localized idle placeholder
                  unlockComposerForClarify();
                  applyLocaleToDOM();
                  const got2 = document.getElementById('msg').placeholder;
                  if (got2 !== 'Сообщение для Hermes\\u2026') fail.push(`[unlock] placeholder: expected RU idle, got ${JSON.stringify(got2)}`);
                  return fail;
                }"""
            )
            failures.extend(r)

            # 5) A second clarify that replaces the prompt WITHOUT unlocking
            #    first must not let the repaint restore the previous question
            #    (_composerLockState.text has to track the current one).
            r = page.evaluate(
                """() => {
                  const fail = [];
                  setLocale('ru');
                  lockComposerForClarify('FIRST-QUESTION');
                  applyLocaleToDOM();
                  if (document.getElementById('msg').placeholder !== 'FIRST-QUESTION')
                    fail.push('[relock] the first prompt was not applied');
                  lockComposerForClarify('SECOND-QUESTION');
                  applyLocaleToDOM();
                  const got = document.getElementById('msg').placeholder;
                  if (got !== 'SECOND-QUESTION') fail.push(`[relock] placeholder: expected 'SECOND-QUESTION', got ${JSON.stringify(got)}`);
                  unlockComposerForClarify();
                  applyLocaleToDOM();
                  return fail;
                }"""
            )
            failures.extend(r)

            # 6) The auto-compression guidance is a LIVE instruction: a language
            #    switch while compression runs must re-localize it (not replace it
            #    with the idle text), and completion must recompute in the CURRENT
            #    locale instead of resurrecting the pre-compression text.
            r = page.evaluate(
                """() => {
                  const fail = [];
                  const msg = document.getElementById('msg');
                  const sid = (S.session && S.session.session_id) || 'locale-test';
                  const hint = () => t('composer_compression_will_queue') || '';
                  const idle = () => t('composer_placeholder_idle', assistantDisplayName());

                  setLocale('en');
                  applyLocaleToDOM();
                  const enIdle = idle();

                  setCompressionUi({automatic:true, phase:'running', sessionId:sid});
                  if (msg.placeholder !== hint())
                    fail.push(`[compression/en-start] expected ${JSON.stringify(hint())}, got ${JSON.stringify(msg.placeholder)}`);

                  setLocale('ru');
                  applyLocaleToDOM();
                  if (msg.placeholder !== hint())
                    fail.push(`[compression/ru-live] expected the RU guidance ${JSON.stringify(hint())}, got ${JSON.stringify(msg.placeholder)}`);
                  if (msg.placeholder === idle())
                    fail.push('[compression/ru-live] the RU idle placeholder replaced the active guidance');

                  setLocale('en');
                  applyLocaleToDOM();
                  if (msg.placeholder !== hint())
                    fail.push(`[compression/en-live] expected ${JSON.stringify(hint())}, got ${JSON.stringify(msg.placeholder)}`);

                  setCompressionUi({automatic:true, phase:'done', sessionId:sid});
                  if (msg.placeholder !== idle())
                    fail.push(`[compression/en-done] expected the EN idle placeholder ${JSON.stringify(idle())}, got ${JSON.stringify(msg.placeholder)}`);

                  setCompressionUi({automatic:true, phase:'running', sessionId:sid});
                  setLocale('ru');
                  applyLocaleToDOM();
                  setCompressionUi({automatic:true, phase:'done', sessionId:sid});
                  if (msg.placeholder !== idle())
                    fail.push(`[compression/ru-done] expected the RU idle placeholder ${JSON.stringify(idle())}, got ${JSON.stringify(msg.placeholder)}`);
                  if (msg.placeholder === enIdle)
                    fail.push('[compression/ru-done] the pre-compression EN text came back');

                  // A message typed while compression ran must not leave the stale
                  // guidance behind once the field is cleared: updateSendBtn runs on
                  // every composer change and re-applies the placeholder.
                  msg.value = 'queued while compressing';
                  setCompressionUi({automatic:true, phase:'running', sessionId:sid});
                  setCompressionUi({automatic:true, phase:'done', sessionId:sid});
                  msg.value = '';
                  updateSendBtn();
                  if (msg.placeholder !== idle())
                    fail.push(`[compression/queued] stale guidance survived clearing the draft: ${JSON.stringify(msg.placeholder)}`);

                  // An attachment staged while compression ran keeps the composer
                  // "with content" (the busy pass declines), so completion must
                  // still replace the guidance: it is visible while the textarea
                  // is empty, and it is false once compression has finished.
                  S.pendingFiles = [{name: 'staged.txt'}];
                  setCompressionUi({automatic:true, phase:'running', sessionId:sid});
                  setCompressionUi({automatic:true, phase:'done', sessionId:sid});
                  if (msg.placeholder === hint())
                    fail.push(`[compression/attachment] the guidance outlived compression: ${JSON.stringify(msg.placeholder)}`);
                  if (msg.placeholder !== idle())
                    fail.push(`[compression/attachment] expected the idle placeholder ${JSON.stringify(idle())}, got ${JSON.stringify(msg.placeholder)}`);
                  S.pendingFiles = [];
                  updateSendBtn();

                  clearCompressionUi();
                  setLocale('en');
                  applyLocaleToDOM();
                  return fail;
                }"""
            )
            failures.extend(r)

            # 7) The reasoning chip's accessible name (title + aria-label) must
            #    follow the locale; the English prefix used to leak into RU.
            r = page.evaluate(
                """() => {
                  const fail = [];
                  const chip = document.getElementById('composerReasoningChip');
                  if (!chip) return ['[chip] #composerReasoningChip is missing'];
                  for (const lang of ['en', 'ru', 'en']) {
                    setLocale(lang);
                    _applyReasoningChip('medium', {supported_efforts: ['low', 'medium', 'high']});
                    const expected = t('reasoning_effort_title', t('reasoning_effort_medium'));
                    if (expected === 'reasoning_effort_title')
                      fail.push(`[chip/${lang}] reasoning_effort_title is missing from this bundle`);
                    const title = String(chip.title || '');
                    const aria = String(chip.getAttribute('aria-label') || '');
                    if (title !== expected)
                      fail.push(`[chip/${lang}] title: expected ${JSON.stringify(expected)}, got ${JSON.stringify(title)}`);
                    if (aria !== expected)
                      fail.push(`[chip/${lang}] aria-label: expected ${JSON.stringify(expected)}, got ${JSON.stringify(aria)}`);
                    if (lang !== 'en' && /^Reasoning effort/.test(aria))
                      fail.push(`[chip/${lang}] the English prefix leaked into the accessible name: ${JSON.stringify(aria)}`);
                  }
                  setLocale('en');
                  return fail;
                }"""
            )
            failures.extend(r)

            # 8) A locale repaint must repaint the context meter from the input it
            #    was painted with, never from S.lastUsage: a restored session or a
            #    context-window change in settings leaves S.lastUsage stale, and
            #    re-feeding it rewrites the percentage and the compression
            #    threshold with older numbers (#7697 review).
            r = page.evaluate(
                """() => {
                  const fail = [];
                  const txt = id => { const el = document.getElementById(id); return el ? String(el.textContent || '').trim() : null; };
                  setLocale('en');
                  applyLocaleToDOM();
                  // Resolved input: 1M window, 120K prompt → 12%, threshold 800K.
                  S.lastUsage = {
                    last_prompt_tokens: 120000, context_length: 1000000,
                    threshold_tokens: 800000, input_tokens: 0, output_tokens: 0,
                  };
                  _syncCtxIndicator(S.lastUsage);
                  const painted = txt('ctxTooltipUsage');
                  if (!/12% used/.test(String(painted)))
                    fail.push(`[ctx-snapshot] setup did not paint 12%: ${JSON.stringify(painted)}`);
                  // Poison S.lastUsage the way a restore / stale poll leaves it.
                  S.lastUsage = {last_prompt_tokens: 120000, context_length: 200000};
                  setLocale('ru');
                  applyLocaleToDOM();
                  const after = String(txt('ctxTooltipUsage') || '');
                  const thr = String(txt('ctxTooltipThreshold') || '');
                  if (!/использовано 12%/.test(after))
                    fail.push(`[ctx-snapshot] locale repaint used stale usage: ${JSON.stringify(after)}`);
                  if (!/800\\.0k/.test(thr))
                    fail.push(`[ctx-snapshot] locale repaint rewrote the threshold: ${JSON.stringify(thr)}`);
                  // And the repaint stays localized (the RU pass still applies).
                  if (!after.startsWith('Контекстное окно')) fail.push(`[ctx-snapshot] not localized after repaint: ${JSON.stringify(after)}`);
                  setLocale('en');
                  applyLocaleToDOM();
                  return fail;
                }"""
            )
            failures.extend(r)

            # 9) A repaint while an attachment is staged must keep the assistant-name
            #    personalization: the declarative pass writes the static
            #    index.html placeholder first, and the busy pass declines on the
            #    content guard, so the personalized idle text has to be written
            #    before delegating (#7697 review).
            r = page.evaluate(
                """() => {
                  const fail = [];
                  const msg = document.getElementById('msg');
                  S.activeProfile = 'default';
                  window._botName = 'GateBot';
                  if (typeof applyBotName === 'function') applyBotName();
                  const personalized = 'Message GateBot…';
                  if (msg.placeholder !== personalized)
                    fail.push(`[idle-name] setup: expected ${JSON.stringify(personalized)}, got ${JSON.stringify(msg.placeholder)}`);
                  S.pendingFiles = [{name: 'staged.txt'}];
                  applyLocaleToDOM();
                  if (msg.placeholder !== personalized)
                    fail.push(`[idle-name] repaint dropped the assistant name: ${JSON.stringify(msg.placeholder)}`);
                  S.pendingFiles = [];
                  window._botName = 'Hermes';
                  applyBotName();
                  updateSendBtn();
                  return fail;
                }"""
            )
            failures.extend(r)

            meaningful = [(k, t, u) for (k, t, u) in errors
                          if not _is_benign(t) and not _is_offline_external(t, u)]
            for kind, txt, _url in meaningful:
                failures.append(f"[runtime] {kind}: {txt}")

            browser.close()

        if failures:
            print("\nLOCALE-SWITCH TEST FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("LOCALE-SWITCH TEST PASSED — en->ru->en repaints all dynamic surfaces; locked placeholder wins")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_live_locale_switch_repaints_dynamic_strings():
    """pytest entry point: run the browser flow and assert it stayed green.

    Skips cleanly where playwright/Chromium are unavailable (mirrors the
    other browser-backed tests in this suite); the file is still runnable as
    a standalone script via `python tests/test_issue7697_live_locale_switch.py`.
    """
    import pytest  # noqa: PLC0415 — only needed when collected by a runner

    pytest.importorskip("playwright.sync_api")
    assert main() == 0, "live locale-switch repaint assertions failed (see output above)"


if __name__ == "__main__":
    sys.exit(main())
