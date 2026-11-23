"""Issue #2361 — the run state consistency contract must stay an explicit,
rebased authority matrix, not prose that drifts away from the source.

These tests pin the RFC artifact the umbrella issue asks for:

- a per-layer authority matrix naming authority, persistence lifetime,
  allowed divergence, and replay/recovery rules for every state layer,
- symbol-based source anchors that still exist in the code they name
  (never hardcoded line numbers, per #5513 / #5542),
- invariant numbering that is append-only, because shipped code cites
  invariants by number (`static/boot.js`, `tests/test_cancel_stream_owner_guard.py`),
- a review checklist that covers derived caches and recovery provenance.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
RFC = ROOT / "docs" / "rfcs" / "webui-run-state-consistency-contract.md"
RFC_INDEX = ROOT / "docs" / "rfcs" / "README.md"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"


def _rfc() -> str:
    assert RFC.exists(), "run state consistency RFC must exist"
    return RFC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_rfc_is_indexed_for_the_umbrella_issue():
    index = RFC_INDEX.read_text(encoding="utf-8")
    contracts = CONTRACTS.read_text(encoding="utf-8")

    assert "webui-run-state-consistency-contract.md" in index
    assert "#2361" in index
    assert "docs/rfcs/webui-run-state-consistency-contract.md" in contracts


def test_rfc_has_an_authority_matrix_with_the_four_required_axes():
    """The maintainer asked for authority, persistence lifetime, allowed
    divergence, and replay/recovery rules per layer (#2361)."""
    text = _rfc()

    header = next(
        (
            line
            for line in text.splitlines()
            if line.startswith("| Layer | Authority")
        ),
        None,
    )
    assert header is not None, "RFC must contain a '| Layer | Authority' matrix"

    for column in (
        "Persistence lifetime",
        "Allowed divergence",
        "Replay / recovery rule",
    ):
        assert column in header, f"authority matrix must have a {column!r} column"


def _authority_matrix_rows(text: str) -> list[list[str]]:
    """Body rows of the '### Authority matrix' table (header and separator
    excluded), each as its list of cells."""
    matrix = _section(text, "### Authority matrix")
    rows = []
    for line in matrix.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or cells[0] == "Layer" or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def test_rfc_authority_matrix_covers_every_state_layer():
    """Coverage is checked against the matrix rows themselves — the purpose
    table above it repeats the same layer names, so checking the whole section
    would let a layer silently disappear from the matrix."""
    rows = _authority_matrix_rows(_rfc())
    assert rows, "authority matrix must have body rows"

    layer_names = [row[0] for row in rows]
    layers = [
        "Visible transcript",
        "Model context",
        "Pending turn metadata",
        "Live stream",
        "Worker lifecycle registry",
        "Run journal",
        "Compression summary",
        "Live UI scene",
        "Sidebar/session metadata",
        "Client-side unread stores",
        "Derived model/context metadata",
    ]
    missing = [
        layer
        for layer in layers
        if not any(name.startswith(layer) for name in layer_names)
    ]
    assert missing == [], f"authority matrix must cover layers: {missing}"

    for row in rows:
        assert len(row) == 5, f"matrix row must have 5 cells: {row!r}"
        empty = [index for index, cell in enumerate(row) if not cell]
        assert empty == [], f"matrix row has empty cells {empty}: {row!r}"


def test_rfc_derived_metadata_row_names_both_sources():
    """The derived layer projects two different things from two different
    sources: the model catalog (config -> /api/models -> cache) and the
    context window/threshold values that come from session/stream usage. A
    row that names only one invites reviewers to check one and miss the other."""
    derived = next(
        (
            row
            for row in _authority_matrix_rows(_rfc())
            if row[0].startswith("Derived model/context metadata")
        ),
        None,
    )
    assert derived is not None, "derived metadata row must exist"
    joined = " | ".join(derived)
    assert "_get_models_cache_path" in joined, "must name the catalog cache"
    assert "threshold_tokens" in joined, "must name the usage-sourced limit"


# (file, symbol, source probe) — the RFC-cited symbol plus a definition-shaped
# substring that must exist in that file, so a rename that leaves the old name
# behind in a comment still fails. Probes are never line numbers (#5513, #5542).
SOURCE_ANCHORS = [
    ("api/config.py", "ACTIVE_RUNS", "ACTIVE_RUNS: dict = {}"),
    ("api/config.py", "STREAMS", "STREAMS: dict = {}"),
    ("api/config.py", "SESSION_INDEX_FILE", "SESSION_INDEX_FILE = SESSION_DIR"),
    ("api/config.py", "_get_models_cache_path", "def _get_models_cache_path"),
    ("api/models.py", "pending_user_message", "session.pending_user_message"),
    ("api/models.py", "context_messages", "self.context_messages ="),
    ("api/run_journal.py", "RUN_JOURNAL_DIR_NAME", 'RUN_JOURNAL_DIR_NAME = "_run_journal"'),
    ("api/turn_journal.py", "TURN_JOURNAL_DIR_NAME", 'TURN_JOURNAL_DIR_NAME = "_turn_journal"'),
    ("api/session_recovery.py", "recover_session", "def recover_session"),
    (
        "api/compression_anchor.py",
        "is_context_compression_marker",
        "def is_context_compression_marker",
    ),
    ("static/boot.js", "threshold_tokens", "S.session.threshold_tokens=data.session.threshold_tokens"),
    (
        "static/sessions.js",
        "SESSION_VIEWED_COUNTS_KEY",
        "const SESSION_VIEWED_COUNTS_KEY = 'hermes-session-viewed-counts';",
    ),
    (
        "static/sessions.js",
        "SESSION_COMPLETION_UNREAD_KEY",
        "const SESSION_COMPLETION_UNREAD_KEY = 'hermes-session-completion-unread';",
    ),
]


def test_rfc_source_anchors_land_on_real_symbols():
    """Anchors must exist as a real definition in their file AND be cited
    inside the layer tables, not somewhere else in the prose."""
    layers_section = _section(_rfc(), "## State Layers")
    for rel, symbol, probe in SOURCE_ANCHORS:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert probe in source, f"{rel} must still define {symbol!r} ({probe!r})"
        assert symbol in layers_section, (
            f"the state-layer tables must cite {symbol!r} as a symbol anchor "
            "(not a line number)"
        )


def test_rfc_cites_no_hardcoded_source_line_numbers():
    text = _rfc()
    stale = re.findall(r"\b\w+\.py:\d+(?:-\d+)?", text)
    assert not stale, f"RFC must not cite source line numbers; found {stale!r}"


def test_rfc_invariants_are_append_only():
    """Shipped code cites invariants by number, so existing numbers and titles
    must never shift when a new invariant is appended."""
    text = _rfc()
    invariants = _section(text, "## Core Invariants")

    numbers = [
        int(match.group(1))
        for match in re.finditer(r"^(\d+)\. \*\*", invariants, re.MULTILINE)
    ]
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"invariants must be numbered sequentially with no gaps: {numbers}"
    )
    assert numbers[-1] >= 11, "RFC must carry the derived-state and recovery invariants"

    shipped = {
        1: "Visible current turns enter model context.",
        2: "Active turn UI keeps its owner.",
        3: "Reattach preserves order or degrades clearly.",
        4: "Maintenance is not activity.",
        5: "Replay is idempotent.",
        6: "Compression is not current intent.",
        7: "Observation has a degraded path.",
        8: "Every mutation names its layer.",
        9: "Lifecycle-busy is not client-attachable.",
    }
    for number, title in shipped.items():
        assert f"{number}. **{title}**" in invariants, (
            f"invariant #{number} {title!r} is cited elsewhere and must not move"
        )

    assert "10. **" in invariants and "derived" in invariants.lower()
    assert "11. **" in invariants and "provenance" in invariants.lower()


def test_rfc_review_checklist_covers_caches_and_recovery_provenance():
    checklist = _section(_rfc(), "## Review Checklist").lower()

    assert "cache" in checklist, "checklist must ask about derived caches"
    assert "provenance" in checklist or "recovered" in checklist, (
        "checklist must ask about recovery provenance metadata"
    )


def test_rfc_issue_map_names_layer_and_invariant_for_new_slices():
    issue_map = _section(_rfc(), "## Existing Issue Map")

    header = next(
        (line for line in issue_map.splitlines() if line.startswith("| Example |")),
        None,
    )
    assert header is not None, "issue map table must exist"
    assert "| Layer |" in header, "issue map must name the touched state layer"

    for issue in ("#2442", "#2443", "#4208", "#4216"):
        assert issue in issue_map, f"issue map must include {issue}"
