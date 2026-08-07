"""Stable compression-lineage identity and cross-process turn exclusion.

The oldest verified WebUI compression segment is the coordination identity for
busy/deferred-turn state.  The newest verified segment is only a delivery target.
This module deliberately reads durable sidecars instead of trusting sidebar
projection metadata or timestamps.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_MAX_LINEAGE_HOPS = 20
_TRANSITION_VERSION = 1
_TRANSITION_STATES = frozenset({"pending", "recoverable", "committed"})
_TRANSITION_DIR_NAME = "_session_lineage_transitions"
_PERMIT_DIR_NAME = "_session_lineage_permits"
_COMPLETION_CLAIM_DIR_NAME = "_completion_delivery_claims"
_COMPLETION_RECEIPT_FILE_NAME = "_completion_delivery_receipts.json"
_COMPLETION_RECEIPT_LOCK_NAME = "_completion_delivery_receipts.lock"
_COMPLETION_RECEIPT_VERSION = 2
_COMPLETION_RECEIPT_STATES = frozenset({"accepted", "incorporated"})
_SAFE_SESSION_ID_CHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
)
_TRANSITION_WRITE_LOCK = threading.RLock()


class LineageResolutionError(RuntimeError):
    """Raised when durable state cannot prove one safe root/current-tip pair."""


class LineageTurnBusyError(RuntimeError):
    """Raised when another process already owns a root's nonblocking permit."""


class LineagePermitUnsupportedError(RuntimeError):
    """Raised before mutation when this platform has no supported lock primitive."""


class CompletionDeliveryBusyError(RuntimeError):
    """Raised when another process owns the exact completion receipt claim."""


class CompletionDeliveryReceiptError(RuntimeError):
    """Raised when a durable completion receipt is malformed or conflicting."""


@dataclass(frozen=True)
class LineageResolution:
    root_session_id: str
    delivery_session_id: str
    profile: str
    hop_count: int


@dataclass(frozen=True)
class CompletionDeliveryContext:
    """Immutable spawn-time identity for one autonomous completion delivery."""

    kind: str
    completion_id: str
    completion_key: str
    session_key: str
    origin_ui_session_id: str
    root_session_id: str
    delivery_session_id: str
    profile: str
    correlation_sha256: str
    turn_id: str
    # Restart repair may route accepted work to a newer compression tip while
    # the immutable receipt remains in the delivery identity that accepted it.
    receipt_delivery_session_id: str | None = None

    @property
    def completion_kind(self) -> str:
        return self.kind

    @property
    def correlation_id(self) -> str:
        return self.correlation_sha256

    @property
    def lineage_id(self) -> str:
        return self.root_session_id

    @property
    def origin_session_id(self) -> str:
        return self.origin_ui_session_id


class CompletionDeliveryClaim:
    """Process-local authority for one locked durable completion receipt."""

    def __init__(
        self,
        *,
        context: CompletionDeliveryContext,
        receipt_path: Path,
        lock_path: Path,
        state: str,
        backend: str,
        handle: BinaryIO,
        lock_module,
        owner_token: str,
        attempt: int,
        reservation_id: str,
    ) -> None:
        self.context = context
        self.receipt_path = receipt_path
        self.lock_path = lock_path
        self.state = state
        self.backend = backend
        self._handle = handle
        self._lock_module = lock_module
        self.owner_token = owner_token
        self.attempt = attempt
        self.reservation_id = reservation_id
        self._released = False

    @property
    def acquired(self) -> bool:
        return not self._released


def _safe_session_id(value: object) -> str:
    session_id = str(value or "").strip()
    if not session_id or any(char not in _SAFE_SESSION_ID_CHARS for char in session_id):
        raise LineageResolutionError(f"unsafe session id {session_id!r}")
    return session_id


def _normalized_profile(value: object) -> str:
    return str(value or "default").strip() or "default"


def _profiles_match(left: object, right: object) -> bool:
    normalized_left = _normalized_profile(left)
    normalized_right = _normalized_profile(right)
    if normalized_left == normalized_right:
        return True
    try:
        from api.profiles import _profiles_match as profiles_match

        return bool(profiles_match(normalized_left, normalized_right))
    except Exception:
        return False


def _resolved_session_dir(session_dir: Path | str | None) -> Path:
    if session_dir is not None:
        return Path(session_dir)
    from api import config as live_config

    return Path(live_config.SESSION_DIR)


def _load_sidecar(path: Path, expected_session_id: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LineageResolutionError(
            f"missing traversed sidecar for session {expected_session_id!r}"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LineageResolutionError(
            f"unreadable sidecar for session {expected_session_id!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise LineageResolutionError(
            f"invalid sidecar object for session {expected_session_id!r}"
        )
    persisted_session_id = _safe_session_id(payload.get("session_id"))
    if persisted_session_id != expected_session_id:
        raise LineageResolutionError(
            f"sidecar identity mismatch for session {expected_session_id!r}"
        )
    return payload


def _load_session_rows(session_dir: Path, requested_session_id: str) -> dict[str, dict]:
    requested_path = session_dir / f"{requested_session_id}.json"
    requested = _load_sidecar(requested_path, requested_session_id)
    rows = {requested_session_id: requested}
    try:
        paths = tuple(session_dir.glob("*.json"))
    except OSError as exc:
        raise LineageResolutionError("session store cannot be enumerated") from exc
    for path in paths:
        if path.name.startswith("_") or path.stem == requested_session_id:
            continue
        try:
            session_id = _safe_session_id(path.stem)
            rows[session_id] = _load_sidecar(path, session_id)
        except LineageResolutionError:
            # An unrelated corrupt sidecar cannot safely be interpreted as an
            # edge.  It is ignored unless a traversed parent explicitly names it;
            # that lookup fails closed below because it is absent from ``rows``.
            continue
    return rows


def _transition_dir(session_dir: Path) -> Path:
    return session_dir / _TRANSITION_DIR_NAME


def _transition_path(session_dir: Path, previous_tip_session_id: str) -> Path:
    digest = hashlib.sha256(previous_tip_session_id.encode("utf-8")).hexdigest()
    return _transition_dir(session_dir) / f"{digest}.json"


def _read_lineage_transitions(session_dir: Path) -> list[dict]:
    directory = _transition_dir(session_dir)
    if not directory.exists():
        return []
    try:
        paths = tuple(directory.glob("*.json"))
    except OSError as exc:
        raise LineageResolutionError("lineage transition store cannot be enumerated") from exc
    records = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise LineageResolutionError("malformed durable lineage transition") from exc
        if not isinstance(record, dict) or record.get("version") != _TRANSITION_VERSION:
            raise LineageResolutionError("unsupported durable lineage transition")
        state = str(record.get("state") or "")
        if state not in _TRANSITION_STATES:
            raise LineageResolutionError("invalid durable lineage transition state")
        for key in (
            "root_session_id",
            "previous_tip_session_id",
            "delivery_session_id",
        ):
            record[key] = _safe_session_id(record.get(key))
        record["profile"] = _normalized_profile(record.get("profile"))
        records.append(record)
    return records


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_lineage_transition(
    *,
    root_session_id: str,
    previous_tip_session_id: str,
    delivery_session_id: str,
    profile: str | None,
    state: str,
    session_dir: Path | str | None = None,
) -> dict:
    """Durably publish one compression-transition lifecycle state."""
    root = _safe_session_id(root_session_id)
    previous_tip = _safe_session_id(previous_tip_session_id)
    delivery = _safe_session_id(delivery_session_id)
    normalized_state = str(state or "").strip().lower()
    if normalized_state not in _TRANSITION_STATES:
        raise ValueError(f"unsupported lineage transition state {state!r}")
    if previous_tip == delivery:
        raise ValueError("lineage transition must rotate the delivery session id")

    directory_root = _resolved_session_dir(session_dir)
    transition_directory = _transition_dir(directory_root)
    target = _transition_path(directory_root, previous_tip)
    record = {
        "version": _TRANSITION_VERSION,
        "state": normalized_state,
        "root_session_id": root,
        "previous_tip_session_id": previous_tip,
        "delivery_session_id": delivery,
        "profile": _normalized_profile(profile),
        "updated_at": time.time(),
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )

    with _TRANSITION_WRITE_LOCK:
        transition_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(transition_directory, 0o700)
        except OSError:
            pass
        temporary = transition_directory / f".{target.name}.tmp-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            _fsync_directory(transition_directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return dict(record)


def _is_non_compression_child(row: dict, transitions: list[dict] | None = None) -> bool:
    session_id = str(row.get("session_id") or "").strip()
    parent_id = str(row.get("parent_session_id") or "").strip()
    for transition in transitions or ():
        if (
            transition.get("state") in {"recoverable", "committed"}
            and transition.get("previous_tip_session_id") == parent_id
            and transition.get("delivery_session_id") == session_id
        ):
            # A durable transition is the authoritative edge. Fork/child source
            # metadata is inherited by continuations for UI identity and must
            # not split every later compressed segment into a new root.
            return False
    source = str(row.get("session_source") or "").strip().lower()
    relationship = str(row.get("relationship_type") or "").strip().lower()
    return source == "fork" or relationship == "child_session"


def resolve_session_lineage(
    session_id: str,
    *,
    session_dir: Path | str | None = None,
    expected_profile: str | None = None,
) -> LineageResolution:
    """Resolve one verified compression chain to its stable root and current tip."""
    requested = _safe_session_id(session_id)
    directory = _resolved_session_dir(session_dir)
    requested_path = directory / f"{requested}.json"
    if not requested_path.exists():
        transitions = _read_lineage_transitions(directory)
        for transition in transitions:
            involved = {
                transition["root_session_id"],
                transition["previous_tip_session_id"],
                transition["delivery_session_id"],
            }
            if requested in involved:
                raise LineageResolutionError(
                    "lineage transition references a missing session sidecar"
                )
        profile = _normalized_profile(expected_profile)
        return LineageResolution(
            root_session_id=requested,
            delivery_session_id=requested,
            profile=profile,
            hop_count=0,
        )
    rows = _load_session_rows(directory, requested)
    requested_row = rows[requested]
    profile = _normalized_profile(requested_row.get("profile"))
    if expected_profile is not None and not _profiles_match(profile, expected_profile):
        raise LineageResolutionError("cross-profile requested session")

    transitions = _read_lineage_transitions(directory)
    for transition in transitions:
        if transition["state"] != "pending":
            continue
        involved = {
            transition["root_session_id"],
            transition["previous_tip_session_id"],
            transition["delivery_session_id"],
        }
        if requested in involved:
            raise LineageResolutionError("pending lineage transition")

    root = requested
    current = requested
    seen_backwards = {requested}
    traversed_compression_edge = False
    for _ in range(_MAX_LINEAGE_HOPS + 1):
        row = rows[current]
        parent_value = row.get("parent_session_id")
        if not parent_value or _is_non_compression_child(row, transitions):
            break
        parent_id = _safe_session_id(parent_value)
        parent = rows.get(parent_id)
        if parent is None:
            if traversed_compression_edge or bool(row.get("pre_compression_snapshot")):
                raise LineageResolutionError(
                    f"missing traversed sidecar for session {parent_id!r}"
                )
            break
        if not bool(parent.get("pre_compression_snapshot")):
            break
        if not _profiles_match(parent.get("profile"), profile):
            raise LineageResolutionError("cross-profile compression edge")
        if parent_id in seen_backwards:
            raise LineageResolutionError("compression lineage cycle")
        if len(seen_backwards) > _MAX_LINEAGE_HOPS:
            raise LineageResolutionError("compression lineage exceeds 20 hops")
        traversed_compression_edge = True
        seen_backwards.add(parent_id)
        root = parent_id
        current = parent_id
    else:  # pragma: no cover - defensive; loop normally exits through the hop guard
        raise LineageResolutionError("compression lineage exceeds 20 hops")

    current = root
    chain = [root]
    seen_forwards = {root}
    hop_count = 0
    while True:
        row = rows[current]
        raw_children = [
            child
            for child in rows.values()
            if str(child.get("parent_session_id") or "").strip() == current
            and not _is_non_compression_child(child, transitions)
        ]
        foreign_children = [
            child
            for child in raw_children
            if not _profiles_match(child.get("profile"), profile)
        ]
        if foreign_children:
            raise LineageResolutionError("cross-profile compression edge")
        children = [
            child for child in raw_children if _profiles_match(child.get("profile"), profile)
        ]
        if len(children) > 1:
            raise LineageResolutionError("compression lineage fork")
        if not children:
            if bool(row.get("pre_compression_snapshot")):
                raise LineageResolutionError(
                    f"missing traversed sidecar after snapshot {current!r}"
                )
            break
        if not bool(row.get("pre_compression_snapshot")):
            raise LineageResolutionError("unsafe child edge from non-snapshot session")
        child_id = _safe_session_id(children[0].get("session_id"))
        if child_id in seen_forwards:
            raise LineageResolutionError("compression lineage cycle")
        hop_count += 1
        if hop_count > _MAX_LINEAGE_HOPS:
            raise LineageResolutionError("compression lineage exceeds 20 hops")
        seen_forwards.add(child_id)
        chain.append(child_id)
        current = child_id

    chain_ids = set(chain)
    for transition in transitions:
        involved = {
            transition["root_session_id"],
            transition["previous_tip_session_id"],
            transition["delivery_session_id"],
        }
        if transition["state"] == "pending" and chain_ids.intersection(involved):
            raise LineageResolutionError("pending lineage transition")
        if transition["state"] not in {"recoverable", "committed"}:
            continue
        previous_tip = transition["previous_tip_session_id"]
        if previous_tip not in chain_ids:
            continue
        if not _profiles_match(transition["profile"], profile):
            raise LineageResolutionError("cross-profile durable transition")
        try:
            previous_index = chain.index(previous_tip)
        except ValueError:  # pragma: no cover - guarded by membership above
            continue
        if previous_index + 1 >= len(chain):
            raise LineageResolutionError("committed transition has no durable delivery tip")
        if chain[previous_index + 1] != transition["delivery_session_id"]:
            raise LineageResolutionError("committed transition conflicts with sidecars")
        if transition["root_session_id"] != root:
            raise LineageResolutionError("committed transition conflicts with stable root")
        if transition["state"] == "recoverable":
            try:
                record_lineage_transition(
                    root_session_id=transition["root_session_id"],
                    previous_tip_session_id=transition["previous_tip_session_id"],
                    delivery_session_id=transition["delivery_session_id"],
                    profile=transition["profile"],
                    state="committed",
                    session_dir=directory,
                )
            except Exception as exc:
                raise LineageResolutionError(
                    "recoverable lineage transition remains uncommitted"
                ) from exc

    return LineageResolution(
        root_session_id=root,
        delivery_session_id=current,
        profile=profile,
        hop_count=hop_count,
    )


def _load_fcntl():
    import fcntl

    return fcntl


def _load_msvcrt():
    import msvcrt

    return msvcrt


class LineageTurnPermit:
    """Held OS lock for one stable lineage root; release is idempotent."""

    def __init__(
        self,
        *,
        root_session_id: str,
        path: Path,
        backend: str,
        handle: BinaryIO,
        lock_module,
    ) -> None:
        self.root_session_id = root_session_id
        self.path = path
        self.backend = backend
        self._handle = handle
        self._lock_module = lock_module
        self._released = False

    @property
    def acquired(self) -> bool:
        return not self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if self.backend == "fcntl":
                self._lock_module.flock(self._handle.fileno(), self._lock_module.LOCK_UN)
            else:
                self._handle.seek(0)
                self._lock_module.locking(
                    self._handle.fileno(), self._lock_module.LK_UNLCK, 1
                )
        finally:
            self._handle.close()

    def __enter__(self) -> "LineageTurnPermit":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@dataclass(frozen=True)
class TurnAdmission:
    """Opaque in-process authority for one reserved lineage turn.

    The identity fields are immutable.  The three Events deliberately carry
    lifecycle state without serialising owner authority into HTTP or journal
    payloads: the worker announces that it is parked, the route opens the gate
    only after preparation/read-back succeeds, and either side may abort.
    """

    reservation_id: str
    stream_id: str
    owner_token: str
    root_session_id: str
    delivery_session_id: str
    permit: LineageTurnPermit
    admitted: threading.Event
    gate: threading.Event
    abort: threading.Event

    def __post_init__(self) -> None:
        if not self.stream_id or self.reservation_id != self.stream_id:
            raise ValueError("turn admission reservation_id must equal stream_id")
        if not self.owner_token or not self.root_session_id or not self.delivery_session_id:
            raise ValueError("turn admission identity fields are required")
        if self.permit is None or not bool(getattr(self.permit, "acquired", False)):
            raise ValueError("turn admission requires an acquired lineage permit")
        for event in (self.admitted, self.gate, self.abort):
            if not isinstance(event, threading.Event):
                raise TypeError("turn admission lifecycle fields must be threading.Event instances")

    @classmethod
    def create_for_test(
        cls,
        *,
        stream_id: str,
        root_session_id: str,
        delivery_session_id: str,
        permit,
    ) -> "TurnAdmission":
        """Construct an isolated admission for wrapper behavior tests."""
        stream = str(stream_id or "").strip()
        return cls(
            reservation_id=stream,
            stream_id=stream,
            owner_token=uuid.uuid4().hex,
            root_session_id=str(root_session_id or "").strip(),
            delivery_session_id=str(delivery_session_id or "").strip(),
            permit=permit,
            admitted=threading.Event(),
            gate=threading.Event(),
            abort=threading.Event(),
        )


def release_turn_admission(admission: TurnAdmission | None) -> bool:
    """Release one exact-owner reservation; repeated cleanup is a no-op."""
    if not isinstance(admission, TurnAdmission):
        return False
    from api import config as live_config

    with live_config.ACTIVE_RUNS_LOCK:
        current = live_config.ACTIVE_RUNS.get(admission.reservation_id)
        if isinstance(current, dict) and (
            current.get("owner_token") != admission.owner_token
            or current.get("admission") is not admission
            or current.get("permit") is not admission.permit
        ):
            return False

    removed = live_config.unregister_active_run(
        admission.reservation_id,
        owner_token=admission.owner_token,
    )
    if not removed:
        if not admission.permit.acquired:
            # Repeated cleanup cannot unregister a later owner that happened to
            # reuse the same stream ID.
            return False
        # Covers setup failure before the reservation row was published.  A
        # foreign row was rejected above; both releases are idempotent and stay
        # outside ACTIVE_RUNS_LOCK.
        admission.permit.release()
        if live_config.stream_owner_session_id(admission.stream_id) in {
            None,
            admission.delivery_session_id,
        }:
            live_config.unregister_stream_owner(admission.stream_id)
    return bool(removed)


def acquire_lineage_turn_permit(
    root_session_id: str,
    *,
    lock_dir: Path | str | None = None,
    backend: str | None = None,
) -> LineageTurnPermit:
    """Acquire a never-unlinked, nonblocking OS permit for one stable root."""
    root = _safe_session_id(root_session_id)
    selected_backend = str(backend or ("msvcrt" if os.name == "nt" else "fcntl"))
    if selected_backend == "fcntl":
        try:
            lock_module = _load_fcntl()
        except (ImportError, AttributeError) as exc:
            raise LineagePermitUnsupportedError("fcntl permit backend unavailable") from exc
    elif selected_backend == "msvcrt":
        try:
            lock_module = _load_msvcrt()
        except (ImportError, AttributeError) as exc:
            raise LineagePermitUnsupportedError("msvcrt permit backend unavailable") from exc
    else:
        raise LineagePermitUnsupportedError(
            f"unsupported lineage permit backend {selected_backend!r}"
        )

    if lock_dir is None:
        directory = _resolved_session_dir(None) / _PERMIT_DIR_NAME
    else:
        directory = Path(lock_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
    path = directory / f"{digest}.lock"
    handle = path.open("a+b", buffering=0)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if selected_backend == "fcntl":
            try:
                lock_module.flock(
                    handle.fileno(), lock_module.LOCK_EX | lock_module.LOCK_NB
                )
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise LineageTurnBusyError(
                        f"lineage root {root!r} already has an active turn"
                    ) from exc
                raise
        else:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                os.fsync(handle.fileno())
            handle.seek(0)
            try:
                lock_module.locking(handle.fileno(), lock_module.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise LineageTurnBusyError(
                        f"lineage root {root!r} already has an active turn"
                    ) from exc
                raise
    except Exception:
        handle.close()
        raise

    return LineageTurnPermit(
        root_session_id=root,
        path=path,
        backend=selected_backend,
        handle=handle,
        lock_module=lock_module,
    )


def build_completion_delivery_context(
    event: dict,
    session_id: str,
    *,
    session_dir: Path | str | None = None,
) -> CompletionDeliveryContext:
    """Bind one process/delegation completion to a verified lineage tip.

    Only immutable routing identity is retained. The eventual completion prompt
    remains in the session checkpoint and turn journal, never in the receipt.
    """
    if not isinstance(event, dict):
        raise TypeError("completion event must be a dict")
    completion_kind = (
        "async_delegation"
        if str(event.get("type") or "") == "async_delegation"
        else "process"
    )
    if completion_kind == "async_delegation":
        completion_id = str(event.get("delegation_id") or event.get("task_id") or "").strip()
    else:
        completion_id = str(event.get("process_id") or event.get("task_id") or "").strip()
    if not completion_id:
        raise CompletionDeliveryReceiptError("completion id is required")

    session_key = str(event.get("session_key") or "").strip()
    if not session_key:
        raise CompletionDeliveryReceiptError("completion session_key is required")
    requested_origin = str(event.get("origin_ui_session_id") or session_id or "").strip()
    if not requested_origin:
        raise CompletionDeliveryReceiptError("completion origin UI session is required")
    expected_profile = str(
        event.get("origin_profile") or event.get("profile") or ""
    ).strip() or None
    directory = _resolved_session_dir(session_dir)
    target = resolve_session_lineage(
        session_id,
        session_dir=directory,
        expected_profile=expected_profile,
    )
    origin = resolve_session_lineage(
        requested_origin,
        session_dir=directory,
        expected_profile=target.profile,
    )
    if origin.root_session_id != target.root_session_id:
        raise LineageResolutionError("cross-lineage completion origin")

    completion_key = f"{completion_kind}:{completion_id}"
    correlation = hashlib.sha256(completion_key.encode("utf-8")).hexdigest()
    return CompletionDeliveryContext(
        kind=completion_kind,
        completion_id=completion_id,
        completion_key=completion_key,
        session_key=session_key,
        origin_ui_session_id=origin.root_session_id,
        root_session_id=target.root_session_id,
        delivery_session_id=target.delivery_session_id,
        profile=target.profile,
        correlation_sha256=correlation,
        turn_id=f"completion-{correlation[:32]}",
    )


def completion_delivery_context(
    event: dict,
    delivery_session_id: str,
    *,
    session_dir: Path | str | None = None,
) -> CompletionDeliveryContext:
    """Canonical P0 name for deterministic completion-delivery identity."""
    return build_completion_delivery_context(
        event,
        delivery_session_id,
        session_dir=session_dir,
    )


def completion_delivery_metadata(context: CompletionDeliveryContext) -> dict:
    """Return the prompt-free identity copied to one row and journal event."""
    if not isinstance(context, CompletionDeliveryContext):
        raise TypeError("completion context is required")
    return {
        "version": 1,
        "completion_kind": context.completion_kind,
        "completion_id": context.completion_id,
        "completion_key": context.completion_key,
        "correlation_id": context.correlation_id,
        "turn_id": context.turn_id,
        "lineage_id": context.lineage_id,
        "origin_session_id": context.origin_session_id,
        "delivery_session_id": context.delivery_session_id,
    }


def _completion_receipt_paths(
    context: CompletionDeliveryContext,
    session_dir: Path | str | None,
) -> tuple[Path, Path]:
    directory = _resolved_session_dir(session_dir)
    claim_directory = directory / _COMPLETION_CLAIM_DIR_NAME
    claim_digest = hashlib.sha256(context.completion_key.encode("utf-8")).hexdigest()
    return (
        directory / _COMPLETION_RECEIPT_FILE_NAME,
        claim_directory / f"{claim_digest}.lock",
    )


def _completion_receipt_store_lock_path(session_dir: Path | str | None) -> Path:
    return _resolved_session_dir(session_dir) / _COMPLETION_RECEIPT_LOCK_NAME


def _completion_receipt_identity(context: CompletionDeliveryContext) -> dict:
    return {
        "completion_kind": context.completion_kind,
        "completion_id": context.completion_id,
        "lineage_id": context.lineage_id,
        "origin_session_id": context.origin_session_id,
        "delivery_session_id": (
            context.receipt_delivery_session_id or context.delivery_session_id
        ),
        "correlation_id": context.correlation_id,
        "turn_id": context.turn_id,
    }


def _new_completion_receipt_record(
    context: CompletionDeliveryContext,
    *,
    owner_token: str,
    attempt: int,
    reservation_id: str,
    accepted_at: float,
) -> dict:
    return {
        **_completion_receipt_identity(context),
        "state": "accepted",
        "owner_token": owner_token,
        "attempt": attempt,
        "accepted_at": accepted_at,
        "reservation_id": reservation_id,
    }


def _write_completion_receipt(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    encoded = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_completion_receipt_document(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": _COMPLETION_RECEIPT_VERSION, "receipts": {}}
    except (OSError, UnicodeError) as exc:
        raise CompletionDeliveryReceiptError("unreadable completion receipt") from exc

    duplicate_keys: list[str] = []

    def reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                duplicate_keys.append(str(key))
            value[key] = item
        return value

    try:
        document = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CompletionDeliveryReceiptError("malformed completion receipt") from exc
    if duplicate_keys:
        raise CompletionDeliveryReceiptError("malformed duplicate completion receipt key")
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "receipts"}
        or document.get("version") != _COMPLETION_RECEIPT_VERSION
        or not isinstance(document.get("receipts"), dict)
    ):
        raise CompletionDeliveryReceiptError("malformed completion receipt")
    return document


def _validated_completion_receipt_record(
    context: CompletionDeliveryContext,
    record: object,
) -> dict:
    if not isinstance(record, dict):
        raise CompletionDeliveryReceiptError("malformed completion receipt")
    if record.get("completion_key") is not None:
        raise CompletionDeliveryReceiptError("malformed completion receipt identity")
    for key, value in _completion_receipt_identity(context).items():
        if record.get(key) != value:
            raise CompletionDeliveryReceiptError("conflicting completion receipt identity")
    state = str(record.get("state") or "")
    if state not in _COMPLETION_RECEIPT_STATES:
        raise CompletionDeliveryReceiptError("malformed completion receipt state")
    owner_token = record.get("owner_token")
    attempt = record.get("attempt")
    reservation_id = record.get("reservation_id")
    accepted_at = record.get("accepted_at")
    if not isinstance(owner_token, str) or not owner_token.strip():
        raise CompletionDeliveryReceiptError("malformed completion receipt owner")
    if type(attempt) is not int or attempt <= 0:
        raise CompletionDeliveryReceiptError("malformed completion receipt attempt")
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise CompletionDeliveryReceiptError("malformed completion reservation")
    if not isinstance(accepted_at, (int, float)) or isinstance(accepted_at, bool):
        raise CompletionDeliveryReceiptError("malformed completion accepted timestamp")
    incorporated_at = record.get("incorporated_at")
    if state == "incorporated":
        if (
            not isinstance(incorporated_at, (int, float))
            or isinstance(incorporated_at, bool)
            or incorporated_at < accepted_at
        ):
            raise CompletionDeliveryReceiptError("malformed completion incorporated timestamp")
    elif incorporated_at is not None:
        raise CompletionDeliveryReceiptError("accepted completion has incorporated timestamp")
    for forbidden in ("prompt", "wakeup_prompt", "output", "tool_output"):
        if forbidden in record:
            raise CompletionDeliveryReceiptError("completion receipt contains prompt payload")
    return dict(record)


def _read_completion_receipt_from_document(
    document: dict,
    context: CompletionDeliveryContext,
) -> dict | None:
    receipts = document["receipts"]
    if context.completion_key not in receipts:
        return None
    return _validated_completion_receipt_record(
        context,
        receipts[context.completion_key],
    )


def read_completion_delivery_receipt(
    context: CompletionDeliveryContext,
    *,
    session_dir: Path | str | None = None,
) -> dict | None:
    receipt_path, _ = _completion_receipt_paths(context, session_dir)
    document = _read_completion_receipt_document(receipt_path)
    return _read_completion_receipt_from_document(document, context)


def accepted_completion_delivery_contexts(
    *,
    session_dir: Path | str | None = None,
) -> list[CompletionDeliveryContext]:
    """Return validated accepted-only receipts for restart repair."""
    directory = _resolved_session_dir(session_dir)
    path = directory / _COMPLETION_RECEIPT_FILE_NAME
    document = _read_completion_receipt_document(path)
    accepted: list[CompletionDeliveryContext] = []
    for completion_key, record in sorted(document["receipts"].items()):
        if not isinstance(record, dict):
            raise CompletionDeliveryReceiptError("malformed completion receipt remains visible")
        completion_kind = record.get("completion_kind")
        completion_id = record.get("completion_id")
        if (
            not isinstance(completion_kind, str)
            or not isinstance(completion_id, str)
            or not completion_id
            or completion_kind not in {"process", "async_delegation"}
            or completion_key != f"{completion_kind}:{completion_id}"
        ):
            raise CompletionDeliveryReceiptError("conflicting completion receipt remains visible")
        try:
            lineage_id = _safe_session_id(record.get("lineage_id"))
            origin_session_id = _safe_session_id(record.get("origin_session_id"))
            receipt_delivery_session_id = _safe_session_id(
                record.get("delivery_session_id")
            )
            if origin_session_id != lineage_id:
                raise CompletionDeliveryReceiptError(
                    "conflicting completion receipt origin"
                )
            correlation = hashlib.sha256(completion_key.encode("utf-8")).hexdigest()
            receipt_context = CompletionDeliveryContext(
                kind=completion_kind,
                completion_id=completion_id,
                completion_key=completion_key,
                session_key=f"ui:{lineage_id}",
                origin_ui_session_id=origin_session_id,
                root_session_id=lineage_id,
                delivery_session_id=receipt_delivery_session_id,
                profile="default",
                correlation_sha256=correlation,
                turn_id=f"completion-{correlation[:32]}",
                receipt_delivery_session_id=receipt_delivery_session_id,
            )
            validated = _validated_completion_receipt_record(receipt_context, record)
        except Exception as exc:
            raise CompletionDeliveryReceiptError(
                "conflicting completion receipt remains visible"
            ) from exc
        if validated["state"] != "accepted":
            continue
        try:
            target = resolve_session_lineage(
                receipt_delivery_session_id,
                session_dir=directory,
            )
        except Exception as exc:
            raise CompletionDeliveryReceiptError(
                "accepted completion delivery lineage is not recoverable"
            ) from exc
        if target.root_session_id != lineage_id:
            raise CompletionDeliveryReceiptError(
                "accepted completion delivery crossed lineage"
            )
        accepted.append(
            CompletionDeliveryContext(
                kind=completion_kind,
                completion_id=completion_id,
                completion_key=completion_key,
                session_key=f"ui:{lineage_id}",
                origin_ui_session_id=origin_session_id,
                root_session_id=lineage_id,
                delivery_session_id=target.delivery_session_id,
                profile=target.profile,
                correlation_sha256=receipt_context.correlation_sha256,
                turn_id=receipt_context.turn_id,
                receipt_delivery_session_id=receipt_delivery_session_id,
            )
        )
    return accepted


def _lock_completion_receipt(path: Path, *, nonblocking: bool = True):
    backend = "msvcrt" if os.name == "nt" else "fcntl"
    lock_module = _load_msvcrt() if backend == "msvcrt" else _load_fcntl()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    handle = path.open("a+b", buffering=0)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if backend == "fcntl":
            operation = lock_module.LOCK_EX
            if nonblocking:
                operation |= lock_module.LOCK_NB
            lock_module.flock(handle.fileno(), operation)
        else:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                os.fsync(handle.fileno())
            handle.seek(0)
            operation = lock_module.LK_NBLCK if nonblocking else lock_module.LK_LOCK
            lock_module.locking(handle.fileno(), operation, 1)
    except OSError as exc:
        handle.close()
        if nonblocking and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise CompletionDeliveryBusyError("completion receipt already claimed") from exc
        raise
    return backend, handle, lock_module


def _unlock_completion_file(backend: str, handle: BinaryIO, lock_module) -> None:
    try:
        if backend == "fcntl":
            lock_module.flock(handle.fileno(), lock_module.LOCK_UN)
        else:
            handle.seek(0)
            lock_module.locking(handle.fileno(), lock_module.LK_UNLCK, 1)
    finally:
        handle.close()


def claim_completion_delivery(
    context: CompletionDeliveryContext,
    *,
    session_dir: Path | str | None = None,
    reservation_id: str | None = None,
) -> CompletionDeliveryClaim | None:
    """Claim or create one accepted receipt; incorporated receipts are final."""
    if not isinstance(context, CompletionDeliveryContext):
        raise TypeError("completion context is required")
    receipt_path, claim_lock_path = _completion_receipt_paths(context, session_dir)
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backend, handle, lock_module = _lock_completion_receipt(claim_lock_path)
    owner_token = uuid.uuid4().hex
    selected_reservation = str(
        reservation_id or context.correlation_sha256[:32]
    ).strip()
    if not selected_reservation:
        _unlock_completion_file(backend, handle, lock_module)
        raise CompletionDeliveryReceiptError("completion reservation is required")
    try:
        store_backend, store_handle, store_module = _lock_completion_receipt(
            _completion_receipt_store_lock_path(session_dir),
            nonblocking=False,
        )
        try:
            document = _read_completion_receipt_document(receipt_path)
            current = _read_completion_receipt_from_document(document, context)
            if current is not None and current["state"] == "incorporated":
                _unlock_completion_file(backend, handle, lock_module)
                return None
            attempt = int(current["attempt"]) + 1 if current is not None else 1
            accepted_at = float(current["accepted_at"]) if current is not None else time.time()
            record = _new_completion_receipt_record(
                context,
                owner_token=owner_token,
                attempt=attempt,
                reservation_id=selected_reservation,
                accepted_at=accepted_at,
            )
            document["receipts"][context.completion_key] = record
            _write_completion_receipt(receipt_path, document)
            durable_document = _read_completion_receipt_document(receipt_path)
            durable = _read_completion_receipt_from_document(durable_document, context)
            if durable != record:
                raise CompletionDeliveryReceiptError("completion receipt read-back failed")
        finally:
            _unlock_completion_file(store_backend, store_handle, store_module)
        return CompletionDeliveryClaim(
            context=context,
            receipt_path=receipt_path,
            lock_path=claim_lock_path,
            state="accepted",
            backend=backend,
            handle=handle,
            lock_module=lock_module,
            owner_token=owner_token,
            attempt=attempt,
            reservation_id=selected_reservation,
        )
    except Exception:
        if not handle.closed:
            _unlock_completion_file(backend, handle, lock_module)
        raise


def release_completion_delivery_claim(claim: CompletionDeliveryClaim | None) -> bool:
    if not isinstance(claim, CompletionDeliveryClaim) or not claim.acquired:
        return False
    claim._released = True
    _unlock_completion_file(claim.backend, claim._handle, claim._lock_module)
    return True


def mark_completion_incorporated(
    claim: CompletionDeliveryClaim,
    *,
    session_dir: Path | str | None = None,
) -> dict:
    """CAS one held accepted receipt to incorporated and read it back."""
    if not isinstance(claim, CompletionDeliveryClaim) or not claim.acquired:
        raise CompletionDeliveryReceiptError("completion claim is not held")
    context = claim.context
    receipt_path, claim_lock_path = _completion_receipt_paths(context, session_dir)
    if receipt_path != claim.receipt_path or claim_lock_path != claim.lock_path:
        raise CompletionDeliveryReceiptError("completion claim belongs to another receipt store")
    store_backend, store_handle, store_module = _lock_completion_receipt(
        _completion_receipt_store_lock_path(session_dir),
        nonblocking=False,
    )
    try:
        document = _read_completion_receipt_document(receipt_path)
        current = _read_completion_receipt_from_document(document, context)
        if current is None or current.get("state") != "accepted":
            raise CompletionDeliveryReceiptError("completion receipt is not accepted")
        if (
            current.get("owner_token") != claim.owner_token
            or current.get("attempt") != claim.attempt
            or current.get("reservation_id") != claim.reservation_id
        ):
            raise CompletionDeliveryReceiptError("completion receipt owner CAS failed")
        updated = dict(current)
        updated["state"] = "incorporated"
        updated["incorporated_at"] = time.time()
        document["receipts"][context.completion_key] = updated
        _write_completion_receipt(receipt_path, document)
        durable_document = _read_completion_receipt_document(receipt_path)
        durable = _read_completion_receipt_from_document(durable_document, context)
        if durable != updated or durable.get("state") != "incorporated":
            raise CompletionDeliveryReceiptError("incorporated receipt read-back failed")
    finally:
        _unlock_completion_file(store_backend, store_handle, store_module)
    claim.state = "incorporated"
    return durable


def verify_completion_incorporation_artifacts(
    claim: CompletionDeliveryClaim,
    *,
    turn_admission: TurnAdmission,
    message: str,
    session_dir: Path | str | None = None,
) -> dict:
    """Prove the parked completion turn exists once at the verified tip."""
    if not isinstance(claim, CompletionDeliveryClaim) or not claim.acquired:
        raise CompletionDeliveryReceiptError("completion claim is not held")
    if not isinstance(turn_admission, TurnAdmission):
        raise CompletionDeliveryReceiptError("completion admission is invalid")
    context = claim.context
    receipt = read_completion_delivery_receipt(context, session_dir=session_dir)
    if (
        receipt is None
        or receipt.get("state") != "accepted"
        or receipt.get("owner_token") != claim.owner_token
        or receipt.get("attempt") != claim.attempt
        or receipt.get("reservation_id") != claim.reservation_id
    ):
        raise CompletionDeliveryReceiptError("completion receipt owner is not accepted")
    if (
        not turn_admission.admitted.is_set()
        or turn_admission.gate.is_set()
        or turn_admission.abort.is_set()
        or not turn_admission.permit.acquired
        or turn_admission.root_session_id != context.root_session_id
        or turn_admission.delivery_session_id != context.delivery_session_id
        or turn_admission.stream_id != claim.reservation_id
    ):
        raise CompletionDeliveryReceiptError("completion worker is not safely parked")

    directory = _resolved_session_dir(session_dir)
    tip = _load_sidecar(
        directory / f"{context.delivery_session_id}.json",
        context.delivery_session_id,
    )
    if (
        tip.get("active_stream_id") != turn_admission.stream_id
        or tip.get("pending_user_message") != message
        or tip.get("pending_turn_id") != context.turn_id
        or tip.get("pending_completion_key") != context.completion_key
        or tip.get("pending_completion_correlation_sha256")
        != context.correlation_sha256
    ):
        raise CompletionDeliveryReceiptError("completion sidecar identity mismatch")

    metadata = completion_delivery_metadata(context)

    def owned_rows(value) -> list[dict]:
        return [
            row
            for row in (value if isinstance(value, list) else [])
            if isinstance(row, dict)
            and row.get("role") == "user"
            and row.get("_completion_delivery") == metadata
            and row.get("content") == message
        ]

    if len(owned_rows(tip.get("messages"))) != 1:
        raise CompletionDeliveryReceiptError("completion display checkpoint is not exact")
    if len(owned_rows(tip.get("context_messages"))) != 1:
        raise CompletionDeliveryReceiptError("completion context checkpoint is not exact")
    if context.root_session_id != context.delivery_session_id:
        root = _load_sidecar(
            directory / f"{context.root_session_id}.json",
            context.root_session_id,
        )
        if owned_rows(root.get("messages")) or owned_rows(root.get("context_messages")):
            raise CompletionDeliveryReceiptError("completion checkpoint leaked to lineage root")

    from api.turn_journal import read_turn_journal

    journal = read_turn_journal(context.delivery_session_id, session_dir=directory)
    if journal.get("malformed"):
        raise CompletionDeliveryReceiptError("malformed completion turn journal")
    matches = [
        row
        for row in journal.get("events") or []
        if isinstance(row, dict)
        and row.get("event") == "submitted"
        and row.get("turn_id") == context.turn_id
        and row.get("_completion_delivery") == metadata
        and row.get("stream_id") == turn_admission.stream_id
        and row.get("content") == message
    ]
    if len(matches) != 1:
        raise CompletionDeliveryReceiptError("completion turn journal is not exact")
    return {"sidecar": tip, "journal_event": matches[0]}


def verify_completion_artifacts(*args, **kwargs) -> dict:
    """Canonical P0 name for the durable incorporation read-back barrier."""
    return verify_completion_incorporation_artifacts(*args, **kwargs)
