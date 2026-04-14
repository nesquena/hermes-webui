import json
import pathlib
import re
import subprocess
import textwrap


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _run_i18n_case(script_expr: str) -> dict:
    wrapped_expr = f"(() => ({script_expr}))()"
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(REPO_ROOT / "static" / "i18n.js"))}, 'utf8');
        const storage = {{}};
        const ctx = {{
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{
            documentElement: {{ lang: '' }},
            querySelectorAll: () => [],
          }},
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const out = vm.runInContext({json.dumps(wrapped_expr)}, ctx);
        process.stdout.write(JSON.stringify(out));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_i18n_exposes_locale_resolvers():
    assert "function resolveLocale(" in I18N_JS
    assert "function resolvePreferredLocale(" in I18N_JS


def test_locale_alias_resolution_and_precedence_logic():
    result = _run_i18n_case(
        """
{
  zhCn: resolveLocale('zh-CN'),
  zhTw: resolveLocale('zh_TW'),
  enUs: resolveLocale('EN-us'),
  esMx: resolveLocale('es-MX'),
  bad: resolveLocale('xx-YY'),
  preferred1: resolvePreferredLocale('zh-CN', 'en'),
  preferred2: resolvePreferredLocale('xx-YY', 'zh-Hant'),
  preferred3: resolvePreferredLocale('', 'xx-YY'),
}
        """
    )
    assert result["zhCn"] == "zh"
    assert result["zhTw"] == "zh-Hant"
    assert result["enUs"] == "en"
    assert result["esMx"] == "es"
    assert result["bad"] is None
    assert result["preferred1"] == "zh"
    assert result["preferred2"] == "zh-Hant"
    assert result["preferred3"] == "en"


def test_set_locale_normalizes_alias_and_persists_canonical_key():
    result = _run_i18n_case(
        """
{
  ...(setLocale('zh-CN'), {}),
  saved: localStorage.getItem('hermes-lang'),
  htmlLang: document.documentElement.lang,
}
        """
    )
    assert result["saved"] == "zh"
    assert result["htmlLang"] == "zh-CN"


def test_boot_and_settings_panel_use_shared_locale_precedence():
    assert re.search(r"resolvePreferredLocale\(s\.language\s*,\s*localStorage\.getItem\('hermes-lang'\)\)", BOOT_JS)
    assert re.search(r"resolvePreferredLocale\(settings\.language\s*,\s*localStorage\.getItem\('hermes-lang'\)\)", PANELS_JS)
