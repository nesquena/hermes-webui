"""Tests for /api/providers CRUD endpoints (provider key management).

Closes #586 — allow provider key update from the WebUI.
Part of #604 — multi-provider model picker support.
"""

import json
import threading
import time
import sys
import types
import urllib.error
import urllib.request

import pytest

import api.config as config
import api.profiles as profiles
from tests._pytest_port import BASE


# ── HTTP helpers ──────────────────────────────────────────────────────────


def _get(path):
    """GET helper — returns parsed JSON."""
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _post(path, body=None):
    """POST helper — returns (parsed_json, status_code)."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text), e.code
        except Exception:
            return {"error": body_text}, e.code


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    # Flush the 60-second TTL model cache so no prior test's result bleeds in.
    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


# ── Unit tests (api/providers.py functions directly) ──────────────────────


class TestGetProviders:
    """Unit tests for get_providers() function."""

    def test_reuses_short_ttl_cache_for_same_profile_home(self, monkeypatch, tmp_path):
        """Back-to-back provider reads should not re-run expensive probes (#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        calls = []
        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})

        def _counting_has_key(pid):
            calls.append(pid)
            return False

        monkeypatch.setattr(prov, "_provider_has_key", _counting_has_key)

        try:
            first = prov.get_providers()
            second = prov.get_providers()
            assert first == second
            assert calls == ["openai"]
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_get_providers_cold_path_parallelizes_provider_probes(self, monkeypatch, tmp_path):
        """Cold providers path should complete in roughly max-provider latency, not sum."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        delays = {
            "openai-codex": 0.25,
            "xai-oauth": 0.25,
            "nous": 0.25,
        }
        started = {}
        started_lock = threading.Lock()
        request_thread_id = threading.get_ident()
        credential_threads = []
        barrier = threading.Barrier(len(delays))

        def _fake_read_live_provider_model_ids(pid: str):
            delay = delays.get(pid)
            if delay is not None:
                with started_lock:
                    started[pid] = time.perf_counter()
                try:
                    barrier.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
                time.sleep(delay)
                return [f"{pid}-live"]
            return []

        fake_models = sys.modules["hermes_cli.models"]
        fake_models.provider_model_ids = lambda pid: (
            _fake_read_live_provider_model_ids(pid) if pid in delays else []
        )
        fake_auth = sys.modules["hermes_cli.auth"]
        fake_auth.get_auth_status = lambda _pid: {}

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {
            "openai-codex": "OpenAI Codex",
            "xai-oauth": "xAI",
            "nous": "Nous",
        })
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {
            "openai-codex": [],
            "xai-oauth": [],
            "nous": [],
        })
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset({"openai-codex", "xai-oauth", "nous"}))
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(
            prov,
            "_provider_has_key",
            lambda _pid: credential_threads.append(threading.get_ident()) or False,
        )
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_read_live_provider_model_ids", _fake_read_live_provider_model_ids)

        try:
            result = prov.get_providers()
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

        ids = {entry["id"] for entry in result["providers"]}
        assert ids == {"openai-codex", "xai-oauth", "nous"}
        assert barrier.broken is False
        assert set(started.keys()) == set(delays)
        assert set(credential_threads) == {request_thread_id}

    def test_get_providers_bedrock_entry_uses_structural_credentials_without_live_probe(self, monkeypatch, tmp_path):
        """Bedrock card construction should use structural env signals, not live auth probes."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        tmp_path.joinpath(".env").write_text(
            "AWS_ACCESS_KEY_ID=provider-path-id\n"
            "AWS_SECRET_ACCESS_KEY=provider-path-secret\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", raising=False)
        monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
        monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)

        calls = []

        fake_auth = sys.modules["hermes_cli.auth"]
        fake_auth.get_auth_status = lambda pid: calls.append(pid) or {
            "logged_in": True,
            "key_source": "env",
        }

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {
            "bedrock": "AWS Bedrock",
            "openai": "OpenAI",
        })
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {
            "bedrock": [{"id": "bedrock-model", "label": "Bedrock Model"}],
            "openai": [],
        })
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "process-id")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "process-secret")
        result = None
        try:
            result = prov.get_providers()
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()
        assert result is not None

        ids = {entry["id"]: entry for entry in result["providers"]}
        assert ids["bedrock"]["has_key"] is True
        assert ids["bedrock"]["key_source"] == "env_var"
        assert calls == []

    def test_get_providers_workers_inherit_request_context(self, monkeypatch, tmp_path):
        """Context-variable overrides propagate from request thread into worker providers."""
        _install_fake_hermes_cli(monkeypatch)

        hermes_constants = pytest.importorskip("hermes_constants")
        secret_scope = pytest.importorskip("agent.secret_scope")

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"strange": "Strange"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"strange": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset({"strange"}))
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)

        captured = []

        def fake_auth_status(_pid):
            captured.append((
                hermes_constants.get_hermes_home(),
                dict(secret_scope.current_secret_scope() or {}),
            ))
            return {"logged_in": True, "key_source": "env"}

        fake_auth = sys.modules["hermes_cli.auth"]
        fake_auth.get_auth_status = fake_auth_status

        def _run_scenario(home_path, secret_map):
            home_token = hermes_constants.set_hermes_home_override(home_path)
            scope_token = secret_scope.set_secret_scope(dict(secret_map))
            try:
                return prov.get_providers()
            finally:
                secret_scope.reset_secret_scope(scope_token)
                hermes_constants.reset_hermes_home_override(home_token)
                prov.invalidate_providers_cache()

        home_a = tmp_path / "home-a"
        home_a.mkdir()
        home_b = tmp_path / "home-b"
        home_b.mkdir()
        secrets_a = {"HOME_A_SENTINEL": "value-from-a"}
        secrets_b = {"HOME_B_SENTINEL": "value-from-b"}

        prov.invalidate_providers_cache()
        try:
            # Exercise two distinct homes/secret maps sequentially, invalidating
            # the providers cache between runs so each actually rebuilds and
            # calls the worker-thread auth probe again (#3957).
            captured.clear()
            result_a = _run_scenario(home_a, secrets_a)
            ids_a = {entry["id"]: entry for entry in result_a["providers"]}
            assert ids_a["strange"]["has_key"] is True
            assert captured == [(home_a, secrets_a)]

            captured.clear()
            result_b = _run_scenario(home_b, secrets_b)
            ids_b = {entry["id"]: entry for entry in result_b["providers"]}
            assert ids_b["strange"]["has_key"] is True
            assert captured == [(home_b, secrets_b)]

            # No leakage: the second run's captured scope/home must not carry
            # any trace of the first run's home or secrets.
            assert captured[0][0] != home_a
            assert "HOME_A_SENTINEL" not in captured[0][1]
        finally:
            prov.invalidate_providers_cache()

    def test_get_providers_singleflight_reuses_inflight_build(self, monkeypatch, tmp_path):
        """Concurrent callers for the same cache key share one provider build."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        provider_ids = [f"provider-{i:02d}" for i in range(24)]
        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {pid: pid for pid in provider_ids})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {pid: [] for pid in provider_ids})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)

        started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        seen = set()
        active = {"value": 0}
        max_active = {"value": 0}
        call_count = {"value": 0}

        def fake_build_entry(
            pid,
            initial_has_key,
            providers_cfg,
            active_profile_name,
            request_thread_env,
            request_block_process_env_fallback,
        ):
            with lock:
                call_count["value"] += 1
                active["value"] += 1
                if active["value"] > max_active["value"]:
                    max_active["value"] = active["value"]
                seen.add(str(pid))
                if active["value"] >= prov._PROVIDERS_MAX_WORKERS:
                    started.set()
            try:
                release.wait()
            finally:
                with lock:
                    active["value"] -= 1
            return {
                "id": str(pid),
                "display_name": str(pid),
                "has_key": bool(initial_has_key),
                "configurable": True,
                "is_self_hosted": False,
                "base_url": None,
                "is_plugin_provider": False,
                "is_oauth": False,
                "key_source": "none",
                "auth_error": None,
                "models": [],
                "models_total": 0,
            }

        monkeypatch.setattr(prov, "_build_provider_entry", fake_build_entry)

        waiter_entered = threading.Event()
        real_wait_provider_build = prov._wait_provider_build

        def spy_wait_provider_build(state, cache_key):
            waiter_entered.set()
            return real_wait_provider_build(state, cache_key)

        monkeypatch.setattr(prov, "_wait_provider_build", spy_wait_provider_build)

        outputs = {}
        def first_call():
            outputs["first"] = prov.get_providers()

        def second_call():
            outputs["second"] = prov.get_providers()

        t1 = threading.Thread(target=first_call)
        t2 = threading.Thread(target=second_call)
        t1.start()
        assert started.wait(2)
        t2.start()
        assert waiter_entered.wait(2)
        release.set()
        t1.join()
        t2.join()

        assert not t1.is_alive()
        assert not t2.is_alive()
        assert call_count["value"] == len(provider_ids)
        assert max_active["value"] <= prov._PROVIDERS_MAX_WORKERS
        assert outputs["first"]["providers"] != []
        assert outputs["second"]["providers"] is not outputs["first"]["providers"]

        third = prov.get_providers()
        assert third["providers"] is not outputs["first"]["providers"]

    def test_invalidate_during_blocked_build_starts_new_generation_and_ignores_stale_completion(
        self, monkeypatch, tmp_path,
    ):
        """Invalidating mid-build bumps the generation so a late-completing stale build cannot clobber a newer cached result (#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})

        lock = threading.Lock()
        build_calls = []
        old_entered = threading.Event()
        release_old = threading.Event()
        new_entered = threading.Event()
        release_new = threading.Event()

        def fake_build(
            cfg,
            sorted_known_ids,
            active_profile_name,
            request_thread_env,
            request_block_process_env_fallback,
            providers_cfg,
        ):
            with lock:
                call_id = len(build_calls)
                build_calls.append(call_id)
            if call_id == 0:
                old_entered.set()
                release_old.wait(2)
            else:
                new_entered.set()
                release_new.wait(2)
            return {
                "providers": [{
                    "id": "openai",
                    "display_name": "OpenAI",
                    "has_key": False,
                    "configurable": True,
                    "key_source": "none",
                    "call_id": call_id,
                }],
                "active_provider": None,
            }

        monkeypatch.setattr(prov, "_build_providers_payload", fake_build)

        outputs = {}

        def _old_call():
            outputs["old"] = prov.get_providers()

        try:
            t_old = threading.Thread(target=_old_call)
            t_old.start()
            assert old_entered.wait(2)

            # Invalidate while the old build is still blocked — this must bump
            # the generation and drop the old build's in-flight registration so
            # a concurrent caller starts a brand-new build rather than waiting
            # on the now-stale one.
            prov.invalidate_providers_cache()

            def _new_call():
                outputs["new"] = prov.get_providers()

            t_new = threading.Thread(target=_new_call)
            t_new.start()
            assert new_entered.wait(2)

            # Complete the NEW build first, then let the stale OLD build finish.
            release_new.set()
            t_new.join(2)
            release_old.set()
            t_old.join(2)

            assert outputs["new"]["providers"][0]["call_id"] == 1
            assert outputs["old"]["providers"][0]["call_id"] == 0

            # A subsequent cached read must reflect the new build, not the
            # late-completing stale one.
            cached = prov.get_providers()
            assert cached["providers"][0]["call_id"] == 1
        finally:
            prov.invalidate_providers_cache()

    def test_failed_aggregate_build_clears_inflight_state_and_retries_succeed(
        self, monkeypatch, tmp_path,
    ):
        """A build that raises must not leave stale in-flight/cache state (#6010).

        A subsequent request for the same cache key should retry the build
        (not hang waiting on a dead in-flight entry) and succeed.
        """
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})

        attempt = {"value": 0}

        def flaky_build(
            cfg,
            sorted_known_ids,
            active_profile_name,
            request_thread_env,
            request_block_process_env_fallback,
            providers_cfg,
        ):
            attempt["value"] += 1
            if attempt["value"] == 1:
                raise RuntimeError("simulated aggregate build failure")
            return {
                "providers": [{
                    "id": "openai",
                    "display_name": "OpenAI",
                    "has_key": False,
                    "configurable": True,
                    "key_source": "none",
                }],
                "active_provider": None,
            }

        monkeypatch.setattr(prov, "_build_providers_payload", flaky_build)

        try:
            with pytest.raises(RuntimeError, match="simulated aggregate build failure"):
                prov.get_providers()

            cache_key = prov._providers_cache_key(prov.get_config())
            assert cache_key not in prov._providers_build_inflight
            assert cache_key not in prov._providers_cache

            result = prov.get_providers()
            ids = {entry["id"] for entry in result["providers"]}
            assert ids == {"openai"}
            assert attempt["value"] == 2
        finally:
            prov.invalidate_providers_cache()

    def test_complete_provider_build_cache_publication_lock_error_wakes_waiter_and_clears_inflight(self, monkeypatch, tmp_path):
        """Cache publication lock failure must wake waiters and be recoverable."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)

        payload_begun = threading.Event()
        release = threading.Event()
        build_calls = {"value": 0}

        def flaky_build_payload(
            cfg,
            sorted_known_ids,
            active_profile_name,
            request_thread_env,
            request_block_process_env_fallback,
            providers_cfg,
        ):
            build_calls["value"] += 1
            payload_begun.set()
            release.wait(2)
            return {
                "providers": [{
                    "id": "openai",
                    "display_name": "OpenAI",
                    "has_key": False,
                    "configurable": True,
                    "is_self_hosted": False,
                    "base_url": None,
                    "is_plugin_provider": False,
                    "is_oauth": False,
                    "key_source": "none",
                    "auth_error": None,
                    "models": [],
                    "models_total": 0,
                }],
                "active_provider": "openai",
            }

        monkeypatch.setattr(prov, "_build_providers_payload", flaky_build_payload)

        original_cache_lock = prov._providers_cache_lock
        exit_counter = {"count": 0}
        injected_failures = {"count": 0}
        armed = threading.Event()
        completion_error = RuntimeError("cache publication failed")

        class _OneShotFailingCacheLock:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                return self._inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                exit_counter["count"] += 1
                self._inner.__exit__(exc_type, exc, tb)
                if armed.is_set() and injected_failures["count"] == 0:
                    injected_failures["count"] += 1
                    raise completion_error

        failing_cache_lock = _OneShotFailingCacheLock(original_cache_lock)
        monkeypatch.setattr(prov, "_providers_cache_lock", failing_cache_lock)

        waiter = threading.Event()
        real_wait_provider_build = prov._wait_provider_build

        def spy_wait_provider_build(state, cache_key):
            waiter.set()
            return real_wait_provider_build(state, cache_key)

        monkeypatch.setattr(prov, "_wait_provider_build", spy_wait_provider_build)
        errors = {}

        def call_get_providers(label):
            try:
                prov.get_providers()
            except BaseException as exc:
                errors[label] = exc

        lead = threading.Thread(target=call_get_providers, args=("lead",))
        follower = threading.Thread(target=call_get_providers, args=("waiter",))
        lead_started = False
        follower_started = False

        try:
            lead.start()
            lead_started = True
            assert payload_begun.wait(2)
            follower.start()
            follower_started = True
            assert waiter.wait(2)
            armed.set()

            release.set()
            lead.join(2)
            follower.join(2)

            assert not lead.is_alive()
            assert not follower.is_alive()
            assert isinstance(errors.get("lead"), RuntimeError)
            assert isinstance(errors.get("waiter"), RuntimeError)
            assert errors["lead"].args == errors["waiter"].args == completion_error.args
            assert injected_failures["count"] == 1
            assert exit_counter["count"] >= 1

            assert "cache publication failed" in str(errors["lead"])
            monkeypatch.setattr(prov, "_providers_cache_lock", original_cache_lock)

            cache_key = prov._providers_cache_key(prov.get_config())
            assert cache_key not in prov._providers_build_inflight

            result = prov.get_providers()
            ids = {entry["id"] for entry in result["providers"]}
            assert ids == {"openai"}
            assert build_calls["value"] == 2
        finally:
            release.set()
            if lead_started:
                lead.join(2)
            if follower_started:
                follower.join(2)
            monkeypatch.setattr(prov, "_providers_cache_lock", original_cache_lock)
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_complete_provider_build_skips_stale_publication_after_generation_bump(self, monkeypatch, tmp_path):
        """TOCTOU generation bump between pre-check and lock must not cache stale completion."""
        _install_fake_hermes_cli(monkeypatch)
        from api import providers as prov

        cache_key = ("toctou-generation", "providers")
        stale_payload = {
            "providers": [{"id": "stale", "display_name": "Stale", "has_key": False}],
            "active_provider": None,
        }
        fresh_cache_value = (999.0, {"providers": [{"id": "fresh"}], "active_provider": "fresh"})
        original_cache_lock = prov._providers_cache_lock
        original_generation = prov._providers_cache_generation

        prov._providers_cache.clear()
        prov._providers_cache[cache_key] = fresh_cache_value
        prov._providers_cache_generation = 0
        bump_state = {"count": 0}

        class _GenerationBumpCacheLock:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                if bump_state["count"] == 0:
                    prov._providers_cache_generation += 1
                    bump_state["count"] += 1
                return self._inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                return self._inner.__exit__(exc_type, exc, tb)

        bumping_lock = _GenerationBumpCacheLock(original_cache_lock)
        monkeypatch.setattr(prov, "_providers_cache_lock", bumping_lock)

        stale_state = prov._ProvidersBuildInFlight(0)

        try:
            cached_before = prov._providers_cache[cache_key]
            result = prov._complete_provider_build(cache_key, stale_state, payload=stale_payload)

            assert result == stale_payload
            assert prov._providers_cache.get(cache_key) is cached_before
            assert prov._providers_cache[cache_key] == fresh_cache_value
            assert bump_state["count"] == 1
            assert prov._providers_cache_generation == 1
        finally:
            monkeypatch.setattr(prov, "_providers_cache_lock", original_cache_lock)
            prov._providers_cache_generation = original_generation
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_build_provider_entry_outer_fallback_preserves_env_key_source(self, monkeypatch, tmp_path):
        """Outer catch should retain env key source for configured API-key providers."""
        _install_fake_hermes_cli(monkeypatch)
        (tmp_path / ".env").write_text("LM_API_KEY=lmstudio-fallback-key\n", encoding="utf-8")
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"lmstudio": "LM Studio"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"lmstudio": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())

        plugin_probe_calls = {"value": 0}

        def _raise_plugin_probe(_provider_id):
            plugin_probe_calls["value"] += 1
            if plugin_probe_calls["value"] == 1:
                raise RuntimeError("forcing fallback")
            return False

        monkeypatch.setattr(prov, "is_plugin_model_provider", _raise_plugin_probe)

        entry = prov._build_provider_entry(
            provider_id="lmstudio",
            initial_has_key=True,
            providers_cfg={},
            active_profile_name="",
            request_thread_env={},
            block_process_env_fallback=False,
        )

        assert entry is not None
        assert entry["has_key"] is True
        assert entry["key_source"] == "env_file"
        assert plugin_probe_calls["value"] >= 2

    def test_build_provider_entry_initial_bedrock_key_source_prefers_env_var_for_structural_credentials(self, monkeypatch, tmp_path):
        """Initial bedrock credentials from structural signals should report env_var."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"bedrock": "AWS Bedrock"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"bedrock": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())

        entry = prov._build_provider_entry(
            provider_id="bedrock",
            initial_has_key=True,
            providers_cfg={},
            active_profile_name="",
            request_thread_env={
                "AWS_ACCESS_KEY_ID": "thread-access-key",
                "AWS_SECRET_ACCESS_KEY": "thread-secret-key",
            },
            block_process_env_fallback=False,
        )

        assert entry is not None
        assert entry["has_key"] is True
        assert entry["key_source"] == "env_var"
        assert "thread-access-key" not in str(entry)
        assert "thread-secret-key" not in str(entry)

    def test_build_provider_entry_initial_bedrock_key_source_falls_back_config_yaml(self, monkeypatch, tmp_path):
        """Initial bedrock credentials without structural signals should report config_yaml."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        for env_name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
        ):
            monkeypatch.delenv(env_name, raising=False)

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"bedrock": "AWS Bedrock"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"bedrock": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())

        entry = prov._build_provider_entry(
            provider_id="bedrock",
            initial_has_key=True,
            providers_cfg={},
            active_profile_name="",
            request_thread_env={},
            block_process_env_fallback=False,
        )

        assert entry is not None
        assert entry["has_key"] is True
        assert entry["key_source"] == "config_yaml"

    def test_build_provider_entry_cleans_up_request_context_after_setup_error(self, monkeypatch, tmp_path):
        """Partial request-context setup failure must not leave thread-local/profile residue."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov
        from api import config as provider_cfg

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())

        original_clear_thread_env = provider_cfg._clear_thread_env
        clear_thread_env_calls = {"value": 0}
        set_thread_env_calls = {"value": 0}

        original_set_thread_env = provider_cfg._set_thread_env

        def flaky_set_thread_env(**kwargs):
            set_thread_env_calls["value"] += 1
            if set_thread_env_calls["value"] == 1:
                # Partially apply caller state, then fail to prove the finally
                # cleanup still runs and restores worker-local context.
                provider_cfg._thread_ctx.env = dict(kwargs)
                raise RuntimeError("simulated context setup failure")
            return original_set_thread_env(**kwargs)

        def tracked_clear_thread_env():
            clear_thread_env_calls["value"] += 1
            return original_clear_thread_env()

        try:
            profiles.clear_request_profile()
            provider_cfg._clear_thread_env()
            provider_cfg._thread_ctx.block_process_env_fallback = False
            assert getattr(profiles._tls, "profile", None) is None
            assert provider_cfg._thread_ctx.env == {}
            assert provider_cfg._thread_ctx.block_process_env_fallback is False

            monkeypatch.setattr(provider_cfg, "_set_thread_env", flaky_set_thread_env)
            monkeypatch.setattr(provider_cfg, "_clear_thread_env", tracked_clear_thread_env)

            entry = prov._build_provider_entry(
                provider_id="openai",
                initial_has_key=True,
                providers_cfg={},
                active_profile_name="quarantine-profile",
                request_thread_env={"THREAD_ENV_KEY": "thread-env-value"},
                block_process_env_fallback=False,
            )

            assert entry is not None
            assert set_thread_env_calls["value"] == 1
            assert clear_thread_env_calls["value"] == 1
            assert getattr(profiles._tls, "profile", None) is None
            assert provider_cfg._thread_ctx.env == {}
            assert provider_cfg._thread_ctx.block_process_env_fallback is False
        finally:
            # Ensure this test never leaves request/thread state on the shared
            # pytest worker thread, even on assertion failure.
            monkeypatch.setattr(provider_cfg, "_set_thread_env", original_set_thread_env)
            monkeypatch.setattr(provider_cfg, "_clear_thread_env", original_clear_thread_env)
            profiles.clear_request_profile()
            provider_cfg._clear_thread_env()
            provider_cfg._thread_ctx.block_process_env_fallback = False

    def test_bedrock_structural_credentials_require_complete_access_key_pair(self, monkeypatch):
        """A lone AWS access-key field is not a usable Bedrock credential signal."""
        from api import providers as prov

        for env_name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            *prov._BEDROCK_SINGLE_CREDENTIAL_ENV_SIGNALS,
        ):
            monkeypatch.delenv(env_name, raising=False)

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access-key-without-secret")
        assert prov._provider_has_structural_bedrock_credentials() is False

    def test_get_providers_bedrock_structural_credentials_respect_profile_thread_local(self, monkeypatch, tmp_path):
        """Bedrock should read process-thread credentials, not raw process env, in named profiles."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "process-id")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "process-secret")
        profile_home = tmp_path / "profiles" / "work"
        profile_home.mkdir(parents=True)
        (tmp_path / ".env").write_text(
            "OTHER_SHARED_KEY=from-process-file\n",
            encoding="utf-8",
        )
        (profile_home / ".env").write_text(
            "OTHER_PROFILE_KEY=from-profile\n",
            encoding="utf-8",
        )

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"bedrock": "AWS Bedrock"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"bedrock": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})

        profiles.set_request_profile("work")
        try:
            with profiles.profile_env_for_active_request_readonly("test"):
                no_profile_keys = prov.get_providers()
            bedrock = next((p for p in no_profile_keys["providers"] if p["id"] == "bedrock"), None)
            assert bedrock is not None
            assert bedrock["has_key"] is False

            (profile_home / ".env").write_text(
                "AWS_ACCESS_KEY_ID=profile-id\nAWS_SECRET_ACCESS_KEY=profile-secret\n",
                encoding="utf-8",
            )
            with profiles.profile_env_for_active_request_readonly("test"):
                result = prov.get_providers()
            bedrock = next((p for p in result["providers"] if p["id"] == "bedrock"), None)
            assert bedrock is not None
            assert bedrock["has_key"] is True

        finally:
            profiles.clear_request_profile()
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_provider_cache_is_scoped_by_profile_home(self, monkeypatch, tmp_path):
        """Provider cache entries must not leak across profile homes (#3957/#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        home_a = tmp_path / "a"
        home_b = tmp_path / "b"
        home_a.mkdir()
        home_b.mkdir()
        active_home = {"path": home_a}
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home["path"])

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: active_home["path"] == home_b)

        try:
            first = prov.get_providers()
            active_home["path"] = home_b
            second = prov.get_providers()
            first_openai = next(p for p in first["providers"] if p["id"] == "openai")
            second_openai = next(p for p in second["providers"] if p["id"] == "openai")
            assert first_openai["has_key"] is False
            assert second_openai["has_key"] is True
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_set_provider_key_invalidates_providers_cache(self, monkeypatch, tmp_path):
        """Saving a key should invalidate the cached Providers response (#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        from api import providers as prov

        key_present = {"value": False}
        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"anthropic": "Anthropic"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"anthropic": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: key_present["value"])

        def _fake_write_env_file(_path, values):
            key_present["value"] = bool(values.get("ANTHROPIC_API_KEY"))

        monkeypatch.setattr(prov, "_write_env_file", _fake_write_env_file)
        monkeypatch.setattr(prov, "invalidate_models_cache", lambda: None)
        monkeypatch.setattr(prov, "invalidate_account_usage_status_cache", lambda _provider_id=None: None)

        try:
            before = prov.get_providers()
            result = prov.set_provider_key("anthropic", "sk-test-12345678")
            after = prov.get_providers()

            before_anthropic = next(p for p in before["providers"] if p["id"] == "anthropic")
            after_anthropic = next(p for p in after["providers"] if p["id"] == "anthropic")
            assert result["ok"] is True
            assert before_anthropic["has_key"] is False
            assert after_anthropic["has_key"] is True
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_oauth_credential_updates_invalidate_providers_cache(self, monkeypatch, tmp_path):
        """OAuth credential updates should invalidate cached Providers responses (#6010)."""
        from api import oauth
        from api import providers as prov

        invalidated_credentials = []
        providers_invalidated = []
        monkeypatch.setattr(config, "invalidate_credential_pool_cache", invalidated_credentials.append)
        monkeypatch.setattr(prov, "invalidate_providers_cache", lambda: providers_invalidated.append(True))

        oauth._persist_codex_credentials(
            tmp_path,
            {"access_token": "access-token", "refresh_token": "refresh-token"},
        )

        assert invalidated_credentials == ["openai-codex"]
        assert providers_invalidated == [True]

    def test_returns_list_of_known_providers(self, monkeypatch, tmp_path):
        """GET /api/providers should return a list of all known providers."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            assert "providers" in result
            assert "active_provider" in result
            assert isinstance(result["providers"], list)
            # Should include at least the built-in providers
            provider_ids = {p["id"] for p in result["providers"]}
            assert "anthropic" in provider_ids
            assert "openai" in provider_ids
            assert "openrouter" in provider_ids
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_provider_entries_have_required_fields(self, monkeypatch, tmp_path):
        """Each provider entry should have id, display_name, has_key, configurable."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            for p in result["providers"]:
                assert "id" in p, f"Missing 'id' in provider entry"
                assert "display_name" in p, f"Missing 'display_name' for {p['id']}"
                assert "has_key" in p, f"Missing 'has_key' for {p['id']}"
                assert "configurable" in p, f"Missing 'configurable' for {p['id']}"
                assert "key_source" in p, f"Missing 'key_source' for {p['id']}"
                assert isinstance(p["has_key"], bool)
                assert isinstance(p["configurable"], bool)
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_oauth_providers_not_configurable(self, monkeypatch, tmp_path):
        """OAuth providers (copilot, nous, openai-codex) should not be configurable."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            for p in result["providers"]:
                if p["id"] in ("copilot", "nous", "openai-codex"):
                    assert p["configurable"] is False, f"{p['id']} should not be configurable"
                # ollama-cloud is now configurable (uses OLLAMA_API_KEY)
                if p["id"] == "ollama-cloud":
                    assert p["configurable"] is True, "ollama-cloud should be configurable"
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_openai_codex_provider_card_prefers_live_catalog(self, monkeypatch, tmp_path):
        """OpenAI Codex provider cards should not advertise stale static fallback models.

        /api/models already uses hermes_cli/Codex cache discovery for Codex.  The
        provider card should share that source order so rejected stale entries
        such as gpt-5.5-mini are not presented as currently available when the
        live account catalog excludes them (#1807).
        """
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        fake_models = sys.modules["hermes_cli.models"]
        fake_models.provider_model_ids = lambda pid: (
            ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]
            if pid == "openai-codex"
            else []
        )
        codex_home = tmp_path / "empty-codex-home"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": "openai-codex", "default": "gpt-5.5"}
        config.cfg["providers"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            codex = next(p for p in result["providers"] if p["id"] == "openai-codex")
            model_ids = [m["id"] for m in codex["models"]]
            assert model_ids == [
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex",
                "gpt-5.2",
            ]
            assert "gpt-5.5-mini" not in model_ids
            assert codex["models_total"] == len(model_ids)
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime


class TestSetProviderKey:
    """Unit tests for set_provider_key() function."""

    def test_set_key_writes_to_env_file(self, monkeypatch, tmp_path):
        """Setting a key should write the env var to ~/.hermes/.env."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        # Also pin HERMES_HOME so code that reads it directly gets tmp_path,
        # not the conftest session TEST_STATE_DIR that bleeds into the main process.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            result = set_provider_key("anthropic", "sk-ant-test-key-12345678")
            assert result["ok"] is True
            assert result["provider"] == "anthropic"
            assert result["action"] == "updated"

            # Verify .env file was written
            env_path = tmp_path / ".env"
            assert env_path.exists(), f".env not written to {env_path}; HERMES_HOME={__import__('os').environ.get('HERMES_HOME')!r}"
            content = env_path.read_text()
            assert "ANTHROPIC_API_KEY=sk-ant-test-key-12345678" in content
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_remove_key_deletes_from_env_file(self, monkeypatch, tmp_path):
        """Removing a key should delete the env var from .env."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            # First set a key
            set_provider_key("anthropic", "sk-ant-test-key-12345678")
            # Then remove it
            result = set_provider_key("anthropic", None)
            assert result["ok"] is True
            assert result["action"] == "removed"

            # Verify .env file no longer has the key
            env_path = tmp_path / ".env"
            content = env_path.read_text() if env_path.exists() else ""
            assert "ANTHROPIC_API_KEY" not in content
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_oauth_provider_rejected(self, monkeypatch, tmp_path):
        """Setting a key for an OAuth provider should fail."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            result = set_provider_key("copilot", "some-key")
            assert result["ok"] is False
            assert "OAuth" in result["error"]
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_short_key_rejected(self, monkeypatch, tmp_path):
        """API keys shorter than 8 chars should be rejected."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            result = set_provider_key("anthropic", "short")
            assert result["ok"] is False
            assert "too short" in result["error"]
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_empty_provider_id_rejected(self, monkeypatch, tmp_path):
        """Empty provider ID should be rejected."""
        from api.providers import set_provider_key
        result = set_provider_key("", "some-key")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_newline_in_key_rejected(self, monkeypatch, tmp_path):
        """API keys with newlines should be rejected."""
        from api.providers import set_provider_key
        result = set_provider_key("anthropic", "sk-ant-key\nINJECTED=evil")
        assert result["ok"] is False
        assert "newline" in result["error"]


class TestRemoveProviderKey:
    """Unit tests for remove_provider_key() wrapper."""

    def test_clean_provider_key_uses_late_bound_config_path(self, monkeypatch, tmp_path):
        """Config cleanup must honor api.config._get_config_path monkeypatches.

        PR #1597 fixed provider-key cleanup by resolving the config path through
        the api.config module at call time. If the implementation goes back to
        the function imported into api.providers at module load, this test cleans
        stale_config instead of active_config.
        """
        import yaml

        import api.config as cfg_mod
        import api.providers as providers

        stale_config = tmp_path / "stale-config.yaml"
        active_config = tmp_path / "active-config.yaml"
        stale_config.write_text(
            "providers:\n  openai:\n    api_key: stale-secret\n",
            encoding="utf-8",
        )
        active_config.write_text(
            "providers:\n  openai:\n    api_key: active-secret\nmodel:\n  provider: openai\n  api_key: active-model-secret\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(providers, "_get_config_path", lambda: stale_config, raising=False)
        monkeypatch.setattr(cfg_mod, "_get_config_path", lambda: active_config)
        monkeypatch.setattr(providers, "reload_config", lambda: None)

        providers._clean_provider_key_from_config("openai")

        stale = yaml.safe_load(stale_config.read_text(encoding="utf-8"))
        active = yaml.safe_load(active_config.read_text(encoding="utf-8"))
        assert stale["providers"]["openai"]["api_key"] == "stale-secret"
        assert "api_key" not in active["providers"]["openai"]
        assert active["model"] == {"provider": "openai"}

    def test_clean_custom_provider_key_matches_safe_name_slug(self, monkeypatch, tmp_path):
        """Custom-provider key removal must match the canonical safe name slug."""
        import yaml

        import api.config as cfg_mod
        import api.providers as providers

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({
                "custom_providers": [{
                    "name": "Local (127.0.0.1:15721)",
                    "base_url": "http://127.0.0.1:15721/v1",
                    "api_key": "${LOCAL_PORT_API_KEY}",
                    "model": "deepseek-v4-flash",
                }],
            }),
            encoding="utf-8",
        )

        monkeypatch.setattr(cfg_mod, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(providers, "reload_config", lambda: None)

        providers._clean_provider_key_from_config("custom:local-127.0.0.1-15721")

        reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        custom_provider = reloaded["custom_providers"][0]
        assert custom_provider["name"] == "Local (127.0.0.1:15721)"
        assert "api_key" not in custom_provider

    def test_remove_provider_key_calls_set_with_none(self, monkeypatch, tmp_path):
        """remove_provider_key should delegate to set_provider_key(id, None)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import remove_provider_key
        try:
            result = remove_provider_key("anthropic")
            assert result["ok"] is True
            assert result["action"] == "removed"
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime


# ── Integration tests (via HTTP endpoints) ───────────────────────────────


class TestProvidersEndpoints:
    """Integration tests for /api/providers HTTP endpoints."""

    def test_get_providers_returns_200(self):
        """GET /api/providers should return 200 with provider list."""
        result = _get("/api/providers")
        assert "providers" in result
        assert isinstance(result["providers"], list)

    def test_post_provider_set_key(self):
        """POST /api/providers with provider + api_key should set the key."""
        body, status = _post("/api/providers", {
            "provider": "anthropic",
            "api_key": "sk-ant-integration-test-key-12345678",
        })
        assert status == 200
        assert body.get("ok") is True
        assert body.get("provider") == "anthropic"

    def test_post_provider_remove_key(self):
        """POST /api/providers with provider but no api_key should remove the key."""
        body, status = _post("/api/providers", {
            "provider": "anthropic",
            "api_key": None,
        })
        assert status == 200
        assert body.get("ok") is True
        assert body.get("action") == "removed"

    def test_post_provider_delete(self):
        """POST /api/providers/delete should remove the key."""
        body, status = _post("/api/providers/delete", {
            "provider": "anthropic",
        })
        assert status == 200
        assert body.get("ok") is True

    def test_post_provider_missing_id(self):
        """POST /api/providers without provider should return 400."""
        body, status = _post("/api/providers", {"api_key": "some-key"})
        assert status == 400
        assert "required" in body.get("error", "").lower()

    def test_post_provider_delete_missing_id(self):
        """POST /api/providers/delete without provider should return 400."""
        body, status = _post("/api/providers/delete", {})
        assert status == 400


class TestIssue1410OllamaEnvVarBleed:
    """Regression: Ollama Cloud key must not flip local Ollama to has_key=True.

    Both providers used to share OLLAMA_API_KEY in _PROVIDER_ENV_VAR. After
    a user added a key for Ollama Cloud, the local Ollama card also lit up
    "API key configured" — incorrect because the runtime in
    hermes_cli/runtime_provider.py only consumes OLLAMA_API_KEY when the
    base URL hostname is ollama.com. Local Ollama is keyless by default.

    Fix: drop bare "ollama" from _PROVIDER_ENV_VAR so the env-var check is
    only applied to ollama-cloud. Local Ollama users who genuinely need a
    key can still set providers.ollama.api_key in config.yaml.
    """

    def test_ollama_local_not_configured_when_only_cloud_env_var_set(
        self, monkeypatch, tmp_path,
    ):
        """OLLAMA_API_KEY in env should mark ollama-cloud configured but not bare ollama."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("OLLAMA_API_KEY", "sk-cloud-key-xyz")

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert "ollama-cloud" in by_id, "ollama-cloud should appear in provider list"
            assert "ollama" in by_id, "ollama (local) should appear in provider list"
            assert by_id["ollama-cloud"]["has_key"] is True, \
                "ollama-cloud should be has_key=True when OLLAMA_API_KEY is set"
            assert by_id["ollama"]["has_key"] is False, (
                "ollama (local) must NOT be has_key=True when only the cloud env "
                "var is set — local Ollama is keyless and shares no env var with "
                "Ollama Cloud (#1410)."
            )
            # ollama-cloud should be configurable, but local ollama should not
            # (it has no env var mapping — keys go through providers.ollama.api_key
            # in config.yaml if the user explicitly opts in).
            assert by_id["ollama-cloud"]["configurable"] is True
            assert by_id["ollama"]["configurable"] is False
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_ollama_local_still_configured_via_config_yaml(
        self, monkeypatch, tmp_path,
    ):
        """providers.ollama.api_key in config.yaml should still mark local ollama configured."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        # Important: clear the env var so the only signal is config.yaml.
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        config.cfg["providers"] = {"ollama": {"api_key": "local-token-abc"}}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert by_id["ollama"]["has_key"] is True, (
                "Local Ollama users with providers.ollama.api_key in config.yaml "
                "should still report configured (#1410 fix must not regress this)."
            )
            # And ollama-cloud should NOT be configured by ollama's config entry.
            assert by_id["ollama-cloud"]["has_key"] is False
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
