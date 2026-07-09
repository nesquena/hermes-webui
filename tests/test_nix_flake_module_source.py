from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLAKE_NIX = (ROOT / "flake.nix").read_text(encoding="utf-8")
PACKAGES_NIX = (ROOT / "nix" / "packages.nix").read_text(encoding="utf-8")
MODULE_NIX = (ROOT / "nix" / "nixosModules.nix").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_nix_package_installs_runtime_directories_with_stable_names():
    assert 'cp -r "${./../api}" "$out/${runtimeDir}/api"' in PACKAGES_NIX
    assert 'cp -r "${./../static}" "$out/${runtimeDir}/static"' in PACKAGES_NIX


def test_flake_check_covers_runtime_layout_and_wrapper_help():
    assert "runtime-layout" in FLAKE_NIX
    assert "test -d ${package}/hermes-webui/api" in FLAKE_NIX
    assert "test -d ${package}/hermes-webui/static" in FLAKE_NIX
    assert "${package}/bin/hermes-webui --help >/dev/null" in FLAKE_NIX
    assert "import api.config, server" in FLAKE_NIX


def test_nix_package_disables_store_local_venv_fallback():
    assert "--set HERMES_WEBUI_DISABLE_LOCAL_VENV 1" in PACKAGES_NIX


def test_nix_package_version_comes_from_flake_source_revision():
    assert 'version ? "unstable"' in PACKAGES_NIX
    assert 'packageVersion = self.shortRev or (self.dirtyShortRev or "unstable");' in FLAKE_NIX
    assert "version = packageVersion;" in FLAKE_NIX
    assert "0.51.0" not in FLAKE_NIX
    assert "0.51.0" not in PACKAGES_NIX
    assert 'api/_version.py' in PACKAGES_NIX
    assert 'chmod u+w "$out/${runtimeDir}/api"' in PACKAGES_NIX


def test_nixos_module_decouples_agent_dir_from_python_inference():
    assert "agent.python" in MODULE_NIX
    assert "configuredAgentPython" in MODULE_NIX
    assert "configuredAgentPython != null" in MODULE_NIX
    assert "cfg.agent.dir == null && configuredAgentPython" not in MODULE_NIX


def test_nixos_module_uses_explicit_agent_dir_passthru_when_available():
    hardcoded_python_site_packages = "lib/" + "python3.12" + "/site-packages"
    legacy_agent_share_dir = "${cfg.agent.package}" + "/share/" + "hermes-agent"

    assert "cfg.agent.package.passthru ? hermesAgentDir" in MODULE_NIX
    assert "${cfg.agent.package.passthru.hermesAgentDir}" in MODULE_NIX
    assert hardcoded_python_site_packages not in MODULE_NIX
    assert legacy_agent_share_dir not in MODULE_NIX


def test_flake_checks_package_with_only_hermes_venv_metadata():
    assert "packageOnlyAgentPackage" in FLAKE_NIX
    assert "passthru.hermesVenv = packageOnlyAgentVenv;" in FLAKE_NIX
    assert "HERMES_WEBUI_PYTHON=${packageOnlyAgentVenv}/bin/python3" in FLAKE_NIX
    assert "! grep -q 'HERMES_WEBUI_AGENT_DIR=' ${packageOnlyEnvProbe}" in FLAKE_NIX


def test_readme_wires_published_agent_flake_package():
    assert 'hermes-agent.url = "github:NousResearch/hermes-agent";' in README
    assert "agent.package = hermes-agent.packages.${pkgs.stdenv.hostPlatform.system}.default;" in README
    assert 'hermesHome = "/var/lib/hermes/.hermes";' in README


def test_nixos_module_does_not_chown_existing_hermes_home():
    assert "d ${cfg.hermesHome}" not in MODULE_NIX


def test_nixos_module_keeps_webui_state_private_by_default_and_custom_path():
    assert 'StateDirectoryMode = "0700";' in MODULE_NIX
    assert 'UMask = "0077";' in MODULE_NIX
    assert '"d ${cfg.stateDir} 0700 ${cfg.user} ${cfg.group} - -"' in MODULE_NIX
    assert "ReadWritePaths" not in MODULE_NIX
    assert "2770" not in MODULE_NIX


def test_nixos_module_only_creates_default_service_identity():
    assert "cfg.group == defaultGroup" in MODULE_NIX
    assert "cfg.user == defaultUser" in MODULE_NIX
    assert "users.groups.${cfg.group}" not in MODULE_NIX
    assert "users.users.${cfg.user}" not in MODULE_NIX
    assert "createHome = true;" not in MODULE_NIX


def test_nixos_module_defaults_to_loopback_with_explicit_firewall_opt_in():
    nix_section = README.split("### Nix flake and NixOS module", 1)[1].split(
        "### Remote access", 1
    )[0]
    assert 'default = "127.0.0.1";' in MODULE_NIX
    assert "openFirewall" in MODULE_NIX
    assert "networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];" in MODULE_NIX
    assert 'host = "127.0.0.1";' in nix_section
    assert 'Set `host = "0.0.0.0"` and `openFirewall = true`' in nix_section
