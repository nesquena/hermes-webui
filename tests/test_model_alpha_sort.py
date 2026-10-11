"""新增测试：provider 组内模型按字母排序（2026-09-05 用户需求）。

覆盖静态 catalog 路径的组内排序行为——模型列表不该保持 config/live
probe 的插入顺序，而应按 id 大小写不敏感字母序排列。
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

import api.config as config


REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


_FRONTEND_SORT_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}

eval([
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_modelPickerSortValue',
  '_compareModelPickerEntries',
  '_sortModelPickerEntries',
].map(extractFunction).join('\n'));

const entries = [
  {id: 'jd-deepseek-v4-flash-0731', providerId: 'custom:newapi'},
  {id: 'sn-kimi-k3', providerId: 'custom:newapi'},
  {id: 'hf-deepseek-v4-flash', providerId: 'custom:newapi'},
  {id: 'sub-glm-5.3', providerId: 'custom:newapi'},
  {id: 'ab-glm-5.3-flash', providerId: 'custom:newapi'},
  {id: '@custom:newapi:MiniMax-M3', providerId: 'custom:newapi'},
  {id: '@custom:newapi:model-a:free', providerId: 'custom:newapi'},
];
process.stdout.write(JSON.stringify(_sortModelPickerEntries(entries).map(entry => entry.id)));
'''


def _install_fake_hermes_cli(monkeypatch):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    config.invalidate_models_cache()


@pytest.fixture(autouse=True)
def _isolate_cache():
    _saved_cfg = copy.deepcopy(config.cfg)
    _saved_paths = {
        name: getattr(config, name)
        for name in ("_cfg_path", "_cfg_mtime", "_cfg_fingerprint")
    }
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    try:
        if isinstance(config.cfg, dict):
            config.cfg.clear()
            config.cfg.update(_saved_cfg)
        for name, value in _saved_paths.items():
            setattr(config, name, value)
    except Exception:
        pass


def _setup_config(tmp_path, monkeypatch, yaml_text):
    _install_fake_hermes_cli(monkeypatch)

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)

    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_path)

    config.reload_config()


class TestInGroupModelAlphabetical:
    def test_static_catalog_sorts_models_within_group(self, tmp_path, monkeypatch):
        """组内模型按 id 忽略大小写字母排序，而非 config 插入顺序。"""
        _setup_config(
            tmp_path,
            monkeypatch,
            (
                "model:\n"
                "  provider: custom:mylocal\n"
                "  default: z-last\n"
                "custom_providers:\n"
                "  - name: MyLocal\n"
                "    base_url: http://localhost:8080/v1\n"
                "    api_key: local-key\n"
                "    models:\n"
                "      - z-last\n"
                "      - m-mid\n"
                "      - a-first\n"
            ),
        )

        result = config.get_available_models(force_refresh=True)
        groups = result.get("groups", [])
        custom = next((g for g in groups if g.get("provider_id") == "custom:mylocal"), None)
        assert custom is not None, f"custom:mylocal group missing: {[g.get('provider_id') for g in groups]}"
        ids = [m.get("id") for m in custom.get("models", [])]
        assert ids == ["a-first", "m-mid", "z-last"], (
            f"expected alphabetical model order, got {ids}"
        )

    def test_static_catalog_natural_numeric_order(self, tmp_path, monkeypatch):
        """catalog 路径必须用自然数字序：model-2 在 model-10 前。

        纯字典序会输出 model-10 在前，与前端 localeCompare(numeric:true)
        相反 —— 同一组模型在 API 与 UI 出现两个顺序（review 反馈 #7528）。
        直接断言 `_static_models_catalog_without_live_probes` 的输出，
        因为 get_available_models 内部另有组装路径（live rebuild），
        无法覆盖静态 catalog 的排序分支。
        """
        _setup_config(
            tmp_path,
            monkeypatch,
            (
                "model:\n"
                "  provider: custom:mylocal\n"
                "  default: model-9\n"
                "custom_providers:\n"
                "  - name: MyLocal\n"
                "    base_url: http://localhost:8080/v1\n"
                "    api_key: local-key\n"
                "    models:\n"
                "      - model-10\n"
                "      - model-2\n"
                "      - model-9\n"
            ),
        )

        result = config._static_models_catalog_without_live_probes()
        groups = result.get("groups", [])
        custom = next((g for g in groups if g.get("provider_id") == "custom:mylocal"), None)
        assert custom is not None, f"custom:mylocal group missing: {[g.get('provider_id') for g in groups]}"
        ids = [m.get("id") for m in custom.get("models", [])]
        assert ids == ["model-2", "model-9", "model-10"], (
            f"expected natural numeric order, got {ids}"
        )

    def test_get_available_models_natural_numeric_order(self, tmp_path, monkeypatch):
        """get_available_models 内部组装路径（live rebuild）同样自然序。"""
        _setup_config(
            tmp_path,
            monkeypatch,
            (
                "model:\n"
                "  provider: custom:mylocal\n"
                "  default: model-9\n"
                "custom_providers:\n"
                "  - name: MyLocal\n"
                "    base_url: http://localhost:8080/v1\n"
                "    api_key: local-key\n"
                "    models:\n"
                "      - model-10\n"
                "      - model-2\n"
                "      - model-9\n"
            ),
        )

        result = config.get_available_models(force_refresh=True)
        groups = result.get("groups", [])
        custom = next((g for g in groups if g.get("provider_id") == "custom:mylocal"), None)
        assert custom is not None, f"custom:mylocal group missing: {[g.get('provider_id') for g in groups]}"
        ids = [m.get("id") for m in custom.get("models", [])]
        assert ids == ["model-2", "model-9", "model-10"], (
            f"expected natural numeric order, got {ids}"
        )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_frontend_picker_sort_ignores_provider_routing_prefix(tmp_path):
    """自绘下拉框按模型 ID 排序，不按 @provider: 路由前缀或异步到达顺序排序。"""
    driver = tmp_path / "frontend_sort_driver.js"
    driver.write_text(_FRONTEND_SORT_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "ab-glm-5.3-flash",
        "hf-deepseek-v4-flash",
        "jd-deepseek-v4-flash-0731",
        "@custom:newapi:MiniMax-M3",
        "@custom:newapi:model-a:free",
        "sn-kimi-k3",
        "sub-glm-5.3",
    ]


# Node driver exercising the frontend comparator on natural-order ids.
_NATURAL_ORDER_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}

eval([
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_modelPickerSortValue',
  '_compareModelPickerEntries',
  '_sortModelPickerEntries',
].map(extractFunction).join('\n'));

const ids = ['model-10', 'model-2', 'MODEL-1', 'model-9', 'model-10b'];
const sorted = _sortModelPickerEntries(ids.map(id => ({id: id}))).map(e => e.id);
process.stdout.write(JSON.stringify(sorted));
'''


def test_natural_numeric_order_matches_across_boundaries(tmp_path):
    """model-2 / model-10 在 API 和 UI 两个边界顺序一致（review 反馈）。

    前端比较器用 localeCompare(numeric:true)（自然数字序），后端此前用纯
    字典序 —— model-2 与 model-10 在 API 与 UI 会得到相反顺序。两边都应
    输出 MODEL-1, model-2, model-9, model-10, model-10b。
    """
    from api.config import _natural_model_id_key

    ids = ["model-10", "model-2", "MODEL-1", "model-9", "model-10b"]
    backend_order = sorted(ids, key=lambda m: _natural_model_id_key({"id": m}))

    driver = tmp_path / "natural_order_driver.js"
    driver.write_text(_NATURAL_ORDER_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    frontend_order = json.loads(result.stdout)

    assert backend_order == frontend_order, (
        f"API/UI order divergence: backend={backend_order} frontend={frontend_order}"
    )
    assert backend_order == ["MODEL-1", "model-2", "model-9", "model-10", "model-10b"]


def test_configured_section_rank_preserved_before_alpha_sort():
    """Configured 区排序必须保留 primary/fallback 语义 rank（review 反馈）。

    纯 ID 字母排序会把字母序靠前的 fallback 顶到 primary 上面，并让
    _configuredRank 沦为死代码。排序比较器必须先比 rank、同 rank 内才比
    字母序，且 _configuredRank 必须仍被引用。
    """
    ui = (REPO / "static" / "ui.js").read_text()

    # _configuredRank is defined and referenced by the Configured sort
    # (definition + two call sites inside the comparator).
    assert ui.count("_configuredRank") >= 3, (
        "_configuredRank must be defined and used by the Configured sort"
    )

    m = re.search(
        r"const configuredModels=\[\.\.\.configuredBySemanticKey\.values\(\)\]\.sort\(\(a,b\)=>\{",
        ui,
    )
    assert m is not None, "Configured section must sort via rank-first comparator"
    snippet = ui[m.start():m.start() + 400]
    assert "_configuredRank(a.badge)-_configuredRank(b.badge)" in snippet, (
        "comparator must compare semantic rank first"
    )
    assert "return _compareModelPickerEntries(a,b);" in snippet, (
        "alpha order must be the tie-breaker within the same rank"
    )


# Node driver exercising the shared locale-independent comparator contract
# (review round-3 blocker 3: no localeCompare, code-point based, matching
# api/config.py `_natural_model_id_key`).
_CONTRACT_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1; i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}
eval([
  '_modelPickerContractRuns','_modelPickerCompareRuns','_modelPickerCompareContract',
  '_modelPickerSortableId','_modelPickerSortValue','_compareModelPickerEntries','_sortModelPickerEntries',
].map(extractFunction).join('\n'));
const vectors = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(_sortModelPickerEntries(vectors.map(id => ({id}))).map(e => e.id)));
'''


def _run_contract_driver(tmp_path, ids):
    """Run the JS contract comparator over ``ids`` and return the sorted ids."""
    driver = tmp_path / "contract_driver.js"
    driver.write_text(_CONTRACT_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js"), json.dumps(ids)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_contract_parity_across_boundaries(tmp_path):
    """Python and JS sort vectors identically under the #7528 round-3
    locale-independent contract (no localeCompare, no browser collation).

    Vectors cover numeric runs, leading zeros, punctuation, Unicode,
    case-folding, provider-routing @ prefixes, slashes and long digit runs.
    """
    from api.config import _natural_model_id_key

    ids = [
        "model-10", "model-2", "MODEL-1", "model-9", "model-10b",
        "007-model", "7-model", "model-007", "model-7",
        "a-2-b", "a-10-b", "a-2-b-2", "a-2-b-10",
        "glm-5.3", "glm-5.10", "glm-5.3-flash", "glm-5.30",
        "v1.0", "v1.0.1", "v1.0.0", "v2.0",
        "ä-model", "z-model", "A-model", "à-model", "Ω-model",
        "model_2", "model-2", "model.2", "model 2", "model!2",
        "@custom:abc:z-model", "@custom:abc:a-model", "plain-z",
        "foo/bar", "foo/baz", "foo/bar-2", "foo/bar-10",
    ]
    backend_order = sorted(ids, key=lambda m: _natural_model_id_key({"id": m}))
    frontend_order = _run_contract_driver(tmp_path, ids)
    assert backend_order == frontend_order, (
        f"contract divergence: backend={backend_order} frontend={frontend_order}"
    )
    # Spot-check natural numeric order survived.
    assert backend_order.index("model-2") < backend_order.index("model-10")
    assert backend_order.index("v1.0.0") < backend_order.index("v1.0.1") < backend_order.index("v2.0")


def test_contract_handles_leading_zeros_and_codepoint_folding(tmp_path):
    """Leading-zero digit runs tie-break deterministically; Unicode text runs
    compare by code point on both boundaries."""
    from api.config import _natural_model_id_key

    ids = ["007-model", "7-model", "model-007", "model-7", "Ω-model", "z-model"]
    backend_order = sorted(ids, key=lambda m: _natural_model_id_key({"id": m}))
    frontend_order = _run_contract_driver(tmp_path, ids)
    assert backend_order == frontend_order, (
        f"leading-zero/unicode divergence: backend={backend_order} frontend={frontend_order}"
    )


# ── #7528 re-gate: the mixed ASCII-digit / Unicode-digit boundary ───────────
#
# The round-6 re-gate's finding 1. ``model-1٢`` and ``model-1a`` are TWO
# DISTINCT ids, so both survive the picker's dedup and both orderings are
# actually visible. Their only difference is what follows the shared ASCII
# digit run ``1``: an Arabic-Indic digit (U+0662) or the letter ``a``.
#
# Python's ``str.isdigit()`` is Unicode-aware and says ``٢`` is a digit; the
# JS contract classifies with ``/^\d+$/`` (ASCII only) and says it is TEXT.
# A digit run sorts before a text run, so the two runtimes disagreed about
# which of these ids came first — an actual order reversal in the picker, not a
# theoretical one.

_ARABIC_INDIC_TWO = "model-1\u0662"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("reverse", [False, True], ids=["as-written", "reversed"])
def test_unicode_digit_is_text_on_both_boundaries(tmp_path, reverse):
    """``model-1٢`` vs ``model-1a`` must order identically in Python and JS.

    Both INPUT ORDERS are exercised (``reverse``), because a single order can
    hide the disagreement: a stable sort keeps whichever pair came first, so
    only a full sort of both directions makes the divergence visible.

    The discriminating property is ``a`` before ``٢``: ASCII ``a`` is U+0061
    and Arabic-Indic ``٢`` is U+0662, so as TEXT runs they compare by code
    point and ``a`` wins. Both boundaries must now agree on that.
    """
    from api.config import _natural_model_id_key

    ids = [_ARABIC_INDIC_TWO, "model-1a"]
    if reverse:
        ids = list(reversed(ids))

    backend_order = sorted(ids, key=lambda m: _natural_model_id_key({"id": m}))
    frontend_order = _run_contract_driver(tmp_path, ids)

    assert backend_order == ["model-1a", _ARABIC_INDIC_TWO], (
        "the Python comparator must treat a non-ASCII decimal digit as text, "
        f"so 'model-1a' (U+0061) precedes 'model-1\\u0662' (U+0662): {backend_order}"
    )
    assert frontend_order == backend_order, (
        f"api/picker divergence on the Unicode-digit boundary: "
        f"backend={backend_order} frontend={frontend_order}"
    )


def test_python_comparator_classifies_only_ascii_digits_as_digit_runs():
    """The classification predicate itself is ASCII-only, by construction.

    Guards the predicate rather than the ordering: ``str.isdigit()`` accepts
    non-ASCII decimal digits, so a regression here would silently reintroduce
    the boundary the ordering test above pins — including for runs the
    tokenizer would never produce today.
    """
    from api.config import _NaturalModelKey

    for ascii_digit in ("0", "7", "007", "000"):
        assert _NaturalModelKey._is_digit_run(ascii_digit) is True
    for unicode_digit in ("\u0662", "\u0660\u0669", "\uff10", "\u09ea"):
        assert _NaturalModelKey._is_digit_run(unicode_digit) is False, (
            "a non-ASCII decimal digit must be classified as a TEXT run, "
            "matching the JS contract's ASCII /^\\d+$/"
        )
    assert _NaturalModelKey._is_digit_run("") is False
    assert _NaturalModelKey._is_digit_run("a") is False


# ── #7528 re-gate: the provider-aware tie is left EQUAL, by contract ─────────
#
# A bare id and its routed twin (``a-model`` vs ``@custom:abc:a-model``) strip
# to the same routing-stripped key, so they compare EQUAL here and Python's
# stable sort preserves input order. The picker's ``_compareModelPickerEntries``
# has a raw-id tie-break and sorts routed first — now verified by running the
# real JS comparator rather than by comparing Python strings.
#
# This test PINS the deliberate asymmetry instead of "fixing" it. Adding a raw
# tie-break to the API key was tried and is WRONG: it flips the API order for
# that pair, and the existing PR test
# ``test_natural_model_id_key_threads_provider_id_for_named_custom`` asserts the
# bare-first order that the maintainer asked for in the round-4 review. The pair
# is deduped away by the final picker, so neither order is visible as a row.

_TIE_BARE = "a-model"
_TIE_ROUTED = "@custom:abc:a-model"


def test_provider_aware_tie_is_left_equal_by_design():
    """The API must leave bare/routed ties EQUAL, not tie-break them.

    Asserts three things separately, so a future change cannot quietly move
    any one of them:

    * the two ids really do share a stripped key (so the tie is real);
    * the API key leaves them equal -- equal keys mean stable input order,
      which is the contract the maintainer's test pins;
    * the frontend comparator DOES break the tie (routed-first), i.e. the
      divergence is documented, not ignored.
    """
    from api.config import _natural_model_id_key, _natural_model_routing_stripped

    provider = "custom:abc"
    bare_key = _natural_model_id_key({"id": _TIE_BARE}, provider)
    routed_key = _natural_model_id_key({"id": _TIE_ROUTED}, provider)

    # 1. the tie is real -- both strip to the same value.
    assert _natural_model_routing_stripped(_TIE_BARE, provider) == _TIE_BARE
    assert _natural_model_routing_stripped(_TIE_ROUTED, provider) == _TIE_BARE

    # 2. the API leaves them equal (stable sort => input order preserved).
    assert bare_key == routed_key
    assert bare_key is not routed_key  # distinct objects that compare equal

    # 3. the frontend's own tie-break is recorded, not silently ignored.
    # The claim below used to be a plain Python string compare
    # (`'@custom:abc:a-model' < 'a-model'`), which asserts Python's opinion
    # rather than the shipped comparator's. It happens to agree on the direction
    # (routed first), but it would keep passing even if the JS tie-break flipped,
    # so run the REAL comparator and pin both directions.
    assert _frontend_compares_routed_first() is True
    assert _frontend_compares_bare_first() is False


# ── Frontend comparator probe (#7528 round-5 maintainer item) ───────────────
# `test_provider_aware_tie_is_left_equal_by_design` used to assert the JS
# tie-break with a Python string compare (`'@custom:abc:a-model' < 'a-model'`).
# That documents Python's opinion, not the browser's, so the tie-policy finding
# stayed open. This driver runs the production `_compareModelPickerEntries`.
_TIE_BREAK_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}

eval([
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_modelPickerSortValue',
  '_compareModelPickerEntries',
].map(extractFunction).join('\n'));

// Tie pair: '@custom:abc:a-model' (routed) vs 'a-model' (bare).
// Pass argv[3]/argv[4] straight through with the provider hint so the probe
// exercises the same raw-id tie-break path as production.
console.log(JSON.stringify({ cmp: _compareModelPickerEntries(process.argv[3], process.argv[4], 'custom:abc') }));
'''


def _frontend_compares_routed_first() -> bool:
    """Run the production JS comparator on the tie pair and report its verdict."""
    if NODE is None:
        pytest.skip("node not on PATH")
    return _run_frontend_tie_comparator(_TIE_ROUTED, _TIE_BARE) < 0


def _frontend_compares_bare_first() -> bool:
    """The mirror question, pinned so the tie-break direction cannot flip quietly."""
    if NODE is None:
        pytest.skip("node not on PATH")
    return _run_frontend_tie_comparator(_TIE_BARE, _TIE_ROUTED) < 0


def _run_frontend_tie_comparator(first: str, second: str) -> int:
    """Call the shipped `_compareModelPickerEntries(first, second)` and return it."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(_TIE_BREAK_DRIVER)
        driver = Path(fh.name)
    try:
        result = subprocess.run(
            [NODE, str(driver), str(REPO / "static" / "ui.js"), first, second],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return int(json.loads(result.stdout)["cmp"])
    finally:
        driver.unlink(missing_ok=True)


# Node driver that reproduces the select-selection reset. It builds a REAL
# minimal DOM (select + optgroups + options), selects one option, runs the
# production _sortModelPickerOptions, and reports what the select still has
# selected. The driver models the browser contract that matters: moving an
# <option> node to a different position inside its <select> clears the select's
# selection unless it is restored by object.
_SELECT_RESTORE_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}

eval([
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_modelPickerSortValue',
  '_compareModelPickerEntries',
  '_sortModelPickerEntries',
  '_sortModelPickerOptions',
].map(extractFunction).join('\n'));

// ── Minimal DOM with the browser's <select> selection contract ────────────
function makeOption(value, text) {
  return {tagName: 'OPTION', value: value, text: text, selected: false,
          parentElement: null, parentNode: null};
}
function makeSelect() {
  const sel = {
    tagName: 'SELECT',
    options: [],
    _selected: null,
    get selectedIndex() {
      return this._selected ? this.options.indexOf(this._selected) : -1;
    },
    set selectedIndex(i) {
      // Browsers map selectedIndex -> whichever option sits there, and setting
      // it clears the previous selection (index -1 clears outright).
      if (this._selected) this._selected.selected = false;
      this._selected = (i >= 0 && i < this.options.length) ? this.options[i] : null;
      if (this._selected) this._selected.selected = true;
    },
    get selectedOptions() { return this._selected ? [this._selected] : []; },
    replaceChildren(...nodes) {
      for (const n of this.children) n.parentElement = n.parentNode = null;
      this.children = [];
      this._selected = null;          // node re-parenting drops the selection
      for (const n of nodes) this._append(n);
    },
    children: [],
    _append(n) {
      this.children.push(n);
      this.options.push(n);
      n.parentElement = n.parentNode = this;
    },
    appendChild(n) { this._append(n); },
  };
  return sel;
}

// NOTE: the optgroup's `children` must be an HTMLCollection-like object, NOT a
// plain Array. A plain Array drives the driver-mode fallback branch in
// _sortModelPickerOptions and never exercises the real replaceChildren() path
// that cleared the browser selection — a plain Array makes this test pass even
// against the buggy code (verified: green on the pre-fix function).
class FakeHTMLCollection {
  constructor(items) { this._items = items.slice(); }
  get length() { return this._items.length; }
  item(i) { return this._items[i]; }
  [Symbol.iterator]() { return this._items[Symbol.iterator](); }
  slice() { return this._items.slice(); }
  indexOf(o) { return this._items.indexOf(o); }
}

const select = makeSelect();
const groupA = {tagName: 'OPTGROUP', dataset: {provider: 'custom:newapi'},
                get children() { return new FakeHTMLCollection(this._children); },
                _children: [], parentElement: select, parentNode: select,
                appendChild(n) {
                  // Browser contract: appending an existing node MOVES it,
                  // which re-parents and (on a <select>) clears the selection.
                  const old = this._children.indexOf(n);
                  if (old >= 0) this._children.splice(old, 1);
                  this._children.push(n);
                  n.parentElement = n.parentNode = this;
                  const si = select.options.indexOf(n);
                  if (select._selected === n && si >= 0) { /* moved, still ours */ }
                  return n;
                },
                removeChild(n) {
                  const i = this._children.indexOf(n);
                  if (i >= 0) this._children.splice(i, 1);
                  const oi = select.options.indexOf(n);
                  if (oi >= 0) select.options.splice(oi, 1);
                  return n;
                },
                // OptGroup IS a ParentNode in real browsers, so it has
                // replaceChildren too. Without this the production code falls
                // through to the driver-only fallbacks and the test never
                // exercises the path that cleared the browser selection.
                replaceChildren(...nodes) {
                  for (const n of this._children) n.parentElement = n.parentNode = null;
                  this._children = [];
                  select._selected = null;   // re-parenting clears the select
                  for (const n of nodes) this.appendChild(n);
                },
                closest(sel) { return sel === 'select' ? select : null; }};
select.children.push(groupA);
for (const [value, text] of [
  ['@custom:newapi:zulu', 'zulu'],
  ['@custom:newapi:middle', 'middle'],
  ['@custom:newapi:alpha', 'alpha'],
]) {
  const opt = makeOption(value, text);
  select._append(opt);
  groupA._children.push(opt);
}

// The user has "middle" selected (session model), then triggers a re-sort —
// either the "Show all" path or a live catalog refresh that reorders the group.
select.selectedIndex = select.options.indexOf(
  select.options.find(o => o.value === '@custom:newapi:middle'));

_sortModelPickerOptions(groupA);

process.stdout.write(JSON.stringify({
  order: Array.from(groupA.children).map(o => o.value),
  selectedValue: select.selectedOptions.length
    ? select.selectedOptions[0].value : null,
}));
'''


def test_sort_model_picker_options_preserves_native_selection(tmp_path):
    """Reordering <option> nodes must not reset the <select>'s selection.

    Regression for the maintainer's 2026-10-07 CORE finding on #7528:
    `_sortModelPickerOptions` called `replaceChildren()`, which re-parents every
    option node and clears the select's selection. After "Show all" (or any live
    catalog refresh that reorders the group) the picker snapped back to the first
    option, and the subsequent change handler hit the unchanged-selection early
    return — so picking another model left the session on the old one.
    """
    if not NODE:
        pytest.skip("node not available")
    driver = tmp_path / "select_restore_driver.js"
    driver.write_text(_SELECT_RESTORE_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # Sorting really happened (alpha order), so the selection had to survive it.
    assert payload["order"] == [
        "@custom:newapi:alpha",
        "@custom:newapi:middle",
        "@custom:newapi:zulu",
    ]
    assert payload["selectedValue"] == "@custom:newapi:middle"


# ── Cross-group selection restore (#7528 round-5 maintainer CORE) ───────────
# The single-group driver above can only catch a restore that looks in the WRONG
# PLACE when the selection is inside the group being sorted. The maintainer's
# remaining [CORE] is the other half: the selection lives in a DIFFERENT
# optgroup. Sorting provider B re-orders B's own <optgroup> and leaves A's
# options untouched, so the captured option is still in `select.options` but is
# no longer in `group.children` — a restore that checks only the sorted group's
# children concludes the option was removed and sets selectedIndex = -1.
#
# Reproduced by the maintainer in Chromium at 1280px and 390px: master retains
# the selection, the PR head cleared it (Settings lost `other-z` on a zero-add
# live refresh; a real HTTP Save left the default on `middle` with dirty state
# cleared).
_CROSS_GROUP_RESTORE_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}

eval([
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_modelPickerSortValue',
  '_compareModelPickerEntries',
  '_sortModelPickerEntries',
  '_sortModelPickerOptions',
].map(extractFunction).join('\n'));

// ── Minimal DOM with the browser's <select> selection contract ────────────
function makeOption(value, text) {
  return {tagName: 'OPTION', value: value, text: text, selected: false,
          parentElement: null, parentNode: null};
}
function makeSelect() {
  const sel = {
    tagName: 'SELECT',
    options: [],
    children: [],
    _selected: null,
    get selectedIndex() {
      return this._selected ? this.options.indexOf(this._selected) : -1;
    },
    set selectedIndex(i) {
      if (this._selected) this._selected.selected = false;
      this._selected = (i >= 0 && i < this.options.length) ? this.options[i] : null;
      if (this._selected) this._selected.selected = true;
    },
    get selectedOptions() { return this._selected ? [this._selected] : []; },
    replaceChildren(...nodes) {
      for (const n of this.children) n.parentElement = n.parentNode = null;
      this.children = [];
      this._selected = null;          // re-parenting drops the selection
      for (const n of nodes) this._append(n);
    },
    _append(n) {
      this.children.push(n);
      this.options.push(n);
      n.parentElement = n.parentNode = this;
    },
    appendChild(n) { this._append(n); },
  };
  return sel;
}

// Same contract as the single-group driver: `children` must be
// HTMLCollection-like, otherwise the production code takes the driver-mode
// fallback branch and never exercises the real replaceChildren() path.
class FakeHTMLCollection {
  constructor(items) { this._items = items.slice(); }
  get length() { return this._items.length; }
  item(i) { return this._items[i]; }
  [Symbol.iterator]() { return this._items[Symbol.iterator](); }
  slice() { return this._items.slice(); }
  indexOf(o) { return this._items.indexOf(o); }
}

function makeGroup(select, provider) {
  const group = {
    tagName: 'OPTGROUP',
    dataset: {provider: provider},
    get children() { return new FakeHTMLCollection(this._children); },
    _children: [],
    parentElement: select, parentNode: select,
    appendChild(n) {
      const old = this._children.indexOf(n);
      if (old >= 0) this._children.splice(old, 1);
      this._children.push(n);
      n.parentElement = n.parentNode = this;
      return n;
    },
    removeChild(n) {
      const i = this._children.indexOf(n);
      if (i >= 0) this._children.splice(i, 1);
      const oi = select.options.indexOf(n);
      if (oi >= 0) select.options.splice(oi, 1);
      return n;
    },
    replaceChildren(...nodes) {
      for (const n of this._children) n.parentElement = n.parentNode = null;
      this._children = [];
      select._selected = null;   // re-parenting clears the select
      for (const n of nodes) this.appendChild(n);
    },
    closest(sel) { return sel === 'select' ? select : null; },
  };
  select.children.push(group);
  return group;
}

function addOption(select, group, value, text) {
  const opt = makeOption(value, text);
  select._append(opt);
  group._children.push(opt);
  opt.parentElement = opt.parentNode = group;
  return opt;
}

const select = makeSelect();
// Two provider groups. `options` is the whole <select>, `children` is per group.
const groupA = makeGroup(select, 'custom:newapi');
const groupB = makeGroup(select, 'custom:other');
addOption(select, groupA, '@custom:newapi:zulu', 'zulu');
addOption(select, groupB, '@custom:other:zulu', 'other-zulu');
addOption(select, groupA, '@custom:newapi:alpha', 'alpha');
addOption(select, groupB, '@custom:other:alpha', 'other-alpha');

// The selection lives in group B. Sorting group A must not disturb it — the
// captured option is still in select.options but is no longer in
// groupA.children, which is exactly what the broken restore missed.
const otherZulu = select.options.find(o => o.value === '@custom:other:zulu');
select.selectedIndex = select.options.indexOf(otherZulu);

_sortModelPickerOptions(groupA);

process.stdout.write(JSON.stringify({
  orderA: Array.from(groupA.children).map(o => o.value),
  orderB: Array.from(groupB.children).map(o => o.value),
  selectedValue: select.selectedOptions.length
    ? select.selectedOptions[0].value : null,
  selectedIndex: select.selectedIndex,
}));
'''


def test_sorting_another_group_keeps_the_selected_option(tmp_path):
    """Sorting provider A must not clear a selection that lives in provider B.

    Regression for the maintainer's 2026-10-08 [CORE] on #7528: the restore
    after a re-sort looked the captured option up only in the SORTED group's
    `children`. Sorting a different provider re-orders that group's own
    <optgroup> and leaves the other groups' options in place, so the selection
    was still in `select.options` but not in `group.children` — the restore
    concluded it had been removed and set `selectedIndex = -1`.

    Verified in Chromium at 1280px and 390px: master retains the selection, the
    PR head cleared it. In Settings a zero-add live refresh lost `other-z`, and
    a real HTTP Save left the default on `middle` with dirty state cleared.
    """
    if not NODE:
        pytest.skip("node not available")
    driver = tmp_path / "cross_group_restore_driver.js"
    driver.write_text(_CROSS_GROUP_RESTORE_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # The sort really happened (alpha order inside group A).
    assert payload["orderA"] == [
        "@custom:newapi:alpha",
        "@custom:newapi:zulu",
    ]
    # Group B was not the sorted one, so its own order is untouched.
    assert payload["orderB"] == [
        "@custom:other:zulu",
        "@custom:other:alpha",
    ]
    # The whole point: the selection in the OTHER group survives.
    assert payload["selectedValue"] == "@custom:other:zulu"
    assert payload["selectedIndex"] == 1


def test_cross_group_restore_preserves_no_selection(tmp_path):
    """`selectedIndex` must stay -1 when nothing was selected before the sort.

    The fix looks the option up in the whole `select.options`; it must not turn
    a genuinely empty selection into an accidental one, nor invent a selection
    where the user had none.
    """
    if not NODE:
        pytest.skip("node not available")
    driver = tmp_path / "cross_group_noselect_driver.js"
    driver.write_text(
        _CROSS_GROUP_RESTORE_DRIVER.replace(
            "const otherZulu = select.options.find(o => o.value === '@custom:other:zulu');\n"
            "select.selectedIndex = select.options.indexOf(otherZulu);",
            "// Nothing selected: leave selectedIndex at its initial -1.",
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["selectedValue"] is None
    assert payload["selectedIndex"] == -1
