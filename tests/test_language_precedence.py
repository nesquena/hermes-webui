import json
import pathlib
import re
import subprocess
import textwrap


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
I18N_CORE_JS = (REPO_ROOT / "static" / "i18n-core.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _run_i18n_case(script_expr: str, bundles: tuple[str, ...] = ()) -> dict:
    wrapped_expr = f"(() => ({script_expr}))()"
    sources = [REPO_ROOT / "static" / "i18n-core.js"] + [
        REPO_ROOT / "static" / "locales" / f"{bundle}.js" for bundle in bundles
    ]
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const sources = {json.dumps([str(source) for source in sources])};
        const storage = {{}};
        const ctx = {{
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{
            baseURI: 'https://example.test/',
            documentElement: {{ lang: '' }},
            querySelectorAll: () => [],
            createElement: () => ({{}}),
            head: {{ appendChild: (script) => script.onerror() }},
          }},
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


def test_settings_locale_continuations_recheck_current_settlement():
    assert "const pendingLanguage=langSel.value;" in PANELS_JS
    assert "_settingsLocaleSettlementIsCurrent(localeResult)" in PANELS_JS
    assert "_reconcileSettingsLocaleSelector(selector,settled)" in PANELS_JS
    assert "const requestedLanguage=(selector&&selector.value)" in PANELS_JS


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
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result == {
        "blockedBeforePostRelease": True,
        "requests": ["de", "fr"],
        "selector": "fr",
        "serverLanguage": "fr",
    }


def test_settings_save_paths_use_the_same_locale_commit_boundary():
    assert PANELS_JS.count("_postSettingsAtLocaleCommit(") >= 3
    assert PANELS_JS.count("_settingsLocaleCommitIsCurrent(settingsLocaleGeneration)") >= 3
    assert "const localeSettlementCurrent=_settingsLocaleSettlementIsCurrent(localeResult);" in PANELS_JS


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
              if (requested !== active) {{ generation++; active = requested; }}
              return {{status: 'applied', requested, active, generation}};
            }},
            api: (path, options) => {{
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

          const normal = vm.runInContext("saveSettings(false)", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const selectorChange = vm.runInContext("_settleSettingsLocale('fr', $('settingsLanguage'))", ctx);
          await new Promise((resolve) => setImmediate(resolve));
          const normalHeld = requests.length === 1;
          releasePost();
          await Promise.all([normal, selectorChange]);
          const normalAfter = selector.value;

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

          saveMode = false;
          await vm.runInContext("loadSettingsPanel()", ctx);
          const loadAfter = selector.value;
          const uiAfter = await vm.runInContext("(async () => {{ const body={{language:'fr'}}; await _applySavedSettingsUi({{}}, body, {{language:'fr'}}); return {{selector: $('settingsLanguage').value, bodyLanguage: body.language}}; }})()", ctx);
          process.stdout.write(JSON.stringify({{requests, normalHeld, passwordHeld, normalAfter, passwordAfter, loadAfter, uiAfter}}));
        }})()
        """
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert [request["language"] for request in result["requests"]] == ["de", "fr"]
    assert result["requests"][0]["language"] == "de"
    assert result["requests"][1]["language"] == "fr"
    assert result["normalHeld"] is True
    assert result["passwordHeld"] is True
    assert result["normalAfter"] == "fr"
    assert result["passwordAfter"] == "de"
    assert result["loadAfter"] == "de"
    assert result["uiAfter"]["selector"] == "de"
    assert result["uiAfter"]["bodyLanguage"] == "de"
