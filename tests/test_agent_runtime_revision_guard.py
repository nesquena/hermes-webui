"""Regression coverage for Hermes Agent source changes during a WebUI process lifetime."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import types

import pytest


REPO = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def changed_agent_checkout(monkeypatch, tmp_path):
    """Exercise the real revision guard and scheduler without replacing a process."""
    from api import agent_runtime, routes, updates

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    module_path = agent_root / "run_agent.py"
    module_path.write_text("class AIAgent: pass\n", encoding="utf-8")
    _git(agent_root, "init", "-q")
    _git(agent_root, "add", "run_agent.py")
    _git(agent_root, "commit", "-qm", "loaded")
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", agent_root)
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", module_path)
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", _git(agent_root, "rev-parse", "HEAD"))
    module_path.write_text("class AIAgent: revision = 'changed'\n", encoding="utf-8")
    _git(agent_root, "commit", "-qam", "changed")

    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    venv_root = tmp_path / "separate-install"
    (venv_root / "venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(agent_runtime, "_HERMES_HOME", hermes_home)
    monkeypatch.setattr(agent_runtime, "_AGENT_PYTHON", venv_root / "venv" / "bin" / "python")
    workers = []

    class CapturedThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            workers.append(self.target)

    monkeypatch.setattr(updates, "threading", types.SimpleNamespace(Thread=CapturedThread))
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(
        routes, "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )

    def no_session_mutation(*_args, **_kwargs):
        pytest.fail("stale runtime reached session materialization")

    monkeypatch.setattr(routes, "_get_or_materialize_session", no_session_mutation)
    return types.SimpleNamespace(
        root=agent_root, home=hermes_home, venv_root=venv_root, workers=workers,
        request=lambda: routes._handle_chat_start(object(), {"session_id": "stale-session"}),
    )


@pytest.mark.parametrize(
    ("marker_state", "diagnostic"),
    [
        ("absent", "unverified"),
        ("failed-exit", "unverified"),
        ("interrupted-exit", "unverified"),
        ("active", "active"),
        ("dead", "stale"),
        ("over-age", "stale"),
        ("malformed", "unknown"),
        ("invalid-encoding", "unknown"),
        ("future", "unknown"),
        ("nonfinite", "unknown"),
        ("oversized-pid", "unknown"),
        ("dangling", "unknown"),
        ("unreadable", "unknown"),
        ("update-incomplete", "incomplete"),
        ("lazy-refresh-incomplete", "incomplete"),
        ("separate-venv-incomplete", "incomplete"),
        ("symlink-venv-incomplete", "incomplete"),
    ],
)
def test_unverified_update_keeps_manual_409_without_restart(
    changed_agent_checkout, marker_state, diagnostic,
):
    """Readable HEAD and lifecycle markers never authorize process replacement."""
    checkout = changed_agent_checkout
    marker = checkout.home / ".hermes-update-in-progress"
    if marker_state in {"active", "failed-exit", "interrupted-exit"}:
        marker.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
        if marker_state != "active":
            marker.unlink()  # Agent removes its marker on failed/interrupted exits.
    elif marker_state == "dead":
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        marker.write_text(f"{process.pid}\n{time.time()}\n", encoding="utf-8")
    elif marker_state == "over-age":
        marker.write_text(f"{os.getpid()}\n{time.time() - 86400}\n", encoding="utf-8")
    elif marker_state == "malformed":
        marker.write_text("not-a-pid\nnot-a-time\n", encoding="utf-8")
    elif marker_state == "invalid-encoding":
        marker.write_bytes(b"\xff\xfe")
    elif marker_state == "oversized-pid":
        marker.write_text(f"{2 ** 100}\n{time.time()}\n", encoding="utf-8")
    elif marker_state in {"future", "nonfinite"}:
        timestamp = time.time() + 86400 if marker_state == "future" else "nan"
        marker.write_text(f"{os.getpid()}\n{timestamp}\n", encoding="utf-8")
    elif marker_state == "dangling":
        marker.symlink_to(checkout.home / "missing")
    elif marker_state == "unreadable":
        marker.mkdir()
    elif marker_state in {"update-incomplete", "lazy-refresh-incomplete"}:
        (checkout.root / f".{marker_state}").touch()
    elif marker_state == "separate-venv-incomplete":
        (checkout.venv_root / ".update-incomplete").touch()
    elif marker_state == "symlink-venv-incomplete":
        (checkout.venv_root / "venv/bin/python").symlink_to(sys.executable)
        (checkout.venv_root / ".update-incomplete").touch()

    response = checkout.request()

    assert checkout.workers == [], "revision mismatch scheduled an automatic restart"
    assert response["status"] == 409
    payload = response["payload"]
    assert payload["type"] == "agent_runtime_stale"
    assert payload["retryable"] is True
    assert payload["restart_scheduled"] is False
    assert payload["agent_update_state"] == diagnostic
    assert "Restart Hermes WebUI manually" in payload["error"]
    assert "success" not in payload["error"].lower()


def test_final_read_cannot_authorize_restart_without_atomic_handoff(
    monkeypatch, changed_agent_checkout,
):
    """An updater can acquire its marker after a read; no worker may be queued."""
    from api import agent_runtime

    checkout = changed_agent_checkout
    read_revision = agent_runtime._read_agent_revision
    marker = checkout.home / ".hermes-update-in-progress"
    revision_reads = []

    def read_then_start_update(*args, **kwargs):
        revision = read_revision(*args, **kwargs)
        revision_reads.append(revision)
        marker.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
        return revision

    monkeypatch.setattr(agent_runtime, "_read_agent_revision", read_then_start_update)
    response = checkout.request()

    assert checkout.workers == [], "an unprotected read authorized automatic restart"
    assert revision_reads == [_git(checkout.root, "rev-parse", "HEAD")]
    assert marker.is_file()
    assert response["status"] == 409
    assert response["payload"]["restart_scheduled"] is False


@pytest.mark.parametrize("response_kind", ["http-error", "raised-error"])
def test_async_compression_preserves_manual_restart_diagnostics(
    monkeypatch, changed_agent_checkout, response_kind,
):
    """The real guard reaches the job's terminal error and repeated status reads."""
    from api import agent_runtime, helpers, routes

    checkout = changed_agent_checkout
    sid = "stale-compression"
    (checkout.root / ".update-incomplete").touch()
    monkeypatch.setattr(routes, "get_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_MANUAL_COMPRESSION_JOBS", {
        sid: {"session_id": sid, "status": "running", "updated_at": time.time()},
    })

    def compress(handler, _body):
        try:
            agent_runtime.ensure_agent_runtime_current()
        except agent_runtime.AgentRuntimeChangedError as exc:
            if response_kind == "raised-error":
                raise
            helpers.j(handler, agent_runtime.agent_runtime_stale_payload(exc), status=409)

    monkeypatch.setattr(routes, "_handle_session_compress", compress)
    routes._run_manual_compression_job(sid, {"session_id": sid})

    for _ in range(2):
        response = routes._handle_session_compress_status(object(), sid)
        payload = response["payload"]
        assert payload["status"] == "error"
        assert payload["error_status"] == 409
        assert payload["type"] == "agent_runtime_stale"
        assert payload["retryable"] is True
        assert payload["restart_scheduled"] is False
        assert payload["agent_update_state"] == "incomplete"
        assert "Restart Hermes WebUI manually" in payload["error"]
    assert checkout.workers == []


def test_loaded_agent_runtime_fails_closed_after_source_revision_changes(tmp_path: Path):
    agent_dir = tmp_path / "hermes-agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        "class AIAgent:\n    revision = 'before'\n",
        encoding="utf-8",
    )
    _git(agent_dir, "init", "-q")
    _git(agent_dir, "add", "run_agent.py")
    _git(agent_dir, "commit", "-qm", "before")

    probe = tmp_path / "probe.py"
    probe.write_text(
        """
import sys

# On a machine where hermes_agent is installed as an editable package, setuptools
# registers a meta-path finder (__editable___hermes_agent_*_finder) that maps
# `run_agent` -> the real on-disk agent module. That finder runs BEFORE the
# PYTHONPATH-based PathFinder, so it would shadow the synthetic per-test agent
# dir this test points HERMES_WEBUI_AGENT_DIR at (the real AIAgent has no
# `.revision` attribute). Drop any such editable finder + purge cached agent
# modules so `import run_agent` resolves from the test's agent_dir on PYTHONPATH.
# On CI (no editable install) this is a harmless no-op.
sys.meta_path[:] = [
    _f for _f in sys.meta_path
    if "__editable__" not in type(_f).__module__ and "__editable__" not in getattr(_f, "__module__", "")
]
for _m in ("run_agent", "hermes_state", "agent", "tools"):
    sys.modules.pop(_m, None)

from pathlib import Path
import subprocess

import api.streaming as streaming
from api import agent_runtime

agent_dir = Path(__file__).parent / "hermes-agent"
assert agent_runtime._AGENT_DIR == agent_dir.resolve()
assert streaming._get_ai_agent().revision == "before"

(agent_dir / "run_agent.py").write_text(
    "class AIAgent:\\n    revision = 'after'\\n",
    encoding="utf-8",
)
subprocess.run(["git", "add", "run_agent.py"], cwd=agent_dir, check=True)
subprocess.run(
    [
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "after",
    ],
    cwd=agent_dir,
    check=True,
)

try:
    streaming._get_ai_agent()
except RuntimeError as exc:
    message = str(exc)
    assert "Hermes Agent was updated" in message
    assert "Restart Hermes WebUI manually" in message
else:
    raise AssertionError("stale in-process AIAgent was reused after its source revision changed")

try:
    agent_runtime.require_ai_agent_class()
except agent_runtime.AgentRuntimeChangedError as exc:
    assert "Restart Hermes WebUI manually" in str(exc)
else:
    raise AssertionError("unguarded AIAgent import was allowed after its source revision changed")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_HOME": str(tmp_path / "hermes-home"),
            "HERMES_WEBUI_STATE_DIR": str(tmp_path / "webui-state"),
            "PYTHONPATH": os.pathsep.join((str(REPO), str(agent_dir))),
        }
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_read_live_agent_update_rejects_fifo_marker_without_hanging(tmp_path: Path):
    """A FIFO in the marker path must classify ``unknown`` fast, not block.

    Regression guard: the marker read once used ``Path.read_text()``, which
    blocks forever on a FIFO (no writer) and would wedge the stale-runtime
    request path. The hardened read opens O_NONBLOCK|O_NOFOLLOW and fstat-checks
    for a small regular file, so a FIFO is rejected immediately.
    """
    from api import agent_runtime

    fifo = tmp_path / "fifo-marker"
    os.mkfifo(fifo)
    start = time.time()
    result = agent_runtime._read_live_agent_update(fifo)
    elapsed = time.time() - start
    assert result == "unknown"
    assert elapsed < 1.0, f"marker read blocked on FIFO for {elapsed:.2f}s"


def test_read_live_agent_update_rejects_oversized_marker(tmp_path: Path):
    """An oversized regular marker must classify ``unknown``, never OOM-read.

    Regression guard against unbounded ``read_text()``: even though the file
    starts with a valid PID/timestamp, its size exceeds the cap so it is
    rejected rather than read whole.
    """
    from api import agent_runtime

    big = tmp_path / "big-marker"
    big.write_bytes(
        f"{os.getpid()}\n{time.time()}\n".encode("utf-8")
        + b"x" * (agent_runtime._AGENT_UPDATE_MARKER_MAX_BYTES + 1024)
    )
    assert agent_runtime._read_live_agent_update(big) == "unknown"


def test_read_live_agent_update_does_not_follow_symlink_marker(tmp_path: Path):
    """A symlinked marker must classify ``unknown`` (O_NOFOLLOW), not be read
    through to its target."""
    from api import agent_runtime

    if not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("O_NOFOLLOW unavailable on this platform")

    target = tmp_path / "real-marker"
    target.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
    link = tmp_path / "link-marker"
    link.symlink_to(target)
    assert agent_runtime._read_live_agent_update(link) == "unknown"


def test_read_live_agent_update_still_classifies_valid_markers(tmp_path: Path):
    """The hardening must not regress the happy path: a small regular marker
    with a live PID still reads as ``active`` and a stale one as ``stale``."""
    from api import agent_runtime

    active = tmp_path / "active-marker"
    active.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
    assert agent_runtime._read_live_agent_update(active) == "active"

    stale = tmp_path / "stale-marker"
    stale.write_text(
        f"{os.getpid()}\n{time.time() - agent_runtime._AGENT_UPDATE_MAX_AGE_SECONDS - 60}\n",
        encoding="utf-8",
    )
    assert agent_runtime._read_live_agent_update(stale) == "stale"

    assert agent_runtime._read_live_agent_update(tmp_path / "absent") == "absent"


def test_read_live_agent_update_windows_fallback_never_opens_marker(
    tmp_path: Path, monkeypatch
):
    """When the atomic open flags are unavailable (e.g. native Windows), the
    read must not call os.open (which would raise on a missing O_NONBLOCK) and
    must fail closed: missing -> absent, anything present -> unknown.

    Regression guard for the portability CORE: os.O_NONBLOCK is Unix-only, so
    the fast path must be gated on both flags being present.
    """
    from api import agent_runtime

    monkeypatch.setattr(agent_runtime, "_MARKER_SAFE_OPEN_AVAILABLE", False)

    def _boom(*_a, **_k):  # os.open must never be reached on the fallback path
        raise AssertionError("os.open called despite unavailable atomic flags")

    monkeypatch.setattr(agent_runtime.os, "open", _boom)

    # A present, otherwise-valid marker is unverifiable without the safe open →
    # classified unknown (never active/absent), and does not raise.
    present = tmp_path / "present-marker"
    present.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
    assert agent_runtime._read_live_agent_update(present) == "unknown"

    # A genuinely missing marker is still absent.
    assert agent_runtime._read_live_agent_update(tmp_path / "missing") == "absent"


def test_initial_non_git_source_preserves_supported_runtime(monkeypatch):
    """Non-Git installs cannot be compared, so they preserve existing behavior."""
    from api import agent_runtime

    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", None)
    monkeypatch.setattr(
        agent_runtime,
        "_read_agent_revision",
        lambda _path, **_kwargs: None,
    )

    agent_runtime.ensure_agent_runtime_current()


def test_untracked_loaded_module_inside_outer_git_repo_is_non_git(
    monkeypatch, tmp_path: Path
):
    """An enclosing unrelated Git repo must not identify an installed module."""
    from api import agent_runtime

    outer_repo = tmp_path / "outer-repo"
    module_dir = outer_repo / "installed" / "hermes-agent"
    module_dir.mkdir(parents=True)
    module_file = module_dir / "run_agent.py"
    module_file.write_text("class AIAgent: pass\n", encoding="utf-8")
    (outer_repo / "tracked.txt").write_text("unrelated\n", encoding="utf-8")
    _git(outer_repo, "init", "-q")
    _git(outer_repo, "add", "tracked.txt")
    _git(outer_repo, "commit", "-qm", "unrelated")

    loaded_module = types.ModuleType("run_agent")
    loaded_module.__file__ = str(module_file)
    monkeypatch.setitem(sys.modules, "run_agent", loaded_module)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", None)

    agent_runtime._capture_loaded_agent_revision()

    assert agent_runtime._AGENT_SOURCE_DIR == module_dir.resolve()
    assert agent_runtime._AGENT_MODULE_PATH == module_file.resolve()
    assert agent_runtime._AGENT_REVISION is None

    (outer_repo / "tracked.txt").write_text("unrelated change\n", encoding="utf-8")
    _git(outer_repo, "add", "tracked.txt")
    _git(outer_repo, "commit", "-qm", "unrelated change")

    agent_runtime.ensure_agent_runtime_current()


def test_untracked_module_pathspec_metacharacters_are_literal(monkeypatch, tmp_path: Path):
    """Git pathspec syntax must not turn an untracked module into a tracked match."""
    from api import agent_runtime

    outer_repo = tmp_path / "outer-repo"
    tracked_dir = outer_repo / "installed" / "agent"
    untracked_dir = outer_repo / "installed" / "[a]gent"
    tracked_dir.mkdir(parents=True)
    untracked_dir.mkdir(parents=True)
    (tracked_dir / "run_agent.py").write_text("class Other: pass\n", encoding="utf-8")
    module_file = untracked_dir / "run_agent.py"
    module_file.write_text("class AIAgent: pass\n", encoding="utf-8")
    _git(outer_repo, "init", "-q")
    _git(outer_repo, "add", "installed/agent/run_agent.py")
    _git(outer_repo, "commit", "-qm", "tracked lookalike")

    loaded_module = types.ModuleType("run_agent")
    loaded_module.__file__ = str(module_file)
    monkeypatch.setitem(sys.modules, "run_agent", loaded_module)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", None)

    agent_runtime._capture_loaded_agent_revision()

    assert agent_runtime._AGENT_SOURCE_DIR == untracked_dir.resolve()
    assert agent_runtime._AGENT_MODULE_PATH == module_file.resolve()
    assert agent_runtime._AGENT_REVISION is None


def test_revision_identity_comes_from_loaded_agent_module(monkeypatch, tmp_path: Path):
    """The configured discovery path must not override the loaded module path."""
    from api import agent_runtime

    configured_dir = tmp_path / "configured-agent"
    loaded_dir = tmp_path / "loaded-agent"
    for agent_dir, revision in ((configured_dir, "configured"), (loaded_dir, "loaded")):
        agent_dir.mkdir()
        (agent_dir / "run_agent.py").write_text(
            f"class AIAgent:\n    revision = '{revision}'\n",
            encoding="utf-8",
        )
        _git(agent_dir, "init", "-q")
        _git(agent_dir, "add", "run_agent.py")
        _git(agent_dir, "commit", "-qm", revision)

    loaded_module = types.ModuleType("run_agent")
    loaded_module.__file__ = str(loaded_dir / "run_agent.py")
    monkeypatch.setitem(sys.modules, "run_agent", loaded_module)
    monkeypatch.setattr(agent_runtime, "_AGENT_DIR", configured_dir)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", None)

    agent_runtime._capture_loaded_agent_revision()

    assert agent_runtime._AGENT_SOURCE_DIR == loaded_dir.resolve()
    assert agent_runtime._AGENT_MODULE_PATH == (loaded_dir / "run_agent.py").resolve()
    assert agent_runtime._AGENT_REVISION == _git(loaded_dir, "rev-parse", "HEAD")


def test_known_revision_becoming_unreadable_fails_closed(monkeypatch):
    """Losing a previously-known revision is indistinguishable from source drift."""
    from api import agent_runtime

    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", "known-revision")
    monkeypatch.setattr(
        agent_runtime,
        "_read_agent_revision",
        lambda _path, **_kwargs: None,
    )

    with pytest.raises(agent_runtime.AgentRuntimeChangedError):
        agent_runtime.ensure_agent_runtime_current()


def test_live_agent_update_marker_reports_active(monkeypatch, tmp_path):
    """A fresh marker owned by a live PID means the Agent update is active."""
    from api import agent_runtime

    hermes_home = tmp_path / "hermes-home"
    agent_root = tmp_path / "hermes-agent"
    hermes_home.mkdir()
    agent_root.mkdir()
    (hermes_home / ".hermes-update-in-progress").write_text(
        f"{os.getpid()}\n{time.time()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_runtime, "_HERMES_HOME", hermes_home)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", agent_root)
    monkeypatch.setattr(agent_runtime, "_AGENT_PYTHON", None)

    assert agent_runtime._agent_update_transaction_state() == "active"


def test_dead_or_over_age_agent_update_marker_is_stale(monkeypatch, tmp_path):
    """A dead or over-age owner does not prove update completion."""
    from api import agent_runtime

    hermes_home = tmp_path / "hermes-home"
    agent_root = tmp_path / "hermes-agent"
    hermes_home.mkdir()
    agent_root.mkdir()
    marker = hermes_home / ".hermes-update-in-progress"
    monkeypatch.setattr(agent_runtime, "_HERMES_HOME", hermes_home)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", agent_root)
    monkeypatch.setattr(agent_runtime, "_AGENT_PYTHON", None)

    marker.write_text(f"99999999\n{time.time()}\n", encoding="utf-8")
    monkeypatch.setattr(agent_runtime, "_pid_is_alive", lambda _pid: False)
    assert agent_runtime._agent_update_transaction_state() == "stale"

    marker.write_text(
        f"{os.getpid()}\n{time.time() - agent_runtime._AGENT_UPDATE_MAX_AGE_SECONDS - 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_runtime, "_pid_is_alive", lambda _pid: True)
    assert agent_runtime._agent_update_transaction_state() == "stale"


@pytest.mark.parametrize(
    "marker_name",
    [".update-incomplete", ".lazy-refresh-incomplete"],
)
def test_agent_recovery_markers_block_automatic_restart(
    monkeypatch, tmp_path, marker_name
):
    """Agent recovery markers report an incomplete environment."""
    from api import agent_runtime

    hermes_home = tmp_path / "hermes-home"
    agent_root = tmp_path / "hermes-agent"
    hermes_home.mkdir()
    agent_root.mkdir()
    (agent_root / marker_name).write_text("incomplete\n", encoding="utf-8")
    monkeypatch.setattr(agent_runtime, "_HERMES_HOME", hermes_home)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", agent_root)
    monkeypatch.setattr(agent_runtime, "_AGENT_PYTHON", None)

    assert agent_runtime._agent_update_transaction_state() == "incomplete"


def test_unreadable_agent_update_state_fails_closed(monkeypatch):
    """An unreadable marker is unknown, never proof that restart is safe."""
    from api import agent_runtime

    class UnreadableMarker:
        def read_text(self, **_kwargs):
            raise PermissionError("denied")

    assert agent_runtime._read_live_agent_update(UnreadableMarker()) == "unknown"


def test_import_recapture_cannot_downgrade_known_revision(monkeypatch, tmp_path: Path):
    """A second unreadable revision read must not erase a known identity."""
    from api import agent_runtime

    source_dir = tmp_path / "loaded-agent"
    source_dir.mkdir()
    loaded_module = types.ModuleType("run_agent")
    loaded_module.__file__ = str(source_dir / "run_agent.py")
    loaded_module.__dict__["AIAgent"] = type("AIAgent", (), {})
    monkeypatch.setitem(sys.modules, "run_agent", loaded_module)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", source_dir.resolve())
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", source_dir / "run_agent.py")
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", "known-revision")
    revisions = iter(("known-revision", None))
    monkeypatch.setattr(
        agent_runtime,
        "_read_agent_revision",
        lambda _path, **_kwargs: next(revisions),
    )

    with pytest.raises(agent_runtime.AgentRuntimeChangedError):
        agent_runtime.require_ai_agent_class()

    assert agent_runtime._AGENT_REVISION == "known-revision"


def test_in_memory_agent_swap_does_not_mimic_source_revision_change(
    monkeypatch, tmp_path: Path
):
    """Only the bound checkout revision, not ``sys.modules`` swaps, defines staleness."""
    from api import agent_runtime

    agent_dir = tmp_path / "loaded-agent"
    agent_dir.mkdir()
    module_file = agent_dir / "run_agent.py"
    module_file.write_text("class AIAgent: pass\n", encoding="utf-8")
    _git(agent_dir, "init", "-q")
    _git(agent_dir, "add", "run_agent.py")
    _git(agent_dir, "commit", "-qm", "loaded agent")

    loaded_module = types.ModuleType("run_agent")
    loaded_module.__file__ = str(module_file)
    loaded_module.__dict__["AIAgent"] = type("LoadedAgent", (), {})
    monkeypatch.setitem(sys.modules, "run_agent", loaded_module)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", None)
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", None)
    agent_runtime._capture_loaded_agent_revision()

    replacement_class = type("ReplacementAgent", (), {})
    replacement_module = types.ModuleType("run_agent")
    replacement_module.__dict__["AIAgent"] = replacement_class
    monkeypatch.setitem(sys.modules, "run_agent", replacement_module)

    assert agent_runtime.require_ai_agent_class() is replacement_class
    assert agent_runtime._AGENT_SOURCE_DIR == agent_dir.resolve()
    assert agent_runtime._AGENT_MODULE_PATH == module_file.resolve()
    assert agent_runtime._AGENT_REVISION == _git(agent_dir, "rev-parse", "HEAD")


def test_runner_local_bypasses_webui_agent_barrier(monkeypatch):
    """Runner-local execution is owned by the runner, not this process."""
    from api import routes

    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            AssertionError("runner-local must not inspect the WebUI Agent checkout")
        ),
    )
    monkeypatch.setattr(
        "api.runtime_adapter.runtime_adapter_runner_enabled",
        lambda: True,
    )

    assert routes._agent_runtime_barrier_response(runner_local_owned=True) is None


def test_runner_flag_does_not_bypass_webui_owned_hidden_turns(monkeypatch):
    """Runner/gateway modes do not transfer ownership of legacy hidden turns."""
    from api import agent_runtime, routes

    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: True)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(
        "api.runtime_adapter.runtime_adapter_runner_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            agent_runtime.AgentRuntimeChangedError("restart required")
        ),
    )

    response = routes._agent_runtime_barrier_response(runner_local_owned=False)

    assert response == {
        "error": "restart required",
        "type": "agent_runtime_stale",
        "retryable": True,
        "restart_scheduled": False,
    }


def test_chat_start_rejects_stale_runtime_before_session_materialization(monkeypatch):
    """A stale local runtime must not claim, create, or mutate session state."""
    from api import agent_runtime, routes

    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})

    def stale():
        raise agent_runtime.AgentRuntimeChangedError("restart required")

    monkeypatch.setattr(routes, "ensure_agent_runtime_current", stale)

    def must_not_materialize(*_args, **_kwargs):
        raise AssertionError("session materialized before stale-runtime barrier")

    monkeypatch.setattr(routes, "_get_or_materialize_session", must_not_materialize)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )

    response = routes._handle_chat_start(object(), {"session_id": "session-1"})

    assert response == {
        "status": 409,
        "payload": {
            "error": "restart required",
            "type": "agent_runtime_stale",
            "retryable": True,
            "restart_scheduled": False,
        },
    }


def test_runner_owned_start_run_does_not_enter_local_stream_barrier(monkeypatch):
    """The runner adapter must not invoke the WebUI in-process delegate."""
    from api import routes

    calls = []

    class RunnerClient:
        def start_run(self, request):
            calls.append(request)
            return {
                "run_id": "runner-run-1",
                "stream_id": "runner-stream-1",
                "session_id": request.session_id,
                "status": "started",
            }

    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: True)
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: RunnerClient())
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("runner-owned start entered the WebUI local stream path")
        ),
    )
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            AssertionError("runner-owned start inspected the WebUI Agent revision")
        ),
    )

    response = routes._start_run(
        session,
        msg="hello",
        attachments=[],
        workspace="/tmp/workspace",
        model="test-model",
        model_provider="test-provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
    )

    assert response.get("stream_id") == "runner-stream-1", response
    assert len(calls) == 1


@pytest.mark.parametrize("gateway_owned", [False, True])
def test_stream_admission_uses_one_gateway_ownership_snapshot(monkeypatch, gateway_owned):
    """The barrier and worker must share one immutable backend decision."""
    from api import routes
    from api import turn_journal
    from api.config import unregister_stream_owner

    gateway_reads = []
    revision_checks = []
    worker_targets = []
    session = types.SimpleNamespace(
        session_id="gateway-snapshot",
        profile=None,
        title="Gateway snapshot",
        active_stream_id=None,
        pending_started_at=None,
    )

    def read_gateway(_cfg):
        gateway_reads.append(True)
        if len(gateway_reads) > 1:
            raise AssertionError("gateway ownership was re-read after admission")
        return gateway_owned

    def prepare(current, **kwargs):
        current.active_stream_id = kwargs["stream_id"]
        current.pending_started_at = 123.0

    class FakeThread:
        def __init__(self, *, target, args, kwargs, daemon):
            worker_targets.append(target)

        def start(self):
            return None

    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", read_gateway)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: revision_checks.append(True),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", prepare)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes.threading, "Thread", FakeThread)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *_args, **_kwargs: {})

    response = routes._start_chat_stream_for_session(
        session,
        msg="goal",
        attachments=[],
        workspace="/tmp/workspace",
        model="test-model",
        goal_related=True,
    )

    try:
        assert response["session_id"] == session.session_id
        assert len(gateway_reads) == 1
        assert revision_checks == ([] if gateway_owned else [True])
        expected_worker = (
            routes._run_gateway_chat_streaming
            if gateway_owned
            else routes._run_agent_streaming
        )
        assert worker_targets == [expected_worker]
    finally:
        stream_id = str(response.get("stream_id") or "")
        with routes.STREAMS_LOCK:
            routes.STREAMS.pop(stream_id, None)
        unregister_stream_owner(stream_id)
        routes.STREAM_GOAL_RELATED.pop(stream_id, None)


@pytest.mark.parametrize(
    ("handler_name", "body"),
    [
        ("_handle_git_commit_message", {"session_id": "git-message"}),
        (
            "_handle_git_commit_message_selected",
            {"session_id": "git-message", "paths": ["selected.py"]},
        ),
    ],
)
def test_git_commit_message_stale_runtime_returns_typed_409(
    monkeypatch, tmp_path, handler_name, body
):
    """Commit-message generation must preserve the stale-runtime contract."""
    from api import routes
    from api import workspace_git

    session = types.SimpleNamespace(workspace=str(tmp_path))
    monkeypatch.setattr(routes, "require", lambda _body, *keys: None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_git_paths_from_body", lambda _body: ["selected.py"])
    monkeypatch.setattr(
        workspace_git,
        "staged_commit_message_prompt",
        lambda _workspace: {"system_prompt": "system", "user_prompt": "user"},
    )
    monkeypatch.setattr(
        workspace_git,
        "selected_commit_message_prompt",
        lambda _workspace, _paths: {
            "system_prompt": "system",
            "user_prompt": "user",
        },
    )
    monkeypatch.setattr(
        routes,
        "_llm_git_commit_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            routes.AgentRuntimeChangedError("restart required")
        ),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: {
            "status": status,
            "payload": payload,
        },
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {
            "status": status,
            "payload": {"error": message},
        },
    )

    response = getattr(routes, handler_name)(object(), body)

    assert response == {
        "status": 409,
        "payload": {
            "error": "restart required",
            "type": "agent_runtime_stale",
            "retryable": True,
            "restart_scheduled": False,
        },
    }


def test_gateway_owned_start_run_bypasses_local_runtime_barrier(monkeypatch):
    """Gateway chat must reach its worker without inspecting the local Agent."""
    from api import routes

    session = types.SimpleNamespace(session_id="session-1", profile=None)
    captured = {}
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "get_config",
        lambda: (_ for _ in ()).throw(AssertionError("_start_run reread shared config")),
    )
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            AssertionError("gateway-owned start inspected the WebUI Agent revision")
        ),
    )

    def start_gateway(_session, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "gateway-stream-1", "session_id": "session-1"}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", start_gateway)

    response = routes._start_run(
        session,
        msg="hello",
        attachments=[],
        workspace="/tmp/workspace",
        model="test-model",
        model_provider="test-provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        gateway_chat_enabled=True,
    )

    assert response["stream_id"] == "gateway-stream-1"
    assert captured["external_runtime_owned"] is True


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("_handle_btw", {"session_id": "session-1", "question": "question"}),
        ("_handle_background", {"session_id": "session-1", "prompt": "prompt"}),
        ("_handle_chat_sync", {"session_id": "session-1", "message": "message"}),
    ],
)
def test_hidden_turn_routes_reject_stale_runtime_before_session_creation(
    monkeypatch, route, body
):
    """Hidden-turn endpoints must reject before creation even in gateway mode."""
    from api import routes

    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: True)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(
        "api.runtime_adapter.runtime_adapter_runner_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            routes.AgentRuntimeChangedError("restart required")
        ),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session loaded before stale-runtime barrier")
        ),
    )

    response = getattr(routes, route)(object(), body)

    assert response["status"] == 409
    assert response["payload"]["type"] == "agent_runtime_stale"


def test_server_side_turn_rejects_stale_runtime_before_session_acceptance(monkeypatch):
    """The direct process-wakeup entrypoint must fail before loading/mutating state."""
    from api import routes

    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **_kwargs: {
            "error": "restart required",
            "type": "agent_runtime_stale",
            "retryable": True,
            "restart_scheduled": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session loaded before stale-runtime barrier")
        ),
    )

    response = routes.start_session_turn("session-1", "wake up")

    assert response == {
        "error": "restart required",
        "type": "agent_runtime_stale",
        "retryable": True,
        "restart_scheduled": False,
        "_status": 409,
    }
