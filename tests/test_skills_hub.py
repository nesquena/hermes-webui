from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from urllib.parse import urlparse

import pytest


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def _call_get(monkeypatch, path: str):
    from api import routes

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(path))
    return handler


def _call_post(monkeypatch, path: str, body: dict | None = None):
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body or {})
    handler = _FakeHandler()
    routes.handle_post(handler, urlparse(path))
    return handler, handler.get_json()


class _FakeStdin:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def close(self):
        self.closed = True


def _make_fake_popen(lines: list[str], returncode: int = 0, on_spawn=None):
    """Build a fake ``subprocess.Popen`` replacement with a writable stdin
    (needed for the scan/uninstall stdin pre-answer) and a readline-able
    stdout (via io.StringIO, which naturally yields '' at EOF)."""

    class _FakeProc:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.kwargs = kwargs
            self.stdout = io.StringIO("".join(lines))
            self.stdin = _FakeStdin() if kwargs.get("stdin") == subprocess.PIPE else None
            self.returncode = returncode
            if on_spawn:
                on_spawn(cmd, kwargs)

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    return _FakeProc


@pytest.fixture(autouse=True)
def _reset_skills_hub_state():
    from api import skills_hub_actions as sha

    def _reset():
        with sha._STATE_LOCK:
            sha._STATE.update(
                {
                    "action": None,
                    "target": None,
                    "status": "idle",
                    "started_at": None,
                    "finished_at": None,
                    "returncode": None,
                    "log": "",
                    "error": None,
                    "scan_result": None,
                }
            )
        if sha._ACTION_LOCK.locked():
            try:
                sha._ACTION_LOCK.release()
            except RuntimeError:
                pass

    _reset()
    yield
    _reset()


def _wait_until_not_running(timeout=5.0):
    from api import skills_hub_actions as sha

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = sha.get_status()
        if status["status"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("skills hub action did not finish in time")


_SCAN_TRANSCRIPT = (
    "Fetching: skills-sh/anthropics/skills/pdf\n"
    "Quarantined to .hub/quarantine/pdf\n"
    "Running security scan...\n"
    "Scan: pdf (skills-sh/anthropics/skills/pdf/trusted)  Verdict: SAFE\n"
    '  MEDIUM   supply_chain   SKILL.md:235                   "# Requires: pip install pytesseract pdf2image"\n'
    "\n"
    "Decision: ALLOWED — Allowed (trusted source, safe verdict)\n"
    "Installation cancelled.\n"
)


# ── parse_scan_report ────────────────────────────────────────────────────────


def test_parse_scan_report_extracts_verdict_and_findings():
    from api.skills_hub_actions import parse_scan_report

    result = parse_scan_report(_SCAN_TRANSCRIPT)
    assert result["name"] == "pdf"
    assert result["source"] == "skills-sh/anthropics/skills/pdf"
    assert result["trust_level"] == "trusted"
    assert result["verdict"] == "safe"
    assert result["decision"] == "ALLOWED"
    assert len(result["findings"]) == 1
    assert result["findings"][0]["severity"] == "MEDIUM"
    assert result["findings"][0]["category"] == "supply_chain"


def test_parse_scan_report_returns_none_without_header():
    from api.skills_hub_actions import parse_scan_report

    assert parse_scan_report("Error: Could not fetch 'bogus' from any source.\n") is None


def test_hub_status_redacts_scan_transcripts_and_common_credentials(monkeypatch):
    """Status helper and route never serialize captured scan source or credentials."""
    from api import skills_hub_actions as sha

    sensitive_value = "".join(("synthetic", "-", "private-value"))
    aws_key = "_".join(("AWS", "SECRET", "ACCESS", "KEY"))
    finding_source = f"{aws_key}={sensitive_value}"
    finding_line = f'  HIGH credential SKILL.md:7 "{finding_source}"\n'
    wrapped_source = "".join(("wrapped", "-", "private-fragment"))
    wrapped_finding = (
        '  HIGH credential SKILL.md:8 "prefix-\n'
        + f"    {wrapped_source}\n"
        + '    suffix"\n\n'
    )
    basic_value = "".join(("basic", "-", "credential"))
    authorization_prefix = "".join(("Authorization", ": ", "Basic", " "))
    session_key = "_".join(("session", "id"))
    cookie_key = "".join(("coo", "kie"))
    with sha._STATE_LOCK:
        sha._STATE.update(
            {
                "status": "failed",
                "log": (
                    "Installed pdf\n"
                    + finding_line
                    + wrapped_finding
                    + "Post-scan progress remains visible\n"
                    + f"{authorization_prefix}{basic_value}\n"
                    + f"{session_key}={sensitive_value}\n"
                    + f"{cookie_key}={sensitive_value}\n"
                ),
                "error": f"secret: {sensitive_value}",
                "scan_result": {
                    "verdict": "dangerous",
                    "findings": [
                        {
                            "severity": "HIGH",
                            "category": "credential",
                            "location": "SKILL.md:7",
                            "match": finding_source,
                        }
                    ],
                },
            }
        )

    direct = sha.get_status()
    handler = _call_get(monkeypatch, "/api/skills/hub/status")
    routed = handler.get_json()

    for status in (direct, routed):
        rendered = json.dumps(status)
        assert sensitive_value not in rendered
        assert basic_value not in rendered
        assert finding_source not in rendered
        assert wrapped_source not in rendered
        assert finding_line.strip() not in rendered
        assert "Installed pdf" in status["log"]
        assert "Post-scan progress remains visible" in status["log"]
        assert "[scan finding redacted]" in status["log"]
        assert "Authorization: Basic <redacted>" in status["log"]
        assert f"{session_key}=<redacted>" in status["log"]
        assert f"{cookie_key}=<redacted>" in status["log"]
        assert status["scan_result"]["findings"] == [
            {"severity": "HIGH", "category": "credential", "location": "SKILL.md:7"}
        ]


# ── list_installed_hub_skills ────────────────────────────────────────────────


def test_list_installed_hub_skills_reads_lock_file(tmp_path):
    from api.skills_hub_actions import list_installed_hub_skills

    skills_dir = tmp_path / "skills"
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir(parents=True)
    (hub_dir / "lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "installed": {
                    "pdf": {
                        "source": "skills.sh",
                        "identifier": "skills-sh/anthropics/skills/pdf",
                        "trust_level": "trusted",
                        "scan_verdict": "safe",
                        "install_path": "pdf",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = list_installed_hub_skills(skills_dir)
    assert result == [
        {
            "name": "pdf",
            "source": "skills.sh",
            "identifier": "skills-sh/anthropics/skills/pdf",
            "trust_level": "trusted",
            "scan_verdict": "safe",
            "install_path": "pdf",
            "installed_at": None,
            "updated_at": None,
        }
    ]


def test_list_installed_hub_skills_missing_file_returns_empty(tmp_path):
    from api.skills_hub_actions import list_installed_hub_skills

    assert list_installed_hub_skills(tmp_path / "skills") == []


# ── Gate (fail-closed) ───────────────────────────────────────────────────────


def test_hub_status_reports_gate_closed_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", raising=False)
    handler = _call_get(monkeypatch, "/api/skills/hub/status")
    data = handler.get_json()
    assert handler.status == 200
    assert data["allowed"] is False
    assert data["status"] == "idle"


def test_hub_post_rejected_with_403_when_gate_closed(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", raising=False)
    for path, body in (
        ("/api/skills/hub/scan", {"identifier": "x"}),
        ("/api/skills/hub/install", {"identifier": "x"}),
        ("/api/skills/hub/update", {}),
        ("/api/skills/hub/uninstall", {"name": "x"}),
    ):
        handler, data = _call_post(monkeypatch, path, body)
        assert handler.status == 403, path
        assert data["allowed"] is False
        assert "HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE" in data["error"]


def test_hub_search_and_installed_stay_open_when_gate_closed(monkeypatch, tmp_path):
    from api import profiles, routes, skills_hub_actions as sha

    monkeypatch.delenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path / "skills")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sha.subprocess, "run", fake_run)
    handler = _call_get(monkeypatch, "/api/skills/hub/search?q=pdf")
    assert handler.status == 200
    handler2 = _call_get(monkeypatch, "/api/skills/hub/installed")
    assert handler2.status == 200


# ── Search ────────────────────────────────────────────────────────────────


def test_hub_search_parses_json_and_builds_expected_command(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        payload = [{"name": "pdf", "identifier": "skills-sh/anthropics/skills/pdf",
                    "source": "skills.sh", "trust_level": "trusted", "description": "PDF tools"}]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(sha.subprocess, "run", fake_run)
    handler = _call_get(monkeypatch, "/api/skills/hub/search?q=pdf&source=all&limit=5")
    data = handler.get_json()
    assert handler.status == 200
    assert data["results"][0]["name"] == "pdf"
    cmd, kwargs = calls[0]
    assert cmd[1:3] == ["skills", "search"]
    assert "--source" in cmd and cmd[cmd.index("--source") + 1] == "all"
    assert "--limit" in cmd and cmd[cmd.index("--limit") + 1] == "5"
    assert "--json" in cmd
    assert cmd[-2:] == ["--", "pdf"]
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)


def test_hub_search_treats_leading_dash_query_as_positional(monkeypatch, tmp_path):
    """The public search route must pass a flag-shaped query as a positional."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sha.subprocess, "run", fake_run)
    handler = _call_get(monkeypatch, "/api/skills/hub/search?q=-h")

    assert handler.status == 200
    cmd = calls[0]
    separator = cmd.index("--")
    assert cmd[-2:] == ["--", "-h"]
    assert "--source" in cmd[:separator]
    assert "--limit" in cmd[:separator]
    assert "--json" in cmd[:separator]


def test_hub_search_empty_query_returns_empty_without_spawning(monkeypatch, tmp_path):
    from api import skills_hub_actions as sha

    spawned = []
    monkeypatch.setattr(sha.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))
    handler = _call_get(monkeypatch, "/api/skills/hub/search?q=")
    data = handler.get_json()
    assert handler.status == 200
    assert data["results"] == []
    assert spawned == []


def test_hub_search_nonzero_exit_returns_502(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(sha.subprocess, "run", fake_run)
    handler = _call_get(monkeypatch, "/api/skills/hub/search?q=pdf")
    assert handler.status == 502


# ── Gate open: action lifecycle ──────────────────────────────────────────────


def test_hub_scan_action_preanswers_stdin_n_and_parses_verdict(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(
        [_SCAN_TRANSCRIPT], returncode=0, on_spawn=lambda cmd, kw: spawned.append((cmd, kw))
    )
    monkeypatch.setattr(sha.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "skills-sh/anthropics/skills/pdf"})
    assert handler.status == 200
    assert data["allowed"] is True
    assert data["action"] == "scan"

    final = _wait_until_not_running()
    assert final["status"] == "completed"
    assert final["scan_result"]["verdict"] == "safe"
    assert final["scan_result"]["decision"] == "ALLOWED"

    cmd, kwargs = spawned[0]
    assert cmd[-4:] == ["skills", "install", "--", "skills-sh/anthropics/skills/pdf"]
    assert "--yes" not in cmd
    assert "--force" not in cmd
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["env"]["COLUMNS"] == "400"


def test_hub_install_action_uses_yes_flag_and_no_stdin_interaction(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(
        ["Installed: pdf\n"], returncode=0, on_spawn=lambda cmd, kw: spawned.append((cmd, kw))
    )
    monkeypatch.setattr(sha.subprocess, "Popen", fake_proc)

    handler, data = _call_post(
        monkeypatch, "/api/skills/hub/install",
        {"identifier": "skills-sh/anthropics/skills/pdf", "category": "docs"},
    )
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "completed"

    cmd, kwargs = spawned[0]
    assert "--yes" in cmd
    assert "--category" in cmd and "docs" in cmd
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_hub_install_force_flag_forwarded(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(["x\n"], returncode=0, on_spawn=lambda cmd, kw: spawned.append(cmd))
    monkeypatch.setattr(sha.subprocess, "Popen", fake_proc)

    _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "id", "force": True})
    _wait_until_not_running()
    assert "--force" in spawned[0]


@pytest.mark.parametrize(
    ("provided_force", "expected_force"),
    [(False, False), ("false", False), (0, False), (1, False), ("0", False), (None, False), (True, True)],
)
def test_hub_install_route_forwards_only_json_boolean_true_as_force(monkeypatch, provided_force, expected_force):
    """Exercise the real POST route, not its source text, at the force seam."""
    from api import skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    forwarded = []

    def fake_start_action(action, target, **kwargs):
        forwarded.append((action, target, kwargs["force"]))
        return True, {"status": "running"}

    monkeypatch.setattr(sha, "start_action", fake_start_action)
    handler, _data = _call_post(
        monkeypatch,
        "/api/skills/hub/install",
        {"identifier": "example", "force": provided_force},
    )

    assert handler.status == 200
    assert forwarded == [("install", "example", expected_force)]


def test_hub_uninstall_action_preanswers_stdin_y(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(["Removed pdf\n"], returncode=0, on_spawn=lambda cmd, kw: spawned.append((cmd, kw)))
    monkeypatch.setattr(sha.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/skills/hub/uninstall", {"name": "pdf"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "completed"

    cmd, kwargs = spawned[0]
    assert cmd[-4:] == ["skills", "uninstall", "--", "pdf"]
    assert "--yes" not in cmd  # the CLI has no such flag for uninstall
    assert kwargs["stdin"] == subprocess.PIPE


def test_hub_update_action_no_stdin_interaction(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(["No updates available.\n"], returncode=0, on_spawn=lambda cmd, kw: spawned.append((cmd, kw)))
    monkeypatch.setattr(sha.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/skills/hub/update", {})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "completed"

    cmd, kwargs = spawned[0]
    assert cmd[-2:] == ["skills", "update"]
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_hub_action_failure_is_reported(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    fake_proc = _make_fake_popen(["Error: not found\n"], returncode=1)
    monkeypatch.setattr(sha.subprocess, "Popen", fake_proc)

    _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "bogus"})
    final = _wait_until_not_running()
    assert final["status"] == "failed"
    assert final["returncode"] == 1


def test_hub_scan_missing_identifier_returns_400(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    handler, data = _call_post(monkeypatch, "/api/skills/hub/scan", {})
    assert handler.status == 400


# ── Regression: audit findings (lock leak, zombie reap, argv robustness) ────


def test_hub_action_lock_is_released_when_profile_home_resolution_raises(monkeypatch, tmp_path):
    """Audit finding (HOCH): get_active_hermes_home() ran outside the
    try/except that releases _ACTION_LOCK on failure -- a raise there left
    the lock held forever, 409-ing every future hub action until a process
    restart."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: (_ for _ in ()).throw(RuntimeError("profile resolution failed"))
    )

    with pytest.raises(RuntimeError):
        sha.start_action("scan", "some-identifier")

    assert not sha._ACTION_LOCK.locked(), "lock leaked after get_active_hermes_home() raised"

    # Prove the leak doesn't linger: a normal call right after must still work.
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sha.subprocess, "Popen", _make_fake_popen(["ok\n"], returncode=0))
    started, _status = sha.start_action("scan", "some-identifier")
    assert started is True
    _wait_until_not_running()


def test_hub_action_timeout_reaps_zombie_via_background_thread(monkeypatch, tmp_path):
    """Audit finding (MEDIUM): after proc.kill(), if the 5s proc.wait(timeout=5)
    also times out, the code assumed returncode=-9 without ever reaping the
    process, leaving a zombie. A background thread must now call a blocking
    proc.wait() to actually reclaim it."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    wait_calls = []
    reaped = threading.Event()

    class _StuckProc:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.stdout = io.StringIO("")
            self.stdin = _FakeStdin() if kwargs.get("stdin") == subprocess.PIPE else None
            self.returncode = None

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            if timeout is None:
                # The background reaper's blocking wait -- simulate the
                # stuck process finally dying now.
                self.returncode = -9
                reaped.set()
                return self.returncode
            # Every timed wait (the initial action timeout, then the 5s
            # post-kill wait) times out: the process is stuck.
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)

        def kill(self):
            pass

    monkeypatch.setattr(sha.subprocess, "Popen", _StuckProc)

    handler, data = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "x"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "timeout"
    assert final["returncode"] == -9

    assert reaped.wait(timeout=2), "background reap thread never issued a blocking proc.wait()"
    assert None in wait_calls, "expected at least one no-timeout (blocking) wait() call to reap the process"


def test_hub_command_uses_double_dash_for_leading_dash_targets():
    """Audit finding (NIEDRIG-MEDIUM): identifier/name/category reach argparse
    as a bare positional. A value starting with "-" (crafted or accidental)
    was silently swallowed as an unrecognized option instead of reaching the
    CLI as the intended identifier/name -- verified live against the real
    CLI: without "--", `hermes skills install "-x/-y"` never reaches the
    identifier positional at all. "--" must be the last token before the
    positional, with every real flag placed before it."""
    from api.skills_hub_actions import _command_for_action

    dashy = "-x/-y"

    scan_cmd = _command_for_action("scan", dashy)
    assert scan_cmd[-2:] == ["--", dashy]

    install_cmd = _command_for_action("install", dashy, category="-docs", name_override="-n", force=True)
    assert install_cmd[-2:] == ["--", dashy]
    # The real flags must all precede "--" so they're still parsed as
    # options, not swallowed as extra positionals after it.
    dd_index = install_cmd.index("--")
    assert "--yes" in install_cmd[:dd_index]
    assert "--force" in install_cmd[:dd_index]
    assert "--category" in install_cmd[:dd_index] and "-docs" in install_cmd[:dd_index]
    assert "--name" in install_cmd[:dd_index] and "-n" in install_cmd[:dd_index]

    uninstall_cmd = _command_for_action("uninstall", dashy)
    assert uninstall_cmd[-2:] == ["--", dashy]

    update_cmd = _command_for_action("update", dashy)
    assert update_cmd[-2:] == ["--", dashy]
    # No target at all: no "--" needed (nothing positional to protect).
    update_cmd_no_target = _command_for_action("update", "")
    assert "--" not in update_cmd_no_target


# ── Single-flight (409) ──────────────────────────────────────────────────────


def test_hub_action_contention_returns_409_without_spawning(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(
        sha.subprocess, "Popen",
        _make_fake_popen(["x\n"], on_spawn=lambda cmd, kw: spawned.append(cmd)),
    )

    acquired = sha._ACTION_LOCK.acquire(blocking=False)
    assert acquired, "lock should be free at test start"
    try:
        handler, data = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "x"})
    finally:
        sha._ACTION_LOCK.release()

    assert handler.status == 409
    assert data["allowed"] is True
    assert spawned == []


# ── Frontend / i18n presence ─────────────────────────────────────────────────


def test_skills_hub_frontend_wiring_present():
    from pathlib import Path

    panels = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    assert "/api/skills/hub/search" in panels
    assert "/api/skills/hub/installed" in panels
    assert "/api/skills/hub/status" in panels
    assert "function switchSkillsTab" in panels
    assert "function scanSkillsHubResult" in panels
    assert "function installSkillsHubResult" in panels
    assert "function updateSkillsHubSkill" in panels
    assert "function uninstallSkillsHubSkill" in panels


def test_skills_hub_i18n_keys_exist():
    from pathlib import Path

    i18n = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in (
        "skills_tab_hub",
        "skills_hub_scan",
        "skills_hub_install",
        "skills_hub_update",
        "skills_hub_uninstall",
        "skills_hub_gate_disabled",
    ):
        assert f"{key}:" in i18n


def test_skills_hub_html_has_tab_and_views():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="skillsHubView"' in html
    assert 'id="skillsMineView"' in html
    assert "switchSkillsTab('hub')" in html
