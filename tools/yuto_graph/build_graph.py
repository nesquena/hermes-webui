"""Build a read-only graph index for Yuto's Markdown second brain.

The graph is generated from Markdown files and written under an output
directory. It never rewrites source notes.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .schema import Edge, Node, note_alias, note_id

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+\.md(?:#[^)]+)?)\)")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
FRONTMATTER_NAME_RE = re.compile(
    r"^---\s*\n.*?^name:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
    re.MULTILINE | re.DOTALL,
)


def extract_wikilinks(text: str) -> list[str]:
    """Return unique wikilink targets in first-seen order."""
    seen: set[str] = set()
    links: list[str] = []
    for match in WIKILINK_RE.finditer(text):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            links.append(target)
    return links


def extract_markdown_links(text: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for _label, url in MD_LINK_RE.findall(text):
        target = url.split("#", 1)[0].strip()
        if target and target not in seen:
            seen.add(target)
            links.append(target)
    return links


def title_from_text(path: Path, text: str) -> str:
    match = HEADING_RE.search(text)
    return match.group(1).strip() if match else path.stem


def classify_node(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if rel == "decisions.md":
        return "decision-log"
    if rel == "sources.md":
        return "source-log"
    if rel == "workflows.md":
        return "workflow-log"
    if rel == "projects.md":
        return "project-log"
    if rel == "yuto.md":
        return "self-memory"
    if rel == "memory-system.md":
        return "memory-policy"
    if rel in {"USER.md", "MEMORY.md"}:
        return "memory"
    if rel.endswith("SKILL.md"):
        return "skill"
    if "partnerships/" in rel:
        return "team"
    return "note"


def skill_name_from_text(text: str) -> str | None:
    match = FRONTMATTER_NAME_RE.search(text)
    return match.group(1).strip() if match else None


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def resolve_target(target: str, alias_to_id: dict[str, str]) -> str | None:
    key = target.strip()
    if key in alias_to_id:
        return alias_to_id[key]
    normalized = key.removesuffix(".md")
    return alias_to_id.get(normalized)


def build_graph(
    root: Path,
    extra_roots: list[Path] | None = None,
    memory_files: list[Path] | None = None,
    include_support_docs: bool = False,
) -> tuple[list[Node], list[Edge], dict[str, list[str]]]:
    roots = [root] + list(extra_roots or [])
    markdown_files: list[tuple[Path, Path, str]] = []
    for base in roots:
        if not base.exists():
            continue
        source_name = "yuto-knowledge" if base == root else base.name
        for path in sorted(base.rglob("*.md")):
            if any(part.startswith(".graph") for part in path.parts):
                continue
            if source_name == "skills" and not include_support_docs and path.name != "SKILL.md":
                continue
            markdown_files.append((base, path, source_name))
    for path in memory_files or []:
        if path.exists() and path.suffix == ".md":
            markdown_files.append((path.parent, path, "active-memory"))

    alias_to_id: dict[str, str] = {}
    texts: dict[str, str] = {}
    nodes: list[Node] = []

    for base, path, source_name in markdown_files:
        rel_id = note_id(path, base)
        node_id = rel_id if source_name == "yuto-knowledge" else f"{source_name}:{rel_id}"
        text = path.read_text(encoding="utf-8", errors="replace")
        texts[node_id] = text
        alias_to_id.setdefault(note_alias(path), node_id)
        alias_to_id.setdefault(rel_id.removesuffix(".md"), node_id)
        if path.name == "SKILL.md":
            alias_to_id.setdefault(path.parent.name, node_id)
            skill_name = skill_name_from_text(text)
            if skill_name:
                alias_to_id.setdefault(skill_name, node_id)
        nodes.append(
            Node(
                id=node_id,
                type=classify_node(path, base) if source_name in {"yuto-knowledge", "active-memory", "skills"} else "external_note",
                title=title_from_text(path, text),
                path=str(path),
                source=source_name,
                mtime=iso_mtime(path),
            )
        )

    edges: list[Edge] = []
    broken_links: list[str] = []
    for node in nodes:
        text = texts[node.id]
        for target in extract_wikilinks(text):
            resolved = resolve_target(target, alias_to_id)
            if resolved:
                edges.append(Edge(node.id, resolved, "links_to", f"[[{target}]]"))
            else:
                broken_links.append(f"{node.id} -> [[{target}]]")
        for target in extract_markdown_links(text):
            resolved = resolve_target(Path(target).stem, alias_to_id)
            if resolved:
                edges.append(Edge(node.id, resolved, "markdown_link", target))
            else:
                broken_links.append(f"{node.id} -> {target}")

    node_ids = {n.id for n in nodes}
    outbound = {e.source for e in edges}
    inbound = {e.target for e in edges}
    orphan_notes = sorted(node_ids - outbound - inbound)
    diagnostics = {"broken_links": sorted(set(broken_links)), "orphan_notes": orphan_notes}
    return nodes, edges, diagnostics


def write_outputs(nodes: list[Node], edges: list[Edge], diagnostics: dict[str, list[str]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nodes.json").write_text(
        json.dumps([n.to_dict() for n in nodes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "edges.json").write_text(
        json.dumps([e.to_dict() for e in edges], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    source_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    node_source_by_id = {node.id: node.source for node in nodes}
    diagnostic_counts: dict[str, dict[str, int]] = {}

    def source_for_item(item: str) -> str:
        node_id = item.split(" -> ", 1)[0]
        if node_id in node_source_by_id:
            return node_source_by_id[node_id]
        if node_id.startswith("skills:"):
            return "skills"
        if node_id.startswith("active-memory:"):
            return "active-memory"
        return "yuto-knowledge"

    for node in nodes:
        source_counts[node.source] = source_counts.get(node.source, 0) + 1
        type_counts[node.type] = type_counts.get(node.type, 0) + 1
    for kind in ("broken_links", "orphan_notes"):
        for item in diagnostics[kind]:
            source = source_for_item(item)
            diagnostic_counts.setdefault(source, {"broken_links": 0, "orphan_notes": 0})[kind] += 1
    report = [
        "# Yuto Graph Report",
        "",
        f"Generated nodes: {len(nodes)}",
        f"Generated edges: {len(edges)}",
        f"Broken links: {len(diagnostics['broken_links'])}",
        f"Orphan notes: {len(diagnostics['orphan_notes'])}",
        "",
        "## Source Counts",
        "",
        *[f"- {source}: {count}" for source, count in sorted(source_counts.items())],
        "",
        "## Type Counts",
        "",
        *[f"- {node_type}: {count}" for node_type, count in sorted(type_counts.items())],
        "",
        "## Diagnostic Counts",
        "",
        *[
            f"- {source} {kind}: {counts.get(kind, 0)}"
            for source, counts in sorted(diagnostic_counts.items())
            for kind in ("broken_links", "orphan_notes")
        ],
        "",
        "## Broken Links",
        "",
    ]
    report.extend(f"- {item}" for item in diagnostics["broken_links"][:200])
    report.extend(["", "## Orphan Notes", ""])
    report.extend(f"- {item}" for item in diagnostics["orphan_notes"][:200])
    (out_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Yuto Markdown graph index")
    parser.add_argument("--root", type=Path, default=Path("/Users/kei/kei-jarvis/knowledge"))
    parser.add_argument("--out", type=Path, default=Path("/Users/kei/kei-jarvis/knowledge/.graph"))
    parser.add_argument("--extra-root", type=Path, action="append", default=[])
    parser.add_argument("--memory-file", type=Path, action="append", default=[])
    parser.add_argument("--include-support-docs", action="store_true", help="Include non-SKILL.md files from skill roots")
    args = parser.parse_args()

    nodes, edges, diagnostics = build_graph(args.root, args.extra_root, args.memory_file, args.include_support_docs)
    write_outputs(nodes, edges, diagnostics, args.out)
    print(f"nodes={len(nodes)} edges={len(edges)} broken={len(diagnostics['broken_links'])} orphans={len(diagnostics['orphan_notes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
