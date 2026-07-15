{
  description = "Hermes Web UI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      linuxSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      hermesModule = import ./nix/nixosModules.nix { inherit self; };
      packageVersion = self.shortRev or (self.dirtyShortRev or "unstable");
      perSystem = forAllSystems (system: let
        pkgs = import nixpkgs { inherit system; };
        package = import ./nix/packages.nix {
          inherit pkgs;
          version = packageVersion;
        };
        moduleChecks = if builtins.elem system linuxSystems then
          let
            moduleConfig = nixpkgs.lib.nixosSystem {
              inherit system;
              modules = [
                hermesModule
                {
                  services.hermes-webui = {
                    enable = true;
                    package = package;
                    host = "127.0.0.1";
                    port = 8787;
                    stateDir = "/var/lib/hermes-webui";
                    agent.dir = "/var/lib/hermes-agent";
                  };
                }
              ];
            };
            packageOnlyAgentVenv = pkgs.runCommand "hermes-agent-package-only-venv-${system}" { } ''
              mkdir -p "$out/bin"
              touch "$out/bin/python3"
            '';
            packageOnlyAgentPackage = pkgs.runCommand "hermes-agent-package-only-${system}" {
              passthru.hermesVenv = packageOnlyAgentVenv;
            } ''
              touch "$out"
            '';
            packageOnlyModuleConfig = nixpkgs.lib.nixosSystem {
              inherit system;
              modules = [
                hermesModule
                {
                  services.hermes-webui = {
                    enable = true;
                    package = package;
                    agent.package = packageOnlyAgentPackage;
                  };
                }
              ];
            };
            moduleServiceEnvironment = nixpkgs.lib.concatStringsSep "\n" moduleConfig.config.systemd.services.hermes-webui.serviceConfig.Environment;
            envProbe = pkgs.writeText "hermes-webui-nixos-env-${system}.txt" moduleServiceEnvironment;
            packageOnlyServiceEnvironment = nixpkgs.lib.concatStringsSep "\n" packageOnlyModuleConfig.config.systemd.services.hermes-webui.serviceConfig.Environment;
            packageOnlyEnvProbe = pkgs.writeText "hermes-webui-nixos-package-only-env-${system}.txt" packageOnlyServiceEnvironment;
          in
          {
            module-env-mapping = pkgs.runCommand "hermes-webui-nixos-module-${system}" {
              nativeBuildInputs = [ pkgs.coreutils ];
            } ''
              grep -q 'HERMES_WEBUI_HOST=127.0.0.1' ${envProbe}
              grep -q 'HERMES_WEBUI_PORT=8787' ${envProbe}
              grep -q 'HERMES_WEBUI_STATE_DIR=/var/lib/hermes-webui' ${envProbe}
              grep -q 'HERMES_WEBUI_AGENT_DIR=/var/lib/hermes-agent' ${envProbe}
              grep -q 'HERMES_WEBUI_PYTHON=${packageOnlyAgentVenv}/bin/python3' ${packageOnlyEnvProbe}
              ! grep -q 'HERMES_WEBUI_AGENT_DIR=' ${packageOnlyEnvProbe}
              touch "$out"
            '';
            runtime-layout = pkgs.runCommand "hermes-webui-runtime-layout-${system}" {
              nativeBuildInputs = [ pkgs.coreutils ];
            } ''
              test -f ${package}/hermes-webui/bootstrap.py
              test -f ${package}/hermes-webui/server.py
              test -d ${package}/hermes-webui/api
              test -d ${package}/hermes-webui/static
              cd ${package}/hermes-webui
              ${package}/bin/hermes-webui --help >/dev/null
              ${package}/bin/hermes-webui --help 2>&1 | grep -q -- '--foreground'
              PYTHONPATH=${package}/hermes-webui ${pkgs.python3.withPackages (ps: with ps; [ pyyaml cryptography ])}/bin/python3 -c 'import api.config, server; print("runtime imports ok")'
              touch "$out"
            '';
          }
        else
          { };
      in
      {
        packages = {
          hermes-webui = package;
          default = package;
        };

        apps = {
          default = {
            type = "app";
            program = "${package}/bin/hermes-webui";
          };
        };

        checks = moduleChecks // {
          package = package;
        };
      });
    in
    {
      packages = forAllSystems (system: perSystem.${system}.packages);
      apps = forAllSystems (system: perSystem.${system}.apps);
      checks = nixpkgs.lib.genAttrs linuxSystems (system: perSystem.${system}.checks);

      nixosModules = {
        default = hermesModule;
        hermes-webui = hermesModule;
      };
    };
}
