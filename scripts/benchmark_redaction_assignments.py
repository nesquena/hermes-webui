"""Synthetic direct-builder benchmark; no app state, providers or HTTP server.

Example (use a supported Python and an installed Agent checkout):
  python scripts/benchmark_redaction_assignments.py --base-ref origin/master \
      --agent-dir /path/to/hermes-agent --rounds 12 --output results.json

The base source is taken from Git; the candidate is the working tree. Only their
actual _build_redact_fn definitions are loaded, plus the installed pure Agent
redactor. No request-latency, browser, RSS or historical-incident claim is made.
"""
import argparse
import ast
import hashlib
import importlib
import json
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def build(source, label):
    tree = ast.parse(source, filename=label)
    nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name == "_build_redact_fn"]
    if len(nodes) != 1:
        raise ValueError("expected exactly one redactor builder")
    scope: dict[str, Any] = {"_re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), label, "exec"), scope)
    return scope["_build_redact_fn"]()


def summarize(values):
    return {"median_ms": statistics.median(values),
            "min_ms": min(values), "max_ms": max(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7421)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 2 or args.base_ref.startswith("-"):
        parser.error("use at least two rounds and a Git revision, not an option")
    root = Path(__file__).resolve().parents[1]
    base = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.base_ref + "^{commit}"],
        cwd=root, text=True).strip()
    old_source = subprocess.check_output(
        ["git", "show", base + ":api/helpers.py"], cwd=root)
    new_source = (root / "api/helpers.py").read_bytes()
    sys.path.insert(0, str(args.agent_dir.resolve()))
    agent = importlib.import_module("agent.redact")
    functions = {"base": build(old_source, "base_helpers.py"),
                 "candidate": build(new_source, "candidate_helpers.py")}
    if any(fn.__name__ != "_combined_redact" for fn in functions.values()):
        raise RuntimeError("benchmark requires the actual Agent redactor")
    cases = [
        ("uppercase_url_64k_no_equals", "https://example.invalid/ " + "A" * 65536),
        ("uppercase_url_256k_no_equals", "https://example.invalid/ " + "A" * 262144),
        ("ordinary_url_64k_no_equals",
         ("https://example.invalid/ ordinary output\n" * 1800)[:65536]),
        ("uppercase_url_64k_has_equals", "https://example.invalid/ " + "A" * 65536 + "=B"),
        ("ordinary_url_64k_equals_first",
         "=" + ("https://example.invalid/ ordinary output\n" * 1800)[:65535]),
        ("ordinary_url_64k_equals_last",
         ("https://example.invalid/ ordinary output\n" * 1800)[:65535] + "="),
        ("small_url_1k_no_equals",
         ("https://example.invalid/ ordinary output\n" * 30)[:1024]),
        ("small_assignment", "MY_API_KEY=SYNTHETIC_BENCHMARK_VALUE_1234567890"),
    ]
    # The first four shapes preserve the original experiment; added cases expose
    # guard overhead and delimiter position rather than favoring only wins.
    rng = random.Random(args.seed)
    results = []
    for name, text in cases:
        expected = functions["base"](text)
        assert functions["candidate"](text) == expected, name
        samples = []
        for round_id in range(args.rounds):
            order = ["base", "candidate"]
            if round_id % 2:
                order.reverse()
            sample = {"round": round_id, "order": order, "timings": {}}
            for version in order:
                fn = functions[version]
                start_wall = time.perf_counter_ns()
                start_cpu = time.process_time_ns()
                value = fn(text)
                end_cpu = time.process_time_ns()
                end_wall = time.perf_counter_ns()
                assert value == expected, (name, round_id, version)
                sample["timings"][version] = {
                    "wall_ms": (end_wall - start_wall) / 1e6,
                    "cpu_ms": (end_cpu - start_cpu) / 1e6,
                }
            samples.append(sample)
        summary = {
            version: {
                kind: summarize([s["timings"][version][kind] for s in samples])
                for kind in ("wall_ms", "cpu_ms")
            } for version in functions
        }
        ratio = statistics.median(
            s["timings"]["base"]["wall_ms"] / s["timings"]["candidate"]["wall_ms"]
            for s in samples
        )
        row = {"shape": name, "characters": len(text),
               "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "samples": samples, "summary": summary,
               "wall_median_speedup": ratio, "all_outputs_equal": True}
        results.append(row)
        print(name, json.dumps({"base_median_ms": summary["base"]["wall_ms"]["median_ms"],
                              "candidate_median_ms": summary["candidate"]["wall_ms"]["median_ms"],
                              "speedup": ratio}), flush=True)
    # Separate differential correctness checks, not included in timing samples.
    fragments = ["SAFE", "AAA", "MY_TOKEN=", "MY_API_KEY=", "https://example.invalid/",
                 "=", " ", "\"", "')", "\n", "字🙂", "\\u003d", "＝"]
    for _ in range(500):
        text = "".join(rng.choices(fragments, k=rng.randrange(1, 35)))
        assert functions["base"](text) == functions["candidate"](text)
    agent_file = agent.__file__
    assert agent_file is not None
    result = {
        "base_commit": base,
        "base_helpers_sha256": hashlib.sha256(old_source).hexdigest(),
        "candidate_helpers_sha256": hashlib.sha256(new_source).hexdigest(),
        "agent_redactor_sha256": hashlib.sha256(Path(agent_file).read_bytes()).hexdigest(),
        "python": sys.version, "rounds": args.rounds, "seed": args.seed,
        "method": "one untimed warmup per version; alternating base/candidate order; uncached actual builders with real Agent; wall+CPU per call",
        "cases": results, "differential_cases": 500,
        "scope": "synthetic direct functions; no live transcripts, HTTP timings, latency SLA, RSS or browser claims",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
