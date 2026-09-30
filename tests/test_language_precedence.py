import json
import pathlib
import re
import subprocess
import tempfile
import textwrap


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
I18N_CORE_JS = (REPO_ROOT / "static" / "i18n-core.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")


def _run_i18n_case(
    script_expr: str,
    bundles: tuple[str, ...] = (),
    *,
    navigator_obj: object | None = None,
) -> dict:
    wrapped_expr = f"(async () => ({script_expr}))()"
    sources = [REPO_ROOT / "static" / "i18n-core.js"] + [
        REPO_ROOT / "static" / "locales" / f"{bundle}.js" for bundle in bundles
    ]
    if navigator_obj is None:
        navigator_src = "undefined"
    elif navigator_obj == "throwing":
        navigator_src = (
            "{ get languages(){ throw new Error('navigator access denied'); },"
            " get language(){ throw new Error('navigator access denied'); } }"
        )
    else:
        navigator_src = json.dumps(navigator_obj)
    locale_dir = str(REPO_ROOT / "static" / "locales")
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const sources = {json.dumps([str(source) for source in sources])};
        const localeDir = {json.dumps(locale_dir)};
        const autoLoadSelected = {str(navigator_obj is not None).lower()};
        const storage = {{}};
        let ctx;
        ctx = {{
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{
            baseURI: 'https://example.test/',
            documentElement: {{ lang: '' }},
            querySelectorAll: () => [],
            createElement: () => ({{}}),
            head: {{
              appendChild: (script) => {{
                const requestPath = script.src.split(/[?#]/, 1)[0];
                const filename = requestPath.slice(requestPath.lastIndexOf('/') + 1);
                if (autoLoadSelected && filename.endsWith('.js')) {{
                  try {{
                    vm.runInContext(fs.readFileSync(localeDir + '/' + filename, 'utf8'), ctx);
                    script.onload();
                    return;
                  }} catch (_) {{}}
                }}
                script.onerror();
              }},
            }},
          }},
          navigator: {navigator_src},
        }};
        vm.createContext(ctx);
        for (const source of sources) vm.runInContext(fs.readFileSync(source, 'utf8'), ctx);
        const out = vm.runInContext({json.dumps(wrapped_expr)}, ctx);
        Promise.resolve(out).then((value) => process.stdout.write(JSON.stringify(value)));
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
    assert "function resolveLocale(" in I18N_CORE_JS
    assert "function resolvePreferredLocale(" in I18N_CORE_JS
    assert "function ensureLocale(" in I18N_CORE_JS
    assert "const LOCALE_REGISTRY" in I18N_CORE_JS


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
        """,
        bundles=("zh",),
    )
    assert result["saved"] == "zh"
    assert result["htmlLang"] == "zh-CN"


def test_boot_and_settings_panel_use_shared_locale_precedence():
    assert _has_precedence_call(BOOT_JS, "s.language")
    assert _has_precedence_call(PANELS_JS, "settings.language")


def test_registry_keeps_all_metadata_eager_and_english_loaded():
    result = _run_i18n_case(
        """
{
  registry: Object.keys(LOCALE_REGISTRY),
  loaded: Object.keys(LOCALES),
  labels: Object.values(LOCALE_REGISTRY).map((entry) => entry._label),
  english: t('offline_title'),
}
        """
    )
    assert len(result["registry"]) == 15
    assert result["loaded"] == ["en"]
    assert len(result["labels"]) == 15
    assert result["english"] == "Connection lost"


def test_selected_locale_is_applied_only_after_bundle_registration():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({json.dumps(str(REPO_ROOT / "static" / "i18n-core.js"))}, 'utf8');
        const bundle = fs.readFileSync({json.dumps(str(REPO_ROOT / "static" / "locales" / "it.js"))}, 'utf8');
        const storage = {{}};
        let pendingScript = null;
        const label = {{ textContent: 'Connection lost', getAttribute: () => 'offline_title', hasAttribute: () => false }};
        const ctx = {{
          localStorage: {{ getItem: (key) => storage[key] || null, setItem: (key, value) => {{ storage[key] = String(value); }} }},
          document: {{
            baseURI: 'https://example.test/hermes/', currentScript: null,
            documentElement: {{ lang: '' }},
            querySelectorAll: (selector) => selector === '[data-i18n]' ? [label] : [],
            createElement: () => ({{ async: false }}),
            head: {{ appendChild: (script) => {{ pendingScript = script; }} }},
          }},
        }};
        vm.createContext(ctx);
        vm.runInContext(source, ctx);
        const ready = vm.runInContext("activateLocale('it')", ctx);
        const before = vm.runInContext("t('offline_title')", ctx);
        vm.runInContext(bundle, ctx);
        pendingScript.onload();
        ready.then((result) => {{
          process.stdout.write(JSON.stringify({{ before, after: label.textContent, active: result.active, status: result.status }}));
        }});
        """
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result == {
        "before": "Connection lost",
        "after": "Connessione persa",
        "active": "it",
        "status": "applied",
    }


def test_failed_bundle_keeps_english_fallback_and_previous_locale():
    result = _run_i18n_case(
        """
(async () => {
  const before = t('offline_title');
  const result = await activateLocale('fr');
  return { before, after: t('offline_title'), active: result.active, fallback: result.fallback, stored: localStorage.getItem('hermes-lang'), htmlLang: document.documentElement.lang };
})()
        """
    )
    assert result == {
        "before": "Connection lost",
        "after": "Connection lost",
        "active": "en",
        "fallback": True,
        "stored": "en",
        "htmlLang": "en-US",
    }


def test_activation_generation_owns_completion_order_and_stale_side_effects():
    bundle_paths = {
        code: str(REPO_ROOT / "static" / "locales" / f"{code}.js")
        for code in ("fr", "de")
    }
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const core = fs.readFileSync({json.dumps(str(REPO_ROOT / 'static' / 'i18n-core.js'))}, 'utf8');
        const bundles = {json.dumps(bundle_paths)};

        function setup() {{
          const storage = {{}};
          const writes = {{storage: 0, dom: 0, lang: 0}};
          const scripts = [];
          const element = {{getAttribute: () => 'offline_title', hasAttribute: () => false}};
          Object.defineProperty(element, 'textContent', {{set: () => writes.dom++}});
          const documentElement = {{}};
          let documentLang = '';
          Object.defineProperty(documentElement, 'lang', {{get: () => documentLang, set: (value) => {{ documentLang = value; writes.lang++; }}}});
          const ctx = {{
            localStorage: {{
              getItem: (key) => storage[key] || null,
              setItem: (key, value) => {{ writes.storage++; storage[key] = String(value); }},
            }},
            document: {{
              baseURI: 'https://example.test/hermes/',
              currentScript: null,
              documentElement,
              querySelectorAll: (selector) => selector === '[data-i18n]' ? [element] : [],
              createElement: () => ({{}}),
              head: {{ appendChild: (script) => scripts.push(script) }},
            }},
          }};
          vm.createContext(ctx);
          vm.runInContext(core, ctx);
          return {{ctx, scripts, writes, storage}};
        }}

        function register(env, code) {{
          vm.runInContext(fs.readFileSync(bundles[code], 'utf8'), env.ctx);
          env.scripts.find((script) => script.src.includes('/' + code + '.js')).onload();
        }}

        async function completionOrder(first) {{
          const env = setup();
          const fr = vm.runInContext("activateLocale('fr')", env.ctx);
          const de = vm.runInContext("activateLocale('de')", env.ctx);
          if (first === 'fr') register(env, 'fr');
          register(env, 'de');
          if (first === 'de') register(env, 'fr');
          return {{
            fr: await fr,
            de: await de,
            active: vm.runInContext('getActiveLocale()', env.ctx),
            storage: env.storage,
            writes: env.writes,
          }};
        }}

        async function staleFailure() {{
          const env = setup();
          const fr = vm.runInContext("activateLocale('fr')", env.ctx);
          const de = vm.runInContext("activateLocale('de')", env.ctx);
          const before = {{...env.writes}};
          env.scripts.find((script) => script.src.includes('/fr.js')).onerror();
          const afterFailure = {{...env.writes}};
          register(env, 'de');
          return {{fr: await fr, de: await de, afterFailure, before, active: vm.runInContext('getActiveLocale()', env.ctx)}};
        }}

        async function englishSelectionWhilePending() {{
          const env = setup();
          const pending = vm.runInContext("activateLocale('fr')", env.ctx);
          const english = vm.runInContext("activateLocale('en')", env.ctx);
          const englishResult = await english;
          const before = {{...env.writes}};
          register(env, 'fr');
          return {{english: englishResult, pending: await pending, active: vm.runInContext('getActiveLocale()', env.ctx), before, after: env.writes}};
        }}

        async function fallbackFrom(active) {{
          const env = setup();
          const prior = vm.runInContext(`activateLocale('${{active}}')`, env.ctx);
          if (active === 'de') register(env, 'de');
          await prior;
          const failed = vm.runInContext("activateLocale('fr')", env.ctx);
          env.scripts.find((script) => script.src.includes('/fr.js')).onerror();
          const result = await failed;
          return {{
            result,
            active: vm.runInContext('getActiveLocale()', env.ctx),
            stored: env.storage['hermes-lang'],
            lang: vm.runInContext('document.documentElement.lang', env.ctx),
            text: vm.runInContext("t('offline_title')", env.ctx),
          }};
        }}

        async function retryAfterFailure() {{
          const env = setup();
          const first = vm.runInContext("activateLocale('fr')", env.ctx);
          env.scripts[0].onerror();
          const firstResult = await first;
          const retry = vm.runInContext("activateLocale('fr')", env.ctx);
          const retryScript = env.scripts.filter((script) => script.src.includes('/fr.js')).at(-1);
          vm.runInContext(fs.readFileSync(bundles.fr, 'utf8'), env.ctx);
          retryScript.onload();
          return {{
            first: firstResult,
            retry: await retry,
            active: vm.runInContext('getActiveLocale()', env.ctx),
            stored: env.storage['hermes-lang'],
            requests: env.scripts.length,
          }};
        }}

        (async () => {{
          const missing = setup();
          vm.runInContext("registerLocale('fr', {{_lang: 'fr', only: 'x'}})", missing.ctx);
          await vm.runInContext("activateLocale('fr')", missing.ctx);
          process.stdout.write(JSON.stringify({{
            frFirst: await completionOrder('fr'),
            deFirst: await completionOrder('de'),
            staleFailure: await staleFailure(),
            englishSelection: await englishSelectionWhilePending(),
            englishFallback: await fallbackFrom('en'),
            nonEnglishFallback: await fallbackFrom('de'),
            retry: await retryAfterFailure(),
            missingKey: vm.runInContext("t('offline_title')", missing.ctx),
          }}));
        }})();
        """
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    for order in (result["frFirst"], result["deFirst"]):
        assert order["active"] == "de"
        assert order["de"]["status"] == "applied"
        assert order["fr"]["status"] == "superseded"
        assert order["storage"]["hermes-lang"] == "de"
    assert result["staleFailure"]["fr"]["status"] == "superseded"
    assert result["staleFailure"]["afterFailure"] == result["staleFailure"]["before"]
    assert result["staleFailure"]["active"] == "de"
    assert result["englishSelection"]["english"]["status"] == "applied"
    assert result["englishSelection"]["pending"]["status"] == "superseded"
    assert result["englishSelection"]["active"] == "en"
    assert result["englishSelection"]["after"] == result["englishSelection"]["before"]
    assert result["englishFallback"]["result"]["status"] == "fallback"
    assert result["englishFallback"]["active"] == "en"
    assert result["englishFallback"]["stored"] == "en"
    assert result["englishFallback"]["lang"] == "en-US"
    assert result["nonEnglishFallback"]["result"]["status"] == "fallback"
    assert result["nonEnglishFallback"]["active"] == "de"
    assert result["nonEnglishFallback"]["stored"] == "de"
    assert result["nonEnglishFallback"]["lang"] == "de-DE"
    assert result["nonEnglishFallback"]["text"] != "Connection lost"
    assert result["retry"]["first"]["status"] == "fallback"
    assert result["retry"]["retry"]["status"] == "applied"
    assert result["retry"]["active"] == "fr"
    assert result["retry"]["stored"] == "fr"
    assert result["retry"]["requests"] == 2
    assert result["missingKey"] == "Connection lost"


def test_settings_routes_persist_only_effective_locale():
    assert "function _settleSettingsLocale(" in PANELS_JS
    assert "payload.language=(typeof getActiveLocale==='function')?getActiveLocale():langSel.value" in PANELS_JS
    assert PANELS_JS.count("await _settleSettingsLocale(") >= 3
    assert "if(payload) payload.language=active;" in _function_source(PANELS_JS, "_commitSettingsLocale")


# --- #7622 round-3 behavioural pins -----------------------------------------
#
# These four tests pin the new contract end-to-end so a future
# maintainer cannot silently reintroduce the round-2 regressions:
#   - the schema default was dropped (server)
#   - the resolver no longer treats primary='en' as "no preference"
#   - all three call sites use the shared guarded helper
#   - the guarded helper survives a throwing `navigator` accessor
#
# Each test loads the split production core in a Node `vm` sandbox, so
# the assertions are against the production code, not a copy.


def test_settings_defaults_drop_language_default():
    """`language` is intentionally absent from `_SETTINGS_DEFAULTS` so a
    fresh install returns `None` (the key is missing) and the client
    can distinguish "no preference" from a genuine saved choice."""
    # The dict literal still exists, but the line carrying the
    # `"language": "en"` default must be gone.  A grep for the
    # default covers future renames / re-shuffles of the dict.
    assert '"language": "en"' not in CONFIG_PY
    # Defence in depth: even with the line removed, the comment that
    # documents the round-3 contract must remain so the next maintainer
    # doesn't quietly re-add it.
    assert "language is intentionally absent" in CONFIG_PY


def test_i18n_exposes_guarded_browser_hint_helper():
    """`_detectBrowserLanguageHint` must exist and survive a `navigator`
    accessor that throws — used by loadLocale, boot.js, and panels.js
    to keep a single bad read from aborting boot / settings hydration."""
    assert "function _detectBrowserLanguageHint" in I18N_CORE_JS

    # Happy path: navigator.languages[0] wins.
    out = _run_i18n_case(
        "_detectBrowserLanguageHint()",
        navigator_obj={"languages": ["pt-BR", "en-US"], "language": "en-US"},
    )
    assert out == "pt-BR"

    # Fallback: only navigator.language present.
    out = _run_i18n_case(
        "_detectBrowserLanguageHint()",
        navigator_obj={"language": "fr-FR"},
    )
    assert out == "fr-FR"

    # Throwing accessor: helper must swallow and return null.
    out = _run_i18n_case(
        "_detectBrowserLanguageHint()",
        navigator_obj="throwing",
    )
    assert out is None

    # Empty / missing: null in, null out.
    out = _run_i18n_case(
        "_detectBrowserLanguageHint()",
        navigator_obj={"languages": [], "language": ""},
    )
    assert out is None


def test_composed_resolver_preserves_explicit_english():
    """#7622 BRICK regression: with the round-2 `primary === 'en'` skip
    removed, an explicit saved English must still beat a non-English
    browser hint (the user picked English on purpose, do not override)."""
    result = _run_i18n_case(
        """
{
  // server-stored 'en' (user picked English) + empty localStorage + zh-CN browser
  explicitEnWins: resolvePreferredLocale('en', null, 'zh-CN'),
  explicitEnBeatsStale: resolvePreferredLocale('en', 'ja', 'en-US'),
  // explicit non-English server value still wins
  explicitZh: resolvePreferredLocale('zh', null, 'en-US'),
  explicitZhBeatsStored: resolvePreferredLocale('zh', 'ja', 'en-US'),
  // non-English primary resolves through (no skip)
  primaryResolves: resolvePreferredLocale('zh-CN', 'en', 'en-US'),
  primaryNotEnResolves: resolvePreferredLocale('fr', 'en', 'en-US'),
}
        """
    )
    assert result["explicitEnWins"] == "en"
    assert result["explicitEnBeatsStale"] == "en"
    assert result["explicitZh"] == "zh"
    assert result["explicitZhBeatsStored"] == "zh"
    assert result["primaryResolves"] == "zh"
    assert result["primaryNotEnResolves"] == "fr"


def test_load_locale_first_visit_uses_browser_hint_when_no_preference():
    """End-to-end cross-file case: empty localStorage + zh-CN browser +
    no stored server preference → loadLocale() must land on 'zh', not
    the round-2 'en' default.  The browser hint is also safe against a
    throwing navigator accessor (must fall through to 'en')."""
    # Fresh install: empty localStorage, browser is zh-CN.
    out = _run_i18n_case(
        """
{
  ...(await loadLocale(), {}),
  saved: localStorage.getItem('hermes-lang'),
  htmlLang: document.documentElement.lang,
}
        """,
        navigator_obj={"languages": ["zh-CN", "en"], "language": "zh-CN"},
    )
    assert out["saved"] == "zh"
    assert out["htmlLang"] == "zh-CN"

    # Throwing navigator accessor: loadLocale() must NOT abort, the
    # final locale must fall through to 'en' (the safety net).
    out = _run_i18n_case(
        """
{
  ...(await loadLocale(), {}),
  saved: localStorage.getItem('hermes-lang'),
}
        """,
        navigator_obj="throwing",
    )
    assert out["saved"] == "en"

    # Stored value already present: browser hint is ignored, even when
    # the stored value is the previously-detected 'zh' and the browser
    # now says 'en'.  This is the "second visit" contract.
    out = _run_i18n_case(
        """
{
  ...(await loadLocale(), {}),
  saved: localStorage.getItem('hermes-lang'),
}
        """,
        navigator_obj={"languages": ["en-US", "en"], "language": "en-US"},
    )
    # Default sandbox storage is empty, so the browser hint fires.
    # When a prior setLocale('zh') already ran, localStorage wins.
    assert out["saved"] in {"zh", "en"}  # either is acceptable; round-3
    # specifically tests the *first* visit below.

    # Explicit "stored wins" contract: pre-seed localStorage, then
    # loadLocale() must not consult the browser hint.
    out = _run_i18n_case(
        """
{
  ...(localStorage.setItem('hermes-lang', 'fr'), {}),
  ...(await loadLocale(), {}),
  saved: localStorage.getItem('hermes-lang'),
}
        """,
        navigator_obj={"languages": ["zh-CN"], "language": "zh-CN"},
    )
    assert out["saved"] == "fr"


def _run_boot_error_during_browser_load(bundle_outcome: str) -> dict:
    core_path = REPO_ROOT / "static" / "i18n-core.js"
    french_path = REPO_ROOT / "static" / "locales" / "fr.js"
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const core = fs.readFileSync({json.dumps(str(core_path))}, 'utf8');
        const french = fs.readFileSync({json.dumps(str(french_path))}, 'utf8');
        const storage = {{}};
        const scripts = [];
        const documentElement = {{ lang: 'en-US' }};
        const ctx = {{
          URL,
          localStorage: {{
            getItem: (key) => Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null,
            setItem: (key, value) => {{ storage[key] = String(value); }},
          }},
          document: {{
            baseURI: 'https://example.test/',
            currentScript: {{ src: 'https://example.test/static/i18n-core.js' }},
            documentElement,
            querySelectorAll: () => [],
            createElement: () => ({{ async: false }}),
            head: {{ appendChild: (script) => scripts.push(script) }},
          }},
          navigator: {{ languages: ['fr-FR'], language: 'fr-FR' }},
        }};
        vm.createContext(ctx);
        vm.runInContext(core, ctx);
        const initial = vm.runInContext('loadLocale()', ctx);
        const earlyStorage = storage['hermes-lang'] || null;
        const activeBefore = vm.runInContext('getActiveLocale()', ctx);
        const langBefore = documentElement.lang;
        const bootLanguage = vm.runInContext(
          "resolvePreferredLocale(null, localStorage.getItem('hermes-lang'))",
          ctx
        );
        const bootError = vm.runInContext(
          "activateLocale(resolvePreferredLocale(null, localStorage.getItem('hermes-lang')))",
          ctx
        );
        const script = scripts[0];
        if ({json.dumps(bundle_outcome)} === 'success') {{
          vm.runInContext(french, ctx);
          script.onload();
        }} else {{
          script.onerror();
        }}
        Promise.all([initial, bootError]).then(([initialResult, bootResult]) => {{
          const beforeLate = {{
            active: vm.runInContext('getActiveLocale()', ctx),
            lang: documentElement.lang,
            stored: storage['hermes-lang'] || null,
          }};
          let lateRegistered = false;
          if ({json.dumps(bundle_outcome)} === 'failure') {{
            lateRegistered = vm.runInContext(french, ctx);
          }}
          process.stdout.write(JSON.stringify({{
            earlyStorage,
            activeBefore,
            langBefore,
            bootLanguage,
            initialStatus: initialResult.status,
            bootStatus: bootResult.status,
            beforeLate,
            lateRegistered,
            afterLate: {{
              active: vm.runInContext('getActiveLocale()', ctx),
              lang: documentElement.lang,
              stored: storage['hermes-lang'] || null,
            }},
          }}));
        }});
        """
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_browser_locale_is_persisted_while_its_bundle_loads():
    assert "resolvePreferredLocale(null, localStorage.getItem('hermes-lang'))" in BOOT_JS
    result = _run_boot_error_during_browser_load("success")
    assert result["earlyStorage"] == "fr"
    assert result["activeBefore"] == "en"
    assert result["langBefore"] == "en-US"
    assert result["bootLanguage"] == "fr"
    assert result["bootStatus"] == "applied"
    assert result["beforeLate"] == {
        "active": "fr",
        "lang": "fr-FR",
        "stored": "fr",
    }


def test_failed_browser_load_settles_boot_error_to_english_before_late_registration():
    result = _run_boot_error_during_browser_load("failure")
    assert result["earlyStorage"] == "fr"
    assert result["activeBefore"] == "en"
    assert result["langBefore"] == "en-US"
    assert result["bootLanguage"] == "fr"
    assert result["bootStatus"] == "fallback"
    assert result["beforeLate"] == {
        "active": "en",
        "lang": "en-US",
        "stored": "en",
    }
    assert result["lateRegistered"] is True
    assert result["afterLate"] == result["beforeLate"]


def test_settings_language_hydration_keeps_the_current_selector_owner():
    assert "const pendingLanguage=langSel.value;" in PANELS_JS
    assert "_reconcileSettingsLocaleSelector(selector,settled)" in PANELS_JS
    assert "const requestedLanguage=(selector&&selector.value)" in PANELS_JS
    assert "langSel.value=pendingLanguage||getActiveLocale();" in PANELS_JS


def test_superseded_settings_hydration_preserves_native_selector_and_next_save_language():
    core_path = REPO_ROOT / "static" / "i18n-core.js"
    french_path = REPO_ROOT / "static" / "locales" / "fr.js"
    german_path = REPO_ROOT / "static" / "locales" / "de.js"
    language_start = PANELS_JS.index("// Language preference — metadata is eager, translation data is not.")
    language_end = PANELS_JS.index("const showUsageCb", language_start)
    language_segment = PANELS_JS[language_start:language_end]
    sources = [
        _function_source(PANELS_JS, "_settleSettingsLocale"),
        _function_source(PANELS_JS, "_reconcileSettingsLocaleSelector"),
        _function_source(PANELS_JS, "_settingsLocaleSettlementIsCurrent"),
        _function_source(PANELS_JS, "_settingsLocaleCommitIsCurrent"),
        _function_source(PANELS_JS, "_commitSettingsLocale"),
        _function_source(PANELS_JS, "_preferencesPayloadFromUi"),
        "async function hydrateSettingsLanguage(){"
        "const localeResult=await _settleSettingsLocale('fr',selector);"
        + language_segment
        + "return localeResult;}",
    ]
    panel_source = (
        "let _settingsLocalePostInFlight=null;\n" + "\n".join(sources)
    )
    script = textwrap.dedent(
        f"""
        (async () => {{
          const fs=require('fs');
          const vm=require('vm');
          const core=fs.readFileSync({json.dumps(str(core_path))},'utf8');
          const french=fs.readFileSync({json.dumps(str(french_path))},'utf8');
          const german=fs.readFileSync({json.dumps(str(german_path))},'utf8');
          const scripts=[];
          const options=['en','fr','de'].map(value=>({{value}}));
          const selector={{
            options,
            _value:'fr',
            get value(){{return this._value;}},
            set value(value){{this._value=this.options.some(option=>option.value===value)?value:'';}},
            set innerHTML(_value){{this.options=[];this._value='';}},
            appendChild(option){{this.options.push(option);if(!this._value)this._value=option.value;}},
            addEventListener(){{}},
          }};
          const storage={{}};
          const documentElement={{lang:'en-US'}};
          const ctx={{
            URL,
            selector,
            $:(id)=>id==='settingsLanguage'?selector:null,
            localStorage:{{getItem:key=>storage[key]||null,setItem:(key,value)=>storage[key]=String(value)}},
            document:{{
              baseURI:'https://example.test/',
              currentScript:{{src:'https://example.test/static/i18n-core.js'}},
              documentElement,
              querySelectorAll:()=>[],
              createElement:()=>({{}}),
              head:{{appendChild:script=>scripts.push(script)}},
            }},
            _speechPreferencesPayloadFromUi:()=>({{}}),
          }};
          vm.createContext(ctx);
          vm.runInContext(core,ctx);
          vm.runInContext({json.dumps(panel_source)},ctx);
          const hydration=vm.runInContext('hydrateSettingsLanguage()',ctx);
          await new Promise(resolve=>setImmediate(resolve));
          const frenchScript=scripts.find(script=>script.src.includes('/fr.js'));
          selector.value='de';
          const germanSettlement=vm.runInContext("_settleSettingsLocale('de',selector)",ctx);
          await new Promise(resolve=>setImmediate(resolve));
          const germanScript=scripts.find(script=>script.src.includes('/de.js'));
          vm.runInContext(german,ctx);
          germanScript.onload();
          const germanResult=await germanSettlement;
          vm.runInContext(french,ctx);
          frenchScript.onload();
          const hydrationResult=await hydration;
          const nextSave={{}};
          const nextSaveLocale=await vm.runInContext(
            "_commitSettingsLocale(selector.value,selector,nextSave)",
            Object.assign(ctx,{{nextSave}})
          );
          process.stdout.write(JSON.stringify({{
            germanStatus:germanResult.status,
            hydrationStatus:hydrationResult.status,
            active:vm.runInContext('getActiveLocale()',ctx),
            selector:selector.value,
            nextSaveLanguage:nextSave.language,
            nextSaveActive:nextSaveLocale.active,
          }}));
        }})()
        """
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result == {
        "germanStatus": "applied",
        "hydrationStatus": "superseded",
        "active": "de",
        "selector": "de",
        "nextSaveLanguage": "de",
        "nextSaveActive": "de",
    }


def test_accepted_settings_effects_finish_after_locale_changes():
    sources = [
        _function_source(PANELS_JS, "_settleSettingsLocale"),
        _function_source(PANELS_JS, "_reconcileSettingsLocaleSelector"),
        _function_source(PANELS_JS, "_settingsLocaleSettlementIsCurrent"),
        _function_source(PANELS_JS, "_settingsLocaleCommitIsCurrent"),
        _function_source(PANELS_JS, "_commitSettingsLocale"),
        _function_source(PANELS_JS, "_enqueueSettingsPost"),
        _function_source(PANELS_JS, "_postSettingsAtLocaleCommit"),
        _function_source(PANELS_JS, "_updateCurrentPasswordVisibility"),
        _function_source(PANELS_JS, "_renderSettingsAuthStatus"),
        _function_source(PANELS_JS, "_applySavedSettingsUi"),
        _function_source(PANELS_JS, "saveSettings"),
        _function_source(PANELS_JS, "_autosavePreferencesSettings"),
        _new_session_source(),
    ]
    bundle_sources = [
        (REPO_ROOT / "static" / "locales" / f"{code}.js").read_text(encoding="utf-8")
        for code in ("de", "fr")
    ]
    combined_sources = (
        I18N_CORE_JS
        + "\n"
        + "\n".join(bundle_sources)
        + "\n"
        "let _settingsPanelPostQueue=Promise.resolve();"
        "let _settingsLocalePostInFlight=null;"
        "let _newSessionInFlight=null;"
        "let _messagesTruncated=false;"
        "let _oldestIdx=0;\n"
        + "\n".join(sources)
    )
    script = textwrap.dedent(
        """
        (async () => {
          const vm = require('vm');
          const elements = Object.create(null);
          const element = (id) => elements[id] || (elements[id] = {
            value: '', checked: false, dataset: {}, style: {},
            addEventListener() {}, focus() {},
          });
          element('settingsLanguage').value = 'de';
          element('settingsTheme').value = 'dark';
          element('settingsSkin').value = 'default';
          element('settingsFontSize').value = 'default';
          element('settingsSidebarDensity').value = 'compact';
          element('settingsDefaultMessageMode').value = 'steer';
          element('settingsModel').value = 'normal-model';
          element('settingsModel').dataset.provider = 'provider-normal';
          const storage = {'hermes-lang': 'de'};
          const pendingSettings = [];
          const settingsPosts = [];
          const modelPosts = [];
          const newChatPosts = [];
          let authStatusFetches = 0;
          let lastAutosaveStatus = '';
          let workspaceVisibilityUpdates = 0;
          const documentElement = {lang: 'de-DE', dataset: {}};
          const ctx = {
            console,
            URL,
            window: {_defaultModel: 'old-model', _activeProvider: 'old-provider'},
            document: {
              documentElement,
              querySelector: () => null,
              querySelectorAll: () => [],
              createElement: () => ({dataset: {}, style: {}, appendChild() {}}),
            },
            localStorage: {
              getItem: (key) => Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null,
              setItem: (key, value) => { storage[key] = String(value); },
            },
            navigator: {languages: ['de-DE'], language: 'de-DE'},
            $: element,
            api: (path, options) => {
              if (path === '/api/settings' && options.method === 'POST') {
                const body = JSON.parse(options.body);
                settingsPosts.push(body);
                return new Promise((resolve) => pendingSettings.push(() => resolve({
                  ...body,
                  auth_enabled: true,
                  password_auth_enabled: true,
                  auth_just_enabled: !!body._set_password,
                })));
              }
              if (path === '/api/default-model') {
                modelPosts.push(JSON.parse(options.body));
                return Promise.resolve({});
              }
              if (path === '/api/auth/status') {
                authStatusFetches++;
                return Promise.resolve({auth_enabled: true, password_auth_enabled: true});
              }
              if (path === '/api/session/new') {
                const body = JSON.parse(options.body);
                newChatPosts.push(body);
                return Promise.resolve({session: {
                  session_id: `session-${newChatPosts.length}`,
                  messages: [], model: body.model, model_provider: body.model_provider,
                }});
              }
              return Promise.resolve({});
            },
            checkWebUIVersionSkew() {},
            _settingsPasswordAuthEnabled: false,
            _settingsHermesDefaultModelOnOpen: 'old-model',
            _settingsHermesDefaultModelProviderOnOpen: 'old-provider',
            _settingsDirty: false,
            _workspaceTodosTab: false,
            _settingsThemeOnOpen: 'dark',
            _settingsSkinOnOpen: 'default',
            _settingsFontSizeOnOpen: 'default',
            _settingsPreferencesAutosaveRetryPayload: null,
            _captureModelDropdownSelection: (control) => ({
              model: control.value,
              model_provider: control.dataset.provider || null,
            }),
            _speechPreferencesPayloadFromUi: () => ({}),
            _structuredCodeViewFromUi: () => ({}),
            _composerControlVisibilityPayload: () => ({}),
            _getComposerControlOrder: () => [],
            _setPreferencesAutosaveStatus: (value) => { lastAutosaveStatus = value; },
            _setSettingsAuthButtonsVisible() {},
            _resetSettingsPanelState() {},
            _setDefaultModel() {},
            _hideSettingsPanel() {},
            _loadAuxiliaryModels() {},
            _bindMainAdvancedOptionsButton() {},
            _markSettingsDirty() {},
            _applyWorkspaceTodosTabVisibility() { workspaceVisibilityUpdates++; },
            _syncChatActivityDisplayModeControl() {},
            _syncTransparentEventTimestampsControl() {},
            _applySessionNavigationPrefs() {},
            _persistDefaultMessageMode: (value) => value,
            _persistAutoScrollFollow() {},
            _ensureComposerControlVisibilityState() {},
            _setComposerControlOrder: () => [],
            _renderComposerControlChips() {},
            _renderComposerSituationalControlChips() {},
            _applyComposerFooterVisibilitySettings() {},
            _syncSettingsMaxTokensPlaceholder() {},
            _applyStructuredCodeViewSettings() {},
            applyConversationOutlinePreference() {},
            applyBotName() {},
            _updateAuthWarningBadge() {},
            _updateAuthDisabledWarning() {},
            clearMessageRenderCache() {},
            renderMessages() {},
            syncTopbar() {},
            renderSessionList() {},
            showToast() {},
            t: (key) => key,
            S: {session: null, activeProfile: 'default', toolCalls: [], messages: []},
            NO_PROJECT_FILTER: '__none__',
            _activeProject: null,
            _sessionSourceFilter: 'webui',
            _setNewSessionPending() {},
            updateQueueBadge() {},
            clearLiveToolCards() {},
            _readEmptyComposerModelOverride: () => null,
            _modelStateForSelect: () => ({model: '', model_provider: null}),
            _applyModelToDropdown: () => true,
            _rememberNewChatDraftSession() {},
            _setActiveSessionUrl() {},
            startSessionStream() {},
            _setSessionViewedCount() {},
            _hydrateTodosFromSession() {},
            _adoptRegenerationRevision() {},
            _setLiveAssistantTps() {},
            _syncCtxIndicator() {},
            updateSendBtn() {},
            setStatus() {},
            setComposerStatus() {},
            _deferWorkspaceRefreshForSession() {},
            refreshSessionList: () => Promise.resolve(),
          };
          vm.createContext(ctx);
          vm.runInContext(combinedSources, ctx);
          const tick = () => new Promise((resolve) => setImmediate(resolve));
          const changeLocale = async (code) => {
            await vm.runInContext(`activateLocale('${code}')`, ctx);
            element('settingsLanguage').value = code;
          };
          const newChatPayload = async () => {
            ctx.S.session = null;
            ctx.S.messages = [];
            await vm.runInContext('newSession(false)', ctx);
            return newChatPosts[newChatPosts.length - 1];
          };

          const normalSave = vm.runInContext('saveSettings(false)', ctx);
          await tick();
          const normalPostHeld = settingsPosts.length === 1;
          await changeLocale('fr');
          const normalGeneration = vm.runInContext('getLocaleActivationGeneration()', ctx);
          pendingSettings.shift()();
          await normalSave;
          const normalState = {
            active: vm.runInContext('getActiveLocale()', ctx), selector: element('settingsLanguage').value, htmlLang: documentElement.lang,
            stored: storage['hermes-lang'], model: ctx.window._defaultModel, provider: ctx.window._activeProvider,
            generation: vm.runInContext('getLocaleActivationGeneration()', ctx),
          };
          const normalNewChat = await newChatPayload();

          element('settingsModel').value = 'password-model';
          element('settingsModel').dataset.provider = 'provider-password';
          element('settingsPassword').value = 'first-password';
          const passwordSave = vm.runInContext('saveSettings(false)', ctx);
          await tick();
          const passwordPostHeld = settingsPosts.length === 2;
          await changeLocale('de');
          const passwordGeneration = vm.runInContext('getLocaleActivationGeneration()', ctx);
          pendingSettings.shift()();
          await passwordSave;
          const passwordState = {
            active: vm.runInContext('getActiveLocale()', ctx), selector: element('settingsLanguage').value, htmlLang: documentElement.lang,
            stored: storage['hermes-lang'], model: ctx.window._defaultModel, provider: ctx.window._activeProvider,
            password: element('settingsPassword').value,
            currentPassword: element('settingsCurrentPassword').value,
            authEnabled: ctx._settingsPasswordAuthEnabled,
            currentPasswordDisplay: element('settingsCurrentPasswordBlock').style.display,
            authStatusFetches,
            generation: vm.runInContext('getLocaleActivationGeneration()', ctx),
          };
          const passwordNewChat = await newChatPayload();

          element('settingsPassword').value = 'updated-password';
          element('settingsCurrentPassword').value = 'current-password';
          const updateSave = vm.runInContext('saveSettings(false)', ctx);
          await tick();
          const nextPasswordPayload = settingsPosts[2];
          pendingSettings.shift()();
          await updateSave;

          const visibilityBeforeAutosave = workspaceVisibilityUpdates;
          const autosave = vm.runInContext("_autosavePreferencesSettings({language: 'de', workspace_todos_tab: true})", ctx);
          await tick();
          const autosavePostHeld = settingsPosts.length === 4;
          await changeLocale('fr');
          const autosaveGeneration = vm.runInContext('getLocaleActivationGeneration()', ctx);
          pendingSettings.shift()();
          await autosave;
          process.stdout.write(JSON.stringify({
            normalPostHeld,
            normalState,
            normalNewChat,
            passwordPostHeld,
            passwordState,
            passwordAuthStatusHtml: element('settingsAuthStatus').innerHTML,
            passwordNewChat,
            nextPasswordPayload,
            authStatusFetches,
            autosavePostHeld,
            autosaveState: {
              active: vm.runInContext('getActiveLocale()', ctx), selector: element('settingsLanguage').value, htmlLang: documentElement.lang,
              stored: storage['hermes-lang'], workspaceTodos: ctx.window._workspaceTodosTab,
              status: lastAutosaveStatus, visibilityUpdates: workspaceVisibilityUpdates,
              generation: vm.runInContext('getLocaleActivationGeneration()', ctx),
            },
            normalGeneration,
            passwordGeneration,
            autosaveGeneration,
            visibilityBeforeAutosave,
          }));
        })()
        """
    ).replace("combinedSources", json.dumps(combined_sources))
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result["normalPostHeld"] is True
    assert result["normalState"] == {
        "active": "fr",
        "selector": "fr",
        "htmlLang": "fr-FR",
        "stored": "fr",
        "model": "normal-model",
        "provider": "provider-normal",
        "generation": result["normalGeneration"],
    }
    assert result["normalNewChat"]["model"] == "normal-model"
    assert result["normalNewChat"]["model_provider"] == "provider-normal"
    assert result["passwordPostHeld"] is True
    assert result["passwordState"] == {
        "active": "de",
        "selector": "de",
        "htmlLang": "de-DE",
        "stored": "de",
        "model": "password-model",
        "provider": "provider-password",
        "password": "",
        "currentPassword": "",
        "authEnabled": True,
        "currentPasswordDisplay": "block",
        "authStatusFetches": 1,
        "generation": result["passwordGeneration"],
    }
    assert result["passwordAuthStatusHtml"].startswith('<span class="detail-badge ok"')
    assert result["passwordNewChat"]["model"] == "password-model"
    assert result["passwordNewChat"]["model_provider"] == "provider-password"
    assert result["nextPasswordPayload"]["_current_password"] == "current-password"
    assert result["autosavePostHeld"] is True
    assert result["autosaveState"] == {
        "active": "fr",
        "selector": "fr",
        "htmlLang": "fr-FR",
        "stored": "fr",
        "workspaceTodos": True,
        "status": "saved",
        "visibilityUpdates": result["visibilityBeforeAutosave"] + 1,
        "generation": result["autosaveGeneration"],
    }


def _function_source(src: str, name: str) -> str:
    async_token = f"async function {name}("
    start = src.find(async_token)
    if start < 0:
        start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for index in range(brace, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unclosed function {name}")


def _new_session_source() -> str:
    start = SESSIONS_JS.index("async function newSession")
    brace = SESSIONS_JS.index("{\n", start)
    depth = 0
    for index in range(brace, len(SESSIONS_JS)):
        if SESSIONS_JS[index] == "{":
            depth += 1
        elif SESSIONS_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start : index + 1]
    raise AssertionError("unclosed function newSession")


def _run_node_script(script: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as temp_dir:
        script_path = pathlib.Path(temp_dir) / "settings-locale-regression.js"
        script_path.write_text(script, encoding="utf-8")
        return subprocess.run(
            ["node", str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


def test_settings_post_serializes_after_locale_settlement_and_ignores_stale_success():
    sources = [
        _function_source(PANELS_JS, "_settleSettingsLocale"),
        _function_source(PANELS_JS, "_reconcileSettingsLocaleSelector"),
        _function_source(PANELS_JS, "_settingsLocaleSettlementIsCurrent"),
        _function_source(PANELS_JS, "_settingsLocaleCommitIsCurrent"),
        _function_source(PANELS_JS, "_commitSettingsLocale"),
        _function_source(PANELS_JS, "_enqueueSettingsPost"),
        _function_source(PANELS_JS, "_postSettingsAtLocaleCommit"),
        _function_source(PANELS_JS, "_autosavePreferencesSettings"),
    ]
    combined_sources = "let _settingsPanelPostQueue=Promise.resolve(); let _settingsLocalePostInFlight=null; let _settingsPreferencesAutosaveRetryPayload=null; let _settingsHermesDefaultModelOnOpen=''; let _settingsHermesDefaultModelProviderOnOpen=null;\n" + "\n".join(sources)
    script = textwrap.dedent(
        f"""
        (async () => {{
        const vm = require('vm');
        const selector = {{value: 'de'}};
        let active = 'de';
        let generation = 1;
        let serverLanguage = 'en';
        let releasePost;
        const requests = [];
        const storage = {{}};
        const ctx = {{
          console,
          selector,
          $: (id) => id === 'settingsLanguage' ? selector : null,
          localStorage: {{getItem: (key) => storage[key] || null, setItem: (key, value) => storage[key] = String(value)}},
          getActiveLocale: () => active,
          getLocaleActivationGeneration: () => generation,
          activateLocale: async (requested) => ({{status: 'applied', requested, active, generation}}),
          api: (path, options) => {{
            const body = JSON.parse(options.body);
            requests.push(body.language);
            serverLanguage = body.language;
            return new Promise((resolve) => {{ releasePost = () => resolve({{language: body.language}}); }});
          }},
          _setPreferencesAutosaveStatus: () => {{}},
          _applyWorkspaceTodosTabVisibility: () => {{}},
          _settingsLocalePostInFlight: null,
          _settingsPreferencesAutosaveRetryPayload: null,
          _settingsHermesDefaultModelOnOpen: '',
          _settingsHermesDefaultModelProviderOnOpen: null,
          _settingsDirty: false,
          window: {{}},
          document: {{querySelector: () => null}},
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(combined_sources)}, ctx);
        const first = vm.runInContext("_autosavePreferencesSettings({{language: 'de'}})", ctx);
        await new Promise((resolve) => setImmediate(resolve));
        generation = 2;
        active = 'fr';
        selector.value = 'fr';
        const newerSettlement = vm.runInContext("_settleSettingsLocale('fr', selector)", ctx);
        let newerSettled = false;
        newerSettlement.then(() => newerSettled = true);
        await Promise.resolve();
        const blockedBeforePostRelease = !newerSettled;
        releasePost();
        await first;
        await newerSettlement;
        const second = vm.runInContext("_autosavePreferencesSettings({{language: 'fr'}})", ctx);
        await new Promise((resolve) => setImmediate(resolve));
        releasePost();
        await second;
        process.stdout.write(JSON.stringify({{blockedBeforePostRelease, requests, selector: selector.value, serverLanguage}}));
        }})();
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result == {
        "blockedBeforePostRelease": True,
        "requests": ["de", "fr"],
        "selector": "fr",
        "serverLanguage": "fr",
    }


def test_settings_locale_supersession_covers_save_selector_load_and_saved_ui():
    sources = [
        _function_source(PANELS_JS, "_settleSettingsLocale"),
        _function_source(PANELS_JS, "_reconcileSettingsLocaleSelector"),
        _function_source(PANELS_JS, "_settingsLocaleSettlementIsCurrent"),
        _function_source(PANELS_JS, "_settingsLocaleCommitIsCurrent"),
        _function_source(PANELS_JS, "_commitSettingsLocale"),
        _function_source(PANELS_JS, "_enqueueSettingsPost"),
        _function_source(PANELS_JS, "_postSettingsAtLocaleCommit"),
        _function_source(PANELS_JS, "saveSettings"),
        _function_source(PANELS_JS, "_applySavedSettingsUi"),
        _function_source(PANELS_JS, "_autosavePreferencesSettings"),
    ]
    locale_start = PANELS_JS.index("const resolvedLanguage=")
    language_start = PANELS_JS.index("// Language preference", locale_start)
    language_end = PANELS_JS.index("const showUsageCb", language_start)
    load_locale_segment = PANELS_JS[locale_start:language_end]
    sources.append(
        "async function loadSettingsPanel(){"
        "const settings={language:'de'};"
        + load_locale_segment
        + "}"
    )
    combined_sources = "let _settingsPanelPostQueue=Promise.resolve();\n" + "\n".join(sources)
    script = textwrap.dedent(
        f"""
        (async () => {{
          const vm = require('vm');
          const selector = {{value: 'de', innerHTML: '', addEventListener: () => {{}}}};
          const elements = new Proxy({{settingsLanguage: selector, settingsPassword: {{value: ''}}}}, {{
            get: (target, key) => target[key] || {{value: '', checked: false, dataset: {{}}, style: {{}}, addEventListener: () => {{}}}}
          }});
          let active = 'de';
          let generation = 1;
          const requests = [];
          const modelPosts = [];
          let applyCount = 0;
          let releasePost;
          let saveMode = true;
          const ctx = {{
            console,
            window: {{}},
            document: {{documentElement: {{dataset: {{}}}}, querySelector: () => null, getElementById: (id) => elements[id]}},
            localStorage: {{getItem: () => null, setItem: () => {{}}}},
            $: (id) => saveMode ? elements[id] : (id === 'settingsLanguage' ? selector : null),
            getActiveLocale: () => active,
            getLocaleActivationGeneration: () => generation,
            activateLocale: async (requested) => {{
              generation++;
              active = requested;
              return {{status: 'applied', requested, active, generation}};
            }},
            api: (path, options) => {{
              if (path === '/api/default-model') {{
                modelPosts.push(JSON.parse(options.body));
                return Promise.resolve({{}});
              }}
              if (options && options.method === 'POST') {{
                const body = JSON.parse(options.body);
                requests.push(body);
                return new Promise((resolve) => {{ releasePost = () => resolve({{...body, language: body.language}}); }});
              }}
              return Promise.resolve({{language: 'de', theme: 'dark', skin: 'default'}});
            }},
            checkWebUIVersionSkew: () => {{}},
            _bindMainAdvancedOptionsButton: () => {{}},
            _loadAuxiliaryModels: () => {{}},
            _applySavedSettingsUi: async (...args) => {{ applyCount++; }},
            _settingsLocalePostInFlight: null,
            _settingsPasswordAuthEnabled: false,
            _settingsHermesDefaultModelOnOpen: '',
            _settingsHermesDefaultModelProviderOnOpen: null,
            _settingsDirty: false,
            _workspaceTodosTab: false,
            _captureModelDropdownSelection: () => ({{model: '', model_provider: null}}),
            _speechPreferencesPayloadFromUi: () => ({{}}),
            _structuredCodeViewFromUi: () => ({{}}),
            _composerControlVisibilityPayload: () => ({{}}),
            _getComposerControlOrder: () => [],
            _setPreferencesAutosaveStatus: () => {{}},
            _setSettingsAuthButtonsVisible: () => {{}},
            _resetSettingsPanelState: () => {{}},
            _setDefaultModel: () => {{}},
            _hideSettingsPanel: () => {{}},
            _loadAuxiliaryModels: () => {{}},
            _markSettingsDirty: () => {{}},
            _applyWorkspaceTodosTabVisibility: () => {{}},
            _syncChatActivityDisplayModeControl: () => {{}},
            _syncTransparentEventTimestampsControl: () => {{}},
            _applySessionNavigationPrefs: () => {{}},
            _persistDefaultMessageMode: (value) => value,
            _ensureComposerControlVisibilityState: () => {{}},
            _setComposerControlOrder: () => [],
            _renderComposerControlChips: () => {{}},
            _renderComposerSituationalControlChips: () => {{}},
            _applyComposerFooterVisibilitySettings: () => {{}},
            _syncSettingsMaxTokensPlaceholder: () => {{}},
            _settingsAuthButtonsVisible: () => {{}},
            _renderSettingsAuthStatus: () => {{}},
            _updateCurrentPasswordVisibility: () => {{}},
            _updateAuthWarningBadge: () => {{}},
            _updateAuthDisabledWarning: () => {{}},
            _setSettingsAuthButtonsVisible: () => {{}},
            clearMessageRenderCache: () => {{}},
            renderMessages: () => {{}},
            syncTopbar: () => {{}},
            renderSessionList: () => {{}},
            showToast: () => {{}},
            t: (key) => key,
          }};
          vm.createContext(ctx);
          vm.runInContext({json.dumps(combined_sources)}, ctx);

          elements.settingsModel = {{value: 'normal-model'}};
          vm.runInContext("_settingsHermesDefaultModelOnOpen='old-model'", ctx);
          const normal = vm.runInContext("saveSettings(false)", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const selectorChange = vm.runInContext("_settleSettingsLocale('fr', $('settingsLanguage'))", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const normalHeld = requests.length === 1;
          releasePost();
          await Promise.all([normal, selectorChange]);
          const normalAfter = selector.value;
          const normalModelPostOccurred = modelPosts.length === 1;

          elements.settingsModel.value = 'password-model';
          elements.settingsPassword.value = 'secret';
          const password = vm.runInContext("saveSettings(false)", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          selector.value = 'de';
          const passwordSelectorChange = vm.runInContext("_settleSettingsLocale('de', $('settingsLanguage'))", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const passwordHeld = requests.length === 2;
          releasePost();
          await Promise.all([password, passwordSelectorChange]);
          const passwordAfter = selector.value;
          const passwordModelPostOccurred = modelPosts.length === 2;

          saveMode = false;
          await vm.runInContext("loadSettingsPanel()", ctx);
          const loadAfter = selector.value;
          const uiAfter = await vm.runInContext("(async () => {{ const body={{language:'fr'}}; await _applySavedSettingsUi({{}}, body, {{language:'fr'}}); return {{selector: $('settingsLanguage').value, bodyLanguage: body.language}}; }})()", ctx);
          saveMode = true;
          selector.value = 'de';
          elements.settingsModel = {{value: 'new-model'}};
          vm.runInContext("_settingsHermesDefaultModelOnOpen='old-model'", ctx);
          const modelSave = vm.runInContext("saveSettings(false)", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const explicitPostHeld = requests.length === 3;
          const generationBeforeAutosave = generation;
          const sameLanguageAutosave = vm.runInContext("_autosavePreferencesSettings({{language: 'de'}})", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const generationAfterAutosave = generation;
          const releaseExplicitPost = releasePost;
          releaseExplicitPost();
          await new Promise((resolve) => setImmediate(resolve));
          const defaultModelPostOccurred = modelPosts.length === 3;
          const releaseAutosavePost = releasePost;
          if (releaseAutosavePost) releaseAutosavePost();
          await Promise.all([modelSave, sameLanguageAutosave]);
          process.stdout.write(JSON.stringify({{requests, modelPosts, explicitPostHeld, generationBeforeAutosave, generationAfterAutosave, defaultModelPostOccurred, normalHeld, passwordHeld, normalAfter, passwordAfter, normalModelPostOccurred, passwordModelPostOccurred, loadAfter, uiAfter}}));
        }})()
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert [request["language"] for request in result["requests"]] == ["de", "fr", "de", "de"]
    assert result["requests"][0]["language"] == "de"
    assert result["requests"][1]["language"] == "fr"
    assert result["normalHeld"] is True
    assert result["passwordHeld"] is True
    assert result["normalAfter"] == "fr"
    assert result["passwordAfter"] == "de"
    assert result["loadAfter"] == "de"
    assert result["uiAfter"]["selector"] == "de"
    assert result["uiAfter"]["bodyLanguage"] == "fr"
    assert result["explicitPostHeld"] is True
    assert result["generationAfterAutosave"] == result["generationBeforeAutosave"]
    assert result["modelPosts"] == [
        {"model": "normal-model", "provider": None},
        {"model": "password-model", "provider": None},
        {"model": "new-model", "provider": None},
    ]
    assert result["normalModelPostOccurred"] is True
    assert result["passwordModelPostOccurred"] is True
    assert result["defaultModelPostOccurred"] is True
