"""Neo WebUI: pt-BR locale must cover the English locale."""

import json
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
I18N = REPO / "static" / "i18n.js"


def _locale_summary() -> dict:
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(I18N))}, 'utf8');
        const ctx = {{
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          document: {{ documentElement: {{ lang: '' }}, querySelectorAll: () => [] }},
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const out = vm.runInContext(`(() => {{
          const en = LOCALES.en;
          const pt = LOCALES['pt-BR'];
          return {{
            hasPtBr: !!pt,
            enKeys: Object.keys(en || {{}}),
            ptKeys: Object.keys(pt || {{}}),
            label: pt && pt._label,
            speech: pt && pt._speech,
            values: pt ? {{
              terminal_title: pt.terminal_title,
              profile_active: pt.profile_active,
              mcp_servers_title: pt.mcp_servers_title,
              composer_send: pt.composer_send,
              workspace_manage: pt.workspace_manage,
            }} : {{}},
          }};
        }})()`, ctx);
        process.stdout.write(JSON.stringify(out));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def _boot_locale_summary(default_locale: str | None, stored_locale: str | None = None) -> dict:
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(I18N))}, 'utf8');
        const storage = {{}};
        if ({json.dumps(stored_locale)} !== null) {{
          storage['hermes-lang'] = {json.dumps(stored_locale)};
        }}
        const ctx = {{
          window: {{ __neoDefaults: {{ locale: {json.dumps(default_locale)} }} }},
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{ documentElement: {{ lang: '' }}, querySelectorAll: () => [] }},
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const out = vm.runInContext(`(() => ({{
          saved: localStorage.getItem('hermes-lang'),
          htmlLang: document.documentElement.lang,
        }}))()`, ctx);
        process.stdout.write(JSON.stringify(out));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_pt_br_locale_exists_with_brazilian_label():
    data = _locale_summary()
    assert data["hasPtBr"], "LOCALES['pt-BR'] is missing"
    assert data["label"] == "Português (Brasil)"
    assert data["speech"] == "pt-BR"


def test_pt_br_locale_covers_english_keys():
    data = _locale_summary()
    missing = sorted(set(data["enKeys"]) - set(data["ptKeys"]))
    assert not missing, f"pt-BR locale missing keys: {missing}"


def test_pt_br_representative_missing_keys_are_translated():
    values = _locale_summary()["values"]
    assert values["terminal_title"] == "Terminal"
    assert values["profile_active"] == "ativo"
    assert values["mcp_servers_title"] == "Servidores MCP"
    assert values["composer_send"] == "Enviar"
    assert values["workspace_manage"] == "Gerenciar espaços"


def test_pt_br_server_default_locale_applies_when_user_has_no_saved_choice():
    data = _boot_locale_summary("pt-BR")
    assert data["saved"] == "pt-BR"
    assert data["htmlLang"] == "pt-BR"


def test_pt_br_server_default_does_not_override_saved_user_locale():
    data = _boot_locale_summary("pt-BR", stored_locale="en")
    assert data["saved"] == "en"
    assert data["htmlLang"] == "en-US"


def test_invalid_server_default_locale_falls_back_to_english():
    data = _boot_locale_summary("xx-YY")
    assert data["saved"] == "en"
    assert data["htmlLang"] == "en-US"
