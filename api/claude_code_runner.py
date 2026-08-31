"""Single-owner launcher for resumable Claude Code sessions."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import select
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from uuid import UUID

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.claude_code_bridge import (
    ClaudeSessionDescriptor,
    build_resume_argv,
    probe_runtime_owner_pids,
    probe_runtime_status,
    resolve_session,
)


LOCK_DIRECTORY_NAME = ".hermes-resume-locks"
READINESS_MAX_BYTES = 256
LEASE_FD_ENVIRONMENT_KEY = "HERMES_RESUME_LEASE_FD"
WRAPPER_FD_ENVIRONMENT_KEY = "HERMES_RESUME_WRAPPER_FD"
PRIVATE_LEASE_FLAG = "--hermes-lease-held"
POST_EXEC_HANDOFF_TIMEOUT_SECONDS = 2.0
POST_EXEC_CONFIRM_DELAY_SECONDS = 0.1


@dataclass
class ResumeLease:
    fd: int
    path: Path
    inode: int

    def close(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# A collision response must not open a lease gap before the one-shot runner
# process is reaped. The OS closes these descriptors at process exit.
_LEASES_RETAINED_UNTIL_EXIT: list[ResumeLease] = []


def _canonical_uuid(value: str) -> str | None:
    try:
        canonical = str(UUID(value))
    except (AttributeError, ValueError):
        return None
    return canonical if value.lower() == canonical else None


def _store_hash(store_id: str) -> str:
    return hashlib.sha256(store_id.encode("utf-8")).hexdigest()[:16]


def resume_lock_path(config_dir: Path, store_id: str, session_id: str) -> Path:
    canonical = _canonical_uuid(session_id)
    if canonical is None or not isinstance(store_id, str) or not store_id:
        raise ValueError("invalid resume lease key")
    return Path(config_dir) / LOCK_DIRECTORY_NAME / f"{_store_hash(store_id)}-{canonical}.lock"


def _private_lock_directory(config_dir: Path) -> tuple[Path, int] | None:
    lock_dir = Path(config_dir) / LOCK_DIRECTORY_NAME
    try:
        os.mkdir(lock_dir, mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(lock_dir, flags)
        info = os.fstat(directory_fd)
    except OSError:
        try:
            os.close(directory_fd)
        except (OSError, UnboundLocalError):
            pass
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(directory_fd)
        return None
    return lock_dir, directory_fd


def acquire_resume_lease(
    config_dir: Path,
    store_id: str,
    session_id: str,
) -> ResumeLease | None:
    """Acquire the stable non-blocking lease, retaining its fd across exec."""
    try:
        lock_path = resume_lock_path(config_dir, store_id, session_id)
    except ValueError:
        return None
    opened = _private_lock_directory(Path(config_dir))
    if opened is None:
        return None
    _lock_dir, directory_fd = opened
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path.name, flags, 0o600, dir_fd=directory_fd)
    except OSError:
        os.close(directory_fd)
        return None
    try:
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or not _named_file_matches(directory_fd, lock_path.name, info)
        ):
            os.close(lock_fd)
            return None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(lock_fd)
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            return None
        after = os.fstat(lock_fd)
        if (
            after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
            or not _named_file_matches(directory_fd, lock_path.name, after)
        ):
            os.close(lock_fd)
            return None
        os.set_inheritable(lock_fd, True)
        return ResumeLease(lock_fd, lock_path, after.st_ino)
    except OSError:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        return None
    finally:
        os.close(directory_fd)


def _named_file_matches(directory_fd: int, name: str, expected) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_uid == os.getuid()
        and current.st_nlink == 1
        and current.st_dev == expected.st_dev
        and current.st_ino == expected.st_ino
    )


def authenticate_inherited_resume_lease(
    config_dir: Path,
    store_id: str,
    session_id: str,
    inherited_fd: int,
) -> ResumeLease | None:
    """Validate and, if needed, lock the runner-inherited stable descriptor."""
    try:
        lock_path = resume_lock_path(config_dir, store_id, session_id)
        inherited = os.fstat(inherited_fd)
    except (OSError, ValueError):
        return None
    if (
        not stat.S_ISREG(inherited.st_mode)
        or inherited.st_uid != os.getuid()
        or inherited.st_nlink != 1
    ):
        return None
    opened = _private_lock_directory(Path(config_dir))
    if opened is None:
        return None
    _lock_dir, directory_fd = opened
    validation_fd = None
    locked = False
    authenticated = False
    try:
        validation_fd = os.open(
            lock_path.name,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        named = os.fstat(validation_fd)
        if (
            named.st_dev != inherited.st_dev
            or named.st_ino != inherited.st_ino
            or not _named_file_matches(directory_fd, lock_path.name, named)
        ):
            return None
        try:
            fcntl.flock(inherited_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            return None
        after = os.fstat(inherited_fd)
        if (
            after.st_dev != named.st_dev
            or after.st_ino != named.st_ino
            or not _named_file_matches(directory_fd, lock_path.name, after)
        ):
            return None
        os.set_inheritable(inherited_fd, True)
        authenticated = True
        return ResumeLease(inherited_fd, lock_path, after.st_ino)
    except OSError:
        return None
    finally:
        if locked and not authenticated:
            try:
                fcntl.flock(inherited_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if validation_fd is not None:
            os.close(validation_fd)
        os.close(directory_fd)


def _explicit_resume_id(arguments: Sequence[str]) -> str | None:
    for index, argument in enumerate(arguments):
        candidate = None
        if argument in {"--resume", "-r"} and index + 1 < len(arguments):
            candidate = arguments[index + 1]
        elif argument.startswith("--resume=") or argument.startswith("-r="):
            candidate = argument.split("=", 1)[1]
        if candidate is not None:
            canonical = _canonical_uuid(candidate)
            if canonical is not None:
                return canonical
    return None


def run_wrapper_protocol(
    config_dir: Path,
    store_id: str,
    command: str,
    arguments: Sequence[str],
    *,
    exec_fn: Callable[[str, Sequence[str]], object] = os.execv,
) -> int:
    """Authenticate runner mode or acquire the direct explicit-resume lease."""
    if not Path(config_dir).is_absolute() or not Path(command).is_absolute():
        return 1
    args = tuple(arguments)
    private_positions = [
        index for index, argument in enumerate(args) if argument == PRIVATE_LEASE_FLAG
    ]
    lease = None
    if private_positions:
        if (
            private_positions != [len(args) - 3]
            or len(args) < 3
            or args[-2] != "--resume"
            or _canonical_uuid(args[-1]) is None
        ):
            return 1
        try:
            inherited_fd = int(os.environ.get(LEASE_FD_ENVIRONMENT_KEY, ""))
        except ValueError:
            return 1
        lease = authenticate_inherited_resume_lease(
            Path(config_dir), store_id, args[-1], inherited_fd
        )
        if lease is None:
            return 1
        args = (*args[:-3], "--resume", str(UUID(args[-1])))
    else:
        session_id = _explicit_resume_id(args)
        if session_id is not None:
            lease = acquire_resume_lease(Path(config_dir), store_id, session_id)
            if lease is None:
                return 1
    wrapper_fd_environment = os.environ.pop(WRAPPER_FD_ENVIRONMENT_KEY, None)
    if wrapper_fd_environment is not None:
        try:
            wrapper_fd = int(wrapper_fd_environment)
            if lease is None or wrapper_fd != lease.fd:
                os.close(wrapper_fd)
        except (OSError, ValueError):
            pass
    inherited_fd_environment = os.environ.pop(LEASE_FD_ENVIRONMENT_KEY, None)
    try:
        exec_fn(command, (command, *args))
        return 0
    except OSError:
        return 1
    finally:
        if inherited_fd_environment is not None:
            os.environ[LEASE_FD_ENVIRONMENT_KEY] = inherited_fd_environment
        if lease is not None:
            lease.close()


def _descriptor_signature(descriptor: ClaudeSessionDescriptor) -> tuple:
    profile = descriptor.profile
    return (
        descriptor.public_id,
        descriptor.store.store_id,
        descriptor.store.config_dir,
        descriptor.store.claude_bin,
        descriptor.claude_session_id,
        descriptor.transcript_path,
        profile.model_id if profile else None,
        profile.argv if profile else None,
        descriptor.cwd,
        descriptor.can_remote_resume,
    )


def _launch_path_signature(descriptor: ClaudeSessionDescriptor) -> tuple | None:
    if descriptor.profile is None or descriptor.cwd is None:
        return None
    paths = (
        descriptor.store.config_dir,
        descriptor.store.claude_bin,
        descriptor.transcript_path,
        Path(descriptor.profile.argv[0]),
        descriptor.cwd,
    )
    identities = []
    try:
        for path in paths:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                return None
            identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
            if stat.S_ISREG(info.st_mode):
                identity += (
                    info.st_size,
                    info.st_nlink,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            identities.append(identity)
    except OSError:
        return None
    return tuple(identities)


def _open_verified_wrapper(
    descriptor: ClaudeSessionDescriptor,
) -> int | None:
    if descriptor.profile is None or not descriptor.profile.argv:
        return None
    path = Path(descriptor.profile.argv[0])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        named = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or not opened.st_mode & stat.S_IXUSR
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
            or stat.S_ISLNK(named.st_mode)
        ):
            os.close(fd)
            return None
        os.set_inheritable(fd, True)
        return fd
    except OSError:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        return None


def _execute_verified_wrapper(
    wrapper_fd: int,
    argv: Sequence[str],
    *,
    exec_fn: Callable[[str, Sequence[str]], object] = os.execv,
    fexec_fn: Callable[[int, Sequence[str], dict[str, str]], object] | None = None,
) -> object:
    previous_wrapper_fd = os.environ.get(WRAPPER_FD_ENVIRONMENT_KEY)
    os.environ[WRAPPER_FD_ENVIRONMENT_KEY] = str(wrapper_fd)
    try:
        if fexec_fn is not None:
            return fexec_fn(wrapper_fd, argv, os.environ)
        native_fexec = getattr(os, "fexecve", None)
        if native_fexec is not None:
            return native_fexec(wrapper_fd, argv, os.environ)
        descriptor_path = f"/dev/fd/{wrapper_fd}"
        opened = os.fstat(wrapper_fd)
        validation_fd = os.open(descriptor_path, os.O_RDONLY)
        try:
            retained = os.fstat(validation_fd)
            if opened.st_dev != retained.st_dev or opened.st_ino != retained.st_ino:
                raise OSError(errno.ESTALE, "verified wrapper descriptor changed")
        finally:
            os.close(validation_fd)
        if sys.platform == "darwin":
            first_line = os.pread(wrapper_fd, 512, 0).split(b"\n", 1)[0]
            try:
                shebang = first_line.decode("ascii")
            except UnicodeError as exc:
                raise OSError(errno.ENOEXEC, "invalid wrapper shebang") from exc
            parts = shebang[2:].strip().split() if shebang.startswith("#!") else []
            if len(parts) != 1 or not Path(parts[0]).is_absolute():
                raise OSError(errno.ENOEXEC, "invalid wrapper shebang")
            interpreter = parts[0]
            allowed_interpreters = {"/bin/sh", "/bin/zsh", sys.executable}
            if interpreter not in allowed_interpreters:
                raise OSError(errno.ENOEXEC, "unsupported wrapper interpreter")
            return exec_fn(
                interpreter,
                (argv[0], descriptor_path, *argv[1:]),
            )
        return exec_fn(descriptor_path, argv)
    finally:
        if previous_wrapper_fd is None:
            os.environ.pop(WRAPPER_FD_ENVIRONMENT_KEY, None)
        else:
            os.environ[WRAPPER_FD_ENVIRONMENT_KEY] = previous_wrapper_fd


def _read_post_exec_handoff(fd: int) -> bool:
    deadline = time.monotonic() + POST_EXEC_HANDOFF_TIMEOUT_SECONDS
    payload = bytearray()
    while len(payload) <= 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return False
        chunk = os.read(fd, 3 - len(payload))
        if not chunk:
            return bytes(payload) == b"E"
        payload.extend(chunk)
    return False


def _post_exec_ownership_state(
    descriptor: ClaudeSessionDescriptor,
    expected_pgid: int,
) -> str:
    for attempt in range(2):
        owners = probe_runtime_owner_pids(descriptor)
        if owners is None:
            return "ownership_unknown"
        foreign_owner = False
        for pid in owners:
            try:
                owner_pgid = os.getpgid(pid)
            except ProcessLookupError:
                continue
            except OSError:
                return "ownership_unknown"
            if owner_pgid != expected_pgid:
                foreign_owner = True
        if foreign_owner:
            return "ownership_conflict"
        if attempt == 0:
            time.sleep(POST_EXEC_CONFIRM_DELAY_SECONDS)
    return "ready"


def _post_exec_ownership_check_child(
    descriptor: ClaudeSessionDescriptor,
    readiness_fd: int,
    exec_status_fd: int,
    expected_pgid: int,
    close_fds: Sequence[int],
) -> None:
    for fd in close_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        state = (
            _post_exec_ownership_state(descriptor, expected_pgid)
            if _read_post_exec_handoff(exec_status_fd)
            else "ownership_unknown"
        )
    except Exception:
        state = "ownership_unknown"
    finally:
        try:
            os.close(exec_status_fd)
        except OSError:
            pass
    _write_readiness(readiness_fd, state)


def _start_post_exec_ownership_check(
    descriptor: ClaudeSessionDescriptor,
    readiness_fd: int,
    expected_pgid: int,
    close_fds: Sequence[int],
) -> int | None:
    exec_status_read, exec_status_write = os.pipe()
    ack_read, ack_write = os.pipe()
    try:
        first_child = os.fork()
    except OSError:
        os.close(exec_status_read)
        os.close(exec_status_write)
        os.close(ack_read)
        os.close(ack_write)
        _write_readiness(readiness_fd, "ownership_unknown")
        return None
    if first_child == 0:
        os.close(exec_status_write)
        os.close(ack_read)
        try:
            checker = os.fork()
        except OSError:
            try:
                os.write(ack_write, b"F")
            except OSError:
                pass
            _write_readiness(readiness_fd, "ownership_unknown")
            os._exit(1)
        if checker:
            os.close(ack_write)
            os.close(exec_status_read)
            os._exit(0)
        try:
            os.write(ack_write, b"O")
        except OSError:
            pass
        os.close(ack_write)
        _post_exec_ownership_check_child(
            descriptor,
            readiness_fd,
            exec_status_read,
            expected_pgid,
            close_fds,
        )
        os._exit(0)

    os.close(exec_status_read)
    os.close(ack_write)
    try:
        acknowledged = os.read(ack_read, 1) == b"O"
    except OSError:
        acknowledged = False
    finally:
        os.close(ack_read)
        try:
            os.waitpid(first_child, 0)
        except (ChildProcessError, OSError):
            acknowledged = False
    if not acknowledged:
        os.close(exec_status_write)
        return None
    os.close(readiness_fd)
    return exec_status_write


def _write_readiness(fd: int, state: str) -> None:
    record = json.dumps({"state": state}, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(record) > READINESS_MAX_BYTES:
        record = b'{"state":"ownership_unknown"}\n'
    try:
        view = memoryview(record)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                break
            view = view[written:]
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def run_session(
    public_id: str,
    readiness_fd: int,
    *,
    exec_fn: Callable[[str, Sequence[str]], object] = os.execv,
    post_exec_check_fn: Callable[
        [ClaudeSessionDescriptor, int, int, Sequence[int]], int | None
    ] = _start_post_exec_ownership_check,
) -> int:
    """Acquire, revalidate, then report readiness after replacing this process."""
    descriptor = resolve_session(public_id)
    if descriptor is None or not descriptor.can_remote_resume:
        _write_readiness(readiness_fd, "invalid_session")
        return 1
    path_signature = _launch_path_signature(descriptor)
    if path_signature is None:
        _write_readiness(readiness_fd, "invalid_session")
        return 1
    lease = acquire_resume_lease(
        descriptor.store.config_dir,
        descriptor.store.store_id,
        descriptor.claude_session_id,
    )
    if lease is None:
        _write_readiness(readiness_fd, "active_elsewhere")
        return 1
    wrapper_fd = -1
    exec_status_fd = -1
    try:
        current = resolve_session(public_id)
        if (
            current is None
            or _descriptor_signature(current) != _descriptor_signature(descriptor)
            or _launch_path_signature(current) != path_signature
        ):
            _write_readiness(readiness_fd, "invalid_session")
            return 1
        runtime = probe_runtime_status(current, fresh=True)
        if runtime.state == "active_elsewhere":
            _write_readiness(readiness_fd, "ownership_conflict")
            _LEASES_RETAINED_UNTIL_EXIT.append(lease)
            lease = None
            return 1
        if runtime.state != "inactive":
            _write_readiness(readiness_fd, "ownership_unknown")
            return 1
        launch_descriptor = resolve_session(public_id)
        if (
            launch_descriptor is None
            or _descriptor_signature(launch_descriptor)
            != _descriptor_signature(current)
            or _launch_path_signature(launch_descriptor) != path_signature
        ):
            _write_readiness(readiness_fd, "invalid_session")
            return 1
        wrapper_fd = _open_verified_wrapper(launch_descriptor)
        if wrapper_fd is None:
            _write_readiness(readiness_fd, "invalid_session")
            return 1
        argv = build_resume_argv(launch_descriptor)
        previous_cwd = Path.cwd()
        try:
            os.chdir(launch_descriptor.cwd)
        except (OSError, TypeError):
            _write_readiness(readiness_fd, "invalid_session")
            return 1
        previous_pwd = os.environ.get("PWD")
        os.environ["PWD"] = str(launch_descriptor.cwd)
        previous_lease_fd = os.environ.get(LEASE_FD_ENVIRONMENT_KEY)
        os.environ[LEASE_FD_ENVIRONMENT_KEY] = str(lease.fd)
        try:
            exec_status = post_exec_check_fn(
                launch_descriptor,
                readiness_fd,
                os.getpgrp(),
                (lease.fd, wrapper_fd),
            )
            if exec_status is None:
                return 1
            exec_status_fd = exec_status
            if exec_status_fd >= 0:
                os.write(exec_status_fd, b"E")
            _execute_verified_wrapper(
                wrapper_fd,
                argv,
                exec_fn=exec_fn,
            )
        finally:
            if exec_status_fd >= 0:
                try:
                    os.write(exec_status_fd, b"F")
                except OSError:
                    pass
                try:
                    os.close(exec_status_fd)
                except OSError:
                    pass
            if previous_lease_fd is None:
                os.environ.pop(LEASE_FD_ENVIRONMENT_KEY, None)
            else:
                os.environ[LEASE_FD_ENVIRONMENT_KEY] = previous_lease_fd
            if previous_pwd is None:
                os.environ.pop("PWD", None)
            else:
                os.environ["PWD"] = previous_pwd
            os.chdir(previous_cwd)
        return 0
    except (OSError, ValueError):
        return 1
    finally:
        if wrapper_fd >= 0:
            try:
                os.close(wrapper_fd)
            except OSError:
                pass
        if lease is not None:
            lease.close()


def main(argv: Sequence[str] | None = None) -> int:
    argv = tuple(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "--wrapper":
        if len(argv) < 4:
            return 2
        return run_wrapper_protocol(Path(argv[1]), argv[2], argv[3], argv[4:])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("public_id")
    parser.add_argument("readiness_fd", type=int)
    args = parser.parse_args(argv)
    if args.readiness_fd < 0:
        return 2
    return run_session(args.public_id, args.readiness_fd)


if __name__ == "__main__":
    raise SystemExit(main())
