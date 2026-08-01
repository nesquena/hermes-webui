"""Fail-closed inventory for WebUI-managed Git worktrees."""

from __future__ import annotations

import errno
import json
import math
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import request


_DAY_SECONDS = 24 * 60 * 60
_GIT_REPORT_FIELDS = (
    "path",
    "branch",
    "repo_root",
    "target_ref",
    "verdict",
    "eligible",
    "exists",
    "listed",
    "dirty",
    "untracked_count",
    "ignored_count",
    "ancestor_of_target",
    "cherry_unique_count",
    "reasons",
)


@dataclass(frozen=True)
class ProcessCwd:
    pid: int
    cwd: str


@dataclass(frozen=True)
class ProcessScan:
    available: bool
    complete: bool
    process_cwds: tuple[ProcessCwd, ...]
    unreadable_count: int = 0
    disappeared_count: int = 0
    error: str | None = None

    @property
    def process_count(self) -> int:
        return len(self.process_cwds)

    @property
    def cwds(self) -> tuple[str, ...]:
        return tuple(item.cwd for item in self.process_cwds)

    def blocking_process_count(self, worktree_path: str | Path) -> int:
        worktree = _canonical_path(worktree_path)
        if worktree is None:
            return 0
        return sum(
            1
            for process in self.process_cwds
            if _path_is_within(Path(process.cwd), worktree)
        )

    def blocking_process_counts(
        self, worktree_paths: tuple[str | Path, ...]
    ) -> dict[str, int]:
        """Count blocking CWDs for many worktrees without a paths × CWDs scan."""
        counts: dict[str, int] = {}
        for raw_path in worktree_paths:
            worktree = _canonical_path(raw_path)
            if worktree is not None:
                counts[str(worktree)] = 0
        for process in self.process_cwds:
            cwd = _canonical_path(process.cwd)
            if cwd is None:
                continue
            for ancestor in (cwd, *cwd.parents):
                key = str(ancestor)
                if key in counts:
                    counts[key] += 1
        return counts


@dataclass(frozen=True)
class HealthProbe:
    reachable: bool
    active_runs: int | None
    reason: str | None = None


@dataclass(frozen=True)
class ManagedWorktreeCandidate:
    session_id: str
    session_ids: tuple[str, ...]
    profile: str
    worktree_path: str | None
    worktree_branch: str | None
    worktree_repo_root: str | None
    worktree_created_at: Any
    updated_at: Any
    archived: bool
    has_active_stream: bool
    has_pending_user_message: bool
    has_pending_attachments: bool
    has_pending_started_at: bool
    linked_session_latest_updated_at: float | None = None
    linked_session_age_uncertain: bool = False
    uncertainty_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorktreeGcDecision:
    session_id: str
    session_ids: tuple[str, ...]
    profile: str
    worktree_path: str | None
    worktree_branch: str | None
    worktree_repo_root: str | None
    worktree_created_at: float | None
    age_days: float | None
    age_source: str | None
    verdict: str
    eligible: bool
    reasons: tuple[str, ...]
    git: dict[str, Any] | None = None


@dataclass(frozen=True)
class _SessionScan:
    candidates: tuple[ManagedWorktreeCandidate, ...]
    workspace_sessions: tuple[_WorkspaceSession, ...]
    sidecars_scanned: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _WorkspaceSession:
    session_id: str
    workspace: str
    updated_at: float | None
    archived: bool
    has_active_stream: bool
    has_pending_user_message: bool
    has_pending_attachments: bool
    has_pending_started_at: bool


def _canonical_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _canonical_absolute_path(value: Any) -> Path | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    if not path.is_absolute() or "\x00" in str(path):
        return None
    resolved = _canonical_path(path)
    if resolved is None or resolved == Path(resolved.anchor):
        return None
    return resolved


def _canonical_repo_filter(value: Any) -> Path | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if not raw or "\x00" in str(raw):
        return None
    resolved = _canonical_path(value)
    if resolved is None or resolved == Path(resolved.anchor):
        return None
    return resolved


def _path_is_within(path: Path, parent: Path) -> bool:
    canonical_path = _canonical_path(path)
    canonical_parent = _canonical_path(parent)
    if canonical_path is None or canonical_parent is None:
        return False
    try:
        canonical_path.relative_to(canonical_parent)
    except ValueError:
        return False
    return True


def scan_process_cwds(proc_root: Path = Path("/proc")) -> ProcessScan:
    """Snapshot readable ``/proc/<pid>/cwd`` links without treating PID exit as failure."""
    root = Path(proc_root)
    try:
        with os.scandir(root) as iterator:
            entries = sorted(
                (entry for entry in iterator if entry.name.isdecimal()),
                key=lambda entry: int(entry.name),
            )
    except OSError as exc:
        return ProcessScan(
            available=False,
            complete=False,
            process_cwds=(),
            error=type(exc).__name__,
        )

    processes: list[ProcessCwd] = []
    unreadable = 0
    disappeared = 0
    for entry in entries:
        cwd_link = root / entry.name / "cwd"
        try:
            raw_cwd = os.readlink(cwd_link)
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in {
                errno.ENOENT,
                errno.ESRCH,
            }:
                disappeared += 1
                continue
            unreadable += 1
            continue
        cwd_path = Path(raw_cwd)
        if not cwd_path.is_absolute():
            cwd_path = cwd_link.parent / cwd_path
        canonical = _canonical_path(cwd_path)
        if canonical is None:
            unreadable += 1
            continue
        processes.append(ProcessCwd(pid=int(entry.name), cwd=str(canonical)))

    return ProcessScan(
        available=True,
        complete=unreadable == 0,
        process_cwds=tuple(processes),
        unreadable_count=unreadable,
        disappeared_count=disappeared,
    )


def probe_webui_health(health_url: str, *, timeout: float = 3.0) -> HealthProbe:
    """Read the non-sensitive active-run count from the WebUI health endpoint."""
    try:
        with request.urlopen(health_url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return HealthProbe(
            reachable=False,
            active_runs=None,
            reason=type(exc).__name__,
        )
    if not isinstance(payload, dict):
        return HealthProbe(False, None, "invalid_payload")
    active_runs = payload.get("active_runs")
    if (
        isinstance(active_runs, bool)
        or not isinstance(active_runs, int)
        or active_runs < 0
    ):
        return HealthProbe(False, None, "invalid_active_runs")
    if payload.get("status") != "ok":
        return HealthProbe(False, active_runs, "unhealthy_status")
    return HealthProbe(True, active_runs)


def _safe_session_id(payload: dict[str, Any], path: Path) -> str:
    value = payload.get("session_id")
    if not isinstance(value, str) or not value:
        value = path.stem
    if value and all(character.isalnum() or character in "_-" for character in value):
        return value[:160]
    fallback = path.stem
    if fallback and all(
        character.isalnum() or character in "_-" for character in fallback
    ):
        return fallback[:160]
    return "unknown"


def _valid_branch(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    branch = value.strip()
    if (
        not branch
        or branch == "@"
        or branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith(("/", "."))
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or any(
            ord(character) <= 32
            or ord(character) == 127
            or character in "~^:?*[\\"
            for character in branch
        )
    ):
        return None
    components = branch.split("/")
    if any(
        component.startswith(".") or component.endswith(".lock")
        for component in components
    ):
        return None
    return branch


def _candidate_from_payload(
    payload: dict[str, Any],
    path: Path,
    *,
    profile: str,
) -> ManagedWorktreeCandidate | None:
    stored_profile = payload["profile"] if "profile" in payload else "default"
    if stored_profile != profile:
        return None

    worktree_keys = (
        "worktree_path",
        "worktree_branch",
        "worktree_repo_root",
        "worktree_created_at",
    )
    if not any(payload.get(key) is not None for key in worktree_keys):
        return None

    uncertainty: list[str] = []
    worktree_path = _canonical_absolute_path(payload.get("worktree_path"))
    if worktree_path is None:
        uncertainty.append("invalid_worktree_path")
    repo_root = _canonical_absolute_path(payload.get("worktree_repo_root"))
    if repo_root is None:
        uncertainty.append("invalid_repo_root")
    branch = _valid_branch(payload.get("worktree_branch"))
    if branch is None:
        uncertainty.append("invalid_worktree_branch")
    session_id = _safe_session_id(payload, path)
    return ManagedWorktreeCandidate(
        session_id=session_id,
        session_ids=(session_id,),
        profile=profile,
        worktree_path=str(worktree_path) if worktree_path else None,
        worktree_branch=branch,
        worktree_repo_root=str(repo_root) if repo_root else None,
        worktree_created_at=payload.get("worktree_created_at"),
        updated_at=payload.get("updated_at"),
        archived=payload.get("archived") is True,
        has_active_stream=bool(payload.get("active_stream_id")),
        has_pending_user_message=bool(payload.get("pending_user_message")),
        has_pending_attachments=bool(payload.get("pending_attachments")),
        has_pending_started_at=payload.get("pending_started_at") is not None,
        uncertainty_reasons=tuple(uncertainty),
    )


def _workspace_session_from_payload(
    payload: dict[str, Any],
    path: Path,
    *,
    profile: str,
) -> tuple[_WorkspaceSession | None, str | None]:
    stored_profile = payload["profile"] if "profile" in payload else "default"
    if stored_profile != profile:
        return None, None
    raw_workspace = payload.get("workspace")
    if raw_workspace is None or raw_workspace == "":
        return None, None
    workspace = _canonical_absolute_path(raw_workspace)
    if workspace is None:
        return None, "session_workspace_invalid"
    return (
        _WorkspaceSession(
            session_id=_safe_session_id(payload, path),
            workspace=str(workspace),
            updated_at=_timestamp(payload.get("updated_at")),
            archived=payload.get("archived") is True,
            has_active_stream=bool(payload.get("active_stream_id")),
            has_pending_user_message=bool(payload.get("pending_user_message")),
            has_pending_attachments=bool(payload.get("pending_attachments")),
            has_pending_started_at=payload.get("pending_started_at") is not None,
        ),
        None,
    )


def _candidate_conflict_key(candidate: ManagedWorktreeCandidate) -> tuple[Any, ...]:
    return (
        candidate.profile,
        candidate.worktree_path,
        candidate.worktree_branch,
        candidate.worktree_repo_root,
        candidate.worktree_created_at,
        candidate.uncertainty_reasons,
    )


def _deduplicate_candidates(
    candidates: list[ManagedWorktreeCandidate],
    *,
    preferred_repo: Path | None = None,
) -> tuple[ManagedWorktreeCandidate, ...]:
    by_path: dict[str, list[ManagedWorktreeCandidate]] = {}
    for candidate in candidates:
        key = candidate.worktree_path or f"invalid:{candidate.session_id}"
        by_path.setdefault(key, []).append(candidate)

    deduplicated: list[ManagedWorktreeCandidate] = []
    for group in by_path.values():
        def preference(candidate: ManagedWorktreeCandidate) -> tuple[int, str]:
            if (
                preferred_repo is not None
                and candidate.worktree_repo_root == str(preferred_repo)
            ):
                rank = 0
            elif candidate.worktree_repo_root is None:
                rank = 1
            else:
                rank = 2
            return rank, candidate.session_id

        group.sort(key=preference)
        first = group[0]
        session_ids = tuple(
            sorted(
                {
                    session_id
                    for candidate in group
                    for session_id in candidate.session_ids
                }
            )
        )
        reasons = list(first.uncertainty_reasons)
        if any(
            _candidate_conflict_key(candidate)
            != _candidate_conflict_key(first)
            for candidate in group[1:]
        ):
            reasons.append("duplicate_conflict")
        updated_at = first.updated_at
        if first.worktree_created_at is None:
            parsed_updates = [
                (_timestamp(candidate.updated_at), candidate.updated_at)
                for candidate in group
            ]
            if any(timestamp is None for timestamp, _value in parsed_updates):
                reasons.append("duplicate_conflict")
            else:
                updated_at = max(parsed_updates, key=lambda item: item[0])[1]
        deduplicated.append(
            replace(
                first,
                session_id=session_ids[0],
                session_ids=session_ids,
                updated_at=updated_at,
                archived=all(candidate.archived for candidate in group),
                has_active_stream=any(
                    candidate.has_active_stream for candidate in group
                ),
                has_pending_user_message=any(
                    candidate.has_pending_user_message for candidate in group
                ),
                has_pending_attachments=any(
                    candidate.has_pending_attachments for candidate in group
                ),
                has_pending_started_at=any(
                    candidate.has_pending_started_at for candidate in group
                ),
                uncertainty_reasons=tuple(dict.fromkeys(reasons)),
            )
        )
    deduplicated.sort(key=lambda candidate: candidate.session_id)
    return tuple(deduplicated)


def _attach_workspace_sessions(
    candidates: tuple[ManagedWorktreeCandidate, ...],
    workspace_sessions: list[_WorkspaceSession],
) -> tuple[ManagedWorktreeCandidate, ...]:
    linked_by_path: dict[str, list[_WorkspaceSession]] = {}
    for candidate in candidates:
        candidate_path = _canonical_absolute_path(candidate.worktree_path)
        if candidate_path is not None:
            linked_by_path.setdefault(str(candidate_path), [])
    for session in workspace_sessions:
        workspace = Path(session.workspace)
        for ancestor in (workspace, *workspace.parents):
            linked = linked_by_path.get(str(ancestor))
            if linked is not None:
                linked.append(session)

    result: list[ManagedWorktreeCandidate] = []
    for candidate in candidates:
        candidate_path = _canonical_absolute_path(candidate.worktree_path)
        if candidate_path is None:
            result.append(candidate)
            continue
        linked = linked_by_path[str(candidate_path)]
        if not linked:
            result.append(candidate)
            continue
        valid_updates = [
            session.updated_at
            for session in linked
            if session.updated_at is not None
        ]
        result.append(
            replace(
                candidate,
                session_ids=tuple(
                    sorted(
                        set(candidate.session_ids)
                        | {session.session_id for session in linked}
                    )
                ),
                archived=candidate.archived
                and all(session.archived for session in linked),
                has_active_stream=candidate.has_active_stream
                or any(session.has_active_stream for session in linked),
                has_pending_user_message=candidate.has_pending_user_message
                or any(
                    session.has_pending_user_message for session in linked
                ),
                has_pending_attachments=candidate.has_pending_attachments
                or any(
                    session.has_pending_attachments for session in linked
                ),
                has_pending_started_at=candidate.has_pending_started_at
                or any(
                    session.has_pending_started_at for session in linked
                ),
                linked_session_latest_updated_at=(
                    max(valid_updates) if valid_updates else None
                ),
                linked_session_age_uncertain=any(
                    session.updated_at is None for session in linked
                ),
            )
        )
    return tuple(result)


def _scan_managed_worktree_sessions(
    state_dir: str | Path,
    *,
    profile: str,
    repo_filter: str | Path | None,
) -> _SessionScan:
    sessions_dir = Path(state_dir).expanduser() / "sessions"
    canonical_filter = (
        _canonical_repo_filter(repo_filter) if repo_filter is not None else None
    )
    if repo_filter is not None and canonical_filter is None:
        raise ValueError("repo_filter must be a valid, non-root path")
    try:
        paths = sorted(sessions_dir.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return _SessionScan((), (), 0, ())
    except OSError as exc:
        return _SessionScan(
            (),
            (),
            0,
            (f"session_directory_{type(exc).__name__}",),
        )

    candidates: list[ManagedWorktreeCandidate] = []
    workspace_sessions: list[_WorkspaceSession] = []
    errors: list[str] = []
    scanned = 0
    for path in paths:
        name = path.name
        if (
            name == "_index.json"
            or path.suffix != ".json"
            or ".tmp." in name
            or name.startswith(".")
        ):
            continue
        scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"session_sidecar_{type(exc).__name__}")
            continue
        if not isinstance(payload, dict):
            errors.append("session_sidecar_invalid_payload")
            continue
        workspace_session, workspace_error = _workspace_session_from_payload(
            payload,
            path,
            profile=profile,
        )
        if workspace_error:
            errors.append(workspace_error)
        if workspace_session is not None:
            workspace_sessions.append(workspace_session)
        candidate = _candidate_from_payload(payload, path, profile=profile)
        if candidate is None:
            continue
        candidates.append(candidate)
    deduplicated = _deduplicate_candidates(
        candidates,
        preferred_repo=canonical_filter,
    )
    if canonical_filter is not None:
        deduplicated = tuple(
            candidate
            for candidate in deduplicated
            if candidate.worktree_repo_root is None
            or Path(candidate.worktree_repo_root) == canonical_filter
        )
    deduplicated = _attach_workspace_sessions(
        deduplicated,
        workspace_sessions,
    )
    return _SessionScan(
        deduplicated,
        tuple(workspace_sessions),
        scanned,
        tuple(errors),
    )


def load_managed_worktree_sessions(
    state_dir: str | Path,
    *,
    profile: str,
    repo_filter: str | Path | None = None,
) -> list[ManagedWorktreeCandidate]:
    """Load profile-scoped sidecars without importing or mutating WebUI runtime state."""
    return list(
        _scan_managed_worktree_sessions(
            state_dir,
            profile=profile,
            repo_filter=repo_filter,
        ).candidates
    )


def _timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            timestamp = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return None
            timestamp = parsed.timestamp()
    else:
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _now_timestamp(now: datetime | float | int | None) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.timestamp()
    parsed = _timestamp(now)
    if parsed is None:
        raise ValueError("now must be a valid timestamp")
    return parsed


def _git_decision_report(decision: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in _GIT_REPORT_FIELDS:
        if isinstance(decision, dict):
            value = decision.get(field)
        else:
            value = getattr(decision, field, None)
        if field == "reasons":
            value = list(value or ())
        if value is not None:
            result[field] = value
    return result


def _decision_report(decision: WorktreeGcDecision) -> dict[str, Any]:
    return {
        "session_id": decision.session_id,
        "session_ids": list(decision.session_ids),
        "profile": decision.profile,
        "worktree_path": decision.worktree_path,
        "worktree_branch": decision.worktree_branch,
        "worktree_repo_root": decision.worktree_repo_root,
        "worktree_created_at": decision.worktree_created_at,
        "age_days": decision.age_days,
        "age_source": decision.age_source,
        "verdict": decision.verdict,
        "eligible": decision.eligible,
        "reasons": list(decision.reasons),
        **({"git": decision.git} if decision.git is not None else {}),
    }


def audit_managed_worktrees(
    *,
    state_dir: str | Path,
    profile: str,
    repo_filter: str | Path,
    min_age_days: int,
    target_ref: str,
    git_backend: Any,
    health_url: str,
    now: datetime | float | int | None = None,
    health_probe: Callable[[str], HealthProbe] = probe_webui_health,
    process_scan: ProcessScan | None = None,
) -> dict[str, Any]:
    """Audit managed worktrees and call Git classification only after all guards."""
    if (
        isinstance(min_age_days, bool)
        or not isinstance(min_age_days, int)
        or min_age_days < 1
    ):
        raise ValueError("min_age_days must be at least 1")
    canonical_repo = _canonical_repo_filter(repo_filter)
    if canonical_repo is None:
        raise ValueError("repo_filter must be a valid, non-root path")
    if not isinstance(target_ref, str) or not target_ref.strip():
        raise ValueError("target_ref is required")

    now_timestamp = _now_timestamp(now)
    session_scan = _scan_managed_worktree_sessions(
        state_dir,
        profile=profile,
        repo_filter=canonical_repo,
    )
    process_snapshot = process_scan or scan_process_cwds()
    health = health_probe(health_url)
    process_counts = process_snapshot.blocking_process_counts(
        tuple(
            candidate.worktree_path
            for candidate in session_scan.candidates
            if candidate.worktree_path is not None
        )
    )

    global_reasons: list[str] = []
    if session_scan.errors:
        global_reasons.append("session_scan_incomplete")
    if not process_snapshot.available or not process_snapshot.complete:
        global_reasons.append("process_scan_incomplete")
    if not health.reachable or health.active_runs is None:
        global_reasons.append("health_unavailable")
    elif health.active_runs > 0:
        global_reasons.append("active_runs")

    decisions: list[WorktreeGcDecision] = []
    for candidate in session_scan.candidates:
        uncertain_reasons = list(candidate.uncertainty_reasons)
        active_reasons: list[str] = []
        keep_reasons: list[str] = []

        if not candidate.archived:
            keep_reasons.append("session_not_archived")
        if (
            candidate.worktree_path is None
            or candidate.worktree_branch is None
            or candidate.worktree_repo_root is None
        ):
            uncertain_reasons.append("incomplete_worktree_metadata")

        created_at = _timestamp(candidate.worktree_created_at)
        age_source: str | None = "worktree_created_at"
        if candidate.worktree_created_at is None:
            created_at = _timestamp(candidate.updated_at)
            age_source = "updated_at"
        if created_at is None:
            uncertain_reasons.append("invalid_or_missing_age")
            age_source = None
            age_days = None
        else:
            age_days = max(0.0, (now_timestamp - created_at) / _DAY_SECONDS)
            if age_days < min_age_days:
                keep_reasons.append("younger_than_min_age")

        if candidate.has_active_stream:
            active_reasons.append("active_stream")
        if candidate.has_pending_user_message:
            active_reasons.append("pending_user_message")
        if candidate.has_pending_attachments:
            active_reasons.append("pending_attachments")
        if candidate.has_pending_started_at:
            active_reasons.append("pending_started_at")
        if candidate.worktree_path is not None:
            blocked_processes = process_counts.get(candidate.worktree_path, 0)
            if blocked_processes:
                active_reasons.append("process_cwd_in_worktree")

        if "session_scan_incomplete" in global_reasons:
            uncertain_reasons.append("session_scan_incomplete")
        if "process_scan_incomplete" in global_reasons:
            uncertain_reasons.append("process_scan_incomplete")
        if "health_unavailable" in global_reasons:
            uncertain_reasons.append("health_unavailable")
        if "active_runs" in global_reasons:
            active_reasons.append("active_runs")

        if candidate.linked_session_age_uncertain:
            uncertain_reasons.append(
                "linked_session_invalid_or_missing_age"
            )
        elif candidate.linked_session_latest_updated_at is not None:
            linked_age_days = max(
                0.0,
                (
                    now_timestamp
                    - candidate.linked_session_latest_updated_at
                )
                / _DAY_SECONDS,
            )
            if linked_age_days < min_age_days:
                keep_reasons.append(
                    "linked_session_younger_than_min_age"
                )

        reasons = tuple(
            dict.fromkeys(uncertain_reasons + active_reasons + keep_reasons)
        )
        common = {
            "session_id": candidate.session_id,
            "session_ids": candidate.session_ids,
            "profile": candidate.profile,
            "worktree_path": candidate.worktree_path,
            "worktree_branch": candidate.worktree_branch,
            "worktree_repo_root": candidate.worktree_repo_root,
            "worktree_created_at": created_at,
            "age_days": round(age_days, 3) if age_days is not None else None,
            "age_source": age_source,
        }
        if uncertain_reasons:
            decisions.append(
                WorktreeGcDecision(
                    **common,
                    verdict="KEEP_UNCERTAIN",
                    eligible=False,
                    reasons=reasons,
                )
            )
            continue
        if active_reasons:
            decisions.append(
                WorktreeGcDecision(
                    **common,
                    verdict="KEEP_ACTIVE",
                    eligible=False,
                    reasons=reasons,
                )
            )
            continue
        if "session_not_archived" in keep_reasons:
            decisions.append(
                WorktreeGcDecision(
                    **common,
                    verdict="KEEP_NOT_ARCHIVED",
                    eligible=False,
                    reasons=reasons,
                )
            )
            continue
        if "younger_than_min_age" in keep_reasons:
            decisions.append(
                WorktreeGcDecision(
                    **common,
                    verdict="KEEP_RECENT",
                    eligible=False,
                    reasons=reasons,
                )
            )
            continue
        if "linked_session_younger_than_min_age" in keep_reasons:
            decisions.append(
                WorktreeGcDecision(
                    **common,
                    verdict="KEEP_RECENT",
                    eligible=False,
                    reasons=reasons,
                )
            )
            continue

        try:
            git_decision = git_backend.classify_git_worktree(
                candidate.worktree_path,
                candidate.worktree_branch,
                candidate.worktree_repo_root,
                target_ref=target_ref,
            )
            git_report = _git_decision_report(git_decision)
            verdict = str(git_report.get("verdict") or "KEEP_UNCERTAIN")
            eligible = bool(git_report.get("eligible"))
            git_reasons = tuple(str(reason) for reason in git_report.get("reasons", ()))
            if not git_report.get("verdict"):
                verdict = "KEEP_UNCERTAIN"
                eligible = False
                git_reasons = ("invalid_git_decision",)
        except Exception:
            git_decision = None
            git_report = None
            verdict = "KEEP_UNCERTAIN"
            eligible = False
            git_reasons = ("git_classification_failed",)
        decisions.append(
            WorktreeGcDecision(
                **common,
                verdict=verdict,
                eligible=eligible,
                reasons=git_reasons,
                git=git_report,
            )
        )
    report_candidates = [_decision_report(decision) for decision in decisions]
    blocking_count = sum(1 for decision in decisions if not decision.eligible)
    return {
        "schema_version": 1,
        "generated_at": datetime.fromtimestamp(
            now_timestamp,
            tz=timezone.utc,
        ).isoformat(),
        "profile": profile,
        "repo": str(canonical_repo),
        "target_ref": target_ref,
        "min_age_days": min_age_days,
        "mode": "dry-run",
        "collection_requested": False,
        "global_reasons": global_reasons,
        "health": {
            "reachable": health.reachable,
            "active_runs": health.active_runs,
            **({"reason": health.reason} if health.reason else {}),
        },
        "process_scan": {
            "available": process_snapshot.available,
            "complete": process_snapshot.complete,
            "process_count": process_snapshot.process_count,
            "unreadable_count": process_snapshot.unreadable_count,
            "disappeared_count": process_snapshot.disappeared_count,
        },
        "session_scan": {
            "sidecars_scanned": session_scan.sidecars_scanned,
            "errors": len(session_scan.errors),
            "error_kinds": sorted(set(session_scan.errors)),
        },
        "candidates": report_candidates,
        "counts": {
            "candidates": len(decisions),
            "eligible": sum(1 for decision in decisions if decision.eligible),
            "blocking": blocking_count,
            "uncertain": sum(
                1 for decision in decisions if decision.verdict == "KEEP_UNCERTAIN"
            ),
            "active": sum(
                1 for decision in decisions if decision.verdict == "KEEP_ACTIVE"
            ),
        },
        "has_blocking_anomalies": bool(global_reasons or blocking_count),
    }


def write_report_atomic(report: dict[str, Any], path: str | Path) -> None:
    """Durably replace a JSON report using a temporary file beside its target."""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                report,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        if os.name != "nt":
            directory_descriptor = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except Exception:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
