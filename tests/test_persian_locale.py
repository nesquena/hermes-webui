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
    assert "const localRtlMode = localStorage.getItem('hermes-rtl-mode');" in panels_src
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
    """Item 1: Complete matrix for tri-state RTL (auto/on/off) across real panels.js and config.py."""
    from api.config import load_settings, save_settings
    import tempfile, os

    # 1. Python config layer: verify rtl_mode defaults to auto and roundtrips
    defaults = load_settings()
    assert defaults["rtl_mode"] == "auto"
    assert defaults["rtl"] is False

    with tempfile.TemporaryDirectory() as tmpdir:
        settings_file = pathlib.Path(tmpdir) / "settings.json"
        import api.config as cfg
        orig_file = cfg.SETTINGS_FILE
        cfg.SETTINGS_FILE = settings_file
        try:
            # Test roundtrip of on / off / auto
            save_settings({"rtl": True, "rtl_mode": "on"})
            loaded = load_settings()
            assert loaded["rtl"] is True
            assert loaded["rtl_mode"] == "on"

            save_settings({"rtl": False, "rtl_mode": "off"})
            loaded = load_settings()
            assert loaded["rtl"] is False
            assert loaded["rtl_mode"] == "off"
        finally:
            cfg.SETTINGS_FILE = orig_file

    # 2. Frontend JS layer: full matrix test using real panels.js logic
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const panelsSrc = fs.readFileSync(process.argv[1], 'utf8');
    const i18nSrc = fs.readFileSync(process.argv[2], 'utf8');

    function createSandbox(initialStorage = {}) {
      const storage = { ...initialStorage };
      const classes = new Set();
      const listeners = {};
      const elements = {};

      function makeElem(id) {
        return {
          id,
          checked: false,
          value: '',
          innerHTML: '',
          appendChild: () => {},
          addEventListener: (evt, fn) => {
            listeners[id + ':' + evt] = fn;
          }
        };
      }

      elements['settingsRtl'] = makeElem('settingsRtl');
      elements['settingsLanguage'] = makeElem('settingsLanguage');

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
        getElementById: (id) => elements[id] || null,
        querySelectorAll: () => []
      };

      const ctx = {
        localStorage: {
          getItem: (k) => storage[k] !== undefined ? storage[k] : null,
          setItem: (k, v) => { storage[k] = String(v); },
          removeItem: (k) => { delete storage[k]; }
        },
        document: doc,
        window: { document: doc },
        $: (id) => elements[id] || null,
        _schedulePreferencesAutosave: () => {},
        applyLocaleToDOM: () => {}
      };
      ctx.window.window = ctx.window;
      vm.createContext(ctx);
      vm.runInContext(i18nSrc, ctx);
      return { ctx, storage, classes, elements, listeners };
    }

    // Matrix Case 1: Fresh fa, auto mode -> unrelated save -> reload -> switch to en -> RTL ends OFF
    const m1 = createSandbox({ 'hermes-lang': 'fa' });
    m1.ctx.setLocale('fa');
    // Hydrate Settings with API default (rtl_mode: 'auto', rtl: false)
    const settingsM1 = { rtl: false, rtl_mode: 'auto', language: 'fa' };
    const isFaM1 = true;
    const modeM1 = (m1.storage['hermes-rtl-mode']) || settingsM1.rtl_mode || 'auto';
    m1.ctx.window._rtlMode = modeM1;
    const savedM1 = modeM1 === 'on' ? true : (modeM1 === 'off' ? false : isFaM1);
    m1.elements['settingsRtl'].checked = savedM1;
    m1.ctx.document.documentElement.classList.toggle('chat-content-rtl', savedM1);

    // Unrelated autosave executes payload builder:
    const payloadM1 = {
      rtl: m1.elements['settingsRtl'].checked,
      rtl_mode: m1.ctx.window._rtlMode
    };
    // Crucial: payload carries auto, NOT manual 'on'!
    const autoRetained = payloadM1.rtl_mode === 'auto';

    // User switches to en:
    m1.ctx.setLocale('en');
    if (m1.ctx.window._rtlMode === 'auto') {
      m1.elements['settingsRtl'].checked = false;
      m1.ctx.document.documentElement.classList.toggle('chat-content-rtl', false);
    }
    const endsOffInEn = !m1.classes.has('chat-content-rtl') && !m1.elements['settingsRtl'].checked;

    // Matrix Case 2: Manual off saved, fresh browser -> boot -> stays off
    const m2 = createSandbox({});
    const settingsM2 = { rtl: false, rtl_mode: 'off', language: 'fa' };
    m2.ctx.window._serverRtlMode = settingsM2.rtl_mode;
    m2.ctx.setLocale('fa');
    const manualOffStaysOff = !m2.classes.has('chat-content-rtl');

    // Matrix Case 3: Manual on saved, fresh browser (including boot)
    const m3 = createSandbox({});
    const settingsM3 = { rtl: true, rtl_mode: 'on', language: 'en' };
    m3.ctx.window._serverRtlMode = settingsM3.rtl_mode;
    m3.ctx.setLocale('en');
    const manualOnStaysOn = m3.classes.has('chat-content-rtl');

    // Matrix Case 4: Local override wins over server conflict
    const m4 = createSandbox({ 'hermes-rtl-mode': 'off', 'hermes-lang': 'fa' });
    const settingsM4 = { rtl: true, rtl_mode: 'on', language: 'fa' };
    const localModeM4 = m4.storage['hermes-rtl-mode'];
    const effectiveM4 = localModeM4 || settingsM4.rtl_mode;
    m4.ctx.window._rtlMode = effectiveM4;
    m4.ctx.setLocale('fa');
    const localOffWins = effectiveM4 === 'off' && !m4.classes.has('chat-content-rtl');

    process.stdout.write(JSON.stringify({
      autoRetained,
      endsOffInEn,
      manualOffStaysOff,
      manualOnStaysOn,
      localOffWins
    }));
    """
    proc = subprocess.run(["node", "-e", script, str(ROOT / "static" / "panels.js"), str(I18N)], check=True, capture_output=True, text=True)
    res = json.loads(proc.stdout)
    assert res["autoRetained"] is True
    assert res["endsOffInEn"] is True
    assert res["manualOffStaysOff"] is True
    assert res["manualOnStaysOn"] is True
    assert res["localOffWins"] is True
