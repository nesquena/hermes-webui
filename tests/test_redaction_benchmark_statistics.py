"""Deterministic regression for the benchmark's paired speedup output."""
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_redaction_assignments.py"


def test_benchmark_reports_median_of_paired_ratios(monkeypatch, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("redaction_benchmark", SCRIPT)
    assert spec is not None and spec.loader is not None
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    output = tmp_path / "results.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--base-ref", "HEAD",
                                     "--agent-dir", str(tmp_path), "--rounds", "3",
                                     "--output", str(output)])
    monkeypatch.setattr(sys, "path", sys.path.copy())

    def git_output(command, **kwargs):
        return "0" * 40 if command[1] == "rev-parse" else b"# deterministic fixture"

    def _combined_redact(text):
        return text

    monkeypatch.setattr(benchmark.subprocess, "check_output", git_output)
    monkeypatch.setattr(benchmark, "build", lambda *args: _combined_redact)
    monkeypatch.setattr(benchmark.importlib, "import_module",
                        lambda name: SimpleNamespace(__file__=__file__))

    # Three pairs: base=[1, 100, 100], candidate=[1, 10, 100] ms.
    # Median(pair ratios)=1, whereas ratio(group medians)=10.
    # Round 1 measures candidate before base; preserve that actual ordering.
    def clock():
        now = 0
        while True:
            for duration_ms in (1, 1, 10, 100, 100, 100):
                yield now
                now += duration_ms * 1_000_000
                yield now

    wall_clock, cpu_clock = clock(), clock()
    monkeypatch.setattr(benchmark.time, "perf_counter_ns", lambda: next(wall_clock))
    monkeypatch.setattr(benchmark.time, "process_time_ns", lambda: next(cpu_clock))
    benchmark.main()

    result = json.loads(output.read_text())
    printed = dict(line.split(" ", 1) for line in capsys.readouterr().out.splitlines())
    assert result["cases"]
    assert set(printed) == {case["shape"] for case in result["cases"]}
    for case in result["cases"]:
        timings = [sample["timings"] for sample in case["samples"]]
        base = [timing["base"]["wall_ms"] for timing in timings]
        candidate = [timing["candidate"]["wall_ms"] for timing in timings]
        assert base == [1, 100, 100]
        assert candidate == [1, 10, 100]
        assert statistics.median(base) / statistics.median(candidate) == 10
        assert case["wall_median_speedup"] == pytest.approx(1)
        assert json.loads(printed[case["shape"]])["speedup"] == pytest.approx(1)
