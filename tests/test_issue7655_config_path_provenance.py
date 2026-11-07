"""#7655 round 7: a profile .env must not repoint installation configuration,
WITHOUT changing the upstream loader contract for HERMES_CONFIG_PATH.

Round 6 protected HERMES_CONFIG_PATH through ``_PROTECTED_ENV_KEYS``. That
broke upstream #8432 round 4 (``76986aef0``): upstream deliberately lets a
profile's .env SET HERMES_CONFIG_PATH, and distinguishes "the operator chose
this" from "a profile chose this" by asking whether the key is in
``_loaded_profile_env_keys`` (``_env_value_is_profile_sourced``). Protecting
the key globally changed a shared, upstream-pinned loader semantic, and
upstream's own test ``test_8432_a_config_path_a_profile_env_chose_is_not_shared``
went red.

So the protection moves to where it belongs: the installation-config RESOLVER.
``_installation_config_path()`` resolves HERMES_CONFIG_PATH from the
INSTALLATION SNAPSHOT -- the operator/launcher env captured once at the
startup boundary, before any profile writer ran -- so a value installed later
by a profile (the loader, the background-worker mirror, or the streaming
turn's ``os.environ.update``) is never followed.

These tests pin both halves of that contract:

  * the loader still projects HERMES_CONFIG_PATH from a profile .env and
    records it in ``_loaded_profile_env_keys`` (upstream contract);
  * the installation resolver still refuses to follow a profile-chosen path
    (the #7611 guarantee, now enforced at the read site).
"""
from __future__ import annotations

import os
from pathlib import Path

from api import profiles as prof


def _write_profile_env(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(body, encoding="utf-8")


def _boot_boundary(monkeypatch, operator_env: dict | None = None) -> None:
    """Drive the REAL startup boundary, then isolate the globals it touched.

    The snapshot is the authority the resolver reads, so a test's operator
    values have to go through the same boundary production does — a value
    written into ``os.environ`` afterwards is, by design, not the operator's.
    """
    saved = {
        "_loaded_profile_env_keys": set(getattr(prof, "_loaded_profile_env_keys", set())),
        "_profile_overridden_env": dict(getattr(prof, "_profile_overridden_env", {})),
        "_INSTALLATION_ENV_SNAPSHOT": dict(getattr(prof, "_INSTALLATION_ENV_SNAPSHOT", {})),
    }
    # Register the restores FIRST, then mutate: monkeypatch.setattr records
    # the value it replaces, so the globals come back exactly as they were.
    for name, value in saved.items():
        monkeypatch.setattr(prof, name, value)
    prof._INSTALLATION_ENV_SNAPSHOT = {}
    for k, v in (operator_env or {}).items():
        monkeypatch.setenv(k, v)
    prof.capture_installation_env_snapshot()


def test_the_loader_still_honours_the_upstream_contract_for_hermes_config_path(
    tmp_path, monkeypatch
):
    """A profile .env MAY set HERMES_CONFIG_PATH — that is upstream behaviour.

    Upstream #8432 round 4 relies on it: ``_env_value_is_profile_sourced``
    asks whether the key is in ``_loaded_profile_env_keys`` to tell a
    profile-chosen path from an operator-chosen one, which only works if
    the loader actually projects the key and records it. Blocking it in
    ``_PROTECTED_ENV_KEYS`` made upstream's
    ``test_8432_a_config_path_a_profile_env_chose_is_not_shared`` fail.
    """
    assert "HERMES_CONFIG_PATH" not in prof._PROTECTED_ENV_KEYS, (
        "HERMES_CONFIG_PATH must not be globally protected: upstream #8432 "
        "round 4 lets a profile .env set it and tells the two apart by the "
        "loader bookkeeping"
    )

    profile_home = tmp_path / "profiles" / "sneaky"
    chosen = tmp_path / "sneaky-config.yaml"
    chosen.write_text("instance_name: hijacked\n", encoding="utf-8")
    _write_profile_env(
        profile_home, f"HERMES_CONFIG_PATH={chosen}\nSOME_OTHER_KEY=x\n"
    )

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "installation.yaml"))
    prev_loaded = prof._loaded_profile_env_keys

    try:
        prof._reload_dotenv(profile_home)

        assert os.environ.get("HERMES_CONFIG_PATH") == str(chosen), (
            "the loader must still project HERMES_CONFIG_PATH: upstream "
            "#8432 round 4 distinguishes a profile-chosen path from an "
            "operator-chosen one by exactly this bookkeeping"
        )
        assert "HERMES_CONFIG_PATH" in (prof._loaded_profile_env_keys or set()), (
            "the key must be recorded as loader state, or upstream cannot "
            "tell that a profile chose it"
        )
        # The unrelated key is projected too, so the loader is not frozen.
        assert os.environ.get("SOME_OTHER_KEY") == "x"
    finally:
        prof._loaded_profile_env_keys = prev_loaded
        monkeypatch.undo()


def test_the_installation_resolver_ignores_a_profile_chosen_config_path(
    tmp_path, monkeypatch
):
    """A profile-chosen HERMES_CONFIG_PATH must not become the installation path.

    The installation-scoped instance label is read from whatever
    ``_installation_config_path()`` resolves to, so following a profile-chosen
    path would let the label differ per profile — exactly the
    profile-scoping #7611 exists to prevent.
    """
    import api.routes as routes

    profile_home = tmp_path / "profiles" / "sneaky"
    chosen = tmp_path / "sneaky-config.yaml"
    chosen.write_text("instance_name: hijacked\n", encoding="utf-8")
    _write_profile_env(profile_home, f"HERMES_CONFIG_PATH={chosen}\n")

    operator_cfg = tmp_path / "installation.yaml"
    operator_cfg.write_text("instance_name: real-deployment\n", encoding="utf-8")

    # The operator's env at the startup boundary.
    _boot_boundary(monkeypatch, {"HERMES_CONFIG_PATH": str(operator_cfg)})

    prev_loaded = prof._loaded_profile_env_keys
    try:
        prof._reload_dotenv(profile_home)
        assert "HERMES_CONFIG_PATH" in (prof._loaded_profile_env_keys or set())

        resolved = routes._installation_config_path()
        assert resolved is not None
        assert Path(resolved) != chosen, (
            "the installation resolver followed a profile-chosen "
            "HERMES_CONFIG_PATH"
        )
        assert Path(resolved) == operator_cfg, (
            "the installation resolver must follow the OPERATOR's path"
        )
    finally:
        prof._loaded_profile_env_keys = prev_loaded
        monkeypatch.undo()


def test_the_installation_resolver_honours_an_operator_chosen_config_path(
    tmp_path, monkeypatch
):
    """An operator-chosen HERMES_CONFIG_PATH is still the installation path.

    The protection must not be so broad that a deployment's explicit
    override stops working — that would break every operator who points
    the config layer at a specific file.
    """
    import api.routes as routes

    operator_cfg = tmp_path / "installation.yaml"
    operator_cfg.write_text("instance_name: real-deployment\n", encoding="utf-8")

    _boot_boundary(monkeypatch, {"HERMES_CONFIG_PATH": str(operator_cfg)})

    resolved = routes._installation_config_path()
    assert resolved is not None
    assert Path(resolved) == operator_cfg
    monkeypatch.undo()


def test_the_installation_resolver_ignores_a_launcher_named_profile_value(
    tmp_path, monkeypatch
):
    """A value the NAMED launch home's .env set is that profile's, not the
    operator's — upstream's own rule, fail closed.

    ``_env_value_is_profile_sourced`` treats a value equal to what the named
    launch home's .env set as profile-sourced even when the launcher also
    exported it. The installation resolver must apply the same rule.
    """
    import api.routes as routes

    launch_home = tmp_path / "profiles" / "launch"
    operator_cfg = tmp_path / "installation.yaml"
    operator_cfg.write_text("instance_name: real-deployment\n", encoding="utf-8")

    _boot_boundary(monkeypatch, {"HERMES_CONFIG_PATH": str(operator_cfg)})
    monkeypatch.setattr(
        prof, "_INITIAL_HERMES_HOME_DOTENV",
        {"HERMES_CONFIG_PATH": str(operator_cfg)},
    )
    monkeypatch.setattr(prof, "_INITIAL_HERMES_HOME", str(launch_home))
    monkeypatch.setattr(
        prof, "get_hermes_home_for_profile", lambda n: tmp_path / "root"
    )

    resolved = routes._installation_config_path()
    assert resolved is not None
    assert Path(resolved) != operator_cfg, (
        "a HERMES_CONFIG_PATH that a named launch home's .env set was treated "
        "as the operator's"
    )
    monkeypatch.undo()


def test_the_instance_name_stays_protected_from_a_profile_dotenv(
    tmp_path, monkeypatch
):
    """HERMES_WEBUI_INSTANCE_NAME is a #7611 key with no upstream contract.

    Unlike HERMES_CONFIG_PATH, upstream never reads it, so protecting it
    breaks nothing: the loader must keep ignoring a profile's attempt to
    set it, and the operator's value must survive.
    """
    assert "HERMES_WEBUI_INSTANCE_NAME" in prof._PROTECTED_ENV_KEYS

    profile_home = tmp_path / "profiles" / "sneaky"
    _write_profile_env(profile_home, "HERMES_WEBUI_INSTANCE_NAME=spoofed\n")

    prev_loaded = prof._loaded_profile_env_keys
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "real-deployment")
    try:
        prof._reload_dotenv(profile_home)
        assert os.environ.get("HERMES_WEBUI_INSTANCE_NAME") == "real-deployment"
        assert "HERMES_WEBUI_INSTANCE_NAME" not in (
            prof._loaded_profile_env_keys or set()
        )
    finally:
        prof._loaded_profile_env_keys = prev_loaded
        monkeypatch.undo()
