"""
Regression tests for #3368 — /model <name> prefix-shadowing.

Reporter: `/model mimo-v2.5` selected `mimo-v2.5-pro` because the typed name is a
clean prefix of the longer tier variant, and the only `mimo-v2.5*` entry in the
Nous catalog is the `-pro` one (there is no bare `mimo-v2.5`).

PR #3394 ("Closes #3368") only hardened the `_bestModelMatch` *fallback* in
`commands.js`, but for this input `_findModelInDropdown` (ui.js) returns the
WRONG longer variant via its step-3 prefix match BEFORE the fallback ever runs —
so the fix never executed and the reporter still saw `mimo-v2.5-pro` after
upgrading to v0.51.216.

This fix hardens BOTH matchers so a COMPLETE versioned query (ends in a digit)
never snaps to a `-suffix` tier variant:

* `_findModelInDropdown` step 3 (ui.js): a prefix hit on a longer option is only
  accepted when the extra text continues the VERSION (".`<digit>`"), not a
  variant/tier suffix (".`pro`" from "-pro"). Otherwise returns null.
* `_bestModelMatch` (commands.js): same boundary rule on the substring fallback.

These tests run the live JS via Node so drift is caught behaviourally.

Crucially they also assert the #1188 legitimate-fuzzy behaviour still holds
(`gpt-5` → `gpt-5.4-mini`, `claude` → `claude-opus-4.6`), since the fix touches
the same step-3 path.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
COMMANDS_JS_PATH = REPO_ROOT / "static" / "commands.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def commands_src() -> str:
    return COMMANDS_JS_PATH.read_text(encoding="utf-8")


# ── driver for _findModelInDropdown (ui.js) ─────────────────────────────────

_FIND_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(src, name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start, i);
}
function _getOptionProviderId(opt){
  if(!opt) return '';
  if(opt.dataset && opt.dataset.provider) return opt.dataset.provider;
  const group = opt.parentElement;
  if(group && group.tagName==='OPTGROUP' && group.dataset && group.dataset.provider) return group.dataset.provider;
  const value = String(opt.value||'');
  if(value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
}
eval(extractFunc(ui, '_findModelInDropdown'));
const args = JSON.parse(process.argv[3]);
const sel = { options: args.options.map(v => ({value: v})) };
const got = _findModelInDropdown(args.modelId, sel, args.preferredProvider || undefined);
process.stdout.write(JSON.stringify(got));
"""


# ── driver for _bestModelMatch (commands.js) ────────────────────────────────

_BEST_DRIVER = r"""
const fs = require('fs');
const cmds = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(src, name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start, i);
}
eval(extractFunc(cmds, '_looksLikeVersionedModel'));
eval(extractFunc(cmds, '_bestModelMatch'));
eval(extractFunc(cmds, '_nearestModelSuggestion'));
const args = JSON.parse(process.argv[3]);
// options: [{value, text}]
const options = args.options.map(o => ({value: o.value, textContent: o.text || o.value}));
const out = {
  best: _bestModelMatch(options, args.query),
  suggestion: _nearestModelSuggestion(options, args.query),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def find_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("find3368") / "driver.js"
    p.write_text(_FIND_DRIVER, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def best_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("best3368") / "driver.js"
    p.write_text(_BEST_DRIVER, encoding="utf-8")
    return str(p)


# ── driver for _buildModelCandidates + _bestModelMatch over the FULL catalog ──
# This is the layer the reporter's case actually needed: on large provider
# catalogs the bare model lives in `extra_models` (not a rendered <option>), so
# /model must resolve against featured `models` PLUS `extra_models`. (#3368)

_CATALOG_DRIVER = r"""
const fs = require('fs');
const cmds = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(src, name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start, i);
}
eval(extractFunc(cmds, '_buildModelCandidates'));
eval(extractFunc(cmds, '_looksLikeVersionedModel'));
eval(extractFunc(cmds, '_bestModelMatch'));
eval(extractFunc(cmds, '_nearestModelSuggestion'));
const args = JSON.parse(process.argv[3]);
// args.groups: [{provider_id, models:[{id,label}], extra_models:[{id,label}]}]
// args.selOptions: ids rendered as <option> (the featured subset)
const sel = { options: (args.selOptions||[]).map(v => ({value: v, textContent: v})) };
const { options: candidates, providerMap } = _buildModelCandidates(sel, args.groups);
// Mirror cmdModel's fallback chain for a query (skipping _findModelInDropdown,
// which is exercised by the find_driver tests and returns null for these cases).
let q = String(args.query||'').toLowerCase();
let match = _bestModelMatch(candidates, q);
if(!match && q.includes('/')){
  const bare = q.slice(q.lastIndexOf('/')+1);
  match = _bestModelMatch(candidates, bare);
}
process.stdout.write(JSON.stringify({
  candidateCount: candidates.length,
  match,
  provider: match ? (providerMap[match]||null) : null,
  suggestion: _nearestModelSuggestion(candidates, q),
}));
"""


@pytest.fixture(scope="module")
def catalog_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("catalog3368") / "driver.js"
    p.write_text(_CATALOG_DRIVER, encoding="utf-8")
    return str(p)


def _resolve(driver, query, groups, sel_options):
    r = subprocess.run(
        [NODE, driver, str(COMMANDS_JS_PATH),
         json.dumps({"query": query, "groups": groups, "selOptions": sel_options})],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node driver failed: {r.stderr}")
    return json.loads(r.stdout)


def _find(driver, model_id, options, preferred=None):
    r = subprocess.run(
        [NODE, driver, str(UI_JS_PATH),
         json.dumps({"modelId": model_id, "options": options, "preferredProvider": preferred})],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node driver failed: {r.stderr}")
    return json.loads(r.stdout)


def _best(driver, query, options):
    r = subprocess.run(
        [NODE, driver, str(COMMANDS_JS_PATH), json.dumps({"query": query, "options": options})],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node driver failed: {r.stderr}")
    return json.loads(r.stdout)


# ═══════════════════════════════════════════════════════════════════════════
# _findModelInDropdown — the layer PR #3394 missed (the reporter's actual path)
# ═══════════════════════════════════════════════════════════════════════════


class TestFindModelDropdownNoTierSnap:
    def test_mimo_v25_does_not_snap_to_pro(self, find_driver):
        """The exact reported case: only mimo-v2.5-pro exists in the catalog,
        and /model mimo-v2.5 must NOT silently resolve to it."""
        got = _find(find_driver, "mimo-v2.5", ["@nous:xiaomi/mimo-v2.5-pro"], "nous")
        assert got is None, f"mimo-v2.5 must not snap to mimo-v2.5-pro (#3368), got {got!r}"

    def test_mimo_v25_no_snap_with_multiple_tiers(self, find_driver):
        got = _find(find_driver, "mimo-v2.5",
                    ["@nous:xiaomi/mimo-v2.5-pro", "@nous:xiaomi/mimo-v2.5-flash"], "nous")
        assert got is None

    def test_bare_mimo_v25_still_resolves_when_it_exists(self, find_driver):
        """If the catalog DOES carry the bare model, it must resolve to it
        (the normalized-equality path, step 2) — not to the -pro variant."""
        got = _find(find_driver, "mimo-v2.5",
                    ["@nous:xiaomi/mimo-v2.5-pro", "@nous:xiaomi/mimo-v2.5"], "nous")
        assert got == "@nous:xiaomi/mimo-v2.5"

    def test_full_pro_name_still_selectable(self, find_driver):
        got = _find(find_driver, "mimo-v2.5-pro", ["@nous:xiaomi/mimo-v2.5-pro"], "nous")
        assert got == "@nous:xiaomi/mimo-v2.5-pro"


class TestFindModelDropdownLegitFuzzyPreserved:
    """#1188 behaviour must survive — incomplete version prefixes still match."""

    def test_gpt5_still_matches_gpt54_mini(self, find_driver):
        got = _find(find_driver, "gpt-5", ["@nous:openai/gpt-5.4-mini"])
        assert got == "@nous:openai/gpt-5.4-mini"

    def test_bare_root_claude_still_matches(self, find_driver):
        got = _find(find_driver, "claude", ["@nous:anthropic/claude-opus-4.6"])
        assert got == "@nous:anthropic/claude-opus-4.6"

    def test_claude_opus_no_version_still_matches(self, find_driver):
        got = _find(find_driver, "claude-opus", ["@nous:anthropic/claude-opus-4.6"])
        assert got == "@nous:anthropic/claude-opus-4.6"

    def test_incomplete_version_still_continues(self, find_driver):
        """mimo-v2 is an INCOMPLETE version (not ending the version), so it may
        still resolve to mimo-v2.5-pro — the extra text continues the version."""
        got = _find(find_driver, "mimo-v2", ["@nous:xiaomi/mimo-v2.5-pro"])
        assert got == "@nous:xiaomi/mimo-v2.5-pro"

    def test_exact_match_unaffected(self, find_driver):
        got = _find(find_driver, "gpt-5.4-mini",
                    ["@nous:openai/gpt-5.4-mini", "@nous:openai/gpt-5.5"])
        assert got == "@nous:openai/gpt-5.4-mini"


# ═══════════════════════════════════════════════════════════════════════════
# _bestModelMatch fallback — must not re-introduce the snap, and must suggest
# ═══════════════════════════════════════════════════════════════════════════


class TestBestModelMatchFallback:
    def test_fallback_does_not_snap_to_pro(self, best_driver):
        out = _best(best_driver, "mimo-v2.5", [
            {"value": "xiaomi/mimo-v2.5-pro", "text": "MiMo V2.5 Pro"},
        ])
        assert out["best"] is None, "fallback must not snap mimo-v2.5 → -pro"

    def test_fallback_suggests_nearest_variant(self, best_driver):
        out = _best(best_driver, "mimo-v2.5", [
            {"value": "xiaomi/mimo-v2.5-pro", "text": "MiMo V2.5 Pro"},
        ])
        assert out["suggestion"] == "xiaomi/mimo-v2.5-pro", (
            "should offer the -pro variant as a 'did you mean' suggestion"
        )

    def test_fallback_exact_still_wins(self, best_driver):
        out = _best(best_driver, "mimo-v2.5", [
            {"value": "xiaomi/mimo-v2.5-pro", "text": "MiMo V2.5 Pro"},
            {"value": "mimo-v2.5", "text": "MiMo V2.5"},
        ])
        assert out["best"] == "mimo-v2.5"

    def test_fallback_shortest_nonversioned_query_unchanged(self, best_driver):
        """A non-versioned query (e.g. 'claude') keeps the shortest-substring
        behaviour from #3394 — the version guard only applies to versioned ones."""
        out = _best(best_driver, "claude", [
            {"value": "anthropic/claude-opus-4.6", "text": "Claude Opus 4.6"},
            {"value": "anthropic/claude-haiku-4.5", "text": "Claude Haiku 4.5"},
        ])
        # shortest option value containing 'claude' — opus-4.6 (25) < haiku-4.5 (26)
        assert out["best"] == "anthropic/claude-opus-4.6"

    def test_fallback_version_continuation_allowed(self, best_driver):
        """An incomplete version (mimo-v2) may still match the longer id via the
        fallback, since the extra text continues the version."""
        out = _best(best_driver, "mimo-v2", [
            {"value": "xiaomi/mimo-v2.5-pro", "text": "MiMo V2.5 Pro"},
        ])
        assert out["best"] == "xiaomi/mimo-v2.5-pro"


class TestDidYouMeanToastAssembly:
    """#3368 review: the 'did you mean' toast must interpolate the suggestion
    correctly. model_did_you_mean is a t()-invoked template `(m) => ...${m}...`,
    so cmdModel must pass the suggestion as an arg: t('model_did_you_mean', suggestion).
    Calling t('model_did_you_mean') with no arg rendered the literal "undefined".
    And no_model_match already ends with an opening quote, so cmdModel must close
    with `${args}"`, not `"${args}"` (which doubled the quote)."""

    # Uses the module-scoped commands_src fixture above.
    def test_toast_passes_suggestion_as_t_argument(self, commands_src):
        assert "t('model_did_you_mean', suggestion)" in commands_src, (
            "cmdModel must pass the suggestion into t() so the template fills in "
            "the model name (otherwise it renders 'undefined')"
        )

    def test_toast_does_not_use_buggy_function_branch(self, commands_src):
        # The old buggy form resolved t() with no arg then branched on typeof.
        assert "const sg=t('model_did_you_mean');" not in commands_src
        assert "typeof sg==='function'" not in commands_src

    def test_no_model_match_quote_not_doubled(self, commands_src):
        # no_model_match value already ends with an opening quote; the assembly
        # must close it with args+" (not re-open with "${args}").
        assert "t('no_model_match')+`${args}\"`" in commands_src
        assert "t('no_model_match')+`\"${args}\"`" not in commands_src

    def test_model_did_you_mean_is_arg_template_in_en_locale(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        i18n = (root / "static" / "i18n.js").read_text(encoding="utf-8")
        # The en template must accept and interpolate an argument.
        assert "model_did_you_mean: (m) =>" in i18n
        assert "${m}" in i18n[i18n.index("model_did_you_mean: (m) =>"):i18n.index("model_did_you_mean: (m) =>") + 120]


class TestSlashQualifiedVersionedNoSnap:
    """#3368 review (Codex CORE): a versioned slash-qualified query whose only near
    catalog entry is a rejected tier variant (e.g. 'xiaomi/mimo-v2.5' when only
    'xiaomi/mimo-v2.5-pro' exists) must NOT take the cross-provider direct-update
    fallback and silently persist the invalid model — it must fall through to the
    no-match/'did you mean?' toast. The cross-provider direct update is gated on
    `!versionedNoSnap` so genuinely-off-catalog providers (no near variant) still work."""

    # Uses the module-scoped commands_src fixture above.
    def test_cross_provider_direct_update_gated_on_versioned_no_snap(self, commands_src):
        # The guard variable is computed from the version check + a near suggestion.
        assert "_looksLikeVersionedModel(bare)" in commands_src
        assert "versionedNoSnap" in commands_src
        # The direct-update branch must include the !versionedNoSnap gate.
        idx = commands_src.find("Cross-provider fallback")
        assert idx != -1, "cross-provider fallback comment not found"
        window = commands_src[idx:idx + 400]
        assert "!versionedNoSnap" in window, (
            "the /api/session/update cross-provider fallback must be gated on "
            "!versionedNoSnap so a versioned tier-variant near-miss doesn't silently persist"
        )


# ═══════════════════════════════════════════════════════════════════════════
# FULL-CATALOG resolution — garyd9's actual case (#3368 reopened correction)
# ═══════════════════════════════════════════════════════════════════════════
#
# garyd9 (Nous Portal subscriber) confirmed `xiaomi/mimo-v2.5` is a REAL,
# distinct, separately-selectable model — selectable in the TUI and CLI. His
# Nous catalog (>25 models) gets truncated by _build_nous_featured_set
# (api/config.py): the curated flagship set carries mimo-v2.5-PRO so that's a
# rendered <option>, while the bare mimo-v2.5 lands in `extra_models` and is
# NOT a <select> option. The pre-fix /model resolved only against sel.options,
# so the bare model was unreachable and the #3437 tier-guard fired a misleading
# "did you mean mimo-v2.5-pro?" toast on a model that actually exists.
#
# The fix: /model resolves against the FULL catalog (featured `models` +
# `extra_models`) — the same complete list the CLI and autocomplete use. With
# the bare model in the candidate set, exact/shortest-match wins and lands on
# mimo-v2.5, while mimo-v2.5-pro stays reachable by its full name.


class TestFullCatalogResolution:
    """The bare model lives in extra_models (truncated catalog tail). /model must
    still resolve to it end-to-end — this is garyd9's exact reported case."""

    # A realistic truncated Nous catalog: -pro is featured (rendered <option>),
    # bare mimo-v2.5 is in the extras tail (absent from sel.options).
    GROUPS = [{
        "provider": "Nous Portal (3 of 6)",
        "provider_id": "nous",
        "models": [
            {"id": "@nous:anthropic/claude-opus-4.8", "label": "Claude Opus 4.8 (via Nous)"},
            {"id": "@nous:openai/gpt-5.5", "label": "GPT-5.5 (via Nous)"},
            {"id": "@nous:xiaomi/mimo-v2.5-pro", "label": "Mimo V2.5 Pro (via Nous)"},
        ],
        "extra_models": [
            {"id": "@nous:xiaomi/mimo-v2.5", "label": "Mimo V2.5 (via Nous)"},
            {"id": "@nous:xiaomi/mimo-v2-pro", "label": "Mimo V2 Pro (via Nous)"},
            {"id": "@nous:deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash (via Nous)"},
        ],
    }]
    # What the picker actually renders (featured only).
    SEL = [
        "@nous:anthropic/claude-opus-4.8",
        "@nous:openai/gpt-5.5",
        "@nous:xiaomi/mimo-v2.5-pro",
    ]

    def test_candidate_set_includes_extras(self, catalog_driver):
        out = _resolve(catalog_driver, "mimo-v2.5", self.GROUPS, self.SEL)
        # 3 featured + 3 extras = 6; the bare model must be reachable.
        assert out["candidateCount"] == 6

    def test_bare_mimo_v25_resolves_from_extras(self, catalog_driver):
        """garyd9's exact case: /model mimo-v2.5 lands on the bare model, NOT -pro."""
        out = _resolve(catalog_driver, "mimo-v2.5", self.GROUPS, self.SEL)
        assert out["match"] == "@nous:xiaomi/mimo-v2.5", (
            f"bare mimo-v2.5 must resolve to itself from the extras tail, got {out['match']!r}"
        )
        assert out["provider"] == "nous", "resolved model must carry its provider for routing"

    def test_qualified_bare_resolves_from_extras(self, catalog_driver):
        out = _resolve(catalog_driver, "xiaomi/mimo-v2.5", self.GROUPS, self.SEL)
        assert out["match"] == "@nous:xiaomi/mimo-v2.5"
        assert out["provider"] == "nous"

    def test_pro_still_reachable_by_full_name(self, catalog_driver):
        out = _resolve(catalog_driver, "mimo-v2.5-pro", self.GROUPS, self.SEL)
        assert out["match"] == "@nous:xiaomi/mimo-v2.5-pro"

    def test_qualified_pro_still_reachable(self, catalog_driver):
        out = _resolve(catalog_driver, "xiaomi/mimo-v2.5-pro", self.GROUPS, self.SEL)
        assert out["match"] == "@nous:xiaomi/mimo-v2.5-pro"

    def test_extras_only_model_reachable(self, catalog_driver):
        """A non-versioned extras-only model (deepseek-v4-flash) is also reachable."""
        out = _resolve(catalog_driver, "deepseek-v4-flash", self.GROUPS, self.SEL)
        assert out["match"] == "@nous:deepseek/deepseek-v4-flash"

    def test_incomplete_version_continues_to_longer(self, catalog_driver):
        """mimo-v2 is an incomplete version; resolving to the shortest continuation
        (bare mimo-v2.5) is correct now that it's in the catalog."""
        out = _resolve(catalog_driver, "mimo-v2", self.GROUPS, self.SEL)
        assert out["match"] == "@nous:xiaomi/mimo-v2.5"

    def test_truly_absent_bare_still_no_snap_and_suggests(self, catalog_driver):
        """When the bare model is genuinely absent (only -pro in BOTH featured and
        extras), the tier-guard still fires: no snap, and -pro is suggested."""
        groups = [{
            "provider": "Nous Portal",
            "provider_id": "nous",
            "models": [{"id": "@nous:xiaomi/mimo-v2.5-pro", "label": "Mimo V2.5 Pro"}],
            "extra_models": [],
        }]
        out = _resolve(catalog_driver, "mimo-v2.5", groups, ["@nous:xiaomi/mimo-v2.5-pro"])
        assert out["match"] is None, "must not snap to -pro when bare model truly absent"
        assert out["suggestion"] == "@nous:xiaomi/mimo-v2.5-pro"

    def test_falls_back_to_sel_options_when_no_groups(self, catalog_driver):
        """If the /api/models fetch failed (no groups), candidates fall back to the
        live <select> options so /model still works in a degraded state."""
        out = _resolve(catalog_driver, "gpt-5.5", None, self.SEL)
        assert out["candidateCount"] == 3
        assert out["match"] == "@nous:openai/gpt-5.5"


class TestCmdModelFullCatalogWiring:
    """Source-level guards: cmdModel must build candidates from the catalog and
    inject the option before selecting, so an extras-only winner is honored."""

    # Uses the module-scoped commands_src fixture above.
    def test_builds_candidates_from_catalog_groups(self, commands_src):
        assert "_buildModelCandidates(sel,modelsData&&modelsData.groups)" in commands_src

    def test_helper_reads_models_and_extra_models(self, commands_src):
        idx = commands_src.find("function _buildModelCandidates")
        assert idx != -1
        window = commands_src[idx:idx + 900]
        assert "g.models" in window and "g.extra_models" in window, (
            "the candidate builder must read both featured models and the extras tail"
        )

    def test_resolves_against_candidates_not_sel_options(self, commands_src):
        # The fuzzy fallbacks must run over the full candidate set, not sel.options.
        assert "_bestModelMatch(candidates,q)" in commands_src
        assert "_bestModelMatch(sel.options,q)" not in commands_src

    def test_injects_option_for_extras_only_winner(self, commands_src):
        # An extras-only match isn't a rendered <option>; cmdModel must inject it
        # (with provider) before selecting, or sel.value=match silently no-ops.
        assert "_ensureModelOptionInDropdown(match,sel,providerMap[match]||null)" in commands_src
