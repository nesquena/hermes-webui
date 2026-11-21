"""#9050 items 1 & 2 — the replay backend check must decide from one strict read.

Item 1: ``_session_runs_in_process`` used to call ``_profile_uses_gateway`` and
then ``_require_backend_inputs_read`` — two separate reads of ``.env`` and
``config.yaml``. A half-written config read as local the first time and as the
finished ``webui_chat_backend: gateway`` the second time, so the row was admitted
even though the finished config sends it to the Gateway. The fix reads once and
decides the backend from what that single read returned.

Item 2: a whitespace-only ``HERMES_WEBUI_CHAT_BACKEND`` counts as *set* (before
stripping) in the real resolver, so ``config.yaml`` is not consulted and the
result is local. The helper stripped first, treated it as unset, read a broken
config and raised forever. The fix tests the raw value the way the resolver does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api import delegation_ledger_owner as owner_mod


@pytest.fixture
def world(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    root = (tmp_path / "root").resolve()
    homes = {"default": root}
    for name in ("named", "gwprof"):
        homes[name] = root / "profiles" / name
        homes[name].mkdir(parents=True)
        (homes[name] / "config.yaml").write_text("{}\n")
    (root / "config.yaml").write_text("{}\n")
    sessions: dict[str, str] = {}
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: root if name in (None, "", "default") else profiles._resolve_named_profile_home(name),
    )
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: root)
    monkeypatch.setattr(models, "get_session_profile_readonly", lambda sid: sessions[sid])
    yield {"root": root, "homes": homes, "sessions": sessions}


# ── Item 1: decide the backend from a single strict read ──────────────────────


def test_backend_is_decided_from_a_single_strict_read(world, monkeypatch):
    """One read of each input; the result is fixed by that read, not a second one."""
    import api.config as config

    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    # A .env that defines no backend: present so both read paths touch it, but
    # it never wins, so config.yaml is still consulted.
    (named / ".env").write_text("# no backend here\n")
    cfg = named / "config.yaml"
    cfg.write_text("{}\n")  # first read: half-written, gateway not flushed yet
    config._yaml_file_cache.clear()  # cold read: the gateway path must hit disk

    reads = {"config": 0, "env": 0}
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "config.yaml":
            reads["config"] += 1
            # A second read would see the finished gateway config; a single-read
            # fix never makes it, so the first (local) read decides.
            return "{}\n" if reads["config"] == 1 else "webui_chat_backend: gateway\n"
        if self.name == ".env":
            reads["env"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    assert owner_mod._session_runs_in_process("s-web") is True
    assert reads == {"config": 1, "env": 1}


def test_gateway_config_row_is_left_to_the_gateway(world):
    gwprof = world["homes"]["gwprof"]
    world["sessions"]["s-gw"] = "gwprof"
    (gwprof / "config.yaml").write_text("webui_chat_backend: gateway\n")
    assert owner_mod._session_runs_in_process("s-gw") is False


def test_local_config_row_runs_in_process(world):
    world["sessions"]["s-web"] = "named"
    assert owner_mod._session_runs_in_process("s-web") is True


def test_runner_owned_turn_is_not_in_process(world, monkeypatch):
    world["sessions"]["s-web"] = "named"
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    assert owner_mod._session_runs_in_process("s-web") is False


def test_invalid_profile_name_is_confirmed_false(world):
    world["sessions"]["s-bad"] = "../etc"
    assert owner_mod._session_runs_in_process("s-bad") is False


def test_missing_session_is_confirmed_false(world):
    assert owner_mod._session_runs_in_process("s-gone") is False  # KeyError -> not a WebUI session


def test_unreadable_config_is_undetermined_and_raises(world):
    import api.config as config

    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").chmod(0)
    config._yaml_file_cache.clear()
    try:
        with pytest.raises(OSError):  # chmod 0 -> unreadable -> undetermined
            owner_mod._session_runs_in_process("s-web")
    finally:
        (named / "config.yaml").chmod(0o600)


# ── Item 2: a whitespace-only env is "set" before stripping ───────────────────


def test_whitespace_env_with_broken_config_resolves_local_without_retry(world):
    """A whitespace-only backend env is set (pre-strip), so a broken config is
    never consulted and the row is not retried forever."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / ".env").write_text('HERMES_WEBUI_CHAT_BACKEND="  "\n')
    (named / "config.yaml").write_text("webui_chat_backend: [gateway\n")  # malformed
    assert owner_mod._session_runs_in_process("s-web") is True  # local, no raise


def test_whitespace_env_matches_the_real_resolver(world):
    """The replay decision agrees with ``webui_chat_backend_mode`` on a
    whitespace-only env even against a *valid* gateway config."""
    from api.gateway_chat import webui_chat_backend_mode

    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / ".env").write_text('HERMES_WEBUI_CHAT_BACKEND="  "\n')
    (named / "config.yaml").write_text("webui_chat_backend: gateway\n")
    # Real resolver: the whitespace env is set before stripping -> config ignored -> local.
    assert webui_chat_backend_mode({"webui_chat_backend": "gateway"}, {"HERMES_WEBUI_CHAT_BACKEND": "  "}) == "legacy"
    assert owner_mod._session_runs_in_process("s-web") is True


def test_explicit_gateway_env_wins_over_config(world):
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / ".env").write_text("HERMES_WEBUI_CHAT_BACKEND=gateway\n")
    (named / "config.yaml").write_text("webui_chat_backend: legacy\n")
    assert owner_mod._session_runs_in_process("s-web") is False


def test_broken_config_without_env_is_still_undetermined(world):
    import api.config as config

    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: [gateway\n")  # malformed
    config._yaml_file_cache.clear()
    with pytest.raises(yaml.YAMLError):  # fail-closed unchanged: undetermined, retried by the sweep
        owner_mod._session_runs_in_process("s-web")


# ── Config ${VAR} references keep the normal profile backend meaning ─────────
#
# The normal profile backend selection reads the profile's config through
# get_config_for_profile_home, which expands ${VAR} under the profile's env
# binding: a reference that points at a Gateway backend means Gateway there.
# The single strict read must expand it the same way, or a Gateway-owned
# completion is admitted into local replay (Greptile P1 on #9238).


def test_env_reference_to_gateway_is_not_local_replay(world, monkeypatch):
    """A ${VAR} reference that resolves to Gateway stays with the Gateway."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: ${CHAT_MODE}\n")
    monkeypatch.setenv("CHAT_MODE", "gateway")
    assert owner_mod._session_runs_in_process("s-web") is False


def test_env_reference_to_local_is_local_replay(world, monkeypatch):
    """The positive control: a ${VAR} reference to a local value stays local."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: ${CHAT_MODE}\n")
    monkeypatch.setenv("CHAT_MODE", "legacy")
    assert owner_mod._session_runs_in_process("s-web") is True


def test_profile_dotenv_overrides_the_process_value_for_a_reference(world, monkeypatch):
    """A profile .env value wins over the process env for the reference."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: ${CHAT_MODE}\n")
    (named / ".env").write_text("CHAT_MODE=gateway\n")
    monkeypatch.setenv("CHAT_MODE", "legacy")  # the process value loses to the profile's .env
    assert owner_mod._session_runs_in_process("s-web") is False


def test_env_reference_reads_each_input_once(world, monkeypatch):
    """The expansion rides the single read: one .env, one config.yaml."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: ${CHAT_MODE}\n")
    monkeypatch.setenv("CHAT_MODE", "gateway")

    reads = {"config": 0, "env": 0}
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "config.yaml":
            reads["config"] += 1
        elif self.name == ".env":
            reads["env"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    assert owner_mod._session_runs_in_process("s-web") is False
    assert reads == {"config": 1, "env": 1}


def test_explicit_env_short_circuits_the_config_read(world, monkeypatch):
    """An explicit backend env value wins over config.yaml without reading it."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: ${CHAT_MODE}\n")
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")

    reads = {"config": 0}
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "config.yaml":
            reads["config"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    assert owner_mod._session_runs_in_process("s-web") is False
    assert reads["config"] == 0  # the env already decided the backend


def test_empty_env_value_is_not_a_blank_override(world, monkeypatch):
    """An empty env value is not set: the config's reference still decides."""
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    (named / "config.yaml").write_text("webui_chat_backend: ${CHAT_MODE}\n")
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "")
    monkeypatch.setenv("CHAT_MODE", "gateway")
    assert owner_mod._session_runs_in_process("s-web") is False
