"""Synthetic worker interleavings for OAuth withdrawal and publication."""

import threading

import pytest

import api.oauth as oauth


@pytest.fixture
def flow_state(monkeypatch, tmp_path):
    monkeypatch.setattr(oauth, "_OAUTH_FLOWS", {})
    monkeypatch.setattr(oauth.time, "sleep", lambda _seconds: None)
    clock = [100.0]
    monkeypatch.setattr(oauth.time, "time", lambda: clock[0])
    return tmp_path, clock


def _worker(monkeypatch, provider, publish):
    if provider == "openai-codex":
        monkeypatch.setattr(oauth, "_poll_codex_authorization", lambda *_a: {"authorization_code": "dummy", "code_verifier": "dummy"})
        monkeypatch.setattr(oauth, "_exchange_codex_authorization", lambda *_a: {"access_token": "dummy"})
        monkeypatch.setattr(oauth, "_persist_codex_credentials", lambda home, _tokens: publish(home))
        return oauth._run_codex_oauth_worker
    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: {"accessToken": "dummy"})
    monkeypatch.setattr(oauth, "_link_anthropic_credentials", publish)
    return oauth._run_anthropic_credential_worker


@pytest.mark.parametrize("provider", ["openai-codex", "anthropic"])
def test_cancel_before_first_write_prevents_late_publication(monkeypatch, flow_state, provider):
    home, _ = flow_state
    entered, release, cancel_done = threading.Event(), threading.Event(), threading.Event()
    result = {}
    oauth._OAUTH_FLOWS["dummy-flow"] = {"status": "pending", "provider": provider, "expires_at": 200.0, "hermes_home": str(home)}

    def publish(target):
        entered.set()
        assert release.wait(5)
        oauth._write_auth_json({"dummy": "published"}, target / "auth.json")

    worker = threading.Thread(target=_worker(monkeypatch, provider, publish), args=("dummy-flow",))

    def cancel():
        result.update(oauth.cancel_onboarding_oauth_flow({"flow_id": "dummy-flow", "provider": provider}))
        cancel_done.set()

    worker.start()
    assert entered.wait(5)
    canceller = threading.Thread(target=cancel)
    canceller.start()
    try:
        assert cancel_done.wait(5), "pre-publication cancellation did not complete"
    finally:
        release.set()
        worker.join(5)
        canceller.join(5)
    assert not worker.is_alive() and not canceller.is_alive()
    assert result["status"] == "cancelled"
    assert oauth._OAUTH_FLOWS["dummy-flow"]["status"] == "cancelled"
    assert not (home / "auth.json").exists()


@pytest.mark.parametrize("provider", ["openai-codex", "anthropic"])
def test_expiry_is_rechecked_after_provider_wait(monkeypatch, flow_state, provider):
    home, clock = flow_state
    oauth._OAUTH_FLOWS["dummy-flow"] = {"status": "pending", "provider": provider, "expires_at": 105.0, "hermes_home": str(home)}
    worker = _worker(monkeypatch, provider, lambda target: oauth._write_auth_json({"dummy": "published"}, target / "auth.json"))

    def late_result(*_args):
        clock[0] = 110.0
        return {"access_token": "dummy", "accessToken": "dummy"}

    if provider == "openai-codex":
        monkeypatch.setattr(oauth, "_exchange_codex_authorization", late_result)
    else:
        monkeypatch.setattr(oauth, "_read_claude_code_credentials", late_result)
    worker("dummy-flow")
    assert not (home / "auth.json").exists()
    assert oauth._OAUTH_FLOWS["dummy-flow"]["status"] == "expired"


@pytest.mark.parametrize("provider", ["openai-codex", "anthropic"])
def test_publication_wins_before_cancel_returns_once_writer_has_started(monkeypatch, flow_state, provider):
    home, _ = flow_state
    entered, release, cancel_done = threading.Event(), threading.Event(), threading.Event()
    result = {}
    oauth._OAUTH_FLOWS["dummy-flow"] = {"status": "pending", "provider": provider, "expires_at": 200.0, "hermes_home": str(home)}
    real_fsync = oauth.os.fsync

    def pause_fsync(fd):
        entered.set()
        assert release.wait(5)
        real_fsync(fd)

    monkeypatch.setattr(oauth.os, "fsync", pause_fsync)
    publish = lambda target: oauth._write_auth_json({"dummy": "published"}, target / "auth.json")
    worker = threading.Thread(target=_worker(monkeypatch, provider, publish), args=("dummy-flow",))

    def cancel():
        result.update(oauth.cancel_onboarding_oauth_flow({"flow_id": "dummy-flow", "provider": provider}))
        cancel_done.set()

    worker.start()
    assert entered.wait(5)
    canceller = threading.Thread(target=cancel)
    canceller.start()
    try:
        assert oauth._OAUTH_FLOWS_LOCK.locked()
        assert not cancel_done.wait(0.15)
    finally:
        release.set()
        worker.join(5)
        canceller.join(5)
    assert not worker.is_alive() and not canceller.is_alive()
    assert result["status"] == "success"
    assert (home / "auth.json").exists()


def test_terminal_flow_status_is_not_overwritten_by_delayed_worker(flow_state):
    oauth._OAUTH_FLOWS["dummy-flow"] = {"status": "cancelled", "updated_at": 100.0}
    oauth._set_flow_status("dummy-flow", "success", access_token="dummy")
    assert oauth._OAUTH_FLOWS["dummy-flow"] == {"status": "cancelled", "updated_at": 100.0}
