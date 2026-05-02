"""Neo HU-02.5: visible errors and toasts must honor pt-BR."""

import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
I18N = STATIC / "i18n.js"
MESSAGES = (STATIC / "messages.js").read_text(encoding="utf-8")
TERMINAL = (STATIC / "terminal.js").read_text(encoding="utf-8")
LOGIN = (STATIC / "login.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")

REQUIRED_KEYS = [
    "chat_queued_toast",
    "chat_session_reconnected_queued",
    "chat_uploading",
    "chat_upload_error_prefix",
    "chat_nothing_to_send",
    "chat_error_generic",
    "chat_error_label",
    "chat_connection_lost",
    "chat_warning_label",
    "chat_reconnecting",
    "chat_reconnected",
    "chat_context_compressed",
    "chat_out_of_credits",
    "chat_rate_limit_reached",
    "chat_no_response_received",
    "chat_check_api_key_model",
    "clarify_unavailable_status",
    "clarify_unavailable_toast",
    "terminal_library_failed",
]


def _locale_values() -> dict:
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
          const keys = {json.dumps(REQUIRED_KEYS)};
          const result = {{}};
          for (const lang of ['en', 'pt-BR']) {{
            result[lang] = {{}};
            for (const key of keys) {{
              const value = LOCALES[lang][key];
              result[lang][key] = typeof value === 'function' ? value('amostra') : value;
            }}
          }}
          return result;
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


def test_pt_br_has_visible_error_and_toast_strings():
    values = _locale_values()
    missing = [
        key
        for key in REQUIRED_KEYS
        if values["en"].get(key) in (None, "undefined")
        or values["pt-BR"].get(key) in (None, "undefined")
    ]
    assert not missing


def test_pt_br_error_and_toast_strings_are_not_english_fallbacks():
    values = _locale_values()["pt-BR"]
    english_fragments = [
        "Queued:",
        "Upload error",
        "Nothing to send",
        "Connection lost",
        "Reconnecting",
        "Reconnected",
        "Out of credits",
        "Rate limit reached",
        "No response received",
        "Check your API key and model selection.",
        "Clarify endpoint unavailable",
        "Terminal library failed",
    ]
    leaked = {
        key: text
        for key, text in values.items()
        if any(fragment in str(text) for fragment in english_fragments)
    }
    assert leaked == {}


def test_chat_errors_and_toasts_use_i18n_keys():
    hardcoded = [
        "Queued: ",
        "Current session is still running. Reconnected and queued your message.",
        "Upload error: ",
        "Nothing to send",
        "**Error:** An error occurred. Check server logs.",
        "**Error:** Connection lost",
        "Out of credits",
        "Rate limit reached",
        "No response received",
        "Reconnecting…",
        "Reconnected",
        "Clarify endpoint unavailable. Please restart server.",
    ]
    leaked = [text for text in hardcoded if text in MESSAGES]
    assert leaked == []


def test_terminal_and_login_visible_errors_have_pt_br_fallbacks():
    assert "Terminal library failed to load. Check network access to cdn.jsdelivr.net." not in TERMINAL
    assert "showErr(data.error || invalidPw)" not in LOGIN
    assert '"pt-BR": {' in ROUTES
    assert '"invalid_pw": "Senha inv\\u00e1lida"' in ROUTES
    assert '"conn_failed": "Falha na conex\\u00e3o"' in ROUTES
