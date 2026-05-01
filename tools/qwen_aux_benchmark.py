#!/usr/bin/env python3
"""Small smoke benchmark for local Qwen auxiliary worker.

This is not a full eval suite. It verifies that the downloaded GGUF can run,
return structured English analysis, follow a no-Thai-generation policy, and do
basic code/security review.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

MODEL = Path("/Users/kei/models/qwen3.6-35b-a3b-gguf/Qwen3.6-35B-A3B-Q4_K_M.gguf")
OUT = Path("/Users/kei/kei-jarvis/docs/qwen-auxiliary-benchmark-result.json")
MD = Path("/Users/kei/kei-jarvis/docs/qwen-auxiliary-benchmark.md")

CASES = [
    {
        "id": "policy_role_gate",
        "prompt": """You are evaluating whether a worker is allowed for a task. Context: Qwen is not the default Thai prose, Thai summarization, or Thai training-data generator. Qwen may help with English reasoning, code/security review, structured critique, and benchmark design. Task: Return JSON only with keys pass_fail, role, allowed_tasks, forbidden_tasks, risks. pass_fail should be PASS if the worker is suitable for the allowed English auxiliary role, not for Thai generation.""",
        "checks": {
            "must_contain": ["English reasoning", "code/security", "Thai"],
            "allowed_pass_values": ["PASS", "pass", "Pass"],
        },
    },
    {
        "id": "security_review",
        "prompt": """Review this Python code for security risk. Return JSON only with keys pass_fail, findings, safer_pattern. Code: import subprocess\ndef fetch(url):\n    return subprocess.check_output('curl ' + url, shell=True).decode()""",
        "checks": {
            "must_contain": ["shell", "injection", "subprocess"],
            "allowed_pass_values": ["PASS", "pass", "Pass", "FAIL", "fail", "Fail"],
        },
    },
    {
        "id": "news_filter_logic",
        "prompt": """Classify this English article headline for a legal/AI news filter. Return JSON only with keys category, include, reason, severity. Allowed categories: AI Technology, AI Harms, AI/AGI Governance, Cybersecurity, Disruption, AR/VR/XR. Headline: US regulator opens inquiry into AI chatbot privacy and child safety practices.""",
        "checks": {
            "must_contain": ["AI Harms", "privacy", "child"],
            "allowed_pass_values": [],
        },
    },
]


def extract_json(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start : end + 1])


def run_case(case):
    cmd = [
        "llama-cli",
        "-m", str(MODEL),
        "-ngl", "99",
        "-c", "4096",
        "-n", "280",
        "--temp", "0",
        "--simple-io",
        "-cnv",
        "-st",
        "--reasoning", "off",
        "--reasoning-budget", "0",
        "--no-display-prompt",
        "-sys", "You are an English technical reviewer. Output valid JSON only. Do not include markdown.",
        "-p", case["prompt"],
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
    seconds = round(time.perf_counter() - t0, 2)
    raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
    parsed = None
    parse_error = None
    try:
        parsed = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        parse_error = repr(exc)
    blob = json.dumps(parsed, ensure_ascii=False) if parsed is not None else raw
    checks = case["checks"]
    contains_ok = all(s.lower() in blob.lower() for s in checks.get("must_contain", []))
    pass_value_ok = True
    if checks.get("allowed_pass_values") and parsed is not None and "pass_fail" in parsed:
        pass_value_ok = parsed.get("pass_fail") in checks["allowed_pass_values"]
    ok = proc.returncode == 0 and parsed is not None and contains_ok and pass_value_ok
    return {
        "id": case["id"],
        "ok": ok,
        "returncode": proc.returncode,
        "seconds": seconds,
        "parsed": parsed,
        "parse_error": parse_error,
        "contains_ok": contains_ok,
        "pass_value_ok": pass_value_ok,
        "raw_tail": raw[-1200:],
    }


def main():
    results = [run_case(case) for case in CASES]
    summary = {
        "model": str(MODEL),
        "model_exists": MODEL.exists(),
        "overall_pass": all(r["ok"] for r in results),
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
        "results": results,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Qwen Auxiliary Worker Benchmark",
        "",
        "Date: 2026-04-25",
        f"Model: `{MODEL}`",
        f"Overall: {'PASS' if summary['overall_pass'] else 'FAIL'} ({summary['passed']}/{summary['total']})",
        "",
        "Purpose: smoke-test Qwen3.6-35B-A3B Q4_K_M as an English-only auxiliary worker for reasoning, code/security review, structured critique, benchmark design, and news-filter critique. This does not authorize Thai summarization/prose/training-data generation.",
        "",
        "## Results",
    ]
    for r in results:
        lines += [
            f"- {r['id']}: {'PASS' if r['ok'] else 'FAIL'}; seconds={r['seconds']}; json={r['parsed'] is not None}",
        ]
    lines += ["", f"Raw JSON result: `{OUT}`", ""]
    MD.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"overall_pass": summary["overall_pass"], "passed": summary["passed"], "total": summary["total"], "out": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
