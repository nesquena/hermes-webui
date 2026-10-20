"""Idea filing — turn a captured spoken idea into a kanban triage card.

This module is the *filing stage* of idea mode (spec t_14ad8e89, §4–§5).  It
consumes the idea payload emitted by the capture stage
(``api.idea_mode`` — sibling module owned by that task), derives a concise
card title/body, deduplicates against the ideas ledger and the boards, and
creates the card in the ``ideas`` board's triage column through the existing
kanban bridge.  It deliberately registers **no routes**: ``api/routes.py``
dispatches ``/api/idea/*`` to ``api.idea_mode``, which calls
:func:`file_ideas` and renders the confirmation lines.

Flow (spec §5 order — validate → derive → dedupe → create → ledger):

1.  **validate** — reject empty / whitespace-only / too-short transcripts
    (< 3 characters) with a ``nothing_heard`` result (capture renders the
    🤷 line; no card, no crash).
2.  **derive** — one aux-LLM call, ≤ 5 s budget, one retry, then a
    deterministic fallback that never blocks capture.  The aux call returns
    strict JSON with 1–3 ideas; the fallback yields exactly one card whose
    title is the transcript truncated at a word boundary.
3.  **dedupe** — normalized-title hash (lowercase, strip punctuation, sort
    tokens — the exact algorithm the batch intake lane uses) against the
    ideas ledger and every board's task titles, plus ≥ 60 % distinctive
    token overlap and quote-prefix checks.  Dupes never create a card.
4.  **create** — primary path is the kanban bridge's in-process
    ``_create_task_payload`` (``hermes_cli.kanban_db.create_task`` under the
    hood) with ``triage=True`` and ``idempotency_key="idea-<hash16>"`` so a
    network retry returns the *same* card instead of a duplicate.  If the
    bridge import fails or the board is unreachable, the CLI fallback runs;
    if that also fails the payload is spooled to ``pending_cards.jsonl``
    (the batch intake lane's queue format) for retry — never dropped.
5.  **ledger** — every decision (filed / dupe / spooled) appends to
    ``/opt/data/ideas/ledger.json`` under an exclusive file lock, so idea
    mode and the batch lane stay consistent.

Secrets spoken aloud are still filed (it is the user's own board) but the
result's ``text`` never echoes them and the card body carries a rotate note.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("api.idea_filing")

# ── Locations (env-overridable for tests; defaults match the intake lane) ──

IDEAS_DIR = Path(os.environ.get("HERMES_IDEAS_DIR", "/opt/data/ideas"))
LEDGER_PATH = IDEAS_DIR / "ledger.json"
SPOOL_PATH = IDEAS_DIR / "pending_cards.jsonl"
LOCK_PATH = IDEAS_DIR / ".ledger.lock"

IDEA_BOARD = "ideas"
CREATED_BY = "idea-mode"

# Ledger is also env-overridable (the webui may run with a different Hermes
# root than /opt/data); kept separate from IDEAS_DIR for test injection.
_LEDGER_ENV = os.environ.get("HERMES_IDEAS_LEDGER", "")
if _LEDGER_ENV:
    LEDGER_PATH = Path(_LEDGER_ENV)

#: Board creation retries after a spool: 5 s, 30 s, 2 m (spec §5).
RETRY_DELAYS = (5.0, 30.0, 120.0)

#: Transcripts below this many characters are "nothing heard" (spec §6.7).
MIN_TRANSCRIPT_CHARS = 3

#: Aux derive call budget (spec §4): total 5 s per attempt incl. one retry.
DERIVE_TIMEOUT_S = 5.0
DERIVE_ATTEMPTS = 2

#: Cap on distinct ideas parsed from one utterance (spec §4).
MAX_IDEAS_PER_UTTERANCE = 3

#: How many parsed ideas to keep around before the cap is applied — the
#: overflow count powers the ``(+n more in source)`` note, so the parser
#: must see past the 3-card cap (spec §4). Hard bound keeps a runaway
#: aux reply (dozens of "ideas") from flooding the ledger.
MAX_DERIVED_IDEAS = 6

#: ``IDEA: `` + at most 72 chars of derived title (board convention).
TITLE_PREFIX = "IDEA: "
MAX_TITLE_CHARS = 72

#: Char cap for the verbatim SOURCE quote in the card body (spec §4).
QUOTE_CHAR_CAP = 200

#: Fallback title length before the word-boundary cut (spec §4).
FALLBACK_TITLE_CHARS = 60

_AUX_SYSTEM_PROMPT = (
    "You turn spoken ideas into kanban cards. Reply with STRICT JSON only "
    "(no markdown fences, no commentary)."
)
_AUX_USER_PROMPT = """transcript: "{transcript}"

Return JSON exactly of this shape:
{{"language": "nl|en", "ideas": [{{"title": "<English noun phrase, max 60 chars>", "problem": "<1-2 sentences, EN>", "first_action": "<single concrete step, EN>", "effort": "S|M|L", "acceptance": "<one measurable criterion, or TBD-specify>"}}]}}

Rules:
- 1 to {max_ideas} ideas ONLY, and only when clearly distinct wishes.
- Keep product/host/person names verbatim.
- Never invent detail not present in the transcript.
- Reply with the JSON object and nothing else."""

#: Strict-JSON extraction: first balanced {...} block, fences stripped.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_ID_RE = re.compile(r'"id"\s*:\s*"(t_[0-9a-zA-Z]+)"')

#: Same normalization the batch intake lane uses (keep in sync — the dedupe
#: hash must match intake_run.title_hash or the two lanes disagree on dupes).
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_IDEA_PREFIX_RE = re.compile(r"^idea:\s*")

#: Template/degenerate words excluded from fuzzy token overlap.
STOP_TOKENS = frozenset(
    {
        "idea",
        "toolbelt",
        "omarchy",
        "the",
        "and",
        "for",
        "not",
        "does",
        "work",
        "no",
        "on",
        "in",
        "to",
        "it",
        "is",
        "with",
        "a",
        "an",
        "of",
        "or",
        "all",
        "also",
        "add",
        "fix",
        "make",
        "can",
        "we",
        "could",
        "would",
        "should",
    }
)

#: Spoken-secret heuristics (spec §6.8): file the card, never echo, add a
#: rotate note. Deliberately coarse — triage reviews every card anyway.
_SECRET_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:sk|pk|rk|ghp|gho|xox[bp]|hb)\b[-_a-z0-9]{16,}",
        r"\b(?:api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]?\s*\S{8,}",
        r"\beyj[a-z0-9_-]{20,}",  # JWT header prefix
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS access key id
        r"\bglpat[-_a-z0-9]{16,}",  # GitLab PAT
        r"\b[-a-z0-9_.]{8,64}@[-a-z0-9.]+\.[a-z]{2,}\b\s*[:/]\s*\S{6,}",  # user:pass
    )
)


# ── Results ────────────────────────────────────────────────────────────────


@dataclass
class IdeaCard:
    """One derived idea ready to become a card."""

    title: str
    problem: str
    first_action: str
    effort: str
    acceptance: str


@dataclass
class FilingResult:
    """Outcome for ONE utterance (aggregates its 0..3 cards)."""

    ok: bool
    kind: str  # filed | dupe | nothing_heard | spooled
    text: str  # the one ≤12-word confirmation line (spec §3.4)
    card_ids: list[str] = field(default_factory=list)
    duplicate_of: Optional[str] = None
    spool_path: Optional[str] = None
    error: Optional[str] = None
    more_in_source: int = 0  # ideas beyond the 3-card cap (spec §4)
    derives_used: list[str] = field(default_factory=list)  # audit: per-card path

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "text": self.text,
            "card_ids": list(self.card_ids),
            "duplicate_of": self.duplicate_of,
            "spool_path": self.spool_path,
            "more_in_source": self.more_in_source,
        }


# ── Normalization / hashing (shared contract with the intake lane) ─────────


def norm_title(title: str) -> str:
    """Normalize a title to a sorted token multiset for hashing/overlap."""
    title = title.lower()
    title = _IDEA_PREFIX_RE.sub("", title)
    tokens = _TOKEN_RE.findall(title)
    return " ".join(sorted(tokens))


def title_hash(title: str) -> str:
    """16-hex normalized-title hash — MUST equal intake_run.title_hash."""
    import hashlib

    return hashlib.sha256(norm_title(title).encode()).hexdigest()[:16]


def idem_key_for(title: str) -> str:
    """Board idempotency key for a card title (spec §5)."""
    return f"idea-{title_hash(title)}"


def token_overlap(a: str, b: str) -> float:
    """Distinctive-token overlap in [0, 1]; < 2 shared tokens proves nothing."""
    ta = set(norm_title(a).split()) - STOP_TOKENS
    tb = set(norm_title(b).split()) - STOP_TOKENS
    shared = ta & tb
    if len(shared) < 2:
        return 0.0
    return len(shared) / min(len(ta), len(tb))


def _has_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _word_bound_cut(text: str, limit: int) -> str:
    """Truncate at a word boundary without exceeding ``limit``."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.strip().rstrip(",;:.-")


# ── Ledger (file-locked; shared with the batch intake lane) ────────────────


class _LedgerLock:
    """Exclusive cross-process lock via ``open(..., O_CREAT|O_EXCL)``.

    ``IDEAS_DIR`` may live on any filesystem (NAS included), so ``fcntl`` is
    not assumed; an atomic-create sentinel file with a stale break works
    everywhere.  Stale locks (holder crash) are broken after 30 s.
    """

    TIMEOUT_S = 10.0
    STALE_S = 30.0

    def __enter__(self):
        self.path = LOCK_PATH
        deadline = time.monotonic() + self.TIMEOUT_S
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.STALE_S:
                        logger.warning(
                            "breaking stale ideas ledger lock (age %.1fs)", age
                        )
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    pass  # raced with the holder releasing — retry
                if time.monotonic() > deadline:
                    raise TimeoutError("ideas ledger lock busy") from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _read_ledger() -> list[dict]:
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        return list(data.get("entries", []))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("ideas ledger unreadable (%s); treating as empty", exc)
        return []


def _write_ledger(entries: list[dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".json.tmp")
    payload = {"entries": entries}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LEDGER_PATH)


def ledger_append(record: dict) -> None:
    """Locked append of one record; the ledger shape matches intake_run."""
    with _LedgerLock():
        entries = _read_ledger()
        entries.append(record)
        _write_ledger(entries)


# ── Board titles (read-only, best effort — dedupe must not crash filing) ───


def _board_db_paths(boards_root: Path) -> list[Path]:
    paths = []
    try:
        for child in sorted(boards_root.iterdir()):
            db = child / "kanban.db"
            if db.is_file():
                paths.append(db)
    except OSError:
        pass
    return paths


def board_task_titles(db_path: Path) -> list[tuple[str, str]]:
    """(id, title) rows from a board DB, read-only; [] on any failure."""
    import sqlite3

    titles: list[tuple[str, str]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute("select id, title from tasks").fetchall()
            titles = [(str(r[0]), str(r[1] or "")) for r in rows]
        finally:
            con.close()
    except Exception as exc:  # locked / missing / corrupt — dedupe degrades
        logger.debug("board titles unreadable %s: %s", db_path, exc)
    return titles


def _all_board_titles() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(ideas_titles, other_titles) across every board; ([], []) on failure."""
    try:
        root = _boards_root()
    except Exception as exc:
        logger.warning("boards root unresolved (%s); board-title dedupe off", exc)
        return [], []
    ideas_titles: list[tuple[str, str]] = []
    other_titles: list[tuple[str, str]] = []
    for db in _board_db_paths(root):
        rows = board_task_titles(db)
        if db.parent.name == IDEA_BOARD:
            ideas_titles.extend(rows)
        else:
            other_titles.extend(rows)
    return ideas_titles, other_titles


def _boards_root() -> Path:
    """Boards root from the agent's kanban library (env-aware, no hardcode)."""
    from api.kanban_bridge import _kb

    kb = _kb()
    return kb.boards_root()


# ── Derivation: aux-LLM with deterministic fallback ────────────────────────


def _load_aux_call() -> Optional[Callable[..., Any]]:
    """Resolve the agent's auxiliary ``call_llm`` without hard imports.

    In the webui the agent package is reached through ``api.agent_compat``
    (the September 2026 split moved names into sibling modules; plain old
    imports fail CI).  Any failure means "aux unavailable" → fallback.
    """
    try:
        from api.agent_compat import agent_attr
        import agent.auxiliary_client as aux

        return agent_attr(aux, "call_llm", "agent.auxiliary_client")
    except Exception:
        return None


def _extract_message_text(resp: Any) -> str:
    """Best-effort message text from an OpenAI-compatible response."""
    try:
        choice = (getattr(resp, "choices", None) or resp["choices"])[0]
        message = getattr(choice, "message", None) or choice["message"]
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return str(content or "")
    except Exception:
        return ""


def _parse_derive_json(raw: str) -> Optional[list[IdeaCard]]:
    """Strict-JSON parse of the aux reply into 1..3 cards, else None."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    ideas = data.get("ideas") if isinstance(data, dict) else None
    if not isinstance(ideas, list) or not ideas:
        return None
    cards: list[IdeaCard] = []
    for item in ideas[:MAX_DERIVED_IDEAS]:
        if not isinstance(item, dict):
            return None
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        cards.append(
            IdeaCard(
                title=_word_bound_cut(title, MAX_TITLE_CHARS),
                problem=str(item.get("problem") or "").strip(),
                first_action=str(item.get("first_action") or "").strip(),
                effort=str(item.get("effort") or "").strip() or "M",
                acceptance=str(item.get("acceptance") or "").strip(),
            )
        )
    return cards


def derive_ideas(
    transcript: str,
    *,
    aux_call: Optional[Callable[..., Any]] = None,
    attempts: int = DERIVE_ATTEMPTS,
    timeout_s: float = DERIVE_TIMEOUT_S,
) -> tuple[list[IdeaCard], str]:
    """Derive cards from the transcript.

    Returns ``(cards, source)`` where source is ``"aux"`` or ``"fallback"``.
    Never raises and never blocks beyond ``attempts × timeout_s``: any aux
    failure falls back to one deterministic card (spec §4).
    """
    call = aux_call if aux_call is not None else _load_aux_call()
    if call is not None:
        messages = [
            {"role": "system", "content": _AUX_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _AUX_USER_PROMPT.format(
                    transcript=transcript.replace('"', "'")[:600],
                    max_ideas=MAX_IDEAS_PER_UTTERANCE,
                ),
            },
        ]
        for _ in range(max(1, attempts)):
            try:
                resp = call(
                    task="title_generation",
                    messages=messages,
                    max_tokens=600,
                    temperature=0.2,
                    timeout=timeout_s,
                )
                cards = _parse_derive_json(_extract_message_text(resp))
                if cards:
                    return cards, "aux"
            except Exception as exc:
                logger.debug("idea derive aux attempt failed: %s", exc)
    return _fallback_cards(transcript), "fallback"


def _fallback_cards(transcript: str) -> list[IdeaCard]:
    """Deterministic single card: transcript itself, cut at a word boundary."""
    title = (
        _word_bound_cut(transcript, FALLBACK_TITLE_CHARS)
        or transcript[:FALLBACK_TITLE_CHARS]
    )
    return [
        IdeaCard(
            title=title,
            problem=transcript.strip(),
            first_action="TBD-specify",
            effort="S (unreviewed)",
            acceptance="TBD-specify",
        )
    ]


# ── Card body (the board's 5-section template; SOURCE is verbatim) ─────────


def build_card_body(
    card: IdeaCard,
    *,
    session_id: str,
    quote: str,
    card_date: Optional[str] = None,
    secret: bool = False,
) -> str:
    """5-section card body — exact shape the board's triage specifiers read."""
    problem = card.problem.strip()
    first_action = card.first_action.strip()
    effort = card.effort.strip()
    acceptance = card.acceptance.strip()
    if not problem:
        problem = quote
    if not first_action:
        first_action = "TBD-specify"
    if not effort:
        effort = "S (unreviewed)"
    if not acceptance:
        acceptance = "TBD-specify"
    note = (
        "\n⚠ contains possible secret — rotate; not quoted in chat.\n" if secret else ""
    )
    return (
        f"PROBLEM\n{problem}\n"
        "\nFIRST ACTION\n"
        f"{first_action}\n"
        "\nEFFORT\n"
        f"{effort}\n"
        "\nSOURCE\n"
        f"session: {session_id}  date: {card_date or _date.today().isoformat()}\n"
        f'"{quote}"\n'
        f"{note}"
        "\nACCEPTANCE\n"
        f"{acceptance}\n"
    )


# ── Card creation: bridge (primary) → CLI (fallback) → spool (last) ────────


def _create_via_bridge(title: str, body: str, hash16: str) -> Optional[str]:
    """In-process create through the webui kanban bridge; None on failure."""
    try:
        from api.kanban_bridge import _create_task_payload

        data = _create_task_payload(
            {
                "title": title,
                "body": body,
                "triage": True,
                "created_by": CREATED_BY,
                "workspace_kind": "scratch",
                "idempotency_key": f"idea-{hash16}",
            },
            board=IDEA_BOARD,
        )
        return str(data["task"]["id"])
    except Exception as exc:
        logger.warning("bridge create failed for %r: %s", title, exc)
        return None


def _hermes_bin() -> str:
    """``hermes`` binary — venv sibling of the running interpreter, else PATH."""
    import sys

    base = getattr(sys, "_base_executable", None) or sys.executable
    candidate = Path(os.path.dirname(os.path.abspath(base))) / "hermes"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("hermes") or "hermes"


def _create_via_cli(title: str, body: str, hash16: str) -> Optional[str]:
    """CLI fallback create (spec §5); parses the FIRST ``"id": "t_…"``.

    The CLI prints provider preamble noise before the JSON blob, so a plain
    ``json.loads`` is wrong — regex the first id (verified quirk).
    """
    tmp_path = None
    try:
        IDEAS_DIR.mkdir(parents=True, exist_ok=True)
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, dir=str(IDEAS_DIR), encoding="utf-8"
        ) as tf:
            tf.write(body)
            tmp_path = tf.name
        try:
            proc = subprocess.run(
                [
                    _hermes_bin(),
                    "kanban",
                    "--board",
                    IDEA_BOARD,
                    "create",
                    title,
                    "--body-file",
                    tmp_path,
                    "--triage",
                    "--idempotency-key",
                    f"idea-{hash16}",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("CLI create failed to run for %r: %s", title, exc)
            return None
        m = _ID_RE.search(proc.stdout or "")
        if m and proc.returncode == 0:
            return m.group(1)
        logger.warning(
            "CLI create failed rc=%s out=%s err=%s",
            proc.returncode,
            (proc.stdout or "")[-300:],
            (proc.stderr or "")[-300:],
        )
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def create_card(
    title: str, body: str, hash16: str
) -> tuple[Optional[str], Optional[str]]:
    """Create the triage card; returns ``(card_id, error)``.

    Bridge first, CLI second, ``(None, reason)`` when both fail (caller
    spools).  Idempotency key makes retries converge on the same card.
    """
    card_id = _create_via_bridge(title, body, hash16)
    if card_id:
        return card_id, None
    card_id = _create_via_cli(title, body, hash16)
    if card_id:
        return card_id, None
    return None, "board-unreachable"


def spool_pending(record: dict) -> str:
    """Append a spool record (intake lane format) and return the path."""
    SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LedgerLock():
        with SPOOL_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return str(SPOOL_PATH)


def retry_spooled(
    *,
    sleep: Optional[Callable[[float], None]] = None,
    create: Optional[
        Callable[[str, str, str], tuple[Optional[str], Optional[str]]]
    ] = None,
    on_filed: Optional[Callable[[dict, str], None]] = None,
    max_rounds: Optional[int] = None,
) -> int:
    """Retry every spooled payload: 5 s/30 s/2 m, then leave it spooled.

    The batch intake lane reads the same file, so anything still spooled
    after the last round is *queued*, not lost (spec §5 "never drop").
    Succeeds once per payload (removed from the spool under the ledger
    lock), ledger-finalized via ``on_filed`` / default append.
    """
    sleep = sleep or time.sleep
    create = create or create_card
    rounds = RETRY_DELAYS if max_rounds is None else RETRY_DELAYS[:max_rounds]
    filed = 0
    for delay in rounds:
        pending = _read_spool()
        if not pending:
            break
        sleep(delay)
        still: list[dict] = []
        for record in pending:
            hash16 = str(record.get("hash") or title_hash(str(record.get("title", ""))))
            title = str(record.get("title", ""))
            body = str(record.get("body", ""))
            card_id, _err = create(title, body, hash16)
            if card_id:
                filed += 1
                _ledger_finalize(record, card_id, hash16)
                if on_filed:
                    on_filed(record, card_id)
            else:
                attempts = int(record.get("attempts", 0)) + 1
                record["attempts"] = attempts
                still.append(record)
        _write_spool(still)
    return filed


def spool_has_records() -> bool:
    """Cheap check: does the spool hold at least one pending record?"""
    try:
        with SPOOL_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    return True
    except (OSError, ValueError):
        return False
    return False


def _read_spool() -> list[dict]:
    records: list[dict] = []
    try:
        for line in SPOOL_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return records


def _write_spool(records: list[dict]) -> None:
    if not records:
        try:
            SPOOL_PATH.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = SPOOL_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(tmp, SPOOL_PATH)


def _ledger_finalize(record: dict, card_id: str, hash16: str) -> None:
    with _LedgerLock():
        entries = _read_ledger()
        for entry in entries:
            if entry.get("hash") == hash16 and not entry.get("card_id"):
                entry["card_id"] = card_id
                entry["finalized"] = True
                entry.pop("pending_card", None)
                break
        else:
            entries.append(
                {
                    "ts": time.time(),
                    "hash": hash16,
                    "quote": str(record.get("quote", "")),
                    "title": str(record.get("title", "")),
                    "session_id": record.get("session_id"),
                    "date": record.get("date"),
                    "card_id": card_id,
                    "finalized": True,
                }
            )
        _write_ledger(entries)


# ── Dedupe ─────────────────────────────────────────────────────────────────


def _dupe_display(dupe: Optional[str]) -> str:
    """Bare card id for the confirmation line (``ledger:t_x`` → ``t_x``)."""
    if not dupe:
        return "?"
    return dupe[len("ledger:") :] if dupe.startswith("ledger:") else dupe


def find_dupe(
    candidate_title: str,
    candidate_quote: str,
    ledger_entries: list[dict],
    ideas_titles: list[tuple[str, str]],
    other_titles: list[tuple[str, str]],
) -> Optional[str]:
    """Exact-hash + fuzzy-overlap + quote-prefix dupe check (spec §4).

    Returns the existing card id (``t_…``) or ``ledger:<id>`` marker.
    """
    h = title_hash(candidate_title)
    for entry in ledger_entries:
        if entry.get("hash") == h and entry.get("card_id"):
            return f"ledger:{entry['card_id']}"
    for tid, title in ideas_titles:
        if title_hash(title) == h:
            return tid
    for tid, title in ideas_titles + other_titles:
        if token_overlap(candidate_title, title) >= 0.6:
            return tid
    prefix = candidate_quote[:80]
    if prefix:
        for entry in ledger_entries:
            q = entry.get("quote", "")
            if q and q[:80] == prefix:
                return f"ledger:{entry.get('card_id') or entry.get('dupe_of') or '?'}"
    return None


# ── Public entry point ─────────────────────────────────────────────────────


def file_ideas(
    payload: dict,
    *,
    aux_call: Optional[Callable[..., Any]] = None,
    create: Optional[
        Callable[[str, str, str], tuple[Optional[str], Optional[str]]]
    ] = None,
    ledger_io: bool = True,
    now: Optional[float] = None,
) -> FilingResult:
    """File one captured idea payload (spec §5 contract) as triage card(s).

    ``payload`` is the capture-stage contract:
    ``{"session_id", "source", "transcript", "ts", "audio_sha16", "attempts"}``.

    Returns a :class:`FilingResult` whose ``text`` is the one confirmation
    line the chat renders (✅ / 🔁 / 🤷 / 💾 — spec §3.4); ``card_ids``
    echoes the created card id(s) back into the session.
    """
    now = now if now is not None else time.time()
    create = create or create_card
    transcript = str(payload.get("transcript") or "").strip()

    # 1) validate — empty / too-short transcripts are rejected, not filed
    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        return FilingResult(
            ok=False,
            kind="nothing_heard",
            text="🤷 Nothing heard — try again.",
        )

    session_id = str(payload.get("session_id") or "unknown-session")
    quote = transcript[:QUOTE_CHAR_CAP]
    secret = _has_secret(transcript)

    # 2) derive — aux-LLM (≤5 s, 1 retry) then deterministic fallback
    cards, source = derive_ideas(transcript, aux_call=aux_call)
    more = max(0, len(cards) - MAX_IDEAS_PER_UTTERANCE)
    cards = cards[:MAX_IDEAS_PER_UTTERANCE]
    derives = [source] * len(cards)

    # 3) dedupe — whole-utterance hash first (retry/re-speak), then per-card
    ledger_entries = _read_ledger() if ledger_io else []
    ideas_titles, other_titles = _all_board_titles() if ledger_io else ([], [])
    utterance_key = idem_key_for(_full_title(cards))
    if _utterance_already_filed(utterance_key, ledger_entries, ideas_titles):
        existing = _dupe_display(
            _existing_card_for(utterance_key, ledger_entries, ideas_titles)
        )
        if ledger_io:
            ledger_append(
                {
                    "ts": now,
                    "hash": title_hash(_full_title(cards)),
                    "quote": quote,
                    "dupe_of": existing,
                    "title": _full_title(cards),
                    "session_id": session_id,
                    "date": _date.today().isoformat(),
                }
            )
        return FilingResult(
            ok=True,
            kind="dupe",
            text=f"🔁 Already on the board: {existing}",
            duplicate_of=existing,
        )

    card_ids: list[str] = []
    filed_titles: list[str] = []  # derived titles, parallel to card_ids
    dupe_of: Optional[str] = None
    error: Optional[str] = None

    for card in cards:
        full_title = TITLE_PREFIX + card.title
        if len(card.title) > MAX_TITLE_CHARS:
            full_title = TITLE_PREFIX + _word_bound_cut(card.title, MAX_TITLE_CHARS)
        hash16 = title_hash(full_title)
        dupe = find_dupe(full_title, quote, ledger_entries, ideas_titles, other_titles)
        if dupe:
            dupe_of = _dupe_display(dupe)
            if ledger_io:
                ledger_append(
                    {
                        "ts": now,
                        "hash": hash16,
                        "quote": quote,
                        "dupe_of": dupe,
                        "title": full_title,
                        "session_id": session_id,
                        "date": _date.today().isoformat(),
                    }
                )
            continue
        body = build_card_body(card, session_id=session_id, quote=quote, secret=secret)
        card_id, err = create(full_title, body, hash16)
        if card_id:
            card_ids.append(card_id)
            filed_titles.append(card.title)
            ideas_titles.append((card_id, full_title))
            if ledger_io:
                ledger_append(
                    {
                        "ts": now,
                        "hash": hash16,
                        "quote": quote,
                        "title": full_title,
                        "session_id": session_id,
                        "date": _date.today().isoformat(),
                        "card_id": card_id,
                        "finalized": True,
                    }
                )
        else:
            error = err or "create-failed"
            break

    # every card a dupe → single 🔁 line for the utterance
    if not card_ids and dupe_of and not error:
        return FilingResult(
            ok=True,
            kind="dupe",
            text=f"🔁 Already on the board: {dupe_of}",
            duplicate_of=dupe_of,
            derives_used=derives,
        )

    if card_ids:
        # one ✅ line per filed card, correct card↔title pairing; a secret is
        # never echoed into the chat (spec §6.8) — only the card id shown
        lines = []
        for cid, title in zip(card_ids, filed_titles, strict=True):
            if secret:
                lines.append(f"✅ {cid}: idea saved (possible secret — not quoted)")
            else:
                lines.append(f"✅ {cid}: {title}")
        text = "\n".join(lines)
        if more:
            text += f"\n(+{more} more in source)"
        return FilingResult(
            ok=True,
            kind="filed",
            text=text,
            card_ids=card_ids,
            more_in_source=more,
            derives_used=derives,
        )

    # 4) all creates failed → spool every unfiled card for retry; never drop
    #    (spec §5).  Records use the batch intake lane's exact pending_cards
    #    format so that lane can file them if in-session retry exhausts.
    spool_path = None
    try:
        for card in cards[len(card_ids) :] or [cards[0]]:
            full_title = TITLE_PREFIX + (
                _word_bound_cut(card.title, MAX_TITLE_CHARS)
                if len(card.title) > MAX_TITLE_CHARS
                else card.title
            )
            spool_record = {
                "hash": title_hash(full_title),
                "title": full_title,
                "body": build_card_body(
                    card, session_id=session_id, quote=quote, secret=secret
                ),
                "session_id": session_id,
                "date": _date.today().isoformat(),
                "quote": quote,
                "source": str(payload.get("source") or "webui"),
                "ts": payload.get("ts") or now,
                "attempts": int(payload.get("attempts", 0) or 0),
            }
            spool_path = spool_pending(spool_record) or spool_path
            if ledger_io:
                ledger_append(
                    {
                        "ts": now,
                        "hash": spool_record["hash"],
                        "quote": quote,
                        "title": full_title,
                        "session_id": session_id,
                        "date": spool_record["date"],
                        "pending_card": True,
                    }
                )
    except Exception as exc:
        logger.error("spool write failed: %s", exc)
    return FilingResult(
        ok=False,
        kind="spooled",
        text="💾 Board unreachable — saved locally, will retry.",
        spool_path=spool_path,
        error=error,
        derives_used=derives,
    )


def _full_title(cards: list[IdeaCard]) -> str:
    """Composite title for utterance-level idempotency (multi-card safe)."""
    return TITLE_PREFIX + " + ".join(card.title for card in cards)


def _utterance_already_filed(
    key: str, ledger_entries: list[dict], ideas_titles: list[tuple[str, str]]
) -> bool:
    want = key[len("idea-") :]
    for entry in ledger_entries:
        if entry.get("hash") == want and (entry.get("card_id") or entry.get("dupe_of")):
            return True
    return False


def _existing_card_for(
    key: str, ledger_entries: list[dict], ideas_titles: list[tuple[str, str]]
) -> str:
    want = key[len("idea-") :]
    for entry in ledger_entries:
        if entry.get("hash") == want:
            cid = entry.get("card_id") or entry.get("dupe_of")
            if cid:
                return str(cid)
    return "?"
