import pathlib
import json
import subprocess

ROOT = pathlib.Path(__file__).parent.parent.resolve()
I18N = ROOT / "static" / "i18n.js"

def test_fa_locale_structure_and_rtl():
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const src = fs.readFileSync(process.argv[1], 'utf8');
    const storage = {};
    const classes = new Set();
    const ctx = {
      localStorage: {
        getItem: (k) => storage[k] || null,
        setItem: (k, v) => { storage[k] = String(v); },
      },
      document: {
        documentElement: {
          lang: '',
          classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
          },
          setAttribute: function(k, v) { this[k] = v; },
          removeAttribute: function(k) { delete this[k]; }
        },
        querySelectorAll: () => [],
      },
    };
    vm.createContext(ctx);
    vm.runInContext(src, ctx);
    const resolved = vm.runInContext("resolveLocale('fa')", ctx);
    const faBundle = vm.runInContext("LOCALES.fa", ctx);
    vm.runInContext("setLocale('fa')", ctx);
    const hasRtlClass = classes.has('chat-content-rtl');
    const lang = ctx.document.documentElement.lang;
    process.stdout.write(JSON.stringify({
      resolved,
      label: faBundle._label,
      hasRtlClass,
      lang,
      settings_tab_preferences: faBundle.settings_tab_preferences,
      settings_label_rtl: faBundle.settings_label_rtl
    }));
    """
    proc = subprocess.run(["node", "-e", script, str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    assert res["resolved"] == "fa"
    assert res["label"] == "فارسی"
    assert res["hasRtlClass"] is True
    assert res["lang"] == "fa-IR"
    assert res["settings_tab_preferences"] == "ترجیحات"
    assert res["settings_label_rtl"] == "چیدمان راست‌به‌چپ چت"


def test_sessions_source_placeholders_preserved():
    """Item 4 regression: sessions_source_webui and sessions_source_cli must contain {0} count placeholder."""
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const src = fs.readFileSync(process.argv[1], 'utf8');
    const ctx = {
      localStorage: { getItem: () => null, setItem: () => {} },
      document: { documentElement: { lang: '' }, querySelectorAll: () => [] }
    };
    vm.createContext(ctx);
    vm.runInContext(src, ctx);
    const fa = vm.runInContext("LOCALES.fa", ctx);
    process.stdout.write(JSON.stringify({
      webui: fa.sessions_source_webui,
      cli: fa.sessions_source_cli
    }));
    """
    proc = subprocess.run(["node", "-e", script, str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    assert "{0}" in res["webui"], f"Expected {0} placeholder in sessions_source_webui, got: {res['webui']}"
    assert "{0}" in res["cli"], f"Expected {0} placeholder in sessions_source_cli, got: {res['cli']}"


def test_opening_settings_preserves_persian_auto_rtl():
    """Item 5 regression: Opening settings panel must not revert automatic RTL for Persian users."""
    panels_src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "const isFaLocale = currentLocale === 'fa';" in panels_src
    assert "let localRtlMode = localStorage.getItem('hermes-rtl-mode');" in panels_src
    assert "window._rtlMode = effectiveMode;" in panels_src
    assert "const saved = effectiveMode === 'on' ? true : (effectiveMode === 'off' ? false : isFaLocale);" in panels_src


def test_vazirmatn_font_license_exists():
    """Item 6: SIL Open Font License must accompany the Vazirmatn font files in static/fonts/."""
    ofl = ROOT / "static" / "fonts" / "OFL.txt"
    assert ofl.exists(), "static/fonts/OFL.txt is missing"
    content = ofl.read_text(encoding="utf-8")
    assert "Saber Rastikerdar" in content
    assert "SIL OPEN FONT LICENSE" in content


def test_fa_function_signatures_and_destructive_warnings():
    """Item 2: Ensure function signatures, named arguments, and safety warnings match en exactly."""
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const src = fs.readFileSync(process.argv[1], 'utf8');
    const ctx = {
      localStorage: { getItem: () => null, setItem: () => {} },
      document: { documentElement: { lang: '' }, querySelectorAll: () => [] }
    };
    vm.createContext(ctx);
    vm.runInContext(src, ctx);
    const fa = vm.runInContext("LOCALES.fa", ctx);
    const t = vm.runInContext("t", ctx);
    vm.runInContext("_locale = LOCALES.fa", ctx);

    const ckpt = t('checkpoint_restore_confirm_message', 'test-snap');
    const untracked = t('session_worktree_remove_untracked_warning', 5);
    const ahead = t('session_worktree_remove_ahead_warning', 3);
    const paused = t('goal_paused', 'build app');
    const resumed = t('goal_resumed', 'build app');
    const achieved = t('goal_achieved', 'tests green');
    const goalSet = t('goal_set', 10, 'my goal');
    const dl = t('downloading', 'file.txt');
    const delConfirm = t('delete_confirm', 'item1');

    process.stdout.write(JSON.stringify({
      ckpt, untracked, ahead, paused, resumed, achieved, goalSet, dl, delConfirm
    }));
    """
    proc = subprocess.run(["node", "-e", script, str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    assert "test-snap" in res["ckpt"], f"Checkpoint label missing: {res['ckpt']}"
    assert "رونویسی" in res["ckpt"] or "بازگردانده" in res["ckpt"]
    assert "5" in res["untracked"] and "ردیابی‌نشده" in res["untracked"]
    assert "3" in res["ahead"] and "ارسال‌نشده" in res["ahead"]
    assert "build app" in res["paused"]
    assert "build app" in res["resumed"]
    assert "tests green" in res["achieved"]
    assert "10" in res["goalSet"] and "my goal" in res["goalSet"]
    assert "file.txt" in res["dl"]
    assert "item1" in res["delConfirm"]


def test_rtl_state_transitions_default_and_explicit():
    """Item 1: End-to-end state transitions for automatic locale default vs explicit user override."""
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const i18nSrc = fs.readFileSync(process.argv[1], 'utf8');

    function setup(storage = {}) {
      const classes = new Set();
      const doc = {
        documentElement: {
          lang: '',
          classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
            toggle: (c, force) => {
              if (force !== undefined) {
                if (force) classes.add(c);
                else classes.delete(c);
              } else {
                if (classes.has(c)) classes.delete(c);
                else classes.add(c);
              }
            }
          },
          setAttribute: () => {},
          removeAttribute: () => {}
        },
        querySelectorAll: () => []
      };
      const ctx = {
        localStorage: {
          getItem: (k) => storage[k] !== undefined ? storage[k] : null,
          setItem: (k, v) => { storage[k] = String(v); },
          removeItem: (k) => { delete storage[k]; }
        },
        document: doc,
        window: {}
      };
      ctx.window = ctx;
      vm.createContext(ctx);
      vm.runInContext(i18nSrc, ctx);
      return { ctx, storage, classes };
    }

    // 1. Initial en locale, no localStorage -> no RTL, no localStorage dirtying
    const s1 = setup();
    s1.ctx.setLocale('en');
    const enHasRtl = s1.classes.has('chat-content-rtl');
    const enStorageClean = s1.storage['hermes-rtl'] === undefined;

    // 2. Switch en -> fa without prior localStorage -> automatic RTL, storage remains clean
    s1.ctx.setLocale('fa');
    const faAutoRtl = s1.classes.has('chat-content-rtl');
    const faStorageClean = s1.storage['hermes-rtl'] === undefined;

    // 3. Explicit fa opt-out -> user unchecks RTL checkbox (stored 'false')
    s1.storage['hermes-rtl'] = 'false';
    s1.ctx.setLocale('fa');
    const explicitFaOff = !s1.classes.has('chat-content-rtl');

    // 4. Explicit en opt-in -> user checks RTL in en (stored 'true')
    const s2 = setup({ 'hermes-rtl': 'true' });
    s2.ctx.setLocale('en');
    const explicitEnOn = s2.classes.has('chat-content-rtl');

    process.stdout.write(JSON.stringify({
      enHasRtl,
      enStorageClean,
      faAutoRtl,
      faStorageClean,
      explicitFaOff,
      explicitEnOn
    }));
    """
    proc = subprocess.run(["node", "-e", script, str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    assert res["enHasRtl"] is False
    assert res["enStorageClean"] is True
    assert res["faAutoRtl"] is True
    assert res["faStorageClean"] is True
    assert res["explicitFaOff"] is True
    assert res["explicitEnOn"] is True


def test_goal_status_argument_contracts_distinct_reason_and_budget():
    """Item 2: goal_status_paused and goal_status_done must accept 4 arguments and preserve budget."""
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const src = fs.readFileSync(process.argv[1], 'utf8');
    const ctx = {
      localStorage: { getItem: () => null, setItem: () => {} },
      document: { documentElement: { lang: '' }, querySelectorAll: () => [] }
    };
    vm.createContext(ctx);
    vm.runInContext(src, ctx);
    const fa = vm.runInContext("LOCALES.fa", ctx);
    const t = vm.runInContext("t", ctx);
    vm.runInContext("_locale = LOCALES.fa", ctx);

    const pausedMsg = t('goal_status_paused', 3, 10, 'waiting on api', 'ship feature');
    const doneMsg = t('goal_status_done', 7, 20, 'refactor codebase');

    process.stdout.write(JSON.stringify({ pausedMsg, doneMsg }));
    """
    proc = subprocess.run(["node", "-e", script, str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    assert "ship feature" in res["pausedMsg"], f"Goal lost in pausedMsg: {res['pausedMsg']}"
    assert "waiting on api" in res["pausedMsg"], f"Reason lost in pausedMsg: {res['pausedMsg']}"
    assert "3/10" in res["pausedMsg"], f"Budget lost in pausedMsg: {res['pausedMsg']}"
    assert "refactor codebase" in res["doneMsg"], f"Goal lost in doneMsg: {res['doneMsg']}"
    assert "7/20" in res["doneMsg"], f"Budget lost in doneMsg: {res['doneMsg']}"


def test_composed_hydration_language_payload_coherence():
    """Driven real production-seam test for RTL preference migration, storage errors, and full state matrix."""
    from api.config import load_settings, save_settings
    import tempfile
    import api.config as cfg

    # 1. Real Python config layer: isolated real settings save/load
    with tempfile.TemporaryDirectory() as tmpdir:
        settings_file = pathlib.Path(tmpdir) / "settings.json"
        orig_file = cfg.SETTINGS_FILE
        cfg.SETTINGS_FILE = settings_file
        try:
            # (a) Defaults
            defaults = load_settings()
            assert defaults["rtl_mode"] == "auto"
            assert defaults["rtl"] is False

            # (b) Roundtrip of explicit on / off
            save_settings({"rtl": True, "rtl_mode": "on"})
            loaded = load_settings()
            assert loaded["rtl"] is True
            assert loaded["rtl_mode"] == "on"

            save_settings({"rtl": False, "rtl_mode": "off"})
            loaded = load_settings()
            assert loaded["rtl"] is False
            assert loaded["rtl_mode"] == "off"

            # (c) Legacy server migration: stored rtl=True without mode becomes on
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump({"language": "en", "rtl": True}, f)
            loaded = load_settings()
            assert loaded["rtl_mode"] == "on"
            assert loaded["rtl"] is True

            # (d) Ambiguous legacy server False remains eligible for auto
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump({"language": "fa", "rtl": False}, f)
            loaded = load_settings()
            assert loaded["rtl_mode"] == "auto"
            assert loaded["rtl"] is False

            # (e) Boolean-only saves translate to rtl_mode when omitted (#7699)
            save_settings({"rtl": True})
            loaded = load_settings()
            assert loaded["rtl"] is True
            assert loaded["rtl_mode"] == "on"

            save_settings({"rtl": False})
            loaded = load_settings()
            assert loaded["rtl"] is False
            assert loaded["rtl_mode"] == "off"

            # (f) Explicit rtl_mode is preserved alongside boolean rtl
            save_settings({"rtl": False, "rtl_mode": "auto"})
            loaded = load_settings()
            assert loaded["rtl"] is False
            assert loaded["rtl_mode"] == "auto"
        finally:
            cfg.SETTINGS_FILE = orig_file

    # 2. Real frontend JS layer: executed against panels.js and i18n.js
    node_test = """
    const fs = require('fs');
    const vm = require('vm');
    const panelsSrc = fs.readFileSync(process.argv[1], 'utf8');
    const i18nSrc = fs.readFileSync(process.argv[2], 'utf8');

    function setupEnvironment(initialStorage = {}, serverSettings = {}, opts = {}) {
      const storage = { ...initialStorage };
      const listeners = {};
      const elements = {};
      const classes = new Set();

      function makeElem(id = '') {
        return {
          id,
          parentElement: null,
          checked: false,
          value: '',
          innerHTML: '',
          dataset: {},
          style: {},
          classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
            toggle: (c, force) => {
              if (force !== undefined) {
                if (force) classes.add(c);
                else classes.delete(c);
              } else {
                if (classes.has(c)) classes.delete(c);
                else classes.add(c);
              }
            }
          },
          appendChild: function() {},
          insertBefore: function() {},
          setAttribute: function() {},
          removeAttribute: function() {},
          querySelector: function() { return null; },
          querySelectorAll: function() { return []; },
          addEventListener: function(evt, fn) {
            listeners[id + ':' + evt] = fn;
          }
        };
      }

      const doc = {
        documentElement: {
          lang: '',
          dataset: {},
          classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
            toggle: (c, force) => {
              if (force !== undefined) {
                if (force) classes.add(c);
                else classes.delete(c);
              } else {
                if (classes.has(c)) classes.delete(c);
                else classes.add(c);
              }
            }
          },
          setAttribute: function() {},
          removeAttribute: function() {}
        },
        getElementById: (id) => elements[id] || (elements[id] = makeElem(id)),
        addEventListener: () => {},
        removeEventListener: () => {},
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (tag) => makeElem(tag)
      };

      let storageThrows = opts.storageThrows || false;
      const ls = {
        getItem: (k) => storage[k] !== undefined ? storage[k] : null,
        setItem: (k, v) => {
          if (storageThrows) throw new Error('QuotaExceededError');
          storage[k] = String(v);
        },
        removeItem: (k) => {
          if (storageThrows) throw new Error('QuotaExceededError');
          delete storage[k];
        }
      };

      let lastSavedPayload = null;
      const win = {
        document: doc,
        window: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        localStorage: ls,
        sessionStorage: ls,
        setInterval: () => 1,
        clearInterval: () => {},
        setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 1; },
        clearTimeout: () => {},
        console: console,
        navigator: { userAgent: 'test' },
        location: { pathname: '/', search: '' },
        S: { session: null, messages: [] },
        $: (id) => {
          if (id === 'settingsModel') return null;
          return elements[id] || (elements[id] = makeElem(id));
        },
        t: (k) => k,
        esc: (s) => s,
        showToast: () => {},
        checkWebUIVersionSkew: () => {},
        api: async (url, fetchOpts) => {
          if (url === '/api/settings') {
            if (fetchOpts && fetchOpts.method === 'POST') {
              const body = JSON.parse(fetchOpts.body);
              lastSavedPayload = body;
              return { ...serverSettings, ...body };
            }
            return { ...serverSettings };
          }
          return {};
        }
      };
      win.window = win;
      doc.defaultView = win;

      vm.createContext(win);
      vm.runInContext(i18nSrc, win);
      vm.runInContext(panelsSrc, win);

      return {
        win,
        storage,
        classes,
        elements,
        listeners,
        setStorageThrows: (val) => { storageThrows = val; },
        getLastSavedPayload: () => lastSavedPayload
      };
    }

    (async () => {
      // 1. Legacy local hermes-rtl='false' + language fa, no stored mode -> RTL turns OFF
      const env1 = setupEnvironment({ 'hermes-rtl': 'false' }, { rtl_mode: 'auto', rtl: false, language: 'fa' });
      await env1.win.loadSettingsPanel();
      const legacyLocalOffTurnsOff = !env1.classes.has('chat-content-rtl') && !env1.elements['settingsRtl'].checked;
      const p1 = env1.win._preferencesPayloadFromUi();
      const legacyLocalOffPayload = p1.rtl === false && p1.rtl_mode === 'off';

      // 2. Legacy local hermes-rtl='true' + language en -> RTL turns ON
      const env2 = setupEnvironment({ 'hermes-rtl': 'true' }, { rtl_mode: 'auto', rtl: false, language: 'en' });
      await env2.win.loadSettingsPanel();
      const legacyLocalOnTurnsOn = env2.classes.has('chat-content-rtl') && env2.elements['settingsRtl'].checked;
      const p2 = env2.win._preferencesPayloadFromUi();
      const legacyLocalOnPayload = p2.rtl === true && p2.rtl_mode === 'on';

      // 3. Server {language:'en', rtl:true}, no mode, fresh browser -> Settings shows ON, persists rtl:true, rtl_mode:'on'
      const env3 = setupEnvironment({}, { rtl: true, language: 'en' });
      await env3.win.loadSettingsPanel();
      const legacyServerOnShowsOn = env3.classes.has('chat-content-rtl') && env3.elements['settingsRtl'].checked;
      const p3 = env3.win._preferencesPayloadFromUi();
      const legacyServerOnPayload = p3.rtl === true && p3.rtl_mode === 'on';
      await env3.win.saveSettings();
      const legacyServerOnSaved = env3.getLastSavedPayload().rtl === true && env3.getLastSavedPayload().rtl_mode === 'on';

      // 4. Storage throws on manual off (fa) -> explicit saveSettings preserves live intent OFF
      const env4 = setupEnvironment({}, { rtl_mode: 'auto', rtl: false, language: 'fa' });
      await env4.win.loadSettingsPanel();
      env4.setStorageThrows(true);
      env4.elements['settingsRtl'].checked = false;
      env4.listeners['settingsRtl:change']();
      await env4.win.saveSettings();
      const storageThrowsManualOffStaysOff = !env4.classes.has('chat-content-rtl') && !env4.elements['settingsRtl'].checked;

      // 5. Storage throws on manual on (en) -> explicit saveSettings preserves live intent ON
      const env5 = setupEnvironment({}, { rtl_mode: 'auto', rtl: false, language: 'en' });
      await env5.win.loadSettingsPanel();
      env5.setStorageThrows(true);
      env5.elements['settingsRtl'].checked = true;
      env5.listeners['settingsRtl:change']();
      await env5.win.saveSettings();
      const storageThrowsManualOnStaysOn = env5.classes.has('chat-content-rtl') && env5.elements['settingsRtl'].checked;

      // 6. Default auto: fresh fa -> Settings shows ON, payload retains auto -> change to en live -> ends OFF
      const env6 = setupEnvironment({}, { rtl_mode: 'auto', rtl: false, language: 'fa' });
      await env6.win.loadSettingsPanel();
      const autoFaShowsOn = env6.classes.has('chat-content-rtl') && env6.elements['settingsRtl'].checked;
      const p6 = env6.win._preferencesPayloadFromUi();
      const autoRetainedInPayload = p6.rtl_mode === 'auto';
      env6.elements['settingsLanguage'].value = 'en';
      env6.listeners['settingsLanguage:change'].call(env6.elements['settingsLanguage']);
      const autoSwitchToEnEndsOff = !env6.classes.has('chat-content-rtl') && !env6.elements['settingsRtl'].checked;

      // 7. Fresh-browser manual on/off persisted
      const env7 = setupEnvironment({}, { rtl_mode: 'on', rtl: true, language: 'en' });
      await env7.win.loadSettingsPanel();
      const freshManualOn = env7.classes.has('chat-content-rtl') && env7.elements['settingsRtl'].checked;

      const env7b = setupEnvironment({}, { rtl_mode: 'off', rtl: false, language: 'fa' });
      await env7b.win.loadSettingsPanel();
      const freshManualOff = !env7b.classes.has('chat-content-rtl') && !env7b.elements['settingsRtl'].checked;

      // 8. Both local/server conflicts:
      // Local off wins over server on
      const env8a = setupEnvironment({ 'hermes-rtl-mode': 'off' }, { rtl_mode: 'on', rtl: true, language: 'fa' });
      await env8a.win.loadSettingsPanel();
      const localOffWinsServerOn = !env8a.classes.has('chat-content-rtl') && !env8a.elements['settingsRtl'].checked;

      // Local on wins over server off
      const env8b = setupEnvironment({ 'hermes-rtl-mode': 'on' }, { rtl_mode: 'off', rtl: false, language: 'en' });
      await env8b.win.loadSettingsPanel();
      const localOnWinsServerOff = env8b.classes.has('chat-content-rtl') && env8b.elements['settingsRtl'].checked;

      // 9. Both save paths (saveSettings & autosave) persist correct values
      const env9 = setupEnvironment({}, { rtl_mode: 'auto', rtl: false, language: 'fa' });
      await env9.win.loadSettingsPanel();
      await env9.win.saveSettings();
      const explicitSavePersisted = env9.getLastSavedPayload().rtl_mode === 'auto';

      env9.elements['settingsRtl'].checked = false;
      env9.listeners['settingsRtl:change']();
      await env9.win._autosavePreferencesSettings(env9.win._preferencesPayloadFromUi());
      const autosavePersisted = env9.getLastSavedPayload().rtl_mode === 'off' && env9.getLastSavedPayload().rtl === false;

      // 10. fa -> en after reload
      const env10 = setupEnvironment({ 'hermes-lang': 'fa' }, { rtl_mode: 'auto', rtl: false, language: 'fa' });
      env10.win.loadLocale();
      const bootFaRtl = env10.classes.has('chat-content-rtl');
      await env10.win.loadSettingsPanel();
      env10.elements['settingsLanguage'].value = 'en';
      env10.listeners['settingsLanguage:change'].call(env10.elements['settingsLanguage']);
      const afterReloadSwitchEnRtlOff = !env10.classes.has('chat-content-rtl');

      process.stdout.write(JSON.stringify({
        legacyLocalOffTurnsOff, legacyLocalOffPayload,
        legacyLocalOnTurnsOn, legacyLocalOnPayload,
        legacyServerOnShowsOn, legacyServerOnPayload, legacyServerOnSaved,
        storageThrowsManualOffStaysOff,
        storageThrowsManualOnStaysOn,
        autoFaShowsOn, autoRetainedInPayload, autoSwitchToEnEndsOff,
        freshManualOn, freshManualOff,
        localOffWinsServerOn, localOnWinsServerOff,
        explicitSavePersisted, autosavePersisted,
        bootFaRtl, afterReloadSwitchEnRtlOff
      }));
    })();
    """
    proc = subprocess.run(["node", "-e", node_test, str(ROOT / "static" / "panels.js"), str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    for k, v in res.items():
        assert v is True, f"Assertion failed for {k}"
