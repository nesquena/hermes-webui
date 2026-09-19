"""Persian (fa) locale + locale-aware RTL direction.

#6664: add a Persian locale (`fa`, speech `fa-IR`) and make the application
shell follow locale direction metadata so Persian and future right-to-left
locales render correctly. The locale declares `_dir: 'rtl'`; setLocale()
applies it to <html dir> and restores `ltr` for non-RTL locales.
"""
from collections import Counter
from pathlib import Path
import os
import re
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"

    start = start_match.end() - 1
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False

    for i in range(start, len(src)):
        ch = src[i]

        if escape:
            escape = False
            continue

        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue

        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue

        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue

        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "`":
            in_backtick = True
            continue

        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]

    raise AssertionError(f"{locale_key} locale block braces are not balanced")


def locale_keys(src: str, locale_key: str) -> list[str]:
    key_pattern = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:", re.MULTILINE)
    return key_pattern.findall(extract_locale_block(src, locale_key))


def test_persian_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    assert fa_block
    assert "_lang: 'fa'" in fa_block
    assert "_label: 'فارسی'" in fa_block
    assert "_speech: 'fa-IR'" in fa_block
    assert "_dir: 'rtl'" in fa_block


def test_persian_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    expected = [
        "settings_title: 'تنظیمات'",
        "settings_label_language: 'زبان'",
        "login_title: 'ورود'",
        "approval_heading: 'تأیید لازم است'",
        "tab_chat: 'گفتگو'",
        "tab_tasks: 'وظایف'",
        "empty_title: 'در چه کاری می‌توانم کمک کنم؟'",
        "onboarding_title: 'به رابط کاربری وب هرمس خوش آمدید'",
    ]
    for entry in expected:
        assert entry in fa_block


def test_persian_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    fa_keys = set(locale_keys(src, "fa"))
    assert sorted((en_keys - fa_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(fa_keys - en_keys - {"_dir"}) == []


def test_persian_locale_has_no_new_duplicate_keys():
    """fa may only duplicate keys that en itself duplicates (shared by design)."""
    src = read(REPO / "static" / "i18n.js")
    en_dups = {k for k, c in Counter(locale_keys(src, "en")).items() if c > 1}
    fa_dups = {k for k, c in Counter(locale_keys(src, "fa")).items() if c > 1}
    assert not (fa_dups - en_dups), f"Persian locale has new duplicate keys: {fa_dups - en_dups}"


def test_persian_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    badly_indented = [
        line.strip()
        for line in fa_block.splitlines()
        if re.match(r"^\s{1,3}[a-zA-Z0-9_]+ *:", line)
    ]
    assert badly_indented == []


def test_persian_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in fa_block, f"Persian locale must not contain {bad!r}"


def test_set_locale_applies_rtl_direction_for_persian():
    """setLocale('fa') must set dir=rtl on <html>; non-RTL locales restore ltr."""
    src = read(REPO / "static" / "i18n.js")
    assert "_dir === 'rtl'" in src, (
        "setLocale() must apply the locale's _dir metadata to <html dir>"
    )
    assert "document.documentElement.dir" in src, (
        "setLocale() must write document.documentElement.dir"
    )
    # Non-RTL locales must explicitly restore LTR so an RTL selection never
    # leaks into the next session or a locale switch.
    assert "'ltr'" in src


def test_rtl_shell_css_keeps_technical_surfaces_ltr():
    """RTL shell CSS must bidi-isolate code/terminal/path/numeric surfaces."""
    css = read(REPO / "static" / "style.css")
    assert 'html[dir="rtl"]' in css
    assert "unicode-bidi:isolate" in css
    assert "unicode-bidi:plaintext" in css
    # Technical surfaces stay LTR even in an RTL shell.
    rtl_block_start = css.index('html[dir="rtl"]')
    rtl_block = css[rtl_block_start:]
    assert "direction:ltr" in rtl_block
    assert ".composer-terminal-surface" in rtl_block
    assert 'input[type="number"]' in rtl_block
    # The sidebar border flips so the right-side rail keeps its divider.
    assert "border-left:1px solid var(--border)" in rtl_block


def test_rtl_shell_css_mirrors_manual_tool_surface_rules():
    """html[dir=rtl] must mirror the .chat-content-rtl plain-text tool rules.

    Review (CHANGES_REQUESTED): setLocale() applies html dir=rtl without the
    manual .chat-content-rtl class, so the plain-text tool surfaces that the
    manual rules force LTR (.tool-call-group-body, .tool-result + descendants)
    must be covered by the automatic shell rules too — otherwise Persian with
    the manual RTL toggle off lets commands, paths, and JSON inherit RTL.
    """
    css = read(REPO / "static" / "style.css")
    rtl_block_start = css.index('html[dir="rtl"]')
    rtl_block = css[rtl_block_start:]
    # Every selector the manual .chat-content-rtl tool rule uses must exist
    # in the automatic html[dir=rtl] block with the same LTR declarations.
    for sel in (
        'html[dir="rtl"] .tool-call-group-body,',
        'html[dir="rtl"] .tool-call-group-body *,',
        'html[dir="rtl"] .tool-result,',
        'html[dir="rtl"] .tool-result *,',
    ):
        assert sel in rtl_block, f"missing mirror selector {sel}"
    # The shared declaration block carries the LTR isolation contract.
    tool_rule_start = rtl_block.index('html[dir="rtl"] .tool-call-group-body')
    tool_rule_end = rtl_block.index("}", tool_rule_start)
    tool_rule = rtl_block[tool_rule_start:tool_rule_end]
    assert "direction:ltr" in tool_rule
    assert "text-align:left" in tool_rule
    assert "unicode-bidi:isolate" in tool_rule
    # Plain-text path/ID surfaces from the #6664 audit stay LTR as well:
    # command palette file paths and model IDs are technical identifiers.
    assert 'html[dir="rtl"] .cmd-item-path-value,' in rtl_block
    assert 'html[dir="rtl"] .model-opt-id,' in rtl_block
    # Remaining technical text controls named by the re-gate review (base
    # URLs, paths/IDs, structured/credential text) get scoped LTR selectors;
    # ordinary Persian form text is untouched (no blanket input/textarea rule).
    for sel in (
        'html[dir="rtl"] #onboardingBaseUrlInput,',
        'html[dir="rtl"] #profileFormBaseUrl,',
        'html[dir="rtl"] #auxAdvancedBaseUrl,',
        'html[dir="rtl"] #settingsDashboardUrl,',
        'html[dir="rtl"] #onboardingWorkspaceInput,',
        'html[dir="rtl"] #workspaceFormPath,',
        'html[dir="rtl"] .model-custom-input,',
        'html[dir="rtl"] #onboardingModelInput,',
        'html[dir="rtl"] #kanbanDependencyInput,',
        'html[dir="rtl"] #kanbanTaskModalWorkspacePath,',
        'html[dir="rtl"] #kanbanTaskModalParents,',
        'html[dir="rtl"] #auxAdvancedApiKey,',
        'html[dir="rtl"] #auxAdvancedExtraBody,',
    ):
        assert sel in rtl_block or sel.rstrip(",") + "{" in rtl_block, (
            f"missing audit selector {sel}"
        )


def test_rtl_four_case_behavior_matrix():
    """Behavior matrix (behavioral, not source-only) for locale-driven vs manual RTL.

    Drives the REAL setLocale() from static/i18n.js and the REAL manual RTL
    class toggle (document.documentElement.classList.toggle('chat-content-rtl')),
    mounts representative tool content (.tool-call-group-body / .tool-result and
    descendants), and resolves the REAL style.css rules against the DOM to
    compute direction/unicode-bidi for each of the four cases:

    Case 1 — fa / manual-off: setLocale('fa') sets html dir=rtl; the automatic
      html[dir="rtl"] rules must force tool surfaces LTR + bidi-isolated even
      without the manual class.
    Case 2 — fa / manual-on: html dir=rtl AND .chat-content-rtl; tool surfaces
      stay LTR under either rule set.
    Case 3 — LTR / manual-on: html dir=ltr with the manual class; the legacy
      .chat-content-rtl rules still isolate tool surfaces.
    Case 4 — fa -> LTR restoration: switching fa -> en restores dir=ltr and no
      RTL/LTR override leaks into the next locale (tool rows inherit shell LTR).
    """
    import json
    import shutil
    import subprocess
    import tempfile

    css = read(REPO / "static" / "style.css")
    src = read(REPO / "static" / "i18n.js")

    # Small source-presence guard (explicitly allowed by the review) — the real
    # behavior assertions follow below. Rule lists end either with ',' (mid-list)
    # or '{' (last selector opening the declaration block).
    for sel in (
        'html[dir="rtl"] .tool-call-group-body,',
        'html[dir="rtl"] .tool-call-group-body *,',
        'html[dir="rtl"] .tool-result,',
        'html[dir="rtl"] .tool-result *',
        ".chat-content-rtl .tool-call-group-body,",
        ".chat-content-rtl .tool-call-group-body *",
        ".chat-content-rtl .tool-result,",
        ".chat-content-rtl .tool-result *",
    ):
        assert sel in css, f"missing mirror selector {sel}"

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node not on PATH")

    js = r"""
const fs = require('fs');
const docEl = { lang:'', dir:'', cls:new Set(), attrs:{},
  setAttribute(k,v){this.attrs[k]=v;},
  getAttribute(k){return this.attrs[k]??null;},
  classList: { toggle(c,on){ on?docEl.cls.add(c):docEl.cls.delete(c); },
               contains(c){ return docEl.cls.has(c); } } };
global.document = { documentElement: docEl, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.window = global;
eval(fs.readFileSync(process.argv[2], 'utf8'));

function makeEl(classes, parent){
  const el = { cls:new Set(classes), parent: parent||null };
  el._matchTail = function(tail){
    const parts = tail.trim().split(/\s+/);
    const head = parts[0];
    if (head === '*') return true;
    if (!head.startsWith('.')) return false;
    const name = head.slice(1);
    if (parts.length === 1) return this.cls.has(name);
    if (parts[parts.length-1] === '*') {
      let p = this.parent;
      while (p) { if (p.cls.has(name)) return true; p = p.parent; }
      return false;
    }
    return false;
  };
  el.matches = function(sel){
    sel = sel.trim();
    let m = sel.match(/^html\[dir="([a-z]+)"\]\s+(.+)$/);
    if (m) return docEl.dir === m[1] && this._matchTail(m[2]);
    m = sel.match(/^\.chat-content-rtl\s+(.+)$/);
    if (m) return docEl.cls.has('chat-content-rtl') && this._matchTail(m[1]);
    return this._matchTail(sel);
  };
  return el;
}
// Representative tool content
const body = makeEl(['msg-body']);
const toolGroup = makeEl(['tool-call-group-body'], body);
const toolGroupChild = makeEl(['tool-detail'], toolGroup);
const toolResult = makeEl(['tool-result'], body);
const toolResultChild = makeEl(['p'], toolResult);

// Parse the REAL style.css: only rules whose selector targets tool surfaces.
const css = fs.readFileSync(process.argv[3], 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '');           // strip comments
const rules = [];
const re = /([^{}@][^{}]*)\{([^{}]*)\}/g;
let m;
while ((m = re.exec(css))) {
  const selGroup = m[1].trim().replace(/\s+/g,' ');
  if (selGroup.includes('tool-call-group-body') || selGroup.includes('tool-result')) {
    // a comma-separated selector group shares one declaration block
    for (const one of selGroup.split(',')) {
      const sel = one.trim();
      if (sel) rules.push({ sel, decls: m[2] });
    }
  }
}
function computed(el){
  let dir = null, bid = null;
  for (const r of rules) {
    if (el.matches(r.sel)) {
      const d = r.decls.match(/direction\s*:\s*([^;]+);/);
      if (d) dir = d[1].trim();
      const b = r.decls.match(/unicode-bidi\s*:\s*([^;]+);/);
      if (b) bid = b[1].trim();
    }
  }
  return { dir: dir, bid: bid };
}
function caseRun(locale, manualOn){
  setLocale(locale);
  docEl.classList.toggle('chat-content-rtl', manualOn);
  return {
    dir: docEl.dir,
    manual: docEl.cls.has('chat-content-rtl'),
    group: computed(toolGroup),
    groupChild: computed(toolGroupChild),
    result: computed(toolResult),
    resultChild: computed(toolResultChild),
  };
}
const out = {};
out.case1 = caseRun('fa', false);
out.case2 = caseRun('fa', true);
out.case3 = caseRun('en', true);
out.case4 = caseRun('fa', false); out.case4_restored = caseRun('en', false);
process.stdout.write(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(js)
        script = tf.name
    try:
        result = subprocess.run(
            [node, script, str(REPO / "static" / "i18n.js"), str(REPO / "static" / "style.css")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"node error: {result.stderr[:500]}"
        out = json.loads(result.stdout)
    finally:
        os.unlink(script)

    # Case 1 — fa / manual off: automatic html[dir=rtl] rules isolate tool rows.
    assert out["case1"]["dir"] == "rtl" and out["case1"]["manual"] is False, out["case1"]
    for surf in ("group", "groupChild", "result", "resultChild"):
        assert out["case1"][surf]["dir"] == "ltr", (surf, out["case1"][surf])
        assert out["case1"][surf]["bid"] == "isolate", (surf, out["case1"][surf])

    # Case 2 — fa / manual on: both rule sets active; still LTR + isolated.
    assert out["case2"]["dir"] == "rtl" and out["case2"]["manual"] is True, out["case2"]
    for surf in ("group", "groupChild", "result", "resultChild"):
        assert out["case2"][surf]["dir"] == "ltr", (surf, out["case2"][surf])
        assert out["case2"][surf]["bid"] == "isolate", (surf, out["case2"][surf])

    # Case 3 — LTR / manual on: legacy .chat-content-rtl rules isolate tool rows.
    assert out["case3"]["dir"] == "ltr" and out["case3"]["manual"] is True, out["case3"]
    for surf in ("group", "groupChild", "result", "resultChild"):
        assert out["case3"][surf]["dir"] == "ltr", (surf, out["case3"][surf])
        assert out["case3"][surf]["bid"] == "isolate", (surf, out["case3"][surf])

    # Case 4 — fa -> LTR restoration: dir restored, manual class off, and no
    # forced-LTR rule leaks (tool rows inherit the LTR shell).
    assert out["case4"]["dir"] == "rtl", out["case4"]
    assert out["case4_restored"]["dir"] == "ltr", out["case4_restored"]
    assert out["case4_restored"]["manual"] is False, out["case4_restored"]
    for surf in ("group", "groupChild", "result", "resultChild"):
        assert out["case4_restored"][surf]["dir"] is None, (surf, out["case4_restored"][surf])


def test_set_locale_rtl_to_ltr_restoration_behavior():
    """Behavioral (node + DOM shims): fa sets dir=rtl; switching to en restores ltr.

    Drives the REAL setLocale() from static/i18n.js (case 4 of the behavior
    matrix: fa -> LTR restoration) and asserts the data-locale stamp.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node not on PATH")
    js = r"""
const docEl = { lang:'', dir:'', attrs:{}, setAttribute(k,v){this.attrs[k]=v;},
  getAttribute(k){return this.attrs[k]??null;} };
global.document = { documentElement: docEl, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.window = global;
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
setLocale('fa');
const faDir = docEl.dir; const faLoc = docEl.attrs['data-locale'];
setLocale('en');
const enDir = docEl.dir; const enLoc = docEl.attrs['data-locale'];
setLocale('fa'); setLocale('en');  // restoration path after an RTL session
const restoredDir = docEl.dir;
process.stdout.write(JSON.stringify({faDir, faLoc, enDir, enLoc, restoredDir}));
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(js)
        script = tf.name
    try:
        result = subprocess.run(
            [node, script, str(REPO / "static" / "i18n.js")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"node error: {result.stderr}"
        import json

        out = json.loads(result.stdout)
        assert out["faDir"] == "rtl", out
        assert out["faLoc"] == "fa", out
        assert out["enDir"] == "ltr", out
        assert out["enLoc"] == "en", out
        assert out["restoredDir"] == "ltr", out
    finally:
        os.unlink(script)
