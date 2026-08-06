"""Regression guards for fast profile dropdown opening.

The user-visible failure was: clicking the composer/titlebar profile chip waited
for a cold /api/profiles request before showing the menu. On machines where the
profile metadata scan is slow, that made the click feel frozen for seconds.
"""
import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _function_body(src: str, marker: str, next_marker: str | None = None) -> str:
    start = src.index(marker)
    if next_marker is not None:
        end = src.index(next_marker, start)
        return src[start:end]
    depth = 0
    opened = False
    for idx, ch in enumerate(src[start:], start):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Could not extract function body for {marker}")


def test_profile_dropdown_opens_shell_before_network_fetch():
    body = _function_body(PANELS_JS, "function toggleProfileDropdown(e) {")
    cache_idx = body.index("const cached = _profileDropdownBestCachedData();")
    open_idx = body.index("_openProfileDropdownShell();")
    fetch_idx = body.index("_profileDropdownFetchFresh().then")
    assert cache_idx < open_idx < fetch_idx, (
        "toggleProfileDropdown must render/open from cache or loading shell before "
        "awaiting the slow /api/profiles refresh"
    )


def test_profile_dropdown_uses_shared_fetch_promise_and_validated_local_storage_cache():
    data_cache_body = _function_body(PANELS_JS, "function _profileDropdownDataCacheUsable(data){")
    assert "Array.isArray(data.profiles)" in data_cache_body
    assert "data.profiles.every(" in data_cache_body
    assert "typeof p.name==='string'" in data_cache_body
    # #5412 gate: the predicate must also reject rows whose renderer-read `model`
    # field is a non-string truthy value (poison {name,model:{}} bricked the dropdown).
    assert "typeof p.model==='string'" in data_cache_body

    cache_body = _function_body(PANELS_JS, "function _profileDropdownCacheUsable(data){")
    assert "_profileDropdownDataCacheUsable(data)" in cache_body
    assert "data.single_profile_mode !== true" in cache_body

    fetch_body = _function_body(PANELS_JS, "function _profileDropdownFetchFresh(){")
    assert "if(_profileDropdownFetchPromise) return _profileDropdownFetchPromise;" in fetch_body
    assert "api('/api/profiles', {timeoutToast:false})" in fetch_body
    assert "if(_profileDropdownDataCacheUsable(data)) _profilesCache = data;" in fetch_body
    assert "_profileDropdownWriteStoredCache(data);" in fetch_body
    assert "\n    _profilesCache = data;\n" not in fetch_body

    best_body = _function_body(PANELS_JS, "function _profileDropdownBestCachedData(){")
    assert "if(_profileDropdownDataCacheUsable(_profilesCache)) return null;" in best_body
    assert best_body.index("if(_profileDropdownDataCacheUsable(_profilesCache)) return null;") < best_body.index("_profilesCache = null;")

    read_body = _function_body(PANELS_JS, "function _profileDropdownReadStoredCache(){")
    assert "localStorage.getItem(PROFILE_DROPDOWN_CACHE_KEY)" in read_body
    assert "PROFILE_DROPDOWN_CACHE_TTL_MS" in read_body
    assert "_profileDropdownClearStoredCache();" in read_body

    write_body = _function_body(PANELS_JS, "function _profileDropdownWriteStoredCache(data){")
    assert "if(!_profileDropdownCacheUsable(data)) { _profileDropdownClearStoredCache(); return; }" in write_body


def test_profile_dropdown_closing_invalidates_inflight_refresh():
    close_body = _function_body(PANELS_JS, "function closeProfileDropdown() {")
    assert "_profileDropdownOpenGeneration++;" in close_body

    toggle_body = _function_body(PANELS_JS, "function toggleProfileDropdown(e) {")
    assert "const openGen = ++_profileDropdownOpenGeneration;" in toggle_body
    assert "if(openGen !== _profileDropdownOpenGeneration) return;" in toggle_body


def test_profiles_panel_refresh_updates_dropdown_cache():
    body = _function_body(PANELS_JS, "async function loadProfilesPanel() {")
    api_idx = body.index("const data = await api('/api/profiles');")
    cache_idx = body.index("_profileDropdownWriteStoredCache(data);")
    render_idx = body.index("panel.innerHTML = '';")
    assert api_idx < cache_idx < render_idx


def test_profile_dropdown_prefetches_after_page_load():
    assert "function _warmProfileDropdownCache(){" in PANELS_JS
    assert "window.addEventListener('load'" in PANELS_JS
    assert "_warmProfileDropdownCache();" in PANELS_JS


def test_poisoned_profile_cache_opens_then_switches_after_fresh_refresh():
    snippets = [
        PANELS_JS[
            PANELS_JS.index("let _profilesCache = null;") : PANELS_JS.index("async function _profileSwitchPanelLoad(){")
        ],
        _function_body(PANELS_JS, "function renderProfileDropdown(data) {"),
        _function_body(PANELS_JS, "function toggleProfileDropdown(e) {"),
        _function_body(PANELS_JS, "function closeProfileDropdown() {"),
    ]
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const snippets = {json.dumps(snippets)};

        class ClassList {{
          constructor() {{ this.values = new Set(); }}
          add(name) {{ this.values.add(name); }}
          remove(name) {{ this.values.delete(name); }}
          contains(name) {{ return this.values.has(name); }}
        }}
        class Element {{
          constructor(tag, id) {{
            this.tagName = tag;
            this.id = id || '';
            this.children = [];
            this.className = '';
            this.classList = new ClassList();
            this.dataset = {{}};
            this.style = {{}};
            this.onclick = null;
            this.textContent = '';
            this._innerHTML = '';
          }}
          set innerHTML(value) {{
            this._innerHTML = String(value || '');
            if (this._innerHTML === '') this.children = [];
          }}
          get innerHTML() {{ return this._innerHTML; }}
          appendChild(child) {{ this.children.push(child); return child; }}
        }}
        const elements = new Map();
        for (const id of ['profileDropdown', 'profileChip', 'titlebarProfileBtn', 'titlebarProfileLabel']) {{
          elements.set(id, new Element('div', id));
        }}
        globalThis.document = {{
          createElement: (tag) => new Element(tag),
          getElementById: (id) => elements.get(id) || null,
          addEventListener: () => {{}},
        }};
        globalThis.window = {{ addEventListener: () => {{}} }};
        const store = new Map();
        globalThis.localStorage = {{
          getItem: (key) => store.has(key) ? store.get(key) : null,
          setItem: (key, value) => store.set(key, String(value)),
          removeItem: (key) => store.delete(key),
        }};
        globalThis.$ = (id) => elements.get(id) || null;
        globalThis.S = {{ activeProfile: 'default' }};
        globalThis.t = (key, n) => key === 'profile_skill_count' ? `${{n}} skills` : key;
        globalThis.esc = (value) => String(value == null ? '' : value)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        globalThis.li = () => '';
        globalThis.closeWsDropdown = () => {{}};
        globalThis.closeModelDropdown = () => {{}};
        globalThis._positionProfileDropdown = () => {{}};
        globalThis.showToast = () => {{}};
        let switchedTo = null;
        globalThis.switchToProfile = async (name) => {{ switchedTo = name; S.activeProfile = name; }};
        const multiProfileResponse = {{
          active: 'default',
          single_profile_mode: false,
          profiles: [
            {{ name: 'default', visible: true, is_default: true }},
            {{ name: 'other', visible: true }},
          ],
        }};
        let apiResponse = multiProfileResponse;
        globalThis.api = () => Promise.resolve(apiResponse);

        eval(snippets.join(String.fromCharCode(10)) + String.fromCharCode(10) + `;globalThis.__profileTest={{
          key: PROFILE_DROPDOWN_CACHE_KEY,
          reset() {{
            _profilesCache = null;
            _profileDropdownFetchPromise = null;
            _profileDropdownCacheLoadedFromStorage = false;
            _profileDropdownOpenGeneration = 0;
            switchedTo = null;
            S.activeProfile = 'default';
            const dd = document.getElementById('profileDropdown');
            dd.children = [];
            dd.innerHTML = '';
            dd.classList.remove('open');
          }},
          usable: _profileDropdownCacheUsable,
          dataUsable: _profileDropdownDataCacheUsable,
          setApiResponse(data) {{ apiResponse = data; }},
          setCache(data) {{ _profilesCache = data; }},
          cache: () => _profilesCache,
          profileDetailBacked(name) {{
            return !!(_profilesCache && _profilesCache.profiles && _profilesCache.profiles.find(x => x.name === name));
          }},
          toggle: toggleProfileDropdown,
          close: closeProfileDropdown,
          switchedTo: () => switchedTo,
        }};`);

        async function runPoisonedCase(profiles) {{
          __profileTest.reset();
          __profileTest.setApiResponse(multiProfileResponse);
          localStorage.setItem(__profileTest.key, JSON.stringify({{ ts: Date.now(), data: {{ active: 'default', profiles }} }}));
          assert.strictEqual(__profileTest.usable({{ active: 'default', profiles }}), false);
          assert.doesNotThrow(() => __profileTest.toggle({{ currentTarget: document.getElementById('profileChip') }}));
          const dd = document.getElementById('profileDropdown');
          assert.strictEqual(dd.classList.contains('open'), true, 'dropdown shell should open from loading state');
          assert.strictEqual(localStorage.getItem(__profileTest.key), null, 'invalid stored cache should be removed before refresh');
          await new Promise((resolve) => setImmediate(resolve));
          const profileOptions = dd.children.filter((child) => String(child.className).includes('profile-opt'));
          const other = profileOptions.find((child) => child.innerHTML.includes('other'));
          assert(other, 'fresh refresh should render the valid profile option');
          await other.onclick();
          assert.strictEqual(__profileTest.switchedTo(), 'other');
        }}

        async function runSingleProfilePreservesSharedCache() {{
          __profileTest.reset();
          const singleProfileResponse = {{
            active: 'default',
            single_profile_mode: true,
            profiles: [{{ name: 'default', visible: true, is_default: true }}],
          }};
          __profileTest.setApiResponse(singleProfileResponse);
          __profileTest.setCache(singleProfileResponse);
          assert.strictEqual(__profileTest.dataUsable(singleProfileResponse), true);
          assert.strictEqual(__profileTest.usable(singleProfileResponse), false);
          assert.strictEqual(__profileTest.profileDetailBacked('default'), true, 'single-profile cache should back Profiles panel before click');
          assert.doesNotThrow(() => __profileTest.toggle({{ currentTarget: document.getElementById('profileChip') }}));
          assert.strictEqual(__profileTest.profileDetailBacked('default'), true, 'dropdown open must not null the Profiles panel cache synchronously');
          assert.strictEqual(__profileTest.cache(), singleProfileResponse);
          await new Promise((resolve) => setImmediate(resolve));
          assert.strictEqual(__profileTest.profileDetailBacked('default'), true, 'fresh single-profile response must keep Profiles panel data');
          assert.strictEqual(__profileTest.cache().single_profile_mode, true);
        }}

        async function runFreshSingleProfileReplacesStaleMultiCache() {{
          __profileTest.reset();
          const staleMultiProfileResponse = multiProfileResponse;
          const singleProfileResponse = {{
            active: 'default',
            single_profile_mode: true,
            profiles: [{{ name: 'default', visible: true, is_default: true }}],
          }};
          __profileTest.setCache(staleMultiProfileResponse);
          __profileTest.setApiResponse(singleProfileResponse);
          assert.strictEqual(__profileTest.usable(staleMultiProfileResponse), true);
          assert.doesNotThrow(() => __profileTest.toggle({{ currentTarget: document.getElementById('profileChip') }}));
          await new Promise((resolve) => setImmediate(resolve));
          assert.strictEqual(__profileTest.cache(), singleProfileResponse, 'fresh single-profile payload should replace stale multi-profile memory cache');
          assert.strictEqual(__profileTest.profileDetailBacked('default'), true);
          assert.strictEqual(localStorage.getItem(__profileTest.key), null, 'single-profile payload should not be persisted as dropdown cache');
        }}

        (async () => {{
          await runPoisonedCase([null]);
          await runPoisonedCase([{{}}]);
          // Regression (#5412 gate): a row that PASSES the name check but has a
          // non-string `model` used to slip the cache predicate and throw
          // `p.model.split is not a function` synchronously on dropdown open,
          // bricking profile switching until localStorage was cleared. The
          // predicate must now reject it (usable===false) AND the renderer must
          // not throw even if such a row reaches it.
          await runPoisonedCase([{{ name: 'default', is_default: true, model: {{}} }}, {{ name: 'other', visible: true }}]);
          await runPoisonedCase([{{ name: 'default', is_default: true, model: 123 }}, {{ name: 'other', visible: true }}]);
          await runSingleProfilePreservesSharedCache();
          await runFreshSingleProfileReplacesStaleMultiCache();
        }})().catch((err) => {{ console.error(err && err.stack || err); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", script], cwd=REPO_ROOT, check=True, text=True, capture_output=True)
