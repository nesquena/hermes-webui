"""#9225: the credential pool owns BOTH halves of the outbound pair.

The model-catalog path resolves a record's declared credential and, when the
record has no ``base_url`` of its own, fills the missing endpoint from the
record's credential-pool entry. Those two halves must come from the SAME
authority:

* record declares its own endpoint -> the record's credential may go with it;
* endpoint comes from the pool    -> the credential must come from that same
  pool entry.

Pairing the record's credential with the pool's endpoint sends a key the record
declared for ANOTHER authority's URL. The compatibility spelling
(``api_key_env``, #8959) made that path newly reachable: before it, the record
side resolved nothing for these rows, the pool filled both halves, and no
foreign credential was delivered.

This test observes the REAL outbound requests: two loopback endpoints record the
exact Authorization header they receive. It fails on the un-repaired head
(endpoint A receives beta's ``SHARED_KEY``) and passes once the pool's endpoint
is paired with the pool's own credential.
"""
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api.config as config

SHARED_KEY = "sk-test-shared-key"
POOL_KEY = "sk-test-pool-key"


class _Endpoint:
    """One loopback endpoint; records every Authorization header it receives."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server contract)
                outer.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization")}
                )
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


class _PoolEntry:
    def __init__(self, base_url, runtime_api_key):
        self.base_url = base_url
        self.runtime_api_key = runtime_api_key


class _Pool:
    def __init__(self, entry):
        self._entry = entry

    def select(self):
        return self._entry


def _install_fakes(monkeypatch, pool_entry):
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
    fake_pool_mod.load_pool = lambda provider_id: _Pool(pool_entry)
    fake_agent.credential_pool = fake_pool_mod
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_pool_mod)

    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda provider_id: True)


def _boot_catalog(tmp_path, monkeypatch, custom_providers, cache_rel="models_cache.json"):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        json.dumps({"model": {"default": "dummy-model"}, "custom_providers": custom_providers}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / cache_rel)
    config.reload_config()
    with config._available_models_cache_lock:
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._cache_build_in_progress = False
    config.get_available_models(force_refresh=True)


def test_pool_endpoint_is_only_probed_with_the_pools_own_credential(tmp_path, monkeypatch):
    """Endpoint A is the pool's; it must see the pool key, never beta's key.

    alpha declares NO ``base_url``, so its endpoint can only come from the pool
    entry. That pool entry also owns the credential that may accompany it.
    beta declares its own endpoint B and the SAME env var (``SHARED_KEY``), so
    ``SHARED_KEY`` is beta's credential — it must not reach A.
    """
    monkeypatch.setenv("SHARED_KEY", SHARED_KEY)
    endpoint_a = _Endpoint()
    endpoint_b = _Endpoint()
    try:
        _install_fakes(
            monkeypatch,
            _PoolEntry(base_url=endpoint_a.base_url, runtime_api_key=POOL_KEY),
        )
        _boot_catalog(
            tmp_path,
            monkeypatch,
            [
                {"name": "alpha", "api_key_env": "SHARED_KEY"},
                {"name": "beta", "base_url": endpoint_b.base_url, "key_env": "SHARED_KEY"},
            ],
        )
    finally:
        endpoint_a.close()
        endpoint_b.close()

    # Neutral control: a record-owned endpoint keeps its declared credential.
    assert endpoint_b.auth_headers() == [f"Bearer {SHARED_KEY}"], (
        "the neutral control (record-owned endpoint B + declared key) changed"
    )
    # The regression: A is the pool's, so only the pool's own key may reach it.
    assert endpoint_a.auth_headers() == [f"Bearer {POOL_KEY}"], (
        "the pool's endpoint received a credential the record declared for "
        f"another authority: {endpoint_a.auth_headers()!r}"
    )
    assert f"Bearer {SHARED_KEY}" not in endpoint_a.auth_headers(), (
        "beta's declared credential reached the pool-owned endpoint A"
    )


def test_record_owned_endpoint_keeps_the_records_own_credential(tmp_path, monkeypatch):
    """Neutral control: a record WITH its own endpoint keeps its own credential."""
    monkeypatch.setenv("SHARED_KEY", SHARED_KEY)
    endpoint_a = _Endpoint()
    endpoint_b = _Endpoint()
    try:
        _install_fakes(
            monkeypatch,
            _PoolEntry(base_url=endpoint_b.base_url, runtime_api_key=POOL_KEY),
        )
        _boot_catalog(
            tmp_path,
            monkeypatch,
            [
                {"name": "alpha", "base_url": endpoint_a.base_url, "api_key_env": "SHARED_KEY"},
                {"name": "beta", "base_url": endpoint_b.base_url, "key_env": "SHARED_KEY"},
            ],
        )
    finally:
        endpoint_a.close()
        endpoint_b.close()

    # alpha declares its OWN endpoint, so its declared credential is the right
    # pair for it; the pool is not consulted for either half.
    assert endpoint_a.auth_headers() == [f"Bearer {SHARED_KEY}"]
    assert endpoint_b.auth_headers() == [f"Bearer {SHARED_KEY}"]