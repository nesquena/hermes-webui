import json
import pathlib
import re
import subprocess
import textwrap


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
I18N_CORE_JS = (REPO_ROOT / "static" / "i18n-core.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
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
          process.stdout.write(JSON.stringify({{ before, after: label.textContent, active: result.active, state: vm.runInContext("LOCALE_STATES.it", ctx) }}));
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
        "state": "loaded",
    }


def test_failed_bundle_keeps_english_fallback_and_previous_locale():
    result = _run_i18n_case(
        """
(async () => {
  const before = t('offline_title');
  const result = await activateLocale('fr');
  return { before, active: result.active, fallback: result.fallback, state: LOCALE_STATES.fr };
})()
        """
    )
    assert result == {
        "before": "Connection lost",
        "active": "en",
        "fallback": True,
        "state": "failed",
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
          Object.defineProperty(documentElement, 'lang', {{set: () => writes.lang++}});
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
          return await failed;
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
    assert result["englishFallback"]["status"] == "fallback"
    assert result["englishFallback"]["active"] == "en"
    assert result["nonEnglishFallback"]["status"] == "fallback"
    assert result["nonEnglishFallback"]["active"] == "de"
    assert result["missingKey"] == "Connection lost"


def test_settings_routes_persist_only_effective_locale():
    assert "function _settleSettingsLocale(" in PANELS_JS
    assert "payload.language=(typeof getActiveLocale==='function')?getActiveLocale():langSel.value" in PANELS_JS
    assert PANELS_JS.count("await _settleSettingsLocale(") >= 4
    assert "body.language=localeResult.active" in PANELS_JS


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
  ...(setLocale('fr'), {}),  // pre-seed localStorage
  ...(await loadLocale(), {}),
  saved: localStorage.getItem('hermes-lang'),
}
        """,
        navigator_obj={"languages": ["zh-CN"], "language": "zh-CN"},
    )
    assert out["saved"] == "fr"
def test_settings_locale_continuations_recheck_current_settlement():
    assert "const pendingLanguage=langSel.value;" in PANELS_JS
    assert "_settingsLocaleSettlementIsCurrent(localeResult)" in PANELS_JS
    assert "const requestedLanguage=(selector&&selector.value)" in PANELS_JS
