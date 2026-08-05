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
_TRANSITION_STATES = frozenset({"pending", "committed"})
_TRANSITION_DIR_NAME = "_session_lineage_transitions"
_PERMIT_DIR_NAME = "_session_lineage_permits"
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


@dataclass(frozen=True)
class LineageResolution:
    root_session_id: str
    delivery_session_id: str
    profile: str
    hop_count: int


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
    """Durably publish one pending or committed compression transition."""
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


def _is_non_compression_child(row: dict) -> bool:
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
        if not parent_value or _is_non_compression_child(row):
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
            and not _is_non_compression_child(child)
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
        if transition["state"] != "committed":
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
