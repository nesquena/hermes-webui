from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
loaded_tools = sys.modules.get("tools")
if loaded_tools and not str(getattr(loaded_tools, "__file__", "")).startswith(str(ROOT)):
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            del sys.modules[name]

from tools.yuto_graph.build_graph import (  # noqa: E402
    build_graph,
    extract_markdown_links,
    extract_wikilinks,
    write_outputs,
)


def test_extract_wikilinks():
    text = "Related: [[memory-system]] [[yuto-taeyoon-operating-loop|loop]] [[note#heading]]"
    assert extract_wikilinks(text) == ["memory-system", "yuto-taeyoon-operating-loop", "note"]


def test_extract_markdown_links():
    text = "See [Memory](memory-system.md) and [Loop](yuto-taeyoon-operating-loop.md#core)."
    assert extract_markdown_links(text) == ["memory-system.md", "yuto-taeyoon-operating-loop.md"]


def test_build_graph_tmp_notes(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\nRelated: [[b]]", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n", encoding="utf-8")

    nodes, edges, diagnostics = build_graph(tmp_path)

    assert {node.id for node in nodes} == {"a.md", "b.md"}
    assert [(edge.source, edge.target, edge.type) for edge in edges] == [("a.md", "b.md", "links_to")]
    assert diagnostics["broken_links"] == []


def test_build_graph_indexes_memory_files_as_active_memory_nodes(tmp_path):
    knowledge = tmp_path / "knowledge"
    memory = tmp_path / "memories"
    knowledge.mkdir()
    memory.mkdir()
    (knowledge / "index.md").write_text("# Index\n", encoding="utf-8")
    user = memory / "USER.md"
    active = memory / "MEMORY.md"
    user.write_text("Kei prefers concise answers", encoding="utf-8")
    active.write_text("Yuto authority pointer", encoding="utf-8")

    nodes, _edges, diagnostics = build_graph(knowledge, memory_files=[user, active])

    by_id = {node.id: node for node in nodes}
    assert by_id["active-memory:USER.md"].type == "memory"
    assert by_id["active-memory:USER.md"].source == "active-memory"
    assert by_id["active-memory:MEMORY.md"].type == "memory"
    assert diagnostics["broken_links"] == []


def test_build_graph_indexes_skills_and_resolves_skill_name_links(tmp_path):
    knowledge = tmp_path / "knowledge"
    skills = tmp_path / "skills"
    skill_dir = skills / "software-development" / "completion-contract"
    support_dir = skill_dir / "references"
    knowledge.mkdir()
    support_dir.mkdir(parents=True)
    (knowledge / "yuto.md").write_text("# Yuto\n\nRelated: [[completion-contract]]", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: completion-contract\ndescription: Enforce metric closure\n---\n\n# Completion Contract\n",
        encoding="utf-8",
    )
    (support_dir / "example.md").write_text("# Support\n\nNoisy coordinate [[100, 100]]", encoding="utf-8")

    nodes, edges, diagnostics = build_graph(knowledge, extra_roots=[skills])

    by_id = {node.id: node for node in nodes}
    skill_id = "skills:software-development/completion-contract/SKILL.md"
    assert by_id[skill_id].type == "skill"
    assert by_id[skill_id].source == "skills"
    assert "skills:software-development/completion-contract/references/example.md" not in by_id
    assert ("yuto.md", skill_id, "links_to") in {(edge.source, edge.target, edge.type) for edge in edges}
    assert diagnostics["broken_links"] == []


def test_build_graph_ignores_generated_graph_directories(tmp_path):
    (tmp_path / "index.md").write_text("# Index\n\nRelated: [[real]]", encoding="utf-8")
    (tmp_path / "real.md").write_text("# Real\n", encoding="utf-8")
    graph_dir = tmp_path / ".graph-core"
    graph_dir.mkdir()
    (graph_dir / "report.md").write_text("# Generated\n\nRelated: [[missing-generated]]", encoding="utf-8")

    nodes, _edges, diagnostics = build_graph(tmp_path)

    assert {node.id for node in nodes} == {"index.md", "real.md"}
    assert diagnostics["broken_links"] == []


def test_write_outputs_reports_diagnostics_by_source(tmp_path):
    knowledge = tmp_path / "knowledge"
    skills = tmp_path / "skills"
    skill_dir = skills / "software-development" / "noisy-skill"
    knowledge.mkdir()
    skill_dir.mkdir(parents=True)
    (knowledge / "index.md").write_text("# Index\n\nRelated: [[missing-note]]", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: noisy-skill\n---\n\n# Noisy Skill\n\nRelated: [[missing-skill-link]]",
        encoding="utf-8",
    )

    nodes, edges, diagnostics = build_graph(knowledge, extra_roots=[skills])
    out = tmp_path / "graph"
    write_outputs(nodes, edges, diagnostics, out)

    report = (out / "report.md").read_text()
    assert "## Diagnostic Counts" in report
    assert "- yuto-knowledge broken_links: 1" in report
    assert "- skills broken_links: 1" in report
