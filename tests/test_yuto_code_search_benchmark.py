from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "yuto_code_search_benchmark.py"
spec = importlib.util.spec_from_file_location("yuto_code_search_benchmark", MODULE_PATH)
bench = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["yuto_code_search_benchmark"] = bench
spec.loader.exec_module(bench)


def test_score_rank_contract():
    assert bench.score_rank(1) == 1.0
    assert bench.score_rank(3) == 0.9
    assert bench.score_rank(5) == 0.7
    assert bench.score_rank(6) == 0.4
    assert bench.score_rank(None) == 0.0


def test_canaries_are_defined_for_yuto_core_surfaces():
    queries = [query for query, _ in bench.CANARIES]

    assert any("memory scout" in query for query in queries)
    assert any("memory palace" in query for query in queries)
    assert any("Book Expert Factory" in query for query in queries)
