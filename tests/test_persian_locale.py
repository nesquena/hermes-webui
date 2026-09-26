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
    # Server true or client true or Persian locale defaults to true
    assert "if (settings && settings.rtl === true)" in panels_src
    assert "saved = isFaLocale;" in panels_src


def test_vazirmatn_font_license_exists():
    """Item 6: SIL Open Font License must accompany the Vazirmatn font files in static/fonts/."""
    ofl = ROOT / "static" / "fonts" / "OFL.txt"
    assert ofl.exists(), "static/fonts/OFL.txt is missing"
    content = ofl.read_text(encoding="utf-8")
    assert "Saber Rastikerdar" in content
    assert "SIL OPEN FONT LICENSE" in content
