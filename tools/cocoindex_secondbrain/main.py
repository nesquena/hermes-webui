from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import asdict, dataclass

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import FileLike, PatternFilePathMatcher

ROOT = pathlib.Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT / "knowledge"
OUT_DIR = ROOT / ".cocoindex-secondbrain" / "index"

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True)
class NoteMetadata:
    path: str
    title: str
    sha256: str
    bytes: int
    line_count: int
    headings: list[Heading]
    wikilinks: list[str]
    body: str


def _relative_note_path(path: pathlib.PurePath) -> pathlib.PurePath:
    """Return a stable path relative to KNOWLEDGE_DIR.

    CocoIndex/localfs may expose an absolute path depending on source configuration.
    Absolute paths would make `outdir / rel_path` ignore `outdir`, so normalize first.
    """
    p = pathlib.Path(path)
    try:
        return p.relative_to(KNOWLEDGE_DIR)
    except ValueError:
        return pathlib.PurePath(*p.parts[1:]) if p.is_absolute() else p


def _safe_out_name(rel_path: pathlib.PurePath) -> str:
    return "__".join(rel_path.parts) + ".json"


def _extract_title(rel_path: pathlib.PurePath, text: str) -> str:
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return rel_path.stem.replace("-", " ").strip().title()


def _extract_headings(text: str) -> list[Heading]:
    headings: list[Heading] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        match = HEADING_RE.match(line)
        if match:
            headings.append(Heading(level=len(match.group(1)), text=match.group(2).strip(), line=idx))
    return headings


def _extract_wikilinks(text: str) -> list[str]:
    links = {match.group(1).strip() for match in WIKILINK_RE.finditer(text)}
    return sorted(link for link in links if link)


@coco.fn(memo=True)
async def index_note(file: FileLike, outdir: pathlib.Path) -> None:
    text = await file.read_text()
    rel_path = _relative_note_path(file.file_path.path)
    metadata = NoteMetadata(
        path=str(rel_path),
        title=_extract_title(rel_path, text),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        bytes=len(text.encode("utf-8")),
        line_count=len(text.splitlines()),
        headings=_extract_headings(text),
        wikilinks=_extract_wikilinks(text),
        body=text,
    )
    body = json.dumps(asdict(metadata), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    localfs.declare_file(outdir / _safe_out_name(rel_path), body, create_parent_dirs=True)


@coco.fn
async def app_main(sourcedir: pathlib.Path, outdir: pathlib.Path) -> None:
    notes = localfs.walk_dir(
        sourcedir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.md"],
            excluded_patterns=[".graph/**", ".graph-core/**"],
        ),
    )
    await coco.mount_each(index_note, notes.items(), outdir)


app = coco.App(
    coco.AppConfig(name="YutoSecondBrainMetadataIndex"),
    app_main,
    sourcedir=KNOWLEDGE_DIR,
    outdir=OUT_DIR,
)
