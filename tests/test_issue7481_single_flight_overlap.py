"""Tests for #7481 review — single-flight dedup for active ≡ named overlap.

When the active model.base_url matches a named custom_providers entry, the
catalog rebuild must probe that endpoint exactly once and populate both the
named group AND auto_detected_models_by_provider from that single result.

Without this fix, both the named loop and the active-endpoint block probe
the same URL, doubling latency and potentially exceeding the rebuild budget
on cold loads (see #7481 review gate result).
"""

import json
import pathlib
import sys
import urllib.request

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO.parent / ".hermes" / "hermes-agent"))

import api.config as config


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Clear the TTL model cache before and after every test."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _setup_config(tmp_path, yaml_content, monkeypatch):
    """Write a config.yaml and reload."""
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml_content, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    config.reload_config()
    # Patch list_available_providers to avoid real network calls
    try:
        import hermes_cli.models as hm
        monkeypatch.setattr(hm, "list_available_providers", lambda: [])
    except Exception:
        pass


class TestSingleFlightOverlap:
    """Active endpoint ≡ named custom provider must probe exactly once."""

    def test_single_probe_when_active_matches_named(self, tmp_path, monkeypatch):
        """When model.base_url equals a named custom provider's base_url,
        only one HTTP probe should be made (not two)."""
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "live-model-1"}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            requests.append(req.full_url)
            return Response()

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:ollama-local\n"
                "  base_url: http://localhost:11434/v1\n"
                "  api_key: local-key\n"
                "custom_providers:\n"
                "  - name: ollama-local\n"
                "    base_url: http://localhost:11434/v1\n"
                "    api_key: local-key\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        # Exactly one probe — not two
        assert len(requests) == 1, (
            f"Expected exactly 1 probe for active=named overlap, got {len(requests)}: {requests}"
        )
        assert "http://localhost:11434/v1/models" in requests[0]

        # The named group should contain the live model
        groups = result.get("groups", [])
        named_group = next((g for g in groups if g.get("provider_id") == "custom:ollama-local"), None)
        assert named_group is not None, "custom:ollama-local group must exist"
        model_ids = [m["id"] for m in named_group.get("models", [])]
        assert "live-model-1" in model_ids, f"live-model-1 must appear in named group, got {model_ids}"

    def test_single_flight_when_named_has_configured_models_and_matches_active(self, tmp_path, monkeypatch):
        """When a named provider has configured models AND matches active,
        the named probe is skipped (single-flight), but the active block
        still probes once. Total: 1 probe, not 2."""
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "live-model"}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            requests.append(req.full_url)
            return Response()

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:my-local\n"
                "  base_url: http://localhost:8080/v1\n"
                "custom_providers:\n"
                "  - name: my-local\n"
                "    base_url: http://localhost:8080/v1\n"
                "    models:\n"
                "      - configured-model-a\n"
                "      - configured-model-b\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        # Active block probes once; named block is skipped (single-flight).
        # Total: 1 probe (active block only).
        assert len(requests) == 1, f"Expected 1 probe, got {len(requests)}: {requests}"

        groups = result.get("groups", [])
        named_group = next((g for g in groups if g.get("provider_id") == "custom:my-local"), None)
        assert named_group is not None
        model_ids = [m["id"] for m in named_group.get("models", [])]
        # Configured models take priority
        assert "configured-model-a" in model_ids
        assert "configured-model-b" in model_ids


class TestOriginalStarvationFix:
    """Original #7481 case: unreachable active, reachable named."""

    def test_reachable_named_not_starved_by_unreachable_active(self, tmp_path, monkeypatch):
        """When the active endpoint is unreachable but a named provider is
        reachable, the named provider's models must appear in the foreground
        response (not just after background completion)."""
        call_count = [0]

        class SuccessResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "named-live-model"}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            url = req.full_url
            # Named provider succeeds
            if "named-server" in url:
                return SuccessResponse()
            # Active endpoint times out (simulated by raising)
            raise TimeoutError("active endpoint unreachable")

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:named-server\n"
                "  base_url: http://unreachable-host:9999/v1\n"
                "custom_providers:\n"
                "  - name: named-server\n"
                "    base_url: http://named-server:8080/v1\n"
                "    api_key: named-key\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        # Named provider should have probed (1 probe for the named endpoint)
        # The active endpoint probe failure is expected but shouldn't block
        groups = result.get("groups", [])
        named_group = next((g for g in groups if g.get("provider_id") == "custom:named-server"), None)
        assert named_group is not None, "custom:named-server group must exist"
        model_ids = [m["id"] for m in named_group.get("models", [])]
        assert "named-live-model" in model_ids, (
            f"named-live-model must appear in foreground response, got {model_ids}"
        )


class _KeyedResponse:
    """Minimal context-manager response body for a single /v1/models probe."""

    def __init__(self, model_id: str):
        self._model_id = model_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps({"data": [{"id": self._model_id}]}).encode("utf-8")


class TestRegateDeferralCredentials:
    """#7481 re-gate: the single-flight deferral must compare more than the URL."""

    def test_same_url_providers_with_different_keys_keep_their_own_live_models(
        self, tmp_path, monkeypatch
    ):
        """Two named providers sharing the active base_url but using DIFFERENT
        API keys must both come back populated, each from a probe made with its
        own key.  Deferring on the URL alone filed the active result under a
        single provider key and starved (or 401'd) the other one."""
        requests: list[tuple[str, str]] = []  # (url, authorization header)

        def fake_urlopen(req, timeout=None):
            auth = req.get_header("Authorization") or ""
            requests.append((req.full_url, auth))
            if auth == "Bearer key-b":
                return _KeyedResponse("team-b-live")
            if auth == "Bearer key-a":
                return _KeyedResponse("team-a-live")
            return _KeyedResponse("wrong-credentials-model")

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:team-a\n"
                "  base_url: http://localhost:8080/v1\n"
                "  api_key: key-a\n"
                "custom_providers:\n"
                "  - name: team-a\n"
                "    base_url: http://localhost:8080/v1\n"
                "    api_key: key-a\n"
                "  - name: team-b\n"
                "    base_url: http://localhost:8080/v1\n"
                "    api_key: key-b\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        groups = result.get("groups", [])
        team_a = next((g for g in groups if g.get("provider_id") == "custom:team-a"), None)
        team_b = next((g for g in groups if g.get("provider_id") == "custom:team-b"), None)
        assert team_a is not None, "custom:team-a group must exist"
        assert team_b is not None, "custom:team-b group must exist"

        a_ids = [m["id"].removeprefix("@custom:team-a:") for m in team_a.get("models", [])]
        b_ids = [m["id"].removeprefix("@custom:team-b:") for m in team_b.get("models", [])]
        assert "team-a-live" in a_ids, (
            f"team-a must keep its own live models (probed with key-a), got {a_ids}"
        )
        assert "team-b-live" in b_ids, (
            f"team-b must keep its own live models (probed with key-b), got {b_ids}"
        )
        assert "wrong-credentials-model" not in a_ids + b_ids, (
            "no group may carry models fetched with someone else's credentials"
        )

        # Every probe carried a real key: one with key-a, one with key-b.
        auths = [auth for _url, auth in requests]
        assert auths.count("Bearer key-a") == 1, (
            f"team-a must be probed exactly once with key-a, got {requests}"
        )
        assert auths.count("Bearer key-b") == 1, (
            f"team-b must be probed exactly once with key-b, got {requests}"
        )
        assert len(requests) == 2, f"expected one probe per credential, got {requests}"

    def test_named_group_with_configured_model_merges_live_models(self, tmp_path, monkeypatch):
        """A named group that already carries a configured singular ``model``
        entry must still absorb the live /v1/models result — master returns the
        live model plus the configured entry, not the configured entry alone."""
        requests: list[str] = []

        def fake_urlopen(req, timeout=None):
            requests.append(req.full_url)
            return _KeyedResponse("live-from-endpoint")

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:single-model\n"
                "  base_url: http://localhost:11434/v1\n"
                "  api_key: local-key\n"
                "custom_providers:\n"
                "  - name: single-model\n"
                "    base_url: http://localhost:11434/v1\n"
                "    api_key: local-key\n"
                "    model: configured-single-model\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        groups = result.get("groups", [])
        named_group = next(
            (g for g in groups if g.get("provider_id") == "custom:single-model"), None
        )
        assert named_group is not None, "custom:single-model group must exist"
        model_ids = [m["id"] for m in named_group.get("models", [])]

        assert "configured-single-model" in model_ids, (
            f"configured model entry must survive, got {model_ids}"
        )
        assert "live-from-endpoint" in model_ids, (
            f"live model must merge into the configured group, got {model_ids}"
        )
        assert len(model_ids) == len(set(model_ids)), f"deduplicate by id, got {model_ids}"
        # Single-flight still holds: the configured entry must not add a probe.
        assert len(requests) == 1, f"expected exactly 1 probe, got {requests}"
