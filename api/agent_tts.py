"""Profile-bound, bounded supervisor for Agent-owned text-to-speech.

The WebUI parent never imports provider runtimes.  It captures one request
profile, starts one short-lived isolated child, exchanges a capped JSON request
and status file, and tears down the whole process tree and request directory on
every exit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, TypeVar

from api.config import PYTHON_EXE, REPO_ROOT
from api.profiles import (
    build_profile_subprocess_env,
    get_active_profile_name,
    get_hermes_home_for_profile,
)

logger = logging.getLogger(__name__)

AGENT_TTS_SCHEMA = 1
AGENT_TTS_REQUEST_MAX_BYTES = 64 * 1024
AGENT_TTS_STATUS_MAX_BYTES = 64 * 1024
AGENT_TTS_CAPABILITY_TIMEOUT_SECONDS = 10.0
AGENT_TTS_SYNTHESIS_TIMEOUT_SECONDS = 60.0
AGENT_TTS_MAX_AUDIO_BYTES = 16 * 1024 * 1024
AGENT_TTS_DEFAULT_REQUEST_MAX_CHARS = 4000
AGENT_TTS_COMPAT_REQUEST_MAX_CHARS = 2000


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


AGENT_TTS_MAX_WORKERS = _env_int("HERMES_WEBUI_TTS_MAX_WORKERS", 2, 1, 8)
_global_worker_slots = threading.BoundedSemaphore(AGENT_TTS_MAX_WORKERS)
_active_owner_lock = threading.Lock()
_active_owner_keys: set[str] = set()

_ERROR_STATUS = {
    "invalid_request": 400,
    "text_too_long": 400,
    "invalid_provider": 400,
    "legacy_tts_migration_required": 409,
    "stale_config": 409,
    "config_conflict": 409,
    "agent_busy": 429,
    "agent_contract_unavailable": 503,
    "provider_unavailable": 503,
    "managed_config": 503,
    "config_write_failed": 503,
    "agent_worker_start": 503,
    "agent_cancelled": 499,
    "agent_timeout": 504,
    "agent_worker_protocol": 502,
    "synthesis_failed": 502,
    "tts_artifact_invalid": 502,
    "tts_artifact_too_large": 502,
    "provider_mismatch": 502,
}
_ERROR_MESSAGES = {
    "invalid_request": "Invalid TTS request.",
    "text_too_long": "TTS text exceeds the active provider limit.",
    "invalid_provider": "Invalid TTS provider selection.",
    "legacy_tts_migration_required": "This saved TTS engine must be migrated before use.",
    "stale_config": "TTS configuration changed; refresh and try again.",
    "config_conflict": "TTS configuration changed; refresh and try again.",
    "agent_busy": "Agent TTS is busy. Try again shortly.",
    "agent_contract_unavailable": "This Agent installation does not expose a compatible TTS contract.",
    "provider_unavailable": "The selected Agent TTS provider is unavailable.",
    "managed_config": "This Hermes configuration is read-only.",
    "config_write_failed": "The Agent TTS configuration could not be saved.",
    "agent_worker_start": "Agent TTS worker could not be started.",
    "agent_cancelled": "Agent TTS synthesis was cancelled.",
    "agent_timeout": "Agent TTS synthesis timed out.",
    "agent_worker_protocol": "Agent TTS worker returned an invalid response.",
    "synthesis_failed": "Agent TTS synthesis failed.",
    "tts_artifact_invalid": "Agent TTS produced invalid audio.",
    "tts_artifact_too_large": "Agent TTS audio exceeded the size limit.",
    "provider_mismatch": "Agent TTS returned audio from an unexpected provider.",
}


@dataclass
class AgentTtsError(RuntimeError):
    code: str
    status: int
    public_message: str

    def __str__(self) -> str:
        return self.public_message


@dataclass(frozen=True)
class AgentTtsProfileScope:
    name: str
    home: Path
    config_path: Path
    child_env: Mapping[str, str]


@dataclass(frozen=True)
class AgentTtsAudio:
    data: bytes
    content_type: str
    provider: str


T = TypeVar("T")
ResultHandler = Callable[[dict[str, Any], Path], T]


def _error(code: str, *, status: int | None = None) -> AgentTtsError:
    return AgentTtsError(
        code=code,
        status=status if status is not None else _ERROR_STATUS.get(code, 502),
        public_message=_ERROR_MESSAGES.get(code, "Agent TTS failed."),
    )


def capture_agent_tts_profile_scope() -> AgentTtsProfileScope:
    """Capture active profile identity, paths, and child env exactly once."""
    name = get_active_profile_name() or "default"
    home = Path(get_hermes_home_for_profile(name)).expanduser().resolve(strict=False)
    env = build_profile_subprocess_env(name, home)
    return AgentTtsProfileScope(
        name=name,
        home=home,
        config_path=(home / "config.yaml").resolve(strict=False),
        child_env=MappingProxyType(dict(env)),
    )


def _acquire_global_slot() -> bool:
    return _global_worker_slots.acquire(blocking=False)


def _release_global_slot() -> None:
    _global_worker_slots.release()


def _claim_owner(owner_key: str) -> bool:
    with _active_owner_lock:
        if owner_key in _active_owner_keys:
            return False
        _active_owner_keys.add(owner_key)
        return True


def _release_owner(owner_key: str) -> None:
    with _active_owner_lock:
        _active_owner_keys.discard(owner_key)


def _worker_command() -> list[str]:
    return [PYTHON_EXE, "-m", "api.agent_tts_worker"]


class _WindowsWorkerJob:
    """Kill-on-close Windows Job Object owning one worker process tree."""

    def __init__(self, handle: Any, close_handle: Callable[[Any], Any]):
        self._handle = handle
        self._close_handle = close_handle

    def close(self) -> None:
        if self._handle:
            self._close_handle(self._handle)
            self._handle = None


def _create_windows_worker_job(proc: subprocess.Popen) -> _WindowsWorkerJob | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ) or not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(proc._handle)):
            kernel32.CloseHandle(handle)
            return None
        return _WindowsWorkerJob(handle, kernel32.CloseHandle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _terminate_worker_tree(proc: subprocess.Popen) -> None:
    """Terminate, then kill and reap a worker-owned process tree."""
    leader_running = proc.poll() is None
    if os.name == "posix":
        # start_new_session=True makes the worker PID its process-group ID. Signal
        # the group even when the leader has already exited: provider descendants
        # can outlive it and would otherwise escape cleanup.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    elif os.name == "nt" and leader_running:
        try:
            proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        except Exception:
            pass
    elif leader_running:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=1.0)
    except Exception:
        pass
    if os.name == "posix":
        # The leader may already be reaped while its process group still contains
        # provider children. Give SIGTERM a short grace period, then kill the group.
        deadline = time.monotonic() + (1.0 if leader_running else 0.02)
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except OSError:
                return
            time.sleep(0.02)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    elif os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except Exception:
            pass
    elif leader_running:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass


def _request_directory(scope: AgentTtsProfileScope) -> Path:
    home = scope.home.resolve(strict=False)
    cache = home / "cache"
    base = cache / "webui-tts-requests"
    try:
        home_existed = home.exists()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home_metadata = os.lstat(home)
        if stat.S_ISLNK(home_metadata.st_mode) or not stat.S_ISDIR(home_metadata.st_mode):
            raise OSError("profile home is not a real directory")
        if not home_existed:
            os.chmod(home, 0o700)
        for directory in (cache, base):
            directory.mkdir(exist_ok=True, mode=0o700)
            metadata = os.lstat(directory)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("request root is not a real directory")
            if not directory.resolve(strict=True).is_relative_to(home):
                raise OSError("request root escaped profile home")
            os.chmod(directory, 0o700)
        request_dir = Path(tempfile.mkdtemp(prefix="request-", dir=base))
        os.chmod(request_dir, 0o700)
        return request_dir
    except OSError as exc:
        raise _error("agent_worker_start") from exc


def _read_status_file(path: Path) -> dict[str, Any]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _error("agent_worker_protocol") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _error("agent_worker_protocol")
    if metadata.st_size <= 0 or metadata.st_size > AGENT_TTS_STATUS_MAX_BYTES:
        raise _error("agent_worker_protocol")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb", closefd=True) as handle:
            raw = handle.read(AGENT_TTS_STATUS_MAX_BYTES + 1)
    except OSError as exc:
        raise _error("agent_worker_protocol") from exc
    if not raw or len(raw) > AGENT_TTS_STATUS_MAX_BYTES:
        raise _error("agent_worker_protocol")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("agent_worker_protocol") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != AGENT_TTS_SCHEMA
        or not isinstance(payload.get("ok"), bool)
        or not isinstance(payload.get("code"), str)
    ):
        raise _error("agent_worker_protocol")
    return payload


_WORKER_DIAGNOSTICS = frozenset(
    {
        "provider_timeout",
        "provider_rate_limit",
        "provider_auth",
        "provider_request",
        "provider_dependency",
        "provider_network",
        "provider_error",
    }
)
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _raise_worker_error(payload: dict[str, Any]) -> None:
    code = str(payload.get("code") or "agent_worker_protocol")
    if code not in _ERROR_STATUS:
        code = "agent_worker_protocol"
    diagnostic = str(payload.get("diagnostic") or "")
    if diagnostic not in _WORKER_DIAGNOSTICS:
        diagnostic = "none"
    provider = str(payload.get("provider") or "").strip().lower()
    if not _PROVIDER_ID_RE.fullmatch(provider):
        provider = "unknown"
    logger.warning(
        "Agent TTS worker failed code=%s diagnostic=%s provider=%s",
        code,
        diagnostic,
        provider,
    )
    raise _error(code)


def _mpeg_audio_frame_length(data: bytes, offset: int = 0) -> int | None:
    if len(data) < offset + 4:
        return None
    first, second, third, _fourth = data[offset : offset + 4]
    if first != 0xFF or second & 0xE0 != 0xE0:
        return None
    version = (second >> 3) & 0x03
    layer = (second >> 1) & 0x03
    bitrate_index = (third >> 4) & 0x0F
    sample_index = (third >> 2) & 0x03
    if version == 1 or layer == 0 or bitrate_index in (0, 15) or sample_index == 3:
        return None

    if version == 3:
        bitrate_tables = {
            3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
            2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
            1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        }
        sample_rates = (44100, 48000, 32000)
    else:
        bitrate_tables = {
            3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
            2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
            1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        }
        sample_rates = (22050, 24000, 16000) if version == 2 else (11025, 12000, 8000)

    bitrate = bitrate_tables[layer][bitrate_index] * 1000
    sample_rate = sample_rates[sample_index]
    padding = (third >> 1) & 0x01
    if layer == 3:  # Layer I
        frame_length = ((12 * bitrate) // sample_rate + padding) * 4
    elif layer == 1 and version != 3:  # MPEG-2/2.5 Layer III
        frame_length = (72 * bitrate) // sample_rate + padding
    else:
        frame_length = (144 * bitrate) // sample_rate + padding
    return frame_length if offset + frame_length <= len(data) else None


def _audio_signature(data: bytes) -> tuple[str, set[str]] | None:
    if len(data) >= 10 and data.startswith(b"ID3"):
        if data[3] not in (2, 3, 4) or data[4] == 0xFF:
            return None
        size_bytes = data[6:10]
        if any(value & 0x80 for value in size_bytes):
            return None
        tag_size = (
            (size_bytes[0] << 21)
            | (size_bytes[1] << 14)
            | (size_bytes[2] << 7)
            | size_bytes[3]
        )
        audio_offset = tag_size + 10
        if data[3] == 4 and data[5] & 0x10:
            audio_offset += 10
        if _mpeg_audio_frame_length(data, audio_offset) is not None:
            return "audio/mpeg", {".mp3"}
        return None
    if _mpeg_audio_frame_length(data) is not None:
        return "audio/mpeg", {".mp3"}
    if len(data) >= 28 and data.startswith(b"OggS"):
        if data[4] != 0 or data[5] & ~0x07:
            return None
        segment_count = data[26]
        table_end = 27 + segment_count
        if segment_count == 0 or table_end > len(data):
            return None
        payload_length = sum(data[27:table_end])
        payload = data[table_end : table_end + payload_length]
        if (
            payload_length > 0
            and table_end + payload_length <= len(data)
            and payload.startswith((b"OpusHead", b"\x01vorbis", b"Speex   ", b"fLaC"))
        ):
            return "audio/ogg", {".ogg", ".opus"}
        return None
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            return None
        position = 12
        has_format = False
        has_audio = False
        while position + 8 <= len(data):
            chunk_id = data[position : position + 4]
            chunk_length = int.from_bytes(data[position + 4 : position + 8], "little")
            position += 8
            if position + chunk_length > len(data):
                return None
            if chunk_id == b"fmt " and chunk_length >= 16:
                has_format = True
            elif chunk_id == b"data" and chunk_length > 0:
                has_audio = True
            position += chunk_length
            if chunk_length & 1 and position < len(data):
                position += 1
        if has_format and has_audio and position == len(data):
            return "audio/wav", {".wav"}
        return None
    if len(data) >= 8 and data.startswith(b"fLaC"):
        position = 4
        first_block = True
        while position + 4 <= len(data):
            block_header = data[position]
            block_type = block_header & 0x7F
            block_length = int.from_bytes(data[position + 1 : position + 4], "big")
            position += 4
            if block_type == 127 or position + block_length > len(data):
                return None
            if first_block and (block_type != 0 or block_length != 34):
                return None
            first_block = False
            position += block_length
            if block_header & 0x80:
                if len(data) >= position + 2 and data[position] == 0xFF and data[position + 1] & 0xFC == 0xF8:
                    return "audio/flac", {".flac"}
                return None
        return None
    if len(data) >= 16 and data.startswith(b"ADIF") and any(data[4:]):
        return "audio/aac", {".aac"}
    if len(data) >= 7 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0:
        sample_index = (data[2] >> 2) & 0x0F
        frame_length = (
            ((data[3] & 0x03) << 11) | (data[4] << 3) | ((data[5] & 0xE0) >> 5)
        )
        if sample_index != 0x0F and 7 <= frame_length <= len(data):
            return "audio/aac", {".aac"}
    return None


def _consume_audio_artifact(
    status_payload: dict[str, Any], request_dir: Path
) -> AgentTtsAudio:
    """Open once, validate by descriptor, and return bounded audio bytes."""
    artifact_value = status_payload.get("artifact_path")
    provider = status_payload.get("provider")
    if not isinstance(artifact_value, str) or not isinstance(provider, str):
        raise _error("tts_artifact_invalid")
    candidate = Path(artifact_value)
    if not candidate.is_absolute():
        raise _error("tts_artifact_invalid")
    request_root = Path(os.path.abspath(request_dir))
    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        lexical_candidate.relative_to(request_root)
    except ValueError as exc:
        raise _error("tts_artifact_invalid") from exc
    try:
        before = os.lstat(lexical_candidate)
    except OSError as exc:
        raise _error("tts_artifact_invalid") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _error("tts_artifact_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(lexical_candidate, flags)
    except OSError as exc:
        raise _error("tts_artifact_invalid") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise _error("tts_artifact_invalid")
        if opened.st_size <= 0:
            raise _error("tts_artifact_invalid")
        if opened.st_size > AGENT_TTS_MAX_AUDIO_BYTES:
            raise _error("tts_artifact_too_large")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            data = handle.read(AGENT_TTS_MAX_AUDIO_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > AGENT_TTS_MAX_AUDIO_BYTES:
        raise _error("tts_artifact_too_large")
    signature = _audio_signature(data)
    if signature is None:
        raise _error("tts_artifact_invalid")
    content_type, suffixes = signature
    if lexical_candidate.suffix.lower() not in suffixes:
        raise _error("tts_artifact_invalid")
    return AgentTtsAudio(data=data, content_type=content_type, provider=provider)


def synthesize_agent_tts(
    text: str,
    *,
    profile_scope: AgentTtsProfileScope | None = None,
    owner_key: str | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> AgentTtsAudio:
    if not isinstance(text, str) or not text.strip():
        raise _error("invalid_request")
    result = run_agent_tts_operation(
        "synthesize",
        {"text": text},
        profile_scope=profile_scope,
        owner_key=owner_key,
        timeout_seconds=AGENT_TTS_SYNTHESIS_TIMEOUT_SECONDS,
        result_handler=_consume_audio_artifact,
        cancellation_check=cancellation_check,
    )
    if not isinstance(result, AgentTtsAudio):
        raise _error("agent_worker_protocol")
    return result


def run_agent_tts_operation(
    operation: str,
    payload: Mapping[str, Any] | None = None,
    *,
    profile_scope: AgentTtsProfileScope | None = None,
    owner_key: str | None = None,
    timeout_seconds: float = AGENT_TTS_CAPABILITY_TIMEOUT_SECONDS,
    result_handler: ResultHandler[T] | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> dict[str, Any] | T:
    """Run one bounded Agent TTS child operation and clean all request files."""
    if operation not in {"capability", "select_provider", "synthesize", "restore_tts"}:
        raise _error("invalid_request")
    scope = profile_scope or capture_agent_tts_profile_scope()
    owner = owner_key or f"profile:{scope.name}"
    if not _claim_owner(owner):
        raise _error("agent_busy")
    acquired_global = False
    request_dir: Path | None = None
    proc: subprocess.Popen | None = None
    worker_job: _WindowsWorkerJob | None = None
    try:
        acquired_global = _acquire_global_slot()
        if not acquired_global:
            raise _error("agent_busy")
        request_dir = _request_directory(scope)
        status_path = request_dir / "status.json"
        request: dict[str, Any] = {
            "schema": AGENT_TTS_SCHEMA,
            "op": operation,
            "request_dir": str(request_dir),
            "status_path": str(status_path),
        }
        if payload:
            request.update(dict(payload))
        if operation == "synthesize":
            request["output_path"] = str(request_dir / "audio.mp3")
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > AGENT_TTS_REQUEST_MAX_BYTES:
            raise _error("invalid_request")

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": dict(scope.child_env),
            "cwd": str(REPO_ROOT),
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        try:
            proc = subprocess.Popen(_worker_command(), **popen_kwargs)
            worker_job = _create_windows_worker_job(proc)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _error("agent_worker_start") from exc
        if cancellation_check is None:
            try:
                proc.communicate(encoded, timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _terminate_worker_tree(proc)
                proc = None
                raise _error("agent_timeout") from exc
        else:
            try:
                if proc.stdin is None:
                    raise OSError("worker stdin unavailable")
                proc.stdin.write(encoded)
                proc.stdin.close()
                proc.stdin = None
            except (OSError, BrokenPipeError) as exc:
                raise _error("agent_worker_protocol") from exc
            deadline = time.monotonic() + timeout_seconds
            while proc.poll() is None:
                if cancellation_check():
                    _terminate_worker_tree(proc)
                    proc = None
                    raise _error("agent_cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_worker_tree(proc)
                    proc = None
                    raise _error("agent_timeout")
                try:
                    proc.wait(timeout=min(0.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
        status = _read_status_file(status_path)
        if status["ok"] is not True:
            _raise_worker_error(status)
        if proc.returncode not in (0, None):
            raise _error("agent_worker_protocol")
        if result_handler is not None:
            return result_handler(status, request_dir)
        return status
    finally:
        if proc is not None:
            _terminate_worker_tree(proc)
        if worker_job is not None:
            worker_job.close()
        if request_dir is not None:
            shutil.rmtree(request_dir, ignore_errors=True)
        if acquired_global:
            _release_global_slot()
        _release_owner(owner)
