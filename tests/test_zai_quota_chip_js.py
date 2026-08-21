"""Behavioral tests for the Z.AI quota chip JS (Node one-shot harness).

Extracts the chip formatter functions from static/ui.js and executes them in
Node, asserting observable behavior (window selection, strict numeric
coercion, peak marker) rather than source-string presence. Precedent for the
Node harness pattern: tests/test_real_steer.py:379-425.

Run via ./scripts/test.sh per AGENTS.md.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

UI_JS = "static/ui.js"


def _extract_chip_functions() -> str:
    source = open(UI_JS, encoding="utf-8").read()
    names = ["_formatQuotaMoneyShort", "_formatQuotaPercentShort",
             "_finiteQuotaNumber", "_providerQuotaIndicatorText"]
    chunks = []
    for name in names:
        match = re.search(
            rf"(?<![A-Za-z0-9_])(function\s+{name}\s*\([^)]*\)\s*\{{)",
            source,
        )
        if not match:
            pytest.fail(f"{name} not found in {UI_JS}")
        start = match.start(1)
        depth = 0
        for i in range(start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    chunks.append(source[start:i + 1])
                    break
        else:
            pytest.fail(f"could not balance braces for {name}")
    return "\n".join(chunks)


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"node harness failed: {result.stderr[:800]}")
    import json as _json
    return _json.loads(result.stdout)


def _harness(call: str) -> dict:
    body = _extract_chip_functions()
    return _run_node(
        "const window=undefined,document=undefined;\n"
        + body
        + "\nconsole.log(JSON.stringify(" + call + "));"
    )


def test_finite_quota_number_strict_coercion():
    out = _harness(
        "(()=>{const cases=[null,undefined,'','   ',0,91,'91','abc',NaN,Infinity,-5,3.5];"
        "return cases.map(v=>_finiteQuotaNumber(v));})()"
    )
    # 0 and -5 ARE finite numbers: 0% remaining is real data (exhausted quota)
    # and must not be dropped by the coercion helper. Positivity/limits are
    # applied at call sites (multiplier >0, percent clamp).
    assert out == [None, None, None, None, 0, 91, 91, None, None, None, -5, 3.5]


def test_selection_prefers_5_hour_over_monthly():
    out = _harness(
        "(()=>{const status={status:'available',display_name:'Z.AI / GLM',"
        "message:'Z.AI / GLM quota loaded. Off-peak 1x (weekdays 14:00-18:00 UTC+8); switches Wed 04:00 MDT.',"
        "account_limits:{windows:["
        "{label:'Monthly',remaining_percent:60},"
        "{label:'5-hour',remaining_percent:91}]}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert out["label"] == "91%"


def test_selection_5_hour_preferred_even_when_monthly_resets_sooner():
    # Server priority-sort already orders 5-hour first; JS must still prefer
    # the labeled 5-hour window even if ordering were adversarial (null dup
    # first, valid dup second).
    out = _harness(
        "(()=>{const status={status:'available',display_name:'X',message:'m',"
        "account_limits:{windows:["
        "{label:'5-hour',remaining_percent:null},"
        "{label:'Monthly',remaining_percent:42},"
        "{label:'5-hour',remaining_percent:88}]}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert out["label"] == "88%"


def test_selection_null_percent_falls_through_not_zero():
    out = _harness(
        "(()=>{const status={status:'available',display_name:'X',message:'m',"
        "account_limits:{windows:["
        "{label:'5-hour',remaining_percent:null},"
        "{label:'Weekly',remaining_percent:73}]}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert out["label"] == "73%"


def test_empty_windows_returns_null():
    out = _harness(
        "(()=>{return _providerQuotaIndicatorText({status:'available',"
        "display_name:'X',message:'m',account_limits:{windows:[]}});})()"
    )
    assert out is None


def test_peak_marker_in_label_and_summary_once_in_title():
    out = _harness(
        "(()=>{const status={status:'available',display_name:'Z.AI / GLM',"
        "message:'Z.AI / GLM quota loaded. Peak 3x (weekdays 14:00-18:00 UTC+8); switches Wed 04:00 MDT.',"
        "peak:{is_peak:true,multiplier:3,source:'plan_docs'},"
        "account_limits:{windows:[{label:'5-hour',remaining_percent:91}]}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert out["label"] == "91% ⚡3×"
    assert out["title"].count("switches") == 1  # summary appears exactly once (in message)
    assert "91% remaining" in out["title"]


def test_peak_marker_absent_when_off_peak():
    out = _harness(
        "(()=>{const status={status:'available',display_name:'Z.AI / GLM',"
        "message:'Z.AI / GLM quota loaded. Off-peak 1x.',"
        "peak:{is_peak:false,multiplier:1,source:'plan_docs'},"
        "account_limits:{windows:[{label:'5-hour',remaining_percent:91}]}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert "⚡" not in out["label"]


def test_no_peak_field_for_other_providers():
    out = _harness(
        "(()=>{const status={status:'available',display_name:'OpenRouter',"
        "message:'credits',"
        "quota:{limit_remaining:1.24,usage:0.5,limit:5}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert out["label"] == "$1.24"


def test_malformed_multiplier_safe_fallback():
    for bad in ("null", "''", "'   '", "0", "-1", "NaN", "Infinity", "'abc'"):
        out = _harness(
            "(()=>{const status={status:'available',display_name:'X',message:'m',"
            f"peak:{{is_peak:true,multiplier:{bad}}},"
            "account_limits:{windows:[{label:'5-hour',remaining_percent:91}]}};"
            "return _providerQuotaIndicatorText(status);})()"
        )
        assert out["label"] == "91% ⚡3×", f"multiplier={bad!r} produced {out['label']!r}"


def test_numeric_string_multiplier_accepted():
    out = _harness(
        "(()=>{const status={status:'available',display_name:'X',message:'m',"
        "peak:{is_peak:true,multiplier:'2'},"
        "account_limits:{windows:[{label:'5-hour',remaining_percent:91}]}};"
        "return _providerQuotaIndicatorText(status);})()"
    )
    assert out["label"] == "91% ⚡2×"
