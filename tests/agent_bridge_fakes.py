"""Shared fakes for the agent config bridge (``api/agent_config_bridge.py``).

This lives in a plain helper module rather than in a test module because two
test files need it. It used to sit in ``tests/test_agent_config_bridge.py`` and
be pulled into ``tests/test_issue538_mcp_management.py`` with a module-level
``pytest_plugins`` declaration. That coupling is fragile: the target is also
collected as a test module, so whether its fixtures are visible depends on
plugin-registration and import order rather than on anything the importing file
states. A slice that collected the importer without registering the plugin
reported ``fixture 'fake_agent' not found`` and errored out exactly the two
tests that prove the ``.env``/masked-placeholder round trip.

Importing this module by its package-qualified name gives both files the same
fake with no collection-order dependency.
"""
from __future__ import annotations

import copy
import sys
import types
from contextvars import ContextVar

# Which home the agent's context-local override currently points at. A
# ContextVar (not a plain attribute) so a thread-per-profile isolation test
# behaves the way the real `hermes_constants` override does: each thread gets
# its own value and cannot clobber another thread's scope.
_ACTIVE_HOME: ContextVar[str | None] = ContextVar("fake_agent_active_home", default=None)

_DEFAULT_HOME = "__default__"


class FakeAgent:
    """Builds fake agent modules and records calls against them.

    By default every home shares one store, which is what the single-profile
    tests want. Set ``per_home = True`` to give each ``scoped_agent_home()``
    target its own ``config``/``env`` store — that is how a cross-profile
    isolation test proves one profile's write cannot land in another's.

    Failure injection: set ``load_error``, ``save_error`` or ``env_error`` to an
    exception instance and the corresponding agent call raises it. Set
    ``validation_issues`` to a list to make ``validate_mcp_server_entry`` reject.
    """

    def __init__(self):
        self.saved_configs = []
        self.override_calls = []
        self.env_saves = []
        self.per_home = False
        self.load_error: BaseException | None = None
        self.save_error: BaseException | None = None
        self.env_error: BaseException | None = None
        self.validation_issues: list[str] | None = None
        self._homes: dict[str, dict] = {}

        hermes_constants = types.ModuleType("hermes_constants")

        def set_hermes_home_override(path):
            self.override_calls.append(("set", str(path)))
            return _ACTIVE_HOME.set(str(path))

        def reset_hermes_home_override(token):
            self.override_calls.append(("reset", token))
            # A real token from `ContextVar.set`; tolerate a foreign token so a
            # test that stubs the override differently does not explode here.
            try:
                _ACTIVE_HOME.reset(token)
            except (ValueError, TypeError):
                _ACTIVE_HOME.set(None)

        hermes_constants.set_hermes_home_override = set_hermes_home_override
        hermes_constants.reset_hermes_home_override = reset_hermes_home_override

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []  # mark as package

        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.load_config = self._load_config
        config_mod.save_config = self._save_config
        config_mod.save_env_value = self._save_env_value

        security_mod = types.ModuleType("hermes_cli.mcp_security")
        security_mod.validate_mcp_server_entry = self._validate_mcp_server_entry

        mcp_mod = types.ModuleType("hermes_cli.mcp_config")
        mcp_mod._save_bearer_auth_token = self._save_bearer_auth_token

        self.modules = {
            "hermes_constants": hermes_constants,
            "hermes_cli": hermes_cli,
            "hermes_cli.config": config_mod,
            "hermes_cli.mcp_security": security_mod,
            "hermes_cli.mcp_config": mcp_mod,
        }

    # ── store plumbing ──────────────────────────────────────────────────

    def _key(self) -> str:
        if not self.per_home:
            return _DEFAULT_HOME
        return _ACTIVE_HOME.get() or _DEFAULT_HOME

    def _store(self, key: str | None = None) -> dict:
        return self._homes.setdefault(
            key if key is not None else self._key(), {"config": {}, "env": {}}
        )

    def store_for(self, home) -> dict:
        """The ``{"config":…, "env":…}`` store for one home (per-home mode)."""
        return self._store(str(home))

    @property
    def config_store(self) -> dict:
        return self._store(_DEFAULT_HOME)["config"]

    @config_store.setter
    def config_store(self, value) -> None:
        self._store(_DEFAULT_HOME)["config"] = dict(value or {})

    @property
    def env_values(self) -> dict:
        return self._store(_DEFAULT_HOME)["env"]

    @env_values.setter
    def env_values(self, value) -> None:
        self._store(_DEFAULT_HOME)["env"] = dict(value or {})

    # ── faked agent surface ─────────────────────────────────────────────

    def _load_config(self):
        if self.load_error is not None:
            raise self.load_error
        # Deep copy: the bridge mutates the loaded mapping in place
        # (`config.setdefault("mcp_servers", {})[name] = …`). A shallow copy
        # would let that mutation reach the store before `save_config` runs,
        # so a test could not tell a real write from an aborted one.
        return copy.deepcopy(self._store()["config"])

    def _save_config(self, config, **kwargs):
        if self.save_error is not None:
            raise self.save_error
        self.saved_configs.append(copy.deepcopy(config))
        self._store()["config"] = copy.deepcopy(config)

    def _save_env_value(self, key, value):
        if self.env_error is not None:
            raise self.env_error
        self._store()["env"][key] = value
        self.env_saves.append((self._key(), key, value))

    def _validate_mcp_server_entry(self, name, entry):
        if self.validation_issues is not None:
            return list(self.validation_issues)
        return ["suspicious command"] if entry.get("command") == "evil" else []

    def _save_bearer_auth_token(self, name, token):
        if not str(token).strip():
            raise ValueError("Bearer token is required")
        key = f"MCP_{name.upper().replace('-', '_')}_API_KEY"
        self._save_env_value(key, token)
        return {"Authorization": f"Bearer ${{{key}}}"}


def activate_fake_agent(monkeypatch, tmp_path, fake: FakeAgent | None = None) -> FakeAgent:
    """Install *fake* into ``sys.modules`` and point the bridge at it.

    Mirrors what the ``fake_agent`` fixture does, but callable directly so a
    test can build several fakes or activate one mid-test.
    """
    from api import agent_config_bridge as bridge

    fake = fake if fake is not None else FakeAgent()
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
    for name, module in fake.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
    monkeypatch.setattr(bridge, "_import_state", None, raising=False)
    return fake
