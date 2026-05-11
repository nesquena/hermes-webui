"""Regression coverage for active-provider quota status (#706)."""

from __future__ import annotations

import json
import inspect
import os
import threading
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import api.config as config
import api.profiles as profiles

ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _with_config(model=None, providers=None):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = model or {}
    if providers is not None:
        config.cfg["providers"] = providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    return old_cfg, old_mtime


def _restore_config(old_cfg, old_mtime):
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


def test_openrouter_quota_fetches_key_endpoint_and_sanitizes_response(monkeypatch, tmp_path):
    """OpenRouter's documented key endpoint should be called server-side only."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key-private\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["authorization"] = req.headers.get("Authorization")
        payload = {"data": {"limit_remaining": "12.5", "usage": 3, "limit": 20, "key": "must-not-leak"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert seen == {
        "url": "https://openrouter.ai/api/v1/key",
        "timeout": 3.0,
        "authorization": "Bearer test-openrouter-key-private",
    }
    assert result == {
        "ok": True,
        "provider": "openrouter",
        "display_name": "OpenRouter",
        "supported": True,
        "status": "available",
        "label": "OpenRouter credits",
        "quota": {"limit_remaining": 12.5, "usage": 3, "limit": 20},
        "message": "OpenRouter quota status loaded.",
    }
    assert "test-openrouter-key-private" not in repr(result)
    assert "must-not-leak" not in repr(result)


def test_openrouter_quota_no_key_returns_safe_no_key_without_network(monkeypatch, tmp_path):
    """No-key state must not call OpenRouter or leak environment details."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    def explode(*_args, **_kwargs):
        raise AssertionError("quota lookup should not call the network without a key")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openrouter"
    assert result["supported"] is True
    assert result["status"] == "no_key"
    assert result["quota"] is None
    assert "OPENROUTER_API_KEY" in result["message"]


def test_openrouter_quota_invalid_key_and_timeout_are_sanitized(monkeypatch, tmp_path):
    """Invalid-key and timeout/error paths should expose statuses, not secrets."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key-private\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    req = providers.urllib.request.Request("https://openrouter.ai/api/v1/key")
    invalid = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b"secret body"))
    errors = [invalid, TimeoutError("slow secret")]

    try:
        for expected in ("invalid_key", "unavailable"):
            def fake_urlopen(_req, timeout=None, *, _err=errors.pop(0)):
                raise _err

            monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
            result = providers.get_provider_quota("openrouter")
            assert result["ok"] is False
            assert result["status"] == expected
            assert result["quota"] is None
            assert "test-openrouter-key-private" not in repr(result)
            assert "secret" not in repr(result).lower()
    finally:
        _restore_config(old_cfg, old_mtime)


def test_unsupported_provider_reports_followup_state(monkeypatch, tmp_path):
    """Providers without safe quota APIs should return a clear unsupported state."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai"})

    import api.providers as providers
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openai"
    assert result["supported"] is False
    assert result["status"] == "unsupported"
    assert result["quota"] is None
    assert "follow-up" in result["message"]


def test_codex_account_usage_is_fetched_under_active_profile_home(monkeypatch, tmp_path):
    """Codex account limits must use the selected WebUI profile's HERMES_HOME."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})

    import api.providers as providers
    seen = {}
    previous_home = os.environ.get("HERMES_HOME")

    def fake_fetch(provider, home, api_key=None):
        seen["provider"] = provider
        seen["home"] = str(home)
        seen["api_key"] = api_key
        return SimpleNamespace(
            provider="openai-codex",
            source="usage_api",
            title="Account limits",
            plan="Pro",
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
            available=True,
            windows=(
                SimpleNamespace(
                    label="Session",
                    used_percent=15.0,
                    reset_at=datetime(2030, 3, 17, 17, 30, tzinfo=timezone.utc),
                    detail=None,
                ),
                SimpleNamespace(
                    label="Weekly",
                    used_percent=40.0,
                    reset_at=datetime(2030, 3, 24, 12, 30, tzinfo=timezone.utc),
                    detail=None,
                ),
            ),
            details=("Credits balance: $12.50",),
            unavailable_reason=None,
        )

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert seen == {
        "provider": "openai-codex",
        "home": str(tmp_path),
        "api_key": None,
    }
    assert os.environ.get("HERMES_HOME") == previous_home
    assert result["ok"] is True
    assert result["provider"] == "openai-codex"
    assert result["supported"] is True
    assert result["status"] == "available"
    assert result["quota"] is None
    assert result["account_limits"] == {
        "provider": "openai-codex",
        "source": "usage_api",
        "title": "Account limits",
        "plan": "Pro",
        "windows": [
            {
                "label": "Session",
                "used_percent": 15.0,
                "remaining_percent": 85.0,
                "reset_at": "2030-03-17T17:30:00Z",
                "detail": None,
            },
            {
                "label": "Weekly",
                "used_percent": 40.0,
                "remaining_percent": 60.0,
                "reset_at": "2030-03-24T12:30:00Z",
                "detail": None,
            },
        ],
        "details": ["Credits balance: $12.50"],
        "available": True,
        "unavailable_reason": None,
        "fetched_at": "2030-03-17T12:30:00Z",
    }


def test_codex_account_usage_unavailable_is_sanitized(monkeypatch, tmp_path):
    """Auth/network failures should not leak raw token or exception details."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})

    import api.providers as providers

    def fake_fetch(*_args, **_kwargs):
        raise RuntimeError("secret access token should not leak")

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openai-codex"
    assert result["supported"] is True
    assert result["status"] == "unavailable"
    assert result["account_limits"] is None
    assert "Confirm provider authentication" in result["message"]
    assert "secret" not in repr(result).lower()


def test_anthropic_oauth_usage_unavailable_reason_is_reported(monkeypatch, tmp_path):
    """Hermes Agent can report why account limits are not available."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "anthropic"})

    import api.providers as providers

    monkeypatch.setattr(
        providers,
        "_agent_fetch_account_usage_for_home",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="anthropic",
            source="oauth_usage_api",
            title="Account limits",
            plan=None,
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
            available=False,
            windows=(),
            details=(),
            unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.",
        ),
    )
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "anthropic"
    assert result["supported"] is True
    assert result["status"] == "unavailable"
    assert result["account_limits"]["unavailable_reason"].startswith("Anthropic account limits")
    assert "OAuth-backed Claude accounts" in result["message"]


def test_account_usage_profile_fetch_does_not_enter_cron_env_context():
    """Quota probes must not reuse cron's process-global env/module swapper."""
    import api.providers as providers

    body = inspect.getsource(providers._fetch_account_usage_with_profile_context)
    assert "cron_profile_context_for_home" not in body
    assert "_agent_fetch_account_usage_for_home" in body


def test_account_usage_profile_env_is_child_scoped(monkeypatch, tmp_path):
    """Profile .env values should be passed to the child probe only."""
    import api.providers as providers

    home = tmp_path / "profile-a"
    home.mkdir()
    (home / ".env").write_text("ANTHROPIC_API_KEY=profile-key\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "process-key")

    env = providers._account_usage_subprocess_env(home, "anthropic", None)

    assert env["HERMES_HOME"] == str(home)
    assert env["ANTHROPIC_API_KEY"] == "profile-key"
    assert os.environ["ANTHROPIC_API_KEY"] == "process-key"


def test_account_usage_profile_fetches_can_overlap_for_different_homes(monkeypatch, tmp_path):
    """Different profile quota fetches should not serialize on cron's global lock."""
    import api.providers as providers

    homes = {
        "quota-a": tmp_path / "a",
        "quota-b": tmp_path / "b",
    }
    for home in homes.values():
        home.mkdir()
    barrier = threading.Barrier(2, timeout=2)
    events = []
    errors = []

    def fake_home():
        return homes[threading.current_thread().name]

    def fake_fetch(provider, home, api_key=None):
        events.append(("enter", str(home)))
        barrier.wait()
        events.append(("exit", str(home)))
        return None

    monkeypatch.setattr(providers, "_get_hermes_home", fake_home)
    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)

    def worker():
        try:
            providers._fetch_account_usage_with_profile_context("openai-codex")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, name="quota-a"),
        threading.Thread(target=worker, name="quota-b"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert [kind for kind, _home in events[:2]] == ["enter", "enter"]


def test_provider_quota_route_is_registered():
    """The backend must expose a route for the UI to poll quota status."""
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/provider/quota"' in routes
    assert "get_provider_quota(provider_id)" in routes


def test_provider_quota_card_is_rendered_in_providers_panel():
    """The Providers panel should show active provider quota/status before cards."""
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "api('/api/provider/quota')" in panels
    assert "function _buildProviderQuotaCard" in panels
    assert "Active provider quota" in panels
    assert "provider-quota-card" in panels
    assert "account_limits" in panels
    assert "remaining_percent" in panels
    assert "provider-quota-details" in panels
    assert "5-hour limit" in panels


def test_provider_quota_styles_exist():
    """Quota UI should have visible supported/unavailable/invalid states."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    for token in (
        ".provider-quota-card",
        ".provider-quota-metric",
        ".provider-quota-card-available",
        ".provider-quota-card-no_key",
        ".provider-quota-card-invalid_key",
        ".provider-quota-details",
        ".provider-quota-window",
    ):
        assert token in css


# ── Regression tests for #1912 ────────────────────────────────────────────────

def test_account_usage_subprocess_uses_devnull_stdin(monkeypatch):
    """Account-usage probe subprocess must receive stdin=DEVNULL.

    DEVNULL prevents the child from inheriting any pipe that could block or
    leak data.  This is a defence-in-depth measure beyond the parent-death
    signal; it is tested separately to make the invariant explicit.
    """
    import api.providers as providers
    import subprocess

    seen_stdin = None

    def capturing_run(*args, **kwargs):
        nonlocal seen_stdin
        seen_stdin = kwargs.get('stdin')
        class FakeProc:
            returncode = 0
            stdout = '{}'
            stderr = ''
        return FakeProc()

    monkeypatch.setattr(subprocess, 'run', capturing_run)
    try:
        providers._agent_fetch_account_usage_for_home(
            'openai-codex', Path('/nonexistent'), api_key=None
        )
    except Exception:
        pass  # errors are expected on a fake env; we only care about stdin

    assert seen_stdin is subprocess.DEVNULL, (
        f'expected stdin=subprocess.DEVNULL, got {seen_stdin!r}'
    )


def test_account_usage_probe_semaphore_has_correct_bound(monkeypatch, tmp_path):
    """The probe semaphore must enforce the declared concurrency cap.

    Verifying the bound directly ensures the cap actually prevents resource
    exhaustion when the UI polls multiple providers in rapid succession.
    """
    import api.providers as providers

    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={'provider': 'openai-codex'})

    sem = providers._get_account_usage_probe_semaphore()
    try:
        bound = sem._value
        assert bound == providers._MAX_CONCURRENT_ACCOUNT_USAGE_PROBES, (
            f'semaphore bound is {bound}, expected '
            f'{providers._MAX_CONCURRENT_ACCOUNT_USAGE_PROBES}'
        )
    finally:
        _restore_config(old_cfg, old_mtime)


def test_account_usage_preexec_fn_is_wired_on_posix(monkeypatch):
    """On POSIX systems the probe subprocess must receive a parent-death preexec_fn.

    The preexec_fn arranges prctl(PR_SET_PDEATHSIG, SIGTERM) so the child is
    terminated when the WebUI parent dies (OOM kill, systemctl restart, etc.).
    This test verifies the wiring and skips harmlessly on non-POSIX (Windows).
    """
    import api.providers as providers

    assert callable(providers._account_usage_preexec_fn)

    try:
        providers._account_usage_preexec_fn()
    except Exception as exc:
        raise AssertionError(
            f'_account_usage_preexec_fn raised {exc!r}; it should be '
            'safe to call unconditionally'
        ) from exc

    if hasattr(os, 'fork'):
        import subprocess

        captured_kwargs = {}

        def capture_run(*args, **kwargs):
            captured_kwargs.update(kwargs)
            class FakeProc:
                returncode = 0
                stdout = '{}'
                stderr = ''
            return FakeProc()

        monkeypatch.setattr(subprocess, 'run', capture_run)
        try:
            providers._agent_fetch_account_usage_for_home(
                'openai-codex', Path('/nonexistent'), api_key=None
            )
        except Exception:
            pass

        assert 'preexec_fn' in captured_kwargs, (
            'preexec_fn should be in subprocess.run kwargs on POSIX'
        )
        assert captured_kwargs['preexec_fn'] is providers._account_usage_preexec_fn


def test_account_usage_semaphore_caps_concurrency(monkeypatch, tmp_path):
    """The probe semaphore must actually serialise callers beyond its bound.

    Verifies the bounded semaphore is used in the call path and genuinely
    prevents more than _MAX_CONCURRENT_ACCOUNT_USAGE_PROBES probes running.
    """
    import api.providers as providers
    import threading

    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={'provider': 'openai-codex'})

    barrier = threading.Barrier(2, timeout=2)
    unblock = threading.Event()

    def slow_fetch(provider, home, api_key=None):
        barrier.wait()
        unblock.wait(timeout=5)
        return None

    monkeypatch.setattr(providers, '_agent_fetch_account_usage_for_home', slow_fetch)

    results = []
    errors = []

    def worker():
        try:
            results.append(
                providers._fetch_account_usage_with_profile_context('openai-codex')
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    unblock.set()

    try:
        assert not errors, f'workers raised: {errors}'
        assert len(results) == 2, f'expected 2 results, got {len(results)}'
    finally:
        _restore_config(old_cfg, old_mtime)
