"""Background runner + search for the Skills panel's "Hub" tab.

Wraps ``hermes skills search/install/update/uninstall`` as fresh CLI
subprocesses. This module deliberately never imports ``tools.skills_hub`` or
``tools.skills_guard`` into the long-lived WebUI process: both cache
``SKILLS_DIR``/``HERMES_HOME`` at import time, the same problem
``tools.skills_tool`` has for the *local* skills panel (see
``api/profiles.py``'s ``patch_skill_home_modules`` / ``snapshot_skill_home_modules``
/ ``restore_skill_home_modules``, which exist only to work around that for the
one module the WebUI *does* import). A subprocess spawned fresh per request
always resolves paths from the ``HERMES_HOME`` env var set on its own
environment, so it can never go stale across a WebUI profile switch.

Pre-install scanning without installing: ``hermes skills install <id>`` runs
its security scan and prints the verdict *before* the "Confirm [y/N]:"
prompt and *before* any file is copied into ``skills/`` (verified by reading
``hermes_cli/skills_hub.py::do_install`` and by a live dry run against a
throwaway HERMES_HOME). There is no non-mutating "scan only" CLI subcommand,
so the "scan" action here pre-feeds the subprocess's stdin with "n" (decline
the prompt) via a pipe -- the CLI's own cancel path removes the quarantine
directory, so this never leaves a partial install or reads any scan module in
this process. The real "install" action instead passes ``--yes`` (and
optionally ``--force``), which is a normal, always-available CLI flag.

Uninstall has no ``--yes``/``-y`` flag in the CLI (verified via
``hermes skills uninstall --help``) and the plain CLI path always prompts, so
this module pre-feeds "y" the same way; the WebUI's own confirm dialog is the
real consent gate for that action, the same client-confirm-then-fire pattern
used elsewhere in this codebase for destructive server actions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ACTIONS = ("scan", "install", "update", "uninstall")

_ACTION_TIMEOUT_SECONDS = 300
# SIGTERM -> SIGKILL escalation window for the owned process TREE, and how long
# we wait for the group to actually disappear after the kill.
_TREE_TERM_GRACE_SECONDS = 5
_TREE_KILL_WAIT_SECONDS = 5


def _spawn(cmd: list[str], **kwargs):
    """Module-local process-spawn seam. Tests replace THIS, never ``Popen``.

    Monkeypatching ``subprocess.Popen`` swaps the object on the *shared* stdlib
    module, and ``subprocess.run()`` uses that same object as a context
    manager. Any unrelated ``subprocess.run()`` executed while such a patch is
    active therefore dies with "does not support the context manager protocol"
    — including the one ``api.agent_runtime._read_agent_revision()`` issues
    when a route module is first imported. That made this module's action tests
    pass only when some earlier test had already imported ``api.routes``, i.e.
    it made them order-dependent. A module-local seam is not shared with
    anyone, so patching it cannot reach an unrelated caller.
    """
    return subprocess.Popen(cmd, **kwargs)


def _run_capture(cmd: list[str], **kwargs):
    """Module-local synchronous-run seam — same rationale as ``_spawn``."""
    return subprocess.run(cmd, **kwargs)


def _process_group_alive(pgid: int) -> bool:
    """True while any process remains in *pgid*.

    ``killpg(pgid, 0)`` is the POSIX existence probe: it raises
    ``ProcessLookupError`` only when the group is empty. ``PermissionError``
    means the group exists but is not ours — treat that as alive rather than
    silently declaring extinction.
    """
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _extinguish_process_tree(proc) -> bool:
    """Make sure NO descendant of *proc* survives. True once the tree is gone.

    Called after the leader has been reaped on the timeout path. Reaping the
    leader says nothing about its children: a `git clone` or scanner spawned by
    the CLI can outlive a leader that exited cleanly on SIGTERM, and would then
    run concurrently with the next admitted action against the same skills
    directory.

    The return value is the whole point: ``False`` means descendants are still
    alive after SIGKILL, and the caller must NOT release the action slot on
    that answer. Logging the condition and returning — which is what this used
    to do — admitted the next run into a directory a survivor still owned.
    """
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name != "posix":
        # Windows has no process group to signal here (`start_new_session` is
        # POSIX-only). Best-effort tree kill via taskkill; nothing else in the
        # stdlib walks the tree.
        try:
            _run_capture(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TREE_KILL_WAIT_SECONDS,
                check=False,
            )
        except Exception:
            logger.debug("taskkill tree cleanup failed for pid %s", pid, exc_info=True)
            return False
        # taskkill /T reports success only once the tree is gone; there is no
        # portable group-existence probe to re-verify with on this platform.
        return True
    deadline = time.monotonic() + _TREE_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _process_group_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + _TREE_KILL_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _process_group_alive(pid):
            return True
        time.sleep(0.05)
    logger.error(
        "skills hub action left descendants alive in process group %s after SIGKILL",
        pid,
    )
    return False


def _release_after_extinction(slot: "_ActionSlot", gen: int, pgid: int) -> None:
    """Keep the action slot closed until process group *pgid* is empty.

    The escalation already tried SIGTERM and SIGKILL against the group and
    failed to prove extinction — typically a descendant wedged in
    uninterruptible I/O. Releasing the slot now would admit a second CLI into
    a skills directory the survivor still owns, so instead the caller keeps
    getting an honest 409 and a watcher releases the slot the moment the group
    really is gone.

    Unbounded on purpose: there is no timeout after which an alive process
    becomes safe to ignore.
    """
    logger.error(
        "skills hub: process group %s survived SIGKILL; the action slot stays "
        "closed until it exits",
        pgid,
    )

    def _watch() -> None:
        try:
            while _process_group_alive(pgid):
                time.sleep(_EXTINCTION_POLL_SECONDS)
        finally:
            with slot.state_lock:
                slot.running = False
            logger.info("skills hub: process group %s is gone; slot released", pgid)

    try:
        threading.Thread(
            target=_watch, name=f"hermes-webui-skills-hub-reaper-{pgid}", daemon=True
        ).start()
    except Exception:
        # Nothing left that could prove extinction. Leaving the slot closed is
        # the safe direction: the alternative admits a concurrent writer.
        logger.exception(
            "skills hub: could not start the reaper for process group %s; the "
            "action slot stays closed",
            pgid,
        )


_SEARCH_TIMEOUT_SECONDS = 35
_EXTINCTION_POLL_SECONDS = 0.25
_LOG_TAIL_MAX_CHARS = 200_000
# Wall-clock slack when binding a lock entry's own stamp to this run's start.
_LOCK_STAMP_TOLERANCE_SECONDS = 1.0

# Captured CLI output is useful to diagnose hub actions, but it is a browser
# response surface. Redact common inline secret assignments while leaving
# ordinary status/progress output readable. Scan finding bodies are handled
# separately below because a finding is explicitly a matched source fragment.
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"[a-z][a-z0-9_]*(?:access_key|secret_access_key)|cookie|session(?:[_-]?(?:id|token|key))?)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_AUTHORIZATION_BASIC_RE = re.compile(r"(?i)\bauthorization\s*:\s*basic\s+[^\s,;]+")
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\r\n]+")

# Run state is owned by (authenticated principal, canonical profile) and every
# run additionally carries an opaque run id (gate finding 3).
#
# Keying by the raw profile alone was not ownership: the profile name is
# derived from a client-controlled cookie, so two different callers resolving
# to the same profile string shared one another's transcript, error text and
# scan metadata, and could cancel each other out of the single-flight slot. The
# principal is the authenticated identity (or the local-peer pseudo-principal
# for the passwordless-on-localhost posture); the run id is unguessable and
# lets a caller prove which run its status/continuation refers to across a
# profile switch.
_REGISTRY_LOCK = threading.Lock()

# Terminal entries are retained only so the UI can read the result of the run
# it just finished; they are not a log store. Retention is bounded twice — by
# age and by count — so a long-lived WebUI process cannot accumulate state
# keyed by an unbounded set of principal/profile pairs.
_TERMINAL_TTL_SECONDS = 900
_MAX_SLOTS = 64

_IDLE_STATE: dict = {
    "action": None,
    "target": None,  # identifier (scan/install) or name (uninstall/update)
    "status": "idle",  # idle | running | completed | failed | timeout
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": "",
    "error": None,
    "scan_result": None,  # populated whenever the transcript contains a scan report
    "run_id": None,
}

_TERMINAL_STATUSES = ("completed", "failed", "timeout")


def canonical_profile(profile: object) -> str:
    """One spelling per profile, so ownership cannot be split by whitespace."""
    return str(profile or "").strip() or "default"


def canonical_principal(principal: object) -> str:
    """One spelling per identity. Empty means "no authenticated identity"."""
    return str(principal or "").strip() or ""


class _ActionSlot:
    def __init__(self, key: tuple[str, str]) -> None:
        self.key = key
        self.state_lock = threading.Lock()
        self.state = dict(_IDLE_STATE)
        self.running = False
        # When this slot went terminal, for the retention clock. Kept as its
        # own attribute rather than read off `state["finished_at"]`: a slot
        # whose state was set without that field would otherwise look
        # infinitely old and be evicted on its very first read.
        self.retired_at: float | None = None

    def is_terminal(self) -> bool:
        return not self.running and self.state.get("status") in _TERMINAL_STATUSES

    def retirement_stamp(self, now: float) -> float | None:
        """Retirement time of a terminal slot, stamped on first observation."""
        if not self.is_terminal():
            return None
        if self.retired_at is None:
            self.retired_at = float(self.state.get("finished_at") or 0.0) or now
        return self.retired_at


_SLOTS: dict[tuple[str, str], _ActionSlot] = {}


def _evict_slots_locked(now: float) -> None:
    """Drop retired slots. Never touches a slot that still owns a process."""
    for key, slot in list(_SLOTS.items()):
        if slot.running:
            continue
        stamp = slot.retirement_stamp(now)
        if stamp is not None and now - stamp > _TERMINAL_TTL_SECONDS:
            del _SLOTS[key]
    if len(_SLOTS) <= _MAX_SLOTS:
        return
    # Still over the bound: retire the least recently finished slots first.
    # An idle slot that never ran sorts oldest and goes first.
    evictable = sorted(
        (
            (slot.retirement_stamp(now) or 0.0, key)
            for key, slot in _SLOTS.items()
            if not slot.running
        ),
        key=lambda item: item[0],
    )
    for _stamp, key in evictable:
        if len(_SLOTS) <= _MAX_SLOTS:
            break
        del _SLOTS[key]


def _slot_key(principal: object, profile: object) -> tuple[str, str]:
    return (canonical_principal(principal), canonical_profile(profile))


def _get_slot(principal: object, profile: object) -> _ActionSlot | None:
    """Look a slot up WITHOUT creating one — the read path never allocates."""
    with _REGISTRY_LOCK:
        _evict_slots_locked(time.time())
        return _SLOTS.get(_slot_key(principal, profile))


def _open_slot(principal: object, profile: object) -> _ActionSlot:
    """Look a slot up, creating it. Only a run that is about to start may."""
    key = _slot_key(principal, profile)
    with _REGISTRY_LOCK:
        _evict_slots_locked(time.time())
        slot = _SLOTS.get(key)
        if slot is None:
            slot = _ActionSlot(key)
            _SLOTS[key] = slot
        return slot


# ── Evidence-based completion: the CLI exits 0 for fetch failures, scanner
# blocks, already-installed no-ops and invalid uninstall targets, so the return
# code alone is NOT authority.
#
# Neither is the transcript. It carries attacker-influenced content — a skill's
# own name, description and the scanner's matched source fragments are echoed
# into it — so a crafted skill could print "Installed: ..." and impersonate a
# completed install. Success for a mutation is therefore established against
# the hub's own lock artifact (`<skills_dir>/.hub/lock.json`), snapshotted
# before and after the run and compared for the requested target. The
# transcript is retained for diagnostics only: it explains WHY nothing
# changed, it never decides THAT something did.


def hub_lock_snapshot(skills_dir: Path) -> dict[str, dict]:
    """Machine-readable installed-state snapshot, keyed by skill name.

    ``<skills_dir>/.hub/lock.json`` is the hub's own artifact: the CLI writes
    it, the browser cannot influence it, and it records the identifier and the
    install/update timestamps per entry. It is the postcondition authority for
    every mutation.
    """
    return {
        str(entry.get("name") or ""): entry
        for entry in list_installed_hub_skills(skills_dir)
        if entry.get("name")
    }


def _lock_snapshot_or_none(skills_dir: Path) -> dict[str, dict] | None:
    """Mutation-grade read of ``<skills_dir>/.hub/lock.json``. UNKNOWN = None.

    This is deliberately NOT ``list_installed_hub_skills``. That function is
    the display path and is lenient by design: malformed JSON, a wrong
    top-level shape and a non-dict ``installed`` field all collapse to "no hub
    installs". Feeding that into a postcondition turns a corrupt lock file into
    the observation ``{}`` — and an uninstall would then "succeed" precisely
    because the file that proves otherwise had become unreadable.

    So the rule here is stricter than best effort: ``{}`` may only mean the
    file is genuinely ABSENT. Unreadable bytes, undecodable text, malformed
    JSON, a non-dict document, a missing or non-dict ``installed`` map, a
    non-string key and a non-dict entry each leave the installed set in doubt
    and therefore return UNKNOWN, which every mutation treats as failure.

    The bytes are read exactly once and parsed from that one buffer, so a
    concurrent rewrite cannot be observed half-old/half-new across two reads.
    """
    lock_path = Path(skills_dir) / ".hub" / "lock.json"
    try:
        raw = lock_path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        # Includes a directory in the file's place, permission denial and a
        # read race that loses the inode mid-read.
        logger.warning("hub lock file unreadable at %s", lock_path, exc_info=True)
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("hub lock file at %s is not valid JSON", lock_path)
        return None
    if not isinstance(document, dict) or "installed" not in document:
        logger.warning("hub lock file at %s has an unexpected top-level shape", lock_path)
        return None
    installed = document.get("installed")
    if not isinstance(installed, dict):
        logger.warning("hub lock file at %s has a non-mapping 'installed' field", lock_path)
        return None
    snapshot: dict[str, dict] = {}
    for name, entry in installed.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            logger.warning("hub lock file at %s carries an invalid installed entry", lock_path)
            return None
        snapshot[name] = entry
    return snapshot


def _entry_mutation_time(entry: dict | None) -> float | None:
    """When the CLI last wrote this entry, as a POSIX timestamp.

    ``tools.skills_hub.HubLockFile.record_install`` stamps both fields with
    ``datetime.now(timezone.utc).isoformat()`` on every install AND update, so
    the newer of the two is the entry's last mutation. Returns ``None`` when no
    usable stamp is present — callers must treat that as "cannot bind", never
    as "old".
    """
    if not isinstance(entry, dict):
        return None
    stamps = []
    for key in ("updated_at", "installed_at"):
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            continue
        text = value.strip()
        if text.endswith(("z", "Z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamps.append(parsed.timestamp())
    return max(stamps) if stamps else None


def _entry_for_target(snapshot: dict[str, dict], target: str) -> dict | None:
    """Find *target* in a lock snapshot by skill name OR hub identifier."""
    needle = str(target or "").strip()
    if not needle:
        return None
    entry = snapshot.get(needle)
    if entry is not None:
        return entry
    for candidate in snapshot.values():
        if str(candidate.get("identifier") or "") == needle:
            return candidate
    return None


def _entry_revision(entry: dict | None) -> tuple:
    """Everything about a lock entry that a real mutation must change."""
    if not isinstance(entry, dict):
        return ()
    return (
        entry.get("identifier"),
        entry.get("install_path"),
        entry.get("installed_at"),
        entry.get("updated_at"),
        entry.get("scan_verdict"),
        entry.get("trust_level"),
        # The CLI records the installed bundle's own digest. It is the one
        # field that changes when the CONTENT changed and nothing else did.
        entry.get("content_hash"),
    )


def _evaluate_outcome(
    action: str,
    log: str,
    scan_result,
    *,
    target: str = "",
    before: dict[str, dict] | None = None,
    after: dict[str, dict] | None = None,
    started_at: float | None = None,
) -> tuple[bool, str | None]:
    """Return (ok, error). rc==0 and no timeout are already established.

    Success for a MUTATION is established by the hub's own lock artifact
    changing in the expected direction for the expected target — never by
    matching human sentences in the merged stdout transcript. That transcript
    contains attacker-influenced content (a skill's own name, description and
    scanner findings are echoed into it), so a crafted skill could print
    ``Installed: ...`` and impersonate a completed install. The transcript is
    kept for diagnostics only, to explain WHY a run did not change anything.

    ``before``/``after`` are lock snapshots taken around the run. When they are
    unavailable the mutation fails closed: an unverifiable mutation is not a
    successful one.

    ``started_at`` binds the observation to THIS invocation. A changed entry is
    not by itself evidence that *this* run changed it — a concurrent CLI in the
    same profile home moves the same file. An install/update is therefore only
    accepted when the entry's own recorded mutation time lies inside this run's
    window; an entry that carries no parseable stamp cannot be bound and fails
    closed.
    """
    decision = (scan_result or {}).get("decision") if isinstance(scan_result, dict) else None
    if action == "scan":
        # Read-only: the parsed report IS the product, and it is derived from
        # this run's own output rather than asserting that state changed.
        if scan_result is not None:
            return True, None
        return False, "The CLI produced no scan report for this identifier."

    if action not in ("install", "update", "uninstall"):
        return False, f"Unknown action {action!r}"

    if before is None or after is None:
        return False, (
            "Could not verify the result against the hub lock file; "
            "treating the action as failed."
        )

    before_entry = _entry_for_target(before, target)
    after_entry = _entry_for_target(after, target)

    def _written_by_this_run(entry: dict | None) -> tuple[bool, str | None]:
        """Is *entry*'s recorded mutation time inside this run's window?"""
        if started_at is None:
            return True, None
        stamp = _entry_mutation_time(entry)
        if stamp is None:
            return False, (
                "The hub lock entry carries no usable timestamp, so this run "
                "cannot be proven to have written it."
            )
        # The CLI stamps with whole-microsecond wall clock while `started_at`
        # is taken in this process; allow a second of skew rather than
        # rejecting a genuine write that landed on the boundary.
        if stamp + _LOCK_STAMP_TOLERANCE_SECONDS < started_at:
            return False, (
                "The hub lock entry predates this run; it was not written by "
                "this action."
            )
        return True, None

    if action == "install":
        if decision == "BLOCKED":
            return False, "Blocked by the security scan."
        if after_entry is None:
            if "is already installed at" in log:
                return False, "Already installed — nothing was changed."
            return False, "The hub lock file records no installed skill for this identifier."
        if before_entry is not None and _entry_revision(before_entry) == _entry_revision(after_entry):
            return False, "Already installed — nothing was changed."
        return _written_by_this_run(after_entry)

    if action == "update":
        if decision == "BLOCKED":
            return False, "Update blocked by the security scan."
        if after_entry is None:
            return False, "The skill is no longer present in the hub lock file after the update."
        if before_entry is None:
            # An "update" that installed something is still a real change, but
            # it is not what was asked for; report it rather than hiding it.
            return False, "The skill was not installed before this update."
        if _entry_revision(before_entry) == _entry_revision(after_entry):
            if "No updates available." in log:
                return False, "No updates available — nothing was changed."
            return False, "The hub lock file is unchanged; nothing was updated."
        return _written_by_this_run(after_entry)

    # uninstall
    if before_entry is None:
        return False, "Not a hub-installed skill — nothing was removed."
    if after_entry is not None:
        return False, "The skill is still recorded in the hub lock file."
    return True, None


def _resolve_hermes_command() -> str:
    """Resolve the CLI path (mirrors api/gateway_restart.py's helper)."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd
    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _command_for_action(
    action: str,
    target: str,
    *,
    category: str = "",
    name_override: str = "",
) -> list[str]:
    hermes_cmd = _resolve_hermes_command()
    # "--" marks the end of option parsing for argparse, so identifier/name
    # values that start with "-" (e.g. a hub identifier or a plugin/skill
    # name an attacker crafted to look like a flag) are always taken as the
    # positional argument instead of being swallowed as an unrecognized
    # option -- verified live: without "--", `hermes skills install "-x/-y"`
    # never reaches the identifier positional at all (argparse rejects it
    # before the CLI's own logic runs); with "--" it does. It must be the
    # LAST token before the positional, with every real flag placed before
    # it -- argparse treats everything after "--" as positional, so a flag
    # placed after "--" would itself be swallowed as a second positional.
    if action == "scan":
        # No --yes / --force: runs the scan, then stops at (or before) the
        # confirm prompt. Never installs anything on its own.
        return [hermes_cmd, "skills", "install", "--", target]
    if action == "install":
        # NO --force, ever (gate finding 1): the browser must not be able to
        # override a security verdict. Replacement flows go through the
        # scan-gated update pipeline instead.
        cmd = [hermes_cmd, "skills", "install", "--yes"]
        if category:
            cmd.extend(["--category", category])
        if name_override:
            cmd.extend(["--name", name_override])
        cmd.extend(["--", target])
        return cmd
    if action == "update":
        cmd = [hermes_cmd, "skills", "update"]
        if target:
            cmd.extend(["--", target])
        return cmd
    if action == "uninstall":
        return [hermes_cmd, "skills", "uninstall", "--", target]
    raise ValueError(f"unsupported hub action: {action!r}")


# stdin pre-answer for the CLI's single "Confirm [y/N]:" prompt, or None to
# leave stdin closed (DEVNULL) for actions that never prompt.
_STDIN_ANSWER = {
    "scan": "n\n",
    "install": None,  # --yes skips the prompt
    "update": None,  # do_update() forces skip_confirm internally
    "uninstall": "y\n",
}


# ---------------------------------------------------------------------------
# Scan report parsing (best-effort text parsing of a private CLI transcript --
# see tools.skills_guard.format_scan_report; not a stable JSON API)
# ---------------------------------------------------------------------------

# The parenthesized part is "<source>/<trust_level>" where <source> itself may
# contain slashes (it is often the full hub identifier, e.g.
# "skills-sh/anthropics/skills/pdf/trusted") -- trust_level is always one of
# exactly three known values, so anchor on that fixed set from the right
# rather than splitting on the first slash (verified against live output).
_SCAN_HEADER_RE = re.compile(
    r"^Scan:\s+(?P<name>.+?)\s+\((?P<source>.+)/(?P<trust_level>builtin|trusted|community)\)"
    r"\s+Verdict:\s+(?P<verdict>SAFE|CAUTION|DANGEROUS)\s*$"
)
_DECISION_RE = re.compile(
    r"^Decision:\s+(?P<decision>ALLOWED|NEEDS CONFIRMATION|BLOCKED)\s+—\s+(?P<reason>.+)$"
)
_FINDING_RE = re.compile(
    r'^\s{2}(?P<severity>CRITICAL|HIGH|MEDIUM|LOW)\s+(?P<category>\S+)\s+(?P<location>\S+)\s+"(?P<match>.*)"\s*$'
)
# A terminal renderer may wrap the quoted ``match`` field before its closing
# quote. Keep this deliberately narrower than a generic indented log line: it
# recognizes only the fixed scan-finding prefix, including an incomplete record.
_FINDING_RECORD_START_RE = re.compile(
    r"^\s{2}(?:CRITICAL|HIGH|MEDIUM|LOW)\s+\S+\s+\S+(?:\s|$)"
)


def parse_scan_report(log_text: str) -> dict | None:
    """Extract the structured scan verdict from a captured CLI transcript.

    Returns ``None`` when no scan header line is found (e.g. the identifier
    could not be resolved at all) -- callers must treat that as "no verdict
    available", never as a safe/clean result.
    """
    header = None
    decision = None
    findings = []
    for raw_line in log_text.splitlines():
        line = raw_line.rstrip()
        if header is None:
            m = _SCAN_HEADER_RE.match(line)
            if m:
                header = m.groupdict()
                continue
        m = _FINDING_RE.match(line)
        if m:
            findings.append(m.groupdict())
            continue
        m = _DECISION_RE.match(line)
        if m:
            decision = m.groupdict()
    if header is None:
        return None
    return {
        "name": header["name"],
        "source": header["source"],
        "trust_level": header["trust_level"],
        "verdict": header["verdict"].lower(),
        "findings": findings,
        "decision": (decision or {}).get("decision"),
        "decision_reason": (decision or {}).get("reason"),
    }


def _redact_sensitive_text(text: object) -> object:
    """Return text safe for browser-visible action status responses."""
    if not isinstance(text, str):
        return text
    text = _COOKIE_HEADER_RE.sub("Cookie: <redacted>", text)
    text = _AUTHORIZATION_BASIC_RE.sub("Authorization: Basic <redacted>", text)
    text = _INLINE_SECRET_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}<redacted>", text
    )
    return _BEARER_TOKEN_RE.sub("Bearer <redacted>", text)


def _redact_scan_finding_lines(log: object) -> object:
    """Fail closed after a raw scan finding enters a browser transcript.

    ``tools.skills_guard.format_scan_report`` writes ``Finding.match`` directly
    into a finding line. Its ``match[:60]`` value can contain newlines, blank
    rows, or text shaped like a later ``Scan:``/``Decision:`` line, so no raw
    line after a finding prefix is a trustworthy record delimiter. Keep only
    independent progress emitted before that prefix; the private runner
    transcript in ``_STATE`` remains intact and ``scan_result`` supplies the
    browser's structured, match-free finding metadata.
    """
    if not isinstance(log, str):
        return log
    safe_lines = []
    for raw_line in log.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if _FINDING_RECORD_START_RE.match(line):
            newline = raw_line[len(line):]
            safe_lines.append("[scan finding redacted; remaining scan transcript suppressed]" + newline)
            break
        safe_lines.append(raw_line)
    return "".join(safe_lines)


def _safe_scan_decision(scan_result: dict) -> str | None:
    """Derive the scanner's non-force decision without accepting raw report text."""
    trust_level = scan_result.get("trust_level")
    verdict = scan_result.get("verdict")
    if trust_level not in {"builtin", "trusted", "community"}:
        return None
    if verdict not in {"safe", "caution", "dangerous"}:
        return None
    if trust_level == "builtin" or (trust_level == "trusted" and verdict != "dangerous"):
        return "ALLOWED"
    if trust_level == "community" and verdict == "safe":
        return "ALLOWED"
    return "BLOCKED"


def _safe_status_projection(state: dict) -> dict:
    """Project private runner state onto the Skills Hub browser API contract."""
    status = dict(state)
    status["log"] = _redact_sensitive_text(_redact_scan_finding_lines(status.get("log", "")))
    status["error"] = _redact_sensitive_text(status.get("error"))
    scan_result = status.get("scan_result")
    if isinstance(scan_result, dict):
        safe_scan_result = {
            key: scan_result[key]
            for key in ("name", "source", "trust_level", "verdict")
            if key in scan_result
        }
        decision = _safe_scan_decision(scan_result)
        if decision is not None:
            safe_scan_result["decision"] = decision
        findings = scan_result.get("findings")
        if isinstance(findings, list):
            # `match` is a raw fragment of scanned Skill content. Its metadata
            # remains useful to explain the verdict, but never expose its body.
            safe_scan_result["findings"] = [
                {
                    key: finding[key]
                    for key in ("severity", "category", "location")
                    if key in finding
                }
                if isinstance(finding, dict)
                else finding
                for finding in findings
            ]
        status["scan_result"] = safe_scan_result
    return status


# ---------------------------------------------------------------------------
# Search (synchronous, read-only, no single-flight lock)
# ---------------------------------------------------------------------------


def search_hub_skills(query: str, *, source: str = "all", limit: int = 20) -> dict:
    """Run ``hermes skills search <query> --json`` and parse its stdout.

    Synchronous: search is read-only and typically completes well within
    ``_SEARCH_TIMEOUT_SECONDS`` (the CLI itself caps its own fan-out at 30s).
    Raises ``RuntimeError`` on a non-zero exit or unparseable output so the
    route layer can turn that into a clean error response.
    """
    from api.profiles import get_active_hermes_home

    hermes_home = Path(get_active_hermes_home())
    hermes_cmd = _resolve_hermes_command()
    # Keep all actual options before the end-of-options marker. The query is
    # user-controlled and must remain positional even when it starts with "-".
    cmd = [
        hermes_cmd,
        "skills",
        "search",
        "--source",
        source or "all",
        "--limit",
        str(max(1, min(int(limit or 20), 50))),
        "--json",
        "--",
        query,
    ]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    try:
        result = _run_capture(
            cmd,
            cwd=str(hermes_home) if hermes_home.exists() else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Search timed out after {_SEARCH_TIMEOUT_SECONDS}s") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Search failed: {detail or 'exit code ' + str(result.returncode)}")

    try:
        results = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Search returned unparseable output") from exc

    if not isinstance(results, list):
        raise RuntimeError("Search returned unexpected output shape")
    return {"results": results}


# ---------------------------------------------------------------------------
# Installed hub skills (read the lock file directly -- no module import)
# ---------------------------------------------------------------------------


def list_installed_hub_skills(skills_dir: Path) -> list[dict]:
    """Read ``<skills_dir>/.hub/lock.json`` and return its installed entries.

    Reads the file directly with ``json.load`` -- never imports
    ``tools.skills_hub.HubLockFile`` -- so this has none of the import-time
    ``SKILLS_DIR`` binding risk described in the module docstring. Best
    effort: a missing or unparseable lock file yields an empty list rather
    than raising, matching how a profile with no hub installs looks.
    """
    lock_path = Path(skills_dir) / ".hub" / "lock.json"
    if not lock_path.exists():
        return []
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("Failed to read hub lock file at %s", lock_path, exc_info=True)
        return []
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return []
    out = []
    for name, entry in installed.items():
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "name": name,
                "source": entry.get("source"),
                "identifier": entry.get("identifier"),
                "trust_level": entry.get("trust_level"),
                "scan_verdict": entry.get("scan_verdict"),
                "install_path": entry.get("install_path"),
                "installed_at": entry.get("installed_at"),
                "updated_at": entry.get("updated_at"),
            }
        )
    out.sort(key=lambda e: (e.get("name") or ""))
    return out


# ---------------------------------------------------------------------------
# Single-flight background runner (scan / install / update / uninstall)
# ---------------------------------------------------------------------------


def _append_log(hub: "_ActionSlot", gen: int, text: str) -> None:
    if not text:
        return
    with hub.state_lock:
        if hub.state.get("_gen") != gen:
            return  # late reader from a superseded run: never poison new state
        hub.state["log"] += text
        if len(hub.state["log"]) > _LOG_TAIL_MAX_CHARS:
            hub.state["log"] = hub.state["log"][-_LOG_TAIL_MAX_CHARS:]


def _drain_stdout(hub: "_ActionSlot", gen: int, stream) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            _append_log(hub, gen, line)
    except Exception:
        logger.debug("skills hub log reader failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _busy_snapshot() -> dict:
    """What one owner may learn about ANOTHER owner's world: nothing.
    Each (principal, profile) pair has its own slot; this envelope only exists
    for legacy callers that query without a resolvable owner."""
    return {"status": "busy", "busy": True}


def _unknown_status() -> dict:
    """The answer for "no run of yours exists" — a fresh idle projection.

    Deliberately built from the idle template instead of touching the
    registry: a status poll must not be able to create state. Otherwise any
    unauthenticated poll loop with a varying profile cookie would allocate an
    entry per request and the registry would grow without a run ever starting.
    """
    return _safe_status_projection(dict(_IDLE_STATE))


def get_status(profile: str = "default", *, principal: object = "", run_id: object = "") -> dict:
    """Status of the caller's OWN run. Never allocates, never reveals others'.

    ``run_id`` is the caller's proof of which run it is asking about. When it
    is supplied and does not match the slot's current run, the answer is the
    idle projection rather than the current run's state: a continuation issued
    before a profile switch must not silently start reporting on whatever run
    happens to occupy the slot now.
    """
    slot = _get_slot(principal, profile)
    if slot is None:
        return _unknown_status()
    with slot.state_lock:
        snapshot = dict(slot.state)
    snapshot.pop("_gen", None)
    wanted = str(run_id or "").strip()
    if wanted and wanted != str(snapshot.get("run_id") or ""):
        return _unknown_status()
    return _safe_status_projection(snapshot)


def start_action(
    action: str,
    target: str = "",
    *,
    category: str = "",
    name_override: str = "",
    identifier: str = "",
    profile: str = "default",
    principal: object = "",
) -> tuple[bool, dict]:
    """Start *action* for this owner if the owner's slot is free.

    Single-flight PER (PRINCIPAL, PROFILE) (gate finding 3): distinct owners
    neither see nor block each other, and the returned snapshot carries an
    opaque ``run_id`` the caller must present to read this run's status again.
    Returns ``(started, status_snapshot)``; ``started=False`` → HTTP 409 with
    the caller's OWN state.

    ``update`` runs a two-phase, fail-closed pipeline (gate finding 1): a
    plain scan of the installed skill's identifier first, and the actual
    update only when the scanner's NON-FORCE decision is ALLOWED — the
    browser can never override a security verdict, and a denial leaves the
    installed tree untouched because the update CLI is never invoked.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported hub action: {action!r}")
    if action in ("scan", "install") and not target:
        raise ValueError("identifier is required")
    if action == "uninstall" and not target:
        raise ValueError("name is required")
    if action == "update" and not identifier:
        raise ValueError(
            "identifier is required for a verified update (the scan phase "
            "needs the hub identifier of the installed skill)"
        )

    hub = _open_slot(principal, profile)
    with hub.state_lock:
        if hub.running:
            already = True
        else:
            hub.running = True
            already = False
    if already:
        return False, get_status(profile, principal=principal)

    # Imported locally (not at module load) so the active profile is always
    # resolved fresh -- see the module docstring's SKILLS_DIR warning, and
    # api.routes._run_gateway_lifecycle_command for the established pattern.
    from api.profiles import get_active_hermes_home

    # get_active_hermes_home() can raise (profile resolution failure) just
    # like the command-building calls below -- both must stay inside this
    # try/except so the slot reserved above is always released on failure.
    try:
        hermes_home = Path(get_active_hermes_home())
        # Bound to THIS run's profile home, captured once: a profile switch
        # mid-run must not move the postcondition target.
        skills_dir = hermes_home / "skills"
        phases: list[tuple[str, list[str], str | None]] = []
        if action == "update":
            phases.append(
                ("scan", _command_for_action("scan", identifier), _STDIN_ANSWER.get("scan"))
            )
            phases.append(
                ("update", _command_for_action("update", target), _STDIN_ANSWER.get("update"))
            )
        else:
            phases.append(
                (
                    action,
                    _command_for_action(
                        action, target, category=category, name_override=name_override
                    ),
                    _STDIN_ANSWER.get(action),
                )
            )
    except Exception:
        with hub.state_lock:
            hub.running = False
        raise

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    # Rich (used for all CLI console output here) wraps text to the detected
    # console width even when stdout is not a tty; a narrow default width
    # (80) breaks scan-report finding lines across two lines and defeats the
    # line-anchored regexes above. A wide COLUMNS makes the transcript
    # reliably single-line per record (verified live).
    env["COLUMNS"] = "400"

    # Wall clock, taken before the first phase spawns: the postcondition binds
    # the lock entry's own stamp against this instant, so a lock entry written
    # before the run started can never count as this run's work.
    run_started_at = time.time()
    run_id = secrets.token_urlsafe(16)

    with hub.state_lock:
        gen = int(hub.state.get("_gen") or 0) + 1
        hub.retired_at = None  # a new run restarts the retention clock
        hub.state = dict(_IDLE_STATE)
        hub.state.update(
            {
                "_gen": gen,
                "action": action,
                "target": target or None,
                "status": "running",
                "started_at": run_started_at,
                "run_id": run_id,
            }
        )

    def _signal_group(proc, sig: int) -> None:
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.send_signal(sig)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _exec_phase(cmd: list[str], stdin_answer: str | None) -> tuple[int, bool, int | None]:
        """Run one CLI phase, streaming into the log.

        Returns ``(rc, timed_out, unextinguished_pgid)``. The third element is
        the process group that is STILL ALIVE after the full kill escalation,
        or ``None`` when the owned tree is provably gone. The caller must keep
        the action slot closed for as long as it is not ``None``: admitting the
        next run while a survivor still writes into the same skills directory
        is exactly the overlap this escalation exists to prevent.
        """
        proc = _spawn(
            cmd,
            stdin=subprocess.PIPE if stdin_answer is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(hermes_home) if hermes_home.exists() else None,
            start_new_session=True,
        )
        if stdin_answer is not None:
            try:
                proc.stdin.write(stdin_answer)
                proc.stdin.close()
            except Exception:
                logger.debug("Failed to pre-answer hub subprocess stdin", exc_info=True)
        reader = threading.Thread(
            target=_drain_stdout, args=(hub, gen, proc.stdout), daemon=True
        )
        reader.start()
        survivor_pgid: int | None = None
        try:
            rc = proc.wait(timeout=_ACTION_TIMEOUT_SECONDS)
            timed = False
        except subprocess.TimeoutExpired:
            timed = True
            logger.error(
                "skills hub action %r timed out after %ss; terminating process group",
                action,
                _ACTION_TIMEOUT_SECONDS,
            )
            _signal_group(proc, signal.SIGTERM)
            try:
                rc = proc.wait(timeout=_TREE_TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                # Reap-before-release: block until the leader is gone. A
                # process stuck in uninterruptible I/O keeps this profile's
                # slot closed -- honest 409s instead of overlapping trees.
                rc = proc.wait()
            # Reaping the LEADER is not extinguishing the TREE. If the leader
            # exits on SIGTERM while a git/scanner descendant survives, the
            # branch above never escalates, and that descendant overlaps the
            # next admitted run — writing into the same skills directory.
            # Verify extinction (and force it) before returning, i.e. before
            # `_run.finally` releases the profile slot.
            if not _extinguish_process_tree(proc):
                pid = getattr(proc, "pid", None)
                survivor_pgid = pid if isinstance(pid, int) and pid > 0 else None
        # Drain the reader before reporting the phase done. A reader still
        # holding the pipe means output — and therefore the process — is still
        # in flight; joining here keeps "phase returned" honest.
        reader.join(timeout=5)
        return rc, timed, survivor_pgid

    def _finalize(status: str, rc, error: str | None) -> None:
        with hub.state_lock:
            if hub.state.get("_gen") != gen:
                return
            hub.state["returncode"] = rc
            hub.state["finished_at"] = time.time()
            hub.state["status"] = status
            hub.state["error"] = error
            scan_result = parse_scan_report(hub.state["log"])
            if scan_result is not None:
                hub.state["scan_result"] = scan_result

    def _run() -> None:
        survivor_pgid: int | None = None
        try:
            # Postcondition authority: snapshot the hub's own lock artifact
            # before the CLI runs, so success can be established from observed
            # state rather than from sentences in an attacker-influenced
            # transcript.
            before_lock = _lock_snapshot_or_none(skills_dir)
            for phase_name, cmd, stdin_answer in phases:
                rc, timed, survivor_pgid = _exec_phase(cmd, stdin_answer)
                if timed:
                    detail = (
                        f"Action timed out after {_ACTION_TIMEOUT_SECONDS}s and was killed"
                    )
                    if survivor_pgid is not None:
                        detail += (
                            "; descendants survived SIGKILL, so this profile stays "
                            "closed to new actions until they exit"
                        )
                    _finalize("timeout", rc, detail)
                    return
                if rc != 0:
                    _finalize("failed", rc, f"Exited with code {rc}")
                    return
                if action == "update" and phase_name == "scan":
                    # Fail-closed verdict gate BETWEEN the phases: the update
                    # CLI is only ever invoked for a skill whose scan would be
                    # allowed WITHOUT any force/override semantics.
                    with hub.state_lock:
                        scan_result = parse_scan_report(hub.state["log"])
                        if scan_result is not None:
                            hub.state["scan_result"] = scan_result
                    decision = (
                        _safe_scan_decision(scan_result)
                        if isinstance(scan_result, dict)
                        else None
                    )
                    if decision != "ALLOWED":
                        _finalize(
                            "failed",
                            rc,
                            "Update refused: the security scan did not allow "
                            "this skill (installed version left untouched).",
                        )
                        return
                    _append_log(hub, gen, "\n--- scan allowed; updating ---\n")
            with hub.state_lock:
                log_text = hub.state["log"]
                scan_result = parse_scan_report(log_text)
            after_lock = _lock_snapshot_or_none(skills_dir)
            ok, error = _evaluate_outcome(
                action,
                log_text,
                scan_result,
                target=target,
                before=before_lock,
                after=after_lock,
                started_at=run_started_at,
            )
            _finalize("completed" if ok else "failed", 0, error)
        except Exception as exc:
            logger.exception("skills hub action %r runner failed", action)
            _finalize("failed", None, f"{type(exc).__name__}: {exc}")
        finally:
            # Release the slot ONLY against proved extinction. While a
            # descendant of the killed leader still lives it owns this
            # profile's skills directory, and re-admitting a run would let two
            # trees write it at once. An honest 409 is the correct answer for
            # as long as that is true.
            if survivor_pgid is None:
                with hub.state_lock:
                    hub.running = False
            else:
                _release_after_extinction(hub, gen, survivor_pgid)

    try:
        threading.Thread(
            target=_run, name=f"hermes-webui-skills-hub-{action}", daemon=True
        ).start()
    except Exception:
        # Thread admission failed (resource exhaustion): _run never ran, its
        # finally can never release the slot -- release synchronously or the
        # profile 409s forever (review P1, same class as plugin-lifecycle).
        with hub.state_lock:
            hub.running = False
            if hub.state.get("_gen") == gen:
                hub.state["status"] = "failed"
                hub.state["finished_at"] = time.time()
                hub.state["error"] = "Could not start the background runner"
        raise
    return True, get_status(profile, principal=principal, run_id=run_id)
