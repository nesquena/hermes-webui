from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "yuto_code_search.py"
spec = importlib.util.spec_from_file_location("yuto_code_search", MODULE_PATH)
yuto_code_search = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["yuto_code_search"] = yuto_code_search
spec.loader.exec_module(yuto_code_search)


def test_query_terms_dedupes_and_keeps_meaningful_terms():
    assert yuto_code_search.query_terms("memory scout root detection memory") == [
        "memory",
        "scout",
        "root",
        "detection",
    ]


def test_lexical_search_finds_semantic_file_by_terms(tmp_path):
    target = tmp_path / "tools" / "memory_scout.py"
    target.parent.mkdir()
    target.write_text(
        "def detect_repo_root():\n"
        "    return '/Users/kei/kei-jarvis'\n"
        "def second_brain_status():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "noise.md").write_text("memory only", encoding="utf-8")

    hits = yuto_code_search.lexical_search("memory scout root detection", tmp_path, limit=3)

    assert hits[0].path == "tools/memory_scout.py"
    assert hits[0].source == "lexical"


def test_merge_hits_combines_cocoindex_and_lexical_scores():
    lexical = [yuto_code_search.SearchHit(path="tools/a.py", score=0.8, source="lexical")]
    semantic = [yuto_code_search.SearchHit(path="tools/a.py", score=1.0, source="cocoindex")]

    merged = yuto_code_search.merge_hits(lexical, semantic, limit=1)

    assert merged[0].path == "tools/a.py"
    assert merged[0].score == 1.8
    assert merged[0].source == "cocoindex+lexical"


def test_domain_boost_prefers_yuto_latest_recall_authorities():
    terms = yuto_code_search.query_terms("latest recall raw Hermes sessions by mtime")

    assert yuto_code_search.domain_boost("tools/second_brain.py", terms) > 0
    assert yuto_code_search.domain_boost("knowledge/memory-system.md", terms) > 0