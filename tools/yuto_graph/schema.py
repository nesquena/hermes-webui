"""Schema helpers for Yuto's graph-first second brain.

Source of truth remains Markdown. This module only defines the generated
machine map used by tools and reports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    title: str
    path: str
    source: str
    mtime: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    type: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def note_id(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def note_alias(path: Path) -> str:
    return path.stem
