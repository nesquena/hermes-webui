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


def _extract_call_arglists(src: str, fn_name: str) -> list[str]:
    token = f"{fn_name}("
    out = []
    search_from = 0

    while True:
        start = src.find(token, search_from)
        if start < 0:
            return out

        i = start + len(token)
        depth = 1
        in_single = False
        in_double = False
        in_backtick = False
        escape = False

        while i < len(src):
            ch = src[i]

            if escape:
                escape = False
                i += 1
                continue

            if in_single:
                if ch == "\\":
                    escape = True
                elif ch == "'":
                    in_single = False
                i += 1
                continue

            if in_double:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_double = False
                i += 1
                continue

            if in_backtick:
                if ch == "\\":
                    escape = True
                elif ch == "`":
                    in_backtick = False
                i += 1
                continue

            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "`":
                in_backtick = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    out.append(src[start + len(token) : i])
                    break
            i += 1

        search_from = start + len(token)


def _split_top_level_args(arg_src: str) -> list[str]:
    args = []
    cur = []
    paren = 0
    brace = 0
    bracket = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False

    for ch in arg_src:
        if escape:
            cur.append(ch)
            escape = False
            continue

        if in_single:
            cur.append(ch)
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue

        if in_double:
            cur.append(ch)
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue

        if in_backtick:
            cur.append(ch)
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue

        if ch == "'":
            in_single = True
            cur.append(ch)
            continue
        if ch == '"':
            in_double = True
            cur.append(ch)
            continue
        if ch == "`":
            in_backtick = True
            cur.append(ch)
            continue

        if ch == "(":
            paren += 1
            cur.append(ch)
            continue
        if ch == ")":
            paren -= 1
            cur.append(ch)
            continue
        if ch == "{":
            brace += 1
            cur.append(ch)
            continue
        if ch == "}":
            brace -= 1
            cur.append(ch)
            continue
        if ch == "[":
            bracket += 1
            cur.append(ch)
            continue
        if ch == "]":
            bracket -= 1
            cur.append(ch)
            continue

        if ch == "," and paren == 0 and brace == 0 and bracket == 0:
            args.append("".join(cur).strip())
            cur = []
            continue

        cur.append(ch)

    if cur:
        args.append("".join(cur).strip())
    return args


def _has_precedence_call(src: str, first_arg: str) -> bool:
    expected_second = {
        "localStorage.getItem('hermes-lang')",
        'localStorage.getItem("hermes-lang")',
    }
    for arg_src in _extract_call_arglists(src, "resolvePreferredLocale"):
        args = _split_top_level_args(arg_src)
        if len(args) < 2:
            continue
        first = re.sub(r"\s+", "", args[0])
        second = re.sub(r"\s+", "", args[1])
        if first == first_arg and second in expected_second:
            return True
    return False


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
    assert _has_precedence_call(BOOT_JS, "s.language")
    assert _has_precedence_call(PANELS_JS, "settings.language")
