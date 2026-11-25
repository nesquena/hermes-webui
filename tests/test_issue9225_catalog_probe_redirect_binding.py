"""#9225: a catalog-probe credential never leaves the origin its record declared.

The model-catalog path probes each custom record's endpoint for ``/v1/models``.
urllib's default redirect handling follows a 3xx and REPLAYS the request
headers — ``Authorization`` included — to the redirect target, another host
included. A record whose endpoint redirects the probe off its own origin
would therefore have its credential delivered to an endpoint NO record
declared. The compatibility spelling (``api_key_env``, #8959/#9225) made
that path newly reachable: before it, a legacy record resolved no
credential, so there was nothing to replay.

These tests observe the REAL outbound requests: the declared endpoint and
the redirect target are separate loopback servers, each recording the exact
Authorization header it receives. On the un-repaired head the record's
credential arrives at the redirect target; with the origin-bound probe
opener a cross-origin 3xx fails the probe instead, and a same-origin
redirect still serves it.

Only synthetic keys are used: no real ``.env`` is read and no credential
value is printed.
"""
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api.config as config

SHARED_KEY = "***"


class _Endpoint:
    """One loopback endpoint; records every Authorization header it receives.

    ``redirect_to`` makes it answer 302 to another endpoint's /models path,
    modelling a record whose endpoint bounces the probe elsewhere.
    """

    def __init__(self, redirect_to=None):
        self.requests = []
        outer = self
        self.redirect_to = redirect_to

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization")}
                )
                if outer.redirect_to and self.path.endswith("/models"):
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"data": [{"id": "probe-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep pytest output clean
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def auth_headers(self):
        return [r["auth"] for r in self.requests if r["auth"]]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _install_fakes(monkeypatch):
    """Fake the surfaces the catalog path imports; no real auth store is read."""
    fake_hermes = types.ModuleType("hermes_cli")
    fake_hermes.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models._PROVIDER_ALIASES = {}
    fake_models.list_available_providers = lambda: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda provider_id: {"logged_in": False, "key_source": ""}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    fake_agent = types.ModuleType("agent")
    fake_agent.__path__ = []
    fake_pool_mod = types.ModuleType("agent.credential_pool")
    fake_pool_mod.load_pool = lambda provider_id: None
    fake_agent.credential_pool = fake_pool_mod
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_pool_mod)

    # No pool in these scenarios: keep the real auth-store probe out of the
    # catalog path so the tests read no real ``.hermes`` state (same isolation
    # as the round-1 pairing test).
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda provider_id: True)


def _boot_catalog(tmp_path, monkeypatch, custom_providers):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        json.dumps({"model": {"default": "dummy-model"}, "custom_providers": custom_providers}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.reload_config()
    with config._available_models_cache_lock:
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._cache_build_in_progress = False
    return config.get_available_models(force_refresh=True)


def test_api_key_env_credential_is_not_replayed_to_a_cross_origin_redirect_target(
    tmp_path, monkeypatch
):
    """The regression: the record's endpoint 302-bounces the probe to a host
    no record declared. The credential must not follow it there.

    alpha declares its own endpoint B and its key with the compatibility
    ``api_key_env`` spelling; D is an undeclared loopback endpoint B
    redirects to. This fails on the un-repaired head (D receives alpha's
    key, replayed by urllib's default redirect handling).
    """
    monkeypatch.setenv("SHARED_KEY", SHARED_KEY)
    target = _Endpoint()
    declared = _Endpoint(redirect_to=target.base_url)
    try:
        _install_fakes(monkeypatch)
        _boot_catalog(
            tmp_path,
            monkeypatch,
            [{"name": "alpha", "base_url": declared.base_url, "api_key_env": "SHARED_KEY"}],
        )
    finally:
        declared.close()
        target.close()

    # Neutral control: the declared endpoint still receives the record's key.
    assert declared.auth_headers() == [f"Bearer {SHARED_KEY}"], (
        "the neutral control (record-owned endpoint keeps its declared key) changed"
    )
    # The regression: the undeclared redirect target must receive nothing.
    assert target.auth_headers() == [], (
        "the record's credential was replayed to a redirect target no record "
        f"declared: {target.auth_headers()!r}"
    )
    assert f"Bearer {SHARED_KEY}" not in target.auth_headers()


def test_key_env_record_credential_is_not_replayed_to_a_cross_origin_redirect_target(
    tmp_path, monkeypatch
):
    """Control for the primary spelling: ``key_env`` binds the same way.

    The redirect-replay hazard predates #9225 for this spelling (frozen
    master shows it too), so this is a control, not the newly reached
    defect — pinned so the opener protects every spelling, not only the
    compatibility one.
    """
    monkeypatch.setenv("SHARED_KEY", SHARED_KEY)
    target = _Endpoint()
    declared = _Endpoint(redirect_to=target.base_url)
    try:
        _install_fakes(monkeypatch)
        _boot_catalog(
            tmp_path,
            monkeypatch,
            [{"name": "alpha", "base_url": declared.base_url, "key_env": "SHARED_KEY"}],
        )
    finally:
        declared.close()
        target.close()

    assert declared.auth_headers() == [f"Bearer {SHARED_KEY}"]
    assert target.auth_headers() == [], (
        f"the key_env record's credential reached the undeclared redirect target: "
        f"{target.auth_headers()!r}"
    )


def test_literal_api_key_record_credential_is_not_replayed_to_a_cross_origin_redirect_target(
    tmp_path, monkeypatch
):
    """Control for the literal spelling: a configured ``api_key`` binds the same way."""
    target = _Endpoint()
    declared = _Endpoint(redirect_to=target.base_url)
    try:
        _install_fakes(monkeypatch)
        _boot_catalog(
            tmp_path,
            monkeypatch,
            [{"name": "alpha", "base_url": declared.base_url, "api_key": SHARED_KEY}],
        )
    finally:
        declared.close()
        target.close()

    assert declared.auth_headers() == [f"Bearer {SHARED_KEY}"]
    assert target.auth_headers() == [], (
        f"the literal-key record's credential reached the undeclared redirect target: "
        f"{target.auth_headers()!r}"
    )


def test_same_origin_redirect_still_serves_the_probe(tmp_path, monkeypatch):
    """Guard, not a defect reproducer: the fix must not break a redirect that
    stays on the declared origin.

    B redirects /v1/models to its own /models; the probe is followed and the
    credential never leaves B. Green before and after the fix — it pins the
    boundary of the origin rule (same origin is still served).
    """
    monkeypatch.setenv("SHARED_KEY", SHARED_KEY)
    declared = _Endpoint(redirect_to=None)
    # The /v1/models probe 302-bounces to another path on the SAME origin.
    declared.redirect_to = f"{declared.base_url}-list"
    try:
        _install_fakes(monkeypatch)
        catalog = _boot_catalog(
            tmp_path,
            monkeypatch,
            [{"name": "alpha", "base_url": declared.base_url, "api_key_env": "SHARED_KEY"}],
        )
    finally:
        declared.close()

    # Both hops stay on the declared endpoint, each carrying the record's key.
    assert declared.auth_headers() == [f"Bearer {SHARED_KEY}"] * len(declared.requests), (
        f"a same-origin redirect lost the credential mid-chain: {declared.auth_headers()!r}"
    )
    # The probe was followed, not failed: the model it serves is discovered.
    probe_models = [
        str(model.get("id") or "")
        for group in catalog.get("groups", [])
        if isinstance(group, dict)
        for model in (group.get("models") or [])
        if isinstance(model, dict)
    ]
    assert any("probe-model" in mid for mid in probe_models), (
        "the same-origin redirect stopped serving the /v1/models probe: "
        f"{probe_models!r}"
    )
