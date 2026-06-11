from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import api.oauth as oauth


def setup_function():
    with oauth._OAUTH_FLOWS_LOCK:
        oauth._OAUTH_FLOWS.clear()


def teardown_function():
    with oauth._OAUTH_FLOWS_LOCK:
        oauth._OAUTH_FLOWS.clear()


def _run_two_concurrent(fn):
    ready = threading.Barrier(2)

    def wrapped():
        ready.wait(timeout=5)
        return fn()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(wrapped), executor.submit(wrapped)]
        return [future.result(timeout=5) for future in futures]


def test_anthropic_onboarding_oauth_start_reuses_pending_flow(monkeypatch, tmp_path):
    """Repeated unauthenticated starts must not spawn unbounded pending workers."""
    spawned = []
    hermes_home = tmp_path / "hermes-home"

    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(oauth, "_spawn_anthropic_credential_worker", lambda flow_id: spawned.append(flow_id))

    first = oauth._start_anthropic_flow(hermes_home)
    second = oauth._start_anthropic_flow(hermes_home)

    assert first["status"] == "pending"
    assert second["status"] == "pending"
    assert second["flow_id"] == first["flow_id"]
    assert spawned == [first["flow_id"]]
    with oauth._OAUTH_FLOWS_LOCK:
        pending = [flow for flow in oauth._OAUTH_FLOWS.values() if flow.get("status") == "pending"]
    assert len(pending) == 1


def test_anthropic_onboarding_oauth_start_single_flight_is_thread_safe(monkeypatch, tmp_path):
    """Concurrent unauthenticated Anthropic starts must reserve one pending worker atomically."""
    spawned = []
    hermes_home = tmp_path / "hermes-home"
    credential_reads = threading.Barrier(2)

    def no_credentials():
        credential_reads.wait(timeout=5)
        return None

    monkeypatch.setattr(oauth, "_read_claude_code_credentials", no_credentials)
    monkeypatch.setattr(oauth, "_spawn_anthropic_credential_worker", lambda flow_id: spawned.append(flow_id))

    first, second = _run_two_concurrent(lambda: oauth._start_anthropic_flow(hermes_home))

    assert first["status"] == "pending"
    assert second["status"] == "pending"
    assert second["flow_id"] == first["flow_id"]
    assert spawned == [first["flow_id"]]
    with oauth._OAUTH_FLOWS_LOCK:
        pending = [flow for flow in oauth._OAUTH_FLOWS.values() if flow.get("status") == "pending"]
    assert len(pending) == 1


def test_codex_onboarding_oauth_start_reuses_pending_flow_before_requesting_code(monkeypatch, tmp_path):
    """A second Codex start should reuse the live flow before creating a new device code or worker."""
    spawned = []
    requested = []
    hermes_home = tmp_path / "hermes-home"

    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: Path(hermes_home))

    def fake_request_code():
        requested.append(True)
        return {"user_code": "ABCD-EFGH", "device_auth_id": "device-1", "interval": 5, "expires_in": 900}

    monkeypatch.setattr(oauth, "_request_codex_user_code", fake_request_code)
    monkeypatch.setattr(oauth, "_spawn_codex_oauth_worker", lambda flow_id: spawned.append(flow_id))

    first = oauth.start_onboarding_oauth_flow({"provider": "openai-codex"})
    second = oauth.start_onboarding_oauth_flow({"provider": "openai-codex"})

    assert first["status"] == "pending"
    assert second["flow_id"] == first["flow_id"]
    assert second["user_code"] == "ABCD-EFGH"
    assert requested == [True]
    assert spawned == [first["flow_id"]]
    with oauth._OAUTH_FLOWS_LOCK:
        pending = [flow for flow in oauth._OAUTH_FLOWS.values() if flow.get("status") == "pending"]
    assert len(pending) == 1


def test_codex_onboarding_oauth_start_single_flight_is_thread_safe(monkeypatch, tmp_path):
    """Concurrent Codex starts must request one device code and spawn one worker."""
    spawned = []
    requested = []
    hermes_home = tmp_path / "hermes-home"

    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: Path(hermes_home))

    def fake_request_code():
        requested.append(True)
        time.sleep(0.05)
        return {"user_code": "ABCD-EFGH", "device_auth_id": "device-1", "interval": 5, "expires_in": 900}

    monkeypatch.setattr(oauth, "_request_codex_user_code", fake_request_code)
    monkeypatch.setattr(oauth, "_spawn_codex_oauth_worker", lambda flow_id: spawned.append(flow_id))

    first, second = _run_two_concurrent(lambda: oauth.start_onboarding_oauth_flow({"provider": "openai-codex"}))

    assert first["status"] == "pending"
    assert second["status"] == "pending"
    assert second["flow_id"] == first["flow_id"]
    assert second["user_code"] == "ABCD-EFGH"
    assert requested == [True]
    assert spawned == [first["flow_id"]]
    with oauth._OAUTH_FLOWS_LOCK:
        pending = [flow for flow in oauth._OAUTH_FLOWS.values() if flow.get("status") == "pending"]
    assert len(pending) == 1
