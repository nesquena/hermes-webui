"""Extension theme-registration capability (window.registerHermesSkin).

Two layers:
  1. Structural — the public API + sanitizer + reserved-key guard exist in boot.js.
  2. Behavioral — a Node harness extracts the real registration/sanitization
     functions and drives them against adversarial input, proving the
     CSS-injection guard actually rejects unsafe token values (the
     security-critical contract) and that registration is additive + idempotent.

The behavioral layer is skipped (not failed) if `node` is unavailable, so the
suite never goes red purely on a missing optional toolchain.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


# ── Layer 1: structural ──────────────────────────────────────────────────────

def test_register_api_exposed_on_window():
    assert "function registerHermesSkin(descriptor)" in BOOT_JS, (
        "registerHermesSkin API missing from boot.js"
    )
    assert "window.registerHermesSkin=registerHermesSkin" in BOOT_JS, (
        "registerHermesSkin must be exposed on window for extensions to call"
    )


def test_token_sanitizer_and_allowlist_present():
    assert "_sanitizeSkinTokens" in BOOT_JS
    assert "_sanitizeSkinScheme" in BOOT_JS
    assert "_ALLOWED_SKIN_TOKENS" in BOOT_JS, "token allowlist must exist"
    assert "_SAFE_SKIN_VALUE_RE" in BOOT_JS, "value safety regex must exist"


def test_extension_skin_scheme_can_override_effective_theme_class():
    """Extension skins may opt into a light/dark base without changing the
    stored theme preference. This is the core capability that prevents dark
    editor skins from being mixed with light base tokens, and vice versa."""
    assert "function _effectiveThemeDark(baseIsDark)" in BOOT_JS
    assert "if(skinScheme==='dark') return true;" in BOOT_JS
    assert "if(skinScheme==='light') return false;" in BOOT_JS
    assert "document.documentElement.classList.toggle('dark',effectiveDark)" in BOOT_JS
    assert "const scheme=_sanitizeSkinScheme(descriptor.scheme)" in BOOT_JS
    assert "_extScheme:scheme" in BOOT_JS


def test_reserved_core_skins_are_guarded():
    assert "_RESERVED_SKIN_KEYS" in BOOT_JS
    assert "if(_RESERVED_SKIN_KEYS.has(key)) return false" in BOOT_JS, (
        "an extension must never be able to overwrite a core skin key"
    )


def test_skin_picker_label_is_not_an_innerhtml_sink():
    """A registered skin's label/name must render as TEXT, not parsed markup.

    The picker builds each button from extension-controlled descriptor fields
    (label/name). Rendering those via innerHTML would let a malicious extension
    descriptor like label='<img src=x onerror=alert(1)>' execute when Settings ->
    Appearance builds the picker. The picker must use textContent for the label.
    """
    # The old vulnerable construction interpolated the label into a template
    # string assigned to btn.innerHTML — that exact sink must be gone.
    assert 'btn.innerHTML=`' not in BOOT_JS, (
        "skin picker must not assign extension-controlled label via innerHTML"
    )
    # The label must be set as text on a dedicated element.
    assert "labelEl.textContent=skin.label||skin.name" in BOOT_JS, (
        "skin picker label must be rendered via textContent (XSS-safe)"
    )


def test_skin_picker_swatches_set_background_via_style_not_html():
    """Swatch colors are value-sanitized upstream, but defense-in-depth: set
    them via element.style.background rather than interpolating into HTML."""
    assert "dot.style.background=c" in BOOT_JS, (
        "skin swatch colors must be assigned via style.background, not HTML interpolation"
    )


def test_pending_extension_skin_is_preserved_across_boot():
    """A persisted skin that isn't a known core skin (i.e. an extension skin
    not yet registered at boot) must NOT be clobbered to 'default' by the
    boot-time appearance sync, or it won't survive a reload."""
    assert "lsSkinIsPendingExt" in BOOT_JS, (
        "boot sync must detect a pending (unregistered) extension skin"
    )
    # the boot sync must keep the raw ls value for a pending ext skin
    assert "lsSkinIsPendingExt?lsSkin:lsAppearance.skin" in BOOT_JS, (
        "boot sync must preserve the raw pending-ext skin instead of normalizing it away"
    )


INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def test_prepaint_inline_script_preserves_unknown_skin():
    """The pre-paint inline script in index.html must also preserve an unknown
    persisted skin (a likely extension skin) rather than resetting localStorage
    to 'default' before boot.js / the extension runs."""
    assert "pendingExt" in INDEX_HTML, (
        "pre-paint script must detect a pending extension skin and keep it"
    )


# ── Layer 2: behavioral (Node harness drives the real functions) ─────────────

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Extract the self-contained pieces we need from boot.js without a DOM.
function grab(name, kind) {
  // crude but reliable function/const slice by brace/line matching
  const startRe = new RegExp((kind === 'fn' ? 'function ' : '') + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const i = src.search(startRe);
  if (i < 0) throw new Error('not found: ' + name);
  return i;
}

// Minimal globals the extracted code touches.
global.document = {
  getElementById: () => null,
  createElement: () => ({ appendChild(){}, set textContent(v){}, get textContent(){return '';} }),
  querySelectorAll: () => [],
  head: { appendChild(){} },
  documentElement: { dataset: {} },
};
const _store = {};
global.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
global.window = {};

// Pull the exact constants + functions out of boot.js by evaluating just the
// region from `const _EXT_SKIN_STYLE_ID` through the window assignment line.
const startMarker = "const _EXT_SKIN_STYLE_ID";
const endMarker = "window.registerHermesSkin=registerHermesSkin;";
const a = src.indexOf(startMarker);
const b = src.indexOf(endMarker);
if (a < 0 || b < 0) { console.log(JSON.stringify({error: 'markers not found'})); process.exit(0); }
let region = src.slice(a, b + endMarker.length);

// The region references _SKINS / _VALID_SKINS / _applySkin / _buildSkinPicker /
// _syncSkinPicker from the surrounding module — stub them.
const prelude = `
  var _SKINS = [{name:'Default',colors:['#FFD700','#FFBF00','#CD7F32']},
                {name:'Ares',colors:['#FF4444','#CC3333','#992222']}];
  var _VALID_SKINS = new Set(_SKINS.map(s=>(s.value||s.name).toLowerCase()));
  function _applySkin(){}
  function _buildSkinPicker(){}
  function _syncSkinPicker(){}
`;
eval(prelude + region);
// In non-strict eval, `function registerHermesSkin` leaks into this scope.

const results = {};

// 1. valid skin registers
results.valid = registerHermesSkin({
  name: 'E-Ink', value: 'e-ink', colors: ['#000','#fff','#555'],
  tokens: { '--bg':'#ffffff', '--text':'#000000', '--accent':'#000000' }
});

// 2. unsafe token value (CSS injection attempt) is dropped → registration fails
//    because no valid tokens remain
results.injection = registerHermesSkin({
  name: 'Evil', value: 'evil',
  tokens: { '--bg': 'red;} body{display:none}', '--text': 'url(http://x/a.png)' }
});

// 3. partially-unsafe: keeps safe token, drops unsafe one
results.partial = registerHermesSkin({
  name: 'Partial', value: 'partial',
  tokens: { '--bg':'#123456', '--text':'expression(alert(1))', '--accent':'rgb(1,2,3)' }
});

// 4. cannot overwrite a reserved core skin
results.reserved = registerHermesSkin({
  name: 'Default', value: 'default', tokens: { '--bg':'#000000' }
});

// 5. unknown token name is dropped (not in allowlist) → fails (nothing valid)
results.unknownToken = registerHermesSkin({
  name: 'Unknown', value: 'unknown', tokens: { '--evil-prop':'#fff' }
});

// 6. idempotent re-register of same key returns true again
results.idempotent = registerHermesSkin({
  name: 'E-Ink', value: 'e-ink', tokens: { '--bg':'#fefefe' }
});

// 7. garbage input rejected
results.garbage = registerHermesSkin(null);
results.noTokens = registerHermesSkin({ name: 'X', value: 'x' });

// 8. optional scheme is restricted to light/dark and stored on the skin entry
results.darkScheme = registerHermesSkin({
  name: 'Tokyo Night', value: 'tokyo-night', scheme: 'dark',
  tokens: { '--bg':'#1a1b26', '--text':'#c0caf5', '--accent':'#7aa2f7' }
});
results.invalidScheme = registerHermesSkin({
  name: 'Bad Scheme', value: 'bad-scheme', scheme: 'dark;body{display:none}',
  tokens: { '--bg':'#111111', '--text':'#eeeeee' }
});
results.schemes = {};
for (const skin of _SKINS) {
  const key = (skin.value || skin.name).toLowerCase();
  if (key === 'tokyo-night' || key === 'bad-scheme') {
    results.schemes[key] = skin._extScheme || '';
  }
}

console.log(JSON.stringify(results));
"""


_SCHEME_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

const classes = new Set();
global.document = {
  documentElement: {
    dataset: {},
    classList: {
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
  },
  getElementById: () => null,
  querySelectorAll: () => [],
};
global.getComputedStyle = () => ({ getPropertyValue: () => '#141425' });

let systemMatches = false;
const listeners = new Set();
global.window = {
  matchMedia: () => ({
    get matches() { return systemMatches; },
    addEventListener(_name, fn) { listeners.add(fn); },
    removeEventListener(_name, fn) { listeners.delete(fn); },
  }),
};

const startMarker = "const _LEGACY_THEME_MAP";
const endMarker = "function _pickTheme(name){";
const a = src.indexOf(startMarker);
const b = src.indexOf(endMarker);
if (a < 0 || b < 0) { console.log(JSON.stringify({error: 'markers not found'})); process.exit(0); }
const region = src.slice(a, b);

const prelude = `
  var _SKINS = [
    {name:'Default', value:'default'},
    {name:'Tokyo Night', value:'tokyo-night', _extScheme:'dark'},
    {name:'E-Ink', value:'e-ink', _extScheme:'light'}
  ];
  var _VALID_THEMES = new Set(['light','dark','system']);
  var _VALID_SKINS = new Set(_SKINS.map(s => (s.value || s.name).toLowerCase()));
`;
eval(prelude + region);

function state(label) {
  return {
    label,
    dark: document.documentElement.classList.contains('dark'),
    skin: document.documentElement.dataset.skin || 'default',
  };
}

const results = [];

_applyTheme('light');
_applySkin('tokyo-night');
results.push(state('dark-scheme-overrides-light'));

_applyTheme('system');
_applySkin('tokyo-night');
results.push(state('dark-scheme-overrides-system-light'));

_applyTheme('dark');
_applySkin('e-ink');
results.push(state('light-scheme-overrides-dark'));

_applySkin('default');
results.push(state('default-restores-dark-base'));

_applyTheme('light');
_applySkin('default');
results.push(state('default-restores-light-base'));

console.log(JSON.stringify({results}));
"""


def test_registration_and_sanitization_behavior():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available for behavioral harness")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_HARNESS)
        harness_path = f.name
    proc = subprocess.run(
        [node, harness_path, str(REPO / "static" / "boot.js")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr or proc.stdout}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out.get("error") is None, f"harness error: {out.get('error')}"

    assert out["valid"] is True, "a clean skin descriptor must register"
    assert out["injection"] is False, (
        "a descriptor whose only tokens are CSS-injection attempts must be rejected"
    )
    assert out["partial"] is True, "safe tokens survive even if siblings are dropped"
    assert out["reserved"] is False, "must not overwrite a reserved core skin"
    assert out["unknownToken"] is False, "tokens outside the allowlist are dropped"
    assert out["idempotent"] is True, "re-registering an ext skin key is allowed"
    assert out["garbage"] is False, "null descriptor rejected"
    assert out["noTokens"] is False, "descriptor with no tokens rejected"
    assert out["darkScheme"] is True, "light/dark skin scheme should be accepted"
    assert out["invalidScheme"] is True, "invalid scheme should not reject otherwise valid tokens"
    assert out["schemes"]["tokyo-night"] == "dark", "valid dark scheme must be stored"
    assert out["schemes"]["bad-scheme"] == "", "invalid scheme must be ignored"


def test_extension_skin_scheme_drives_effective_dark_class():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available for behavioral harness")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_SCHEME_HARNESS)
        harness_path = f.name
    proc = subprocess.run(
        [node, harness_path, str(REPO / "static" / "boot.js")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr or proc.stdout}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out.get("error") is None, f"harness error: {out.get('error')}"

    states = {row["label"]: row for row in out["results"]}
    assert states["dark-scheme-overrides-light"]["dark"] is True
    assert states["dark-scheme-overrides-system-light"]["dark"] is True
    assert states["light-scheme-overrides-dark"]["dark"] is False
    assert states["default-restores-dark-base"]["dark"] is True
    assert states["default-restores-light-base"]["dark"] is False
