from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest


class _FakeHandler:
    def __init__(self, client_address=("127.0.0.1", 54321)):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}
        # Mutation routes require an authenticated session OR a spoof-resistant
        # local/private peer. Default to loopback so the ordinary tests exercise
        # the action behavior; `test_hub_mutations_require_auth_or_locality`
        # covers the remote-denial composition explicitly.
        self.client_address = client_address

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


def _call_post(monkeypatch, path: str, body: dict | None = None, *, client_address=("127.0.0.1", 54321)):
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body or {})
    handler = _FakeHandler(client_address=client_address)
    routes.handle_post(handler, urlparse(path))
    return handler, handler.get_json()


def _write_hub_lock(skills_dir, installed: dict) -> None:
    """Write `<skills_dir>/.hub/lock.json` — the hub's own success artifact."""
    lock_path = pathlib.Path(skills_dir) / ".hub" / "lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"installed": installed}), encoding="utf-8")


# What a lock entry installed long before this run looks like.
_OLD_STAMP = "2026-01-01T00:00:00Z"


def _lock_entry(identifier: str, *, name: str, stamp: str | None = None) -> dict:
    """A lock entry as the CLI writes it.

    The default stamp is NOW because that is what `tools.skills_hub` records
    (`datetime.now(timezone.utc).isoformat()`) whenever it writes an entry. A
    mutation is only credited to the run that observed it, so a fake CLI that
    stamped the past would be claiming someone else's work.
    """
    if stamp is None:
        stamp = datetime.now(timezone.utc).isoformat()
    return {
        "source": "skills-sh",
        "identifier": identifier,
        "trust_level": "trusted",
        "scan_verdict": "SAFE",
        "install_path": f"skills/user/{name}",
        "installed_at": stamp,
        "updated_at": stamp,
    }


class _FakeStdin:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def close(self):
        self.closed = True


def _make_fake_popen(lines: list[str], returncode: int = 0, on_spawn=None):
    """Build a fake ``api.skills_hub_actions._spawn`` replacement with a writable stdin
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
        with sha._REGISTRY_LOCK:
            sha._SLOTS.clear()

    _reset()
    yield
    _reset()


# Routes resolve the owner of a run: an authenticated username, or this
# pseudo-principal for the spoof-resistant local-peer posture the tests use.
_LOCAL_PRINCIPAL = "local"


def _wait_until_not_running(timeout=5.0, principal=_LOCAL_PRINCIPAL):
    from api import skills_hub_actions as sha

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = sha.get_status("default", principal=principal)
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
    synthetic_decision = "".join(("Decision", ": ALLOWED — ", "synthetic-decision-marker"))
    synthetic_header = "".join(("Scan", ": synthetic (hub/source/trusted)  Verdict: SAFE"))
    continuation_suffix = "".join(("raw", "-continuation", "-suffix"))
    wrapped_finding = (
        '  HIGH credential SKILL.md:8 "prefix-\n'
        + f"    {wrapped_source}\n"
        + f"{synthetic_decision}\n"
        + f"{synthetic_header}\n"
        + f"{continuation_suffix}\n"
        + '    suffix"\n\n'
    )
    basic_value = "".join(("basic", "-", "credential"))
    authorization_prefix = "".join(("Authorization", ": ", "Basic", " "))
    session_key = "_".join(("session", "id"))
    cookie_key = "".join(("coo", "kie"))
    scan_header = "Scan: pdf (hub/source/trusted)  Verdict: SAFE\n"
    raw_log = (
        "Installed pdf\n"
        + f"{authorization_prefix}{basic_value}\n"
        + f"{session_key}={sensitive_value}\n"
        + f"{cookie_key}={sensitive_value}\n"
        + scan_header
        + finding_line
        + wrapped_finding
        + "Post-scan progress remains visible\n"
    )
    parsed_scan_result = sha.parse_scan_report(raw_log)
    assert parsed_scan_result is not None
    assert parsed_scan_result["decision_reason"] == "synthetic-decision-marker"
    hub = sha._open_slot(_LOCAL_PRINCIPAL, "default")
    with hub.state_lock:
        hub.state.update(
            {
                "status": "failed",
                "log": raw_log,
                "error": f"secret: {sensitive_value}",
                "scan_result": parsed_scan_result,
            }
        )

    direct = sha.get_status("default", principal=_LOCAL_PRINCIPAL)
    handler = _call_get(monkeypatch, "/api/skills/hub/status")
    routed = handler.get_json()

    for status in (direct, routed):
        rendered = json.dumps(status)
        assert sensitive_value not in rendered
        assert basic_value not in rendered
        assert finding_source not in rendered
        assert wrapped_source not in rendered
        assert synthetic_decision not in rendered
        assert synthetic_header not in rendered
        assert continuation_suffix not in rendered
        assert finding_line.strip() not in rendered
        assert "Installed pdf" in status["log"]
        assert "Post-scan progress remains visible" not in status["log"]
        assert "[scan finding redacted; remaining scan transcript suppressed]" in status["log"]
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

    monkeypatch.setattr(sha, "_run_capture", fake_run)
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

    monkeypatch.setattr(sha, "_run_capture", fake_run)
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

    monkeypatch.setattr(sha, "_run_capture", fake_run)
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
    monkeypatch.setattr(sha, "_run_capture", lambda cmd, **kw: spawned.append(cmd))
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

    monkeypatch.setattr(sha, "_run_capture", fake_run)
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
    monkeypatch.setattr(sha, "_spawn", fake_proc)

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

    def _on_spawn(cmd, kw):
        spawned.append((cmd, kw))
        # Completion is now established from the hub's own lock artifact, not
        # from the transcript: mirror what the real CLI writes on success.
        _write_hub_lock(
            tmp_path / "skills",
            {"pdf": _lock_entry("skills-sh/anthropics/skills/pdf", name="pdf")},
        )

    fake_proc = _make_fake_popen(
        ["Installed: pdf\n"], returncode=0, on_spawn=_on_spawn
    )
    monkeypatch.setattr(sha, "_spawn", fake_proc)

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


@pytest.mark.parametrize("provided_force", [True, False, "false", 0, 1, "0", None])
def test_hub_route_rejects_any_client_force(monkeypatch, provided_force):
    """Gate finding 1: security verdicts cannot be overridden from the
    browser. ANY force key -- even falsey -- is rejected with 400 so no
    cached client keeps believing the seam exists."""
    from api import skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    called = []
    monkeypatch.setattr(sha, "start_action", lambda *a, **k: called.append(1) or (True, {}))
    handler, _data = _call_post(
        monkeypatch,
        "/api/skills/hub/install",
        {"identifier": "example", "force": provided_force},
    )
    assert handler.status == 400
    assert called == []


def test_hub_install_command_never_contains_force(monkeypatch, tmp_path):
    from api import skills_hub_actions as sha

    cmd = sha._command_for_action("install", "id", category="docs")
    assert "--force" not in cmd


def test_hub_uninstall_action_preanswers_stdin_y(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    # Present before, gone after — that transition IS the success criterion.
    _write_hub_lock(
        tmp_path / "skills",
        {"pdf": _lock_entry("skills-sh/anthropics/skills/pdf", name="pdf")},
    )
    spawned = []

    def _on_spawn(cmd, kw):
        spawned.append((cmd, kw))
        _write_hub_lock(tmp_path / "skills", {})

    fake_proc = _make_fake_popen(
        ["Uninstalled 'pdf' from skills/user/pdf\n"], returncode=0, on_spawn=_on_spawn
    )
    monkeypatch.setattr(sha, "_spawn", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/skills/hub/uninstall", {"name": "pdf"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "completed"

    cmd, kwargs = spawned[0]
    assert cmd[-4:] == ["skills", "uninstall", "--", "pdf"]
    assert "--yes" not in cmd  # the CLI has no such flag for uninstall
    assert kwargs["stdin"] == subprocess.PIPE


_ALLOWED_SCAN_TRANSCRIPT = (
    "Scan: pdf (skills-sh/anthropics/skills/pdf/trusted) Verdict: SAFE\n"
    "Decision: ALLOWED \u2014 Allowed (trusted source, safe verdict)\n"
)
_BLOCKED_SCAN_TRANSCRIPT = (
    "Scan: evil (skills-sh/x/evil/community) Verdict: DANGEROUS\n"
    "Decision: BLOCKED \u2014 Dangerous verdict from community source\n"
)


def _make_phase_popen(transcripts, spawned, on_phase=None):
    """Fake ``_spawn`` returning the next canned transcript per spawn.

    ``on_phase(index, cmd)`` lets a test mutate the hub lock artifact for the
    phase that would really have changed it — completion is established from
    that artifact, not from the transcript.
    """
    calls = {"n": 0}

    def factory(cmd, **kwargs):
        idx = min(calls["n"], len(transcripts) - 1)
        phase_index = calls["n"]
        calls["n"] += 1
        spawned.append((cmd, kwargs))
        if on_phase is not None:
            on_phase(phase_index, cmd)
        return _make_fake_popen([transcripts[idx]], returncode=0)(cmd, **kwargs)

    return factory


def test_hub_update_route_is_disabled_until_agent_contract(monkeypatch):
    """Review P0: without artifact binding (agent prepare/commit), browser
    updates stay off — 501 with a precise message, regardless of payload."""
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    handler, data = _call_post(
        monkeypatch, "/api/skills/hub/update",
        {"name": "pdf", "identifier": "anthropics/skills/pdf"},
    )
    assert handler.status == 501
    assert data.get("update_unavailable") is True
    assert "artifact-bound" in (data.get("error") or "")


def test_hub_update_pipeline_requires_identifier(monkeypatch):
    from api import skills_hub_actions as sha

    with pytest.raises(ValueError, match="identifier"):
        sha.start_action("update", "pdf", profile="default")


def test_hub_update_is_scan_gated_and_two_phase(monkeypatch, tmp_path):
    """Gate finding 1: update runs a NON-FORCE scan first; the update CLI is
    invoked only after Decision ALLOWED."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    # Installed before; the update phase advances the lock entry's revision.
    _write_hub_lock(
        tmp_path / "skills",
        {"pdf": _lock_entry("anthropics/skills/pdf", name="pdf", stamp=_OLD_STAMP)},
    )
    spawned = []

    def _on_phase(index, _cmd):
        if index == 1:  # the update phase, after the scan allowed it
            _write_hub_lock(
                tmp_path / "skills",
                {
                    "pdf": _lock_entry(
                        "anthropics/skills/pdf",
                        name="pdf",
                    )
                },
            )

    monkeypatch.setattr(
        sha,
        "_spawn",
        _make_phase_popen(
            [_ALLOWED_SCAN_TRANSCRIPT, "Updated 1 skill(s).\n"], spawned, on_phase=_on_phase
        ),
    )
    started, _ = sha.start_action(
        "update", "pdf", identifier="anthropics/skills/pdf", profile="default",
        principal=_LOCAL_PRINCIPAL,
    )
    assert started is True
    final = _wait_until_not_running()
    assert final["status"] == "completed", final
    assert len(spawned) == 2
    scan_cmd, _sk = spawned[0]
    update_cmd, update_kw = spawned[1]
    assert "install" in scan_cmd and "--yes" not in scan_cmd and "--force" not in scan_cmd
    assert update_cmd[-4:-2] == ["skills", "update"] or "update" in update_cmd
    assert update_kw["stdin"] == subprocess.DEVNULL


def test_hub_update_blocked_scan_never_invokes_update_cli(monkeypatch, tmp_path):
    """A denial leaves the installed tree untouched: only ONE subprocess
    (the scan) ever runs, and the action fails."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(
        sha,
        "_spawn",
        _make_phase_popen([_BLOCKED_SCAN_TRANSCRIPT], spawned),
    )
    started, _ = sha.start_action(
        "update", "evil", identifier="x/evil", profile="default",
        principal=_LOCAL_PRINCIPAL,
    )
    assert started is True
    final = _wait_until_not_running()
    assert final["status"] == "failed"
    assert "refused" in (final.get("error") or "").lower()
    assert len(spawned) == 1, "the update CLI must never run after a denial"


def test_hub_action_failure_is_reported(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    fake_proc = _make_fake_popen(["Error: not found\n"], returncode=1)
    monkeypatch.setattr(sha, "_spawn", fake_proc)

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
        sha.start_action("scan", "some-identifier", principal=_LOCAL_PRINCIPAL)

    assert sha._open_slot(_LOCAL_PRINCIPAL, "default").running is False, "slot leaked after get_active_hermes_home() raised"

    # Prove the leak doesn't linger: a normal call right after must still work.
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sha, "_spawn", _make_fake_popen(["ok\n"], returncode=0))
    started, _status = sha.start_action("scan", "some-identifier", principal=_LOCAL_PRINCIPAL)
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

    monkeypatch.setattr(sha, "_spawn", _StuckProc)

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

    install_cmd = _command_for_action("install", dashy, category="-docs", name_override="-n")
    assert install_cmd[-2:] == ["--", dashy]
    # The real flags must all precede "--" so they're still parsed as
    # options, not swallowed as extra positionals after it.
    dd_index = install_cmd.index("--")
    assert "--yes" in install_cmd[:dd_index]
    assert "--force" not in install_cmd  # gone entirely (gate finding 1)
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
        sha, "_spawn",
        _make_fake_popen(["x\n"], on_spawn=lambda cmd, kw: spawned.append(cmd)),
    )

    hub = sha._open_slot(_LOCAL_PRINCIPAL, "default")
    with hub.state_lock:
        assert hub.running is False, "slot should be free at test start"
        hub.running = True
    try:
        handler, data = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "x"})
    finally:
        with hub.state_lock:
            hub.running = False

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


# ── Gate follow-ups: profile isolation, rc-0 business errors, auth boundary ──


def test_two_profiles_have_isolated_status_and_slots(monkeypatch, tmp_path):
    """Gate finding 3: profile B neither sees profile A's run state nor is
    blocked by A's slot."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    hub_a = sha._open_slot("", "profile-a")
    with hub_a.state_lock:
        hub_a.running = True
        hub_a.state.update({
            "action": "install", "target": "secret/skill", "status": "running",
            "log": "sensitive transcript", "scan_result": None,
        })

    b_status = sha.get_status("profile-b")
    assert b_status["status"] == "idle"
    assert b_status["log"] == ""
    assert b_status["target"] is None

    # B can start its own action while A's slot is held.
    monkeypatch.setattr(
        sha, "_spawn",
        _make_fake_popen([
            "Scan: x (skills-sh/x/trusted) Verdict: SAFE\n"
            "Decision: ALLOWED — ok\n"
        ], returncode=0),
    )
    started, _ = sha.start_action("scan", "x", profile="profile-b")
    assert started is True
    deadline = time.time() + 5
    while time.time() < deadline:
        if sha.get_status("profile-b")["status"] != "running":
            break
        time.sleep(0.02)
    assert sha.get_status("profile-b")["status"] in ("completed", "failed")
    # A's state is untouched throughout.
    assert sha.get_status("profile-a")["status"] == "running"


@pytest.mark.parametrize("action,transcript,pre_installed,expected_error_fragment", [
    # Already installed: the lock entry exists before AND is unchanged after.
    ("install", "Warning: 'pdf' is already installed at skills/user/pdf\n", True, "Already installed"),
    # Fetch failure: nothing ever reaches the lock file.
    ("install", "Failed to fetch bundle: network unreachable\n", False, "no installed skill"),
    # Not hub-installed: absent before, so there is nothing to remove.
    ("uninstall", "'ghost' is not a hub-installed skill (may be a builtin)\n", False, "nothing was removed"),
])
def test_rc0_business_failures_are_failures(
    monkeypatch, tmp_path, action, transcript, pre_installed, expected_error_fragment
):
    """The CLI exits 0 for business failures, so the return code is not
    authority — and neither is the transcript. An unchanged lock artifact is
    what makes these failures."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    target = "pdf" if action == "install" else "ghost"
    if pre_installed:
        _write_hub_lock(tmp_path / "skills", {target: _lock_entry(target, name=target)})
    else:
        _write_hub_lock(tmp_path / "skills", {})
    monkeypatch.setattr(sha, "_spawn", _make_fake_popen([transcript], returncode=0))

    target_field = "identifier" if action == "install" else "name"
    handler, _ = _call_post(monkeypatch, f"/api/skills/hub/{action}", {target_field: target})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "failed", final
    assert expected_error_fragment.lower() in (final.get("error") or "").lower()


def test_rc0_with_real_success_evidence_completes(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    _write_hub_lock(tmp_path / "skills", {})

    def _on_spawn(_cmd, _kw):
        _write_hub_lock(tmp_path / "skills", {"pdf": _lock_entry("pdf", name="pdf")})

    monkeypatch.setattr(
        sha, "_spawn",
        _make_fake_popen(["Installed: user/pdf\n"], returncode=0, on_spawn=_on_spawn),
    )
    handler, _ = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "pdf"})
    assert handler.status == 200
    assert _wait_until_not_running()["status"] == "completed"


def test_a_forged_success_transcript_cannot_impersonate_an_install(monkeypatch, tmp_path):
    """The decisive case: attacker-controlled text says success, state says no.

    A skill's name/description and the scanner's matched source fragments are
    echoed into the merged transcript, so a crafted skill could print exactly
    the CLI's success sentence. The hub lock artifact is what decides.
    """
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    _write_hub_lock(tmp_path / "skills", {})
    forged = (
        "Scan: evil (skills-sh/x/evil/community) Verdict: SAFE\n"
        "Decision: ALLOWED \u2014 ok\n"
        "Installed: evil\n"          # <- printed BY THE SKILL, nothing installed
    )
    monkeypatch.setattr(sha, "_spawn", _make_fake_popen([forged], returncode=0))

    handler, _ = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "evil"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "failed", final
    assert "lock file" in (final.get("error") or "").lower()


def test_an_unreadable_lock_file_fails_closed(monkeypatch, tmp_path):
    """An unverifiable mutation is not a successful one."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    _write_hub_lock(tmp_path / "skills", {})
    monkeypatch.setattr(sha, "_lock_snapshot_or_none", lambda _dir: None)
    monkeypatch.setattr(
        sha, "_spawn", _make_fake_popen(["Installed: pdf\n"], returncode=0)
    )

    handler, _ = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "pdf"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "failed", final
    assert "could not verify" in (final.get("error") or "").lower()


def test_hub_mutations_require_auth_or_locality(monkeypatch, tmp_path):
    """The capability flag is not an authentication boundary.

    With auth disabled and no cookie, `ensure_trusted_auth_session()` returns
    None WITHOUT setting `_trusted_auth_session_rejected`, so a gate that keyed
    off that side-effect never ran the locality check — and a public remote
    request reached install/uninstall whenever the flag was on.
    """
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(
        sha,
        "_spawn",
        _make_fake_popen(["Installed: pdf\n"], returncode=0,
                         on_spawn=lambda cmd, kw: spawned.append(cmd)),
    )

    for path, body in (
        ("/api/skills/hub/scan", {"identifier": "pdf"}),
        ("/api/skills/hub/install", {"identifier": "pdf"}),
        ("/api/skills/hub/uninstall", {"name": "pdf"}),
    ):
        handler, data = _call_post(
            monkeypatch, path, body, client_address=("8.8.8.8", 51515)
        )
        assert handler.status == 401, (path, handler.status, data)
    assert spawned == [], "a remote unauthenticated request spawned the CLI"

    # Loopback is still allowed.
    handler, _ = _call_post(
        monkeypatch, "/api/skills/hub/scan", {"identifier": "pdf"},
        client_address=("127.0.0.1", 51516),
    )
    assert handler.status == 200


def test_forwarded_header_cannot_promote_a_remote_peer_to_local(monkeypatch, tmp_path):
    """A direct client must not talk its way into "local" with a header."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(
        sha,
        "_spawn",
        _make_fake_popen(["Installed: pdf\n"], returncode=0,
                         on_spawn=lambda cmd, kw: spawned.append(cmd)),
    )

    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"identifier": "pdf"})
    handler = _FakeHandler(client_address=("8.8.8.8", 51515))
    handler.headers = {"X-Forwarded-For": "127.0.0.1"}
    routes.handle_post(handler, urlparse("/api/skills/hub/install"))

    assert handler.status == 401
    assert spawned == []


def test_remote_unauthenticated_mutation_is_denied(monkeypatch):
    """Gate finding 4: the write flag is a capability switch, not an auth
    boundary — a remote request without a trusted session gets 401 even
    with the flag enabled."""
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")

    def _reject(handler):
        handler._trusted_auth_session_rejected = True
        return None

    monkeypatch.setattr("api.auth.ensure_trusted_auth_session", _reject)
    monkeypatch.setattr(routes, "_onboarding_request_is_local", lambda handler: False)
    called = []
    monkeypatch.setattr(
        "api.skills_hub_actions.start_action",
        lambda *a, **k: called.append(1) or (True, {}),
    )
    handler, data = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "x"})
    assert handler.status == 401
    assert called == []


def test_local_unauthenticated_mutation_is_allowed(monkeypatch, tmp_path):
    from api import profiles, routes, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    def _reject(handler):
        handler._trusted_auth_session_rejected = True
        return None

    monkeypatch.setattr("api.auth.ensure_trusted_auth_session", _reject)
    monkeypatch.setattr(routes, "_onboarding_request_is_local", lambda handler: True)
    monkeypatch.setattr(sha, "_spawn", _make_fake_popen(["Installed: x\n"], returncode=0))
    handler, _ = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "x"})
    assert handler.status == 200


# ── Re-gate 4779033194 #1: the changed action tests must not mutate the
#    shared `subprocess` module, or they pass only by import order ──────────


def test_patching_the_spawn_seam_leaves_the_shared_subprocess_module_intact(monkeypatch):
    """The seam is module-local, so patching it cannot reach `subprocess.run`.

    Replacing `subprocess.Popen` swapped the object `subprocess.run()` opens as
    a context manager. Any unrelated `run()` executed while such a patch was
    active therefore raised `TypeError: '_FakeProc' object does not support the
    context manager protocol` — including the `subprocess.run(["git", ...])`
    that `api.agent_runtime._read_agent_revision()` issues when `api.routes` is
    imported cold. The action tests consequently passed only when some earlier
    test had already imported routes.
    """
    import sys

    from api import skills_hub_actions as sha

    real_popen = subprocess.Popen
    real_run = subprocess.run

    monkeypatch.setattr(sha, "_spawn", _make_fake_popen(["x\n"], returncode=0))
    monkeypatch.setattr(sha, "_run_capture", lambda cmd, **kw: None)

    assert subprocess.Popen is real_popen, "the shared stdlib Popen was replaced"
    assert subprocess.run is real_run, "the shared stdlib run was replaced"

    # The exact shape agent_runtime uses, executed while the seam is patched.
    done = subprocess.run(
        [sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0


def test_spawn_seam_is_what_the_runner_actually_calls(monkeypatch, tmp_path):
    """A seam nobody calls would be a decorative fix."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    seen = []
    monkeypatch.setattr(
        sha, "_spawn",
        _make_fake_popen(["x\n"], returncode=0, on_spawn=lambda cmd, kw: seen.append(cmd)),
    )
    handler, _ = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "x"})
    assert handler.status == 200
    _wait_until_not_running()
    assert seen and "skills" in seen[0]


# ── Re-gate 4779033194 #2: malformed lock state must not false-complete ────


@pytest.mark.parametrize("payload,label", [
    (b"{ this is not json", "malformed JSON"),
    (b'["installed"]', "wrong top-level shape"),
    (b'{"installed": ["pdf"]}', "non-mapping installed field"),
    (b'{"installed": {"pdf": "not-an-entry"}}', "non-mapping entry"),
    (b'{"version": 1}', "no installed field at all"),
])
def test_uninstall_fails_closed_on_unusable_lock_state(monkeypatch, tmp_path, payload, label):
    """The real parser path, not a mocked snapshot helper.

    `list_installed_hub_skills` maps every one of these to an empty list, so a
    postcondition built on it would see the target "absent after" and call the
    uninstall a success — precisely when the file that could prove otherwise
    became unreadable. The mutation-grade parser must answer UNKNOWN instead.
    """
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    skills_dir = tmp_path / "skills"
    _write_hub_lock(skills_dir, {"pdf": _lock_entry("skills-sh/pdf", name="pdf")})

    def _corrupt(_cmd, _kw):
        (skills_dir / ".hub" / "lock.json").write_bytes(payload)

    monkeypatch.setattr(
        sha, "_spawn",
        _make_fake_popen(["Uninstalled: pdf\n"], returncode=0, on_spawn=_corrupt),
    )
    handler, _ = _call_post(monkeypatch, "/api/skills/hub/uninstall", {"name": "pdf"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "failed", f"{label} was accepted as a completed uninstall"
    assert "verify" in (final.get("error") or "").lower()


def test_lock_snapshot_reports_absent_and_unknown_differently(tmp_path):
    """`{}` may only mean the file is genuinely absent."""
    from api.skills_hub_actions import _lock_snapshot_or_none

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    assert _lock_snapshot_or_none(skills_dir) == {}, "absent lock file is an empty install set"

    lock_path = skills_dir / ".hub" / "lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text('{"installed": {}}', encoding="utf-8")
    assert _lock_snapshot_or_none(skills_dir) == {}, "an empty installed map is empty, not unknown"

    lock_path.write_text("{ broken", encoding="utf-8")
    assert _lock_snapshot_or_none(skills_dir) is None

    # A directory where the file belongs: readable path, unusable content.
    lock_path.unlink()
    lock_path.mkdir()
    assert _lock_snapshot_or_none(skills_dir) is None


def test_install_is_not_credited_with_a_lock_entry_it_did_not_write(monkeypatch, tmp_path):
    """Binding to this invocation: a pre-existing entry cannot satisfy it.

    Without the binding, any entry appearing between the two snapshots counted
    — including one a concurrent CLI in the same profile home wrote.
    """
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    skills_dir = tmp_path / "skills"
    _write_hub_lock(skills_dir, {})

    def _foreign_write(_cmd, _kw):
        # Same shape a real install writes, but stamped long before this run.
        _write_hub_lock(
            skills_dir, {"pdf": _lock_entry("skills-sh/pdf", name="pdf", stamp=_OLD_STAMP)}
        )

    monkeypatch.setattr(
        sha, "_spawn",
        _make_fake_popen(["Installed: pdf\n"], returncode=0, on_spawn=_foreign_write),
    )
    handler, _ = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "skills-sh/pdf"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "failed", final
    assert "predates this run" in (final.get("error") or "")


def test_install_fails_closed_when_the_entry_has_no_usable_timestamp(monkeypatch, tmp_path):
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    skills_dir = tmp_path / "skills"
    _write_hub_lock(skills_dir, {})

    def _stampless(_cmd, _kw):
        entry = _lock_entry("skills-sh/pdf", name="pdf")
        entry.pop("installed_at")
        entry.pop("updated_at")
        _write_hub_lock(skills_dir, {"pdf": entry})

    monkeypatch.setattr(
        sha, "_spawn",
        _make_fake_popen(["Installed: pdf\n"], returncode=0, on_spawn=_stampless),
    )
    handler, _ = _call_post(monkeypatch, "/api/skills/hub/install", {"identifier": "skills-sh/pdf"})
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "failed", final
    assert "no usable timestamp" in (final.get("error") or "")


# ── Re-gate 4779033194 #3: owner/run retirement and process-tree extinction ─


def test_two_principals_on_one_profile_do_not_share_run_state(monkeypatch, tmp_path):
    """Ownership is the identity, not the client-controlled profile cookie."""
    from api import skills_hub_actions as sha

    alice = sha._open_slot("user:alice", "default")
    with alice.state_lock:
        alice.running = True
        alice.state.update({
            "action": "install", "target": "secret/skill", "status": "running",
            "log": "alice's transcript", "run_id": "alice-run",
        })

    bob = sha.get_status("default", principal="user:bob")
    assert bob["status"] == "idle"
    assert bob["log"] == ""
    assert bob["target"] is None
    assert bob["run_id"] is None

    # And Bob is not blocked by Alice's slot.
    monkeypatch.setattr(sha, "_spawn", _make_fake_popen(["x\n"], returncode=0))
    from api import profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    started, status = sha.start_action("scan", "x", profile="default", principal="user:bob")
    assert started is True
    assert status["run_id"] and status["run_id"] != "alice-run"
    _wait_until_not_running(principal="user:bob")


def test_status_read_never_allocates_a_slot():
    """An unknown-status read must not create state.

    Otherwise a poll loop with a varying profile cookie grows the registry once
    per request without any run ever starting.
    """
    from api import skills_hub_actions as sha

    with sha._REGISTRY_LOCK:
        sha._SLOTS.clear()
    for index in range(50):
        status = sha.get_status(f"profile-{index}", principal=f"user:{index}")
        assert status["status"] == "idle"
    with sha._REGISTRY_LOCK:
        assert sha._SLOTS == {}, "a status poll allocated registry entries"


def test_status_with_a_foreign_run_id_reports_idle(monkeypatch, tmp_path):
    """A continuation issued before a profile switch must not adopt a new run."""
    from api import profiles, skills_hub_actions as sha

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sha, "_spawn", _make_fake_popen(["x\n"], returncode=0))
    started, status = sha.start_action("scan", "x", profile="default", principal=_LOCAL_PRINCIPAL)
    assert started is True
    run_id = status["run_id"]
    assert run_id

    matched = sha.get_status("default", principal=_LOCAL_PRINCIPAL, run_id=run_id)
    assert matched["run_id"] == run_id
    stale = sha.get_status("default", principal=_LOCAL_PRINCIPAL, run_id="a-previous-run")
    assert stale["status"] == "idle"
    assert stale["log"] == ""
    _wait_until_not_running()


def test_terminal_slots_are_evicted_by_age_and_by_count():
    from api import skills_hub_actions as sha

    with sha._REGISTRY_LOCK:
        sha._SLOTS.clear()

    aged = sha._open_slot("user:aged", "default")
    with aged.state_lock:
        aged.state.update({"status": "completed", "finished_at": time.time()})
    aged.retired_at = time.time() - (sha._TERMINAL_TTL_SECONDS + 60)

    live = sha._open_slot("user:live", "default")
    with live.state_lock:
        live.running = True
        live.state.update({"status": "running"})

    sha.get_status("default", principal="user:someone-else")
    with sha._REGISTRY_LOCK:
        assert ("user:aged", "default") not in sha._SLOTS, "expired terminal slot survived"
        assert ("user:live", "default") in sha._SLOTS, "a running slot must never be evicted"

    for index in range(sha._MAX_SLOTS + 20):
        slot = sha._open_slot(f"user:bulk{index}", "default")
        with slot.state_lock:
            slot.state.update({"status": "completed", "finished_at": time.time()})
    with sha._REGISTRY_LOCK:
        assert len(sha._SLOTS) <= sha._MAX_SLOTS + 1, len(sha._SLOTS)
        assert ("user:live", "default") in sha._SLOTS

    with sha._REGISTRY_LOCK:
        sha._SLOTS.clear()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_extinction_kills_a_child_that_outlived_a_sigterm_leader(tmp_path):
    """Leader exits on SIGTERM, descendant ignores it: the tree must still die.

    This is the exact shape the escalation exists for — a `git`/scanner child
    outliving a leader that terminated cleanly. Reaping the leader proves
    nothing about the group.
    """
    import os as _os
    import signal as _signal
    import sys

    from api import skills_hub_actions as sha

    ready = tmp_path / "child-armed"
    child = (
        f"{sys.executable} -c "
        f"\"import signal,time,pathlib;"
        f"signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('armed');"
        f"time.sleep(60)\""
    )
    proc = subprocess.Popen(
        ["sh", "-c", f"{child} & exec sleep 60"], start_new_session=True
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "the SIGTERM-ignoring child never armed itself"

        _os.killpg(proc.pid, _signal.SIGTERM)
        proc.wait(timeout=20)  # the leader dies and is reaped
        assert sha._process_group_alive(proc.pid), (
            "test precondition: a descendant must have survived the SIGTERM"
        )

        assert sha._extinguish_process_tree(proc) is True
        assert not sha._process_group_alive(proc.pid), "a descendant survived extinction"
    finally:
        try:
            _os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_slot_stays_closed_until_extinction_is_proved(monkeypatch, tmp_path):
    """Unproved extinction must produce honest 409s, not a re-admitted run.

    Releasing the slot while a descendant still owns the skills directory is
    what lets two CLI trees write it at once. The kill escalation itself is
    covered by the real-process test above; what is under test here is the
    release rule that consumes its answer.
    """
    import sys

    from api import profiles, skills_hub_actions as sha

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_SKILLS_HUB_WRITE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sha, "_ACTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(sha, "_TREE_TERM_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(sha, "_EXTINCTION_POLL_SECONDS", 0.02)

    # A real, disposable process group of our own, so the runner's group
    # signalling targets something we actually own instead of a guessed pid.
    dummy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )

    class _TimingOutProc:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.pid = dummy.pid
            self.stdout = io.StringIO("")
            self.stdin = _FakeStdin() if kwargs.get("stdin") == subprocess.PIPE else None
            self.returncode = None

        def wait(self, timeout=None):
            if timeout is None:
                self.returncode = -9
                return self.returncode
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)

        def send_signal(self, sig):
            pass

        def kill(self):
            pass

    survivor_alive = {"value": True}
    monkeypatch.setattr(sha, "_spawn", _TimingOutProc)
    # Extinction could not be proved: a descendant is still out there.
    monkeypatch.setattr(sha, "_extinguish_process_tree", lambda proc: False)
    monkeypatch.setattr(sha, "_process_group_alive", lambda pgid: survivor_alive["value"])

    try:
        handler, _ = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "x"})
        assert handler.status == 200
        final = _wait_until_not_running(timeout=10)
        assert final["status"] == "timeout"
        assert "stays closed" in (final.get("error") or "")

        slot = sha._open_slot(_LOCAL_PRINCIPAL, "default")
        assert slot.running is True, "the slot was released while a descendant was alive"

        # A second action is refused with the caller's own state, not admitted.
        handler2, _ = _call_post(monkeypatch, "/api/skills/hub/scan", {"identifier": "y"})
        assert handler2.status == 409

        # Once the group really is gone, the reaper releases the slot.
        survivor_alive["value"] = False
        deadline = time.time() + 5
        while time.time() < deadline and slot.running:
            time.sleep(0.02)
        assert slot.running is False, "the reaper never released the slot after extinction"
    finally:
        dummy.kill()
        dummy.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_extinction_reports_failure_when_the_group_never_disappears(monkeypatch):
    """The False answer is the whole contract: it keeps the slot closed.

    A real SIGKILL always succeeds, so the surviving-group branch cannot be
    reached with real processes — the group's own liveness probe is what the
    function trusts, so that is what this drives. Without an honest False here,
    `_run`'s release rule can never fire and the guard is decorative.
    """
    import sys

    from api import skills_hub_actions as sha

    monkeypatch.setattr(sha, "_TREE_TERM_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(sha, "_TREE_KILL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(sha, "_process_group_alive", lambda pgid: True)

    dummy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        assert sha._extinguish_process_tree(dummy) is False
    finally:
        dummy.kill()
        dummy.wait(timeout=10)
