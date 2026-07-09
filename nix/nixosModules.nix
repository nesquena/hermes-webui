{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.hermes-webui;
  defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
  defaultStateDir = "/var/lib/hermes-webui";

  protectedEnvironment = [
    "HERMES_WEBUI_HOST"
    "HERMES_WEBUI_PORT"
    "HERMES_WEBUI_STATE_DIR"
    "HERMES_HOME"
    "HERMES_WEBUI_AGENT_DIR"
    "HERMES_WEBUI_PYTHON"
  ];

  defaultUser = "hermes-webui";
  defaultGroup = "hermes-webui";

  protectedEnvironmentFileCheck = pkgs.writeShellScript "hermes-webui-protected-envfile-check" ''
    set -eu
    for env_file in "$@"; do
      [ -f "$env_file" ] || continue
      while IFS= read -r raw_line || [ -n "$raw_line" ]; do
        line=$(printf '%s' "$raw_line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        case "$line" in
          ""|\#*) continue ;;
          export\ *) line=''${line#export } ;;
        esac
        key=''${line%%=*}
        case "$key" in
          HERMES_WEBUI_HOST|HERMES_WEBUI_PORT|HERMES_WEBUI_STATE_DIR|HERMES_HOME|HERMES_WEBUI_AGENT_DIR|HERMES_WEBUI_PYTHON)
            echo "environmentFiles must not set protected WebUI runtime key $key; use module options or extraEnvironment for supported keys." >&2
            exit 1
            ;;
        esac
      done < "$env_file"
    done
  '';

  inferredAgentPython =
    if cfg.agent.package == null then
      null
    else if (cfg.agent.package ? passthru) && (cfg.agent.package.passthru ? hermesVenv) then
      "${cfg.agent.package.passthru.hermesVenv}/bin/python3"
    else
      null;

  inferredAgentDir =
    if cfg.agent.package == null then
      null
    else if (cfg.agent.package ? passthru) && (cfg.agent.package.passthru ? hermesAgentDir) then
      "${cfg.agent.package.passthru.hermesAgentDir}"
    else
      null;

  configuredAgentPython =
    if cfg.agent.python != null then
      cfg.agent.python
    else
      inferredAgentPython;

  mappedEnvironment = (lib.mapAttrsToList
    (name: value: "${name}=${value}")
    ({
      HERMES_WEBUI_HOST = cfg.host;
      HERMES_WEBUI_PORT = toString cfg.port;
      HERMES_WEBUI_STATE_DIR = cfg.stateDir;
    }
    // lib.optionalAttrs (cfg.hermesHome != null) {
      HERMES_HOME = cfg.hermesHome;
    }
    // lib.optionalAttrs (cfg.agent.dir != null) {
      HERMES_WEBUI_AGENT_DIR = cfg.agent.dir;
    }
    // lib.optionalAttrs (cfg.agent.dir == null && inferredAgentDir != null) {
      HERMES_WEBUI_AGENT_DIR = inferredAgentDir;
    }
    // lib.optionalAttrs (configuredAgentPython != null) {
      HERMES_WEBUI_PYTHON = configuredAgentPython;
    }
    // lib.filterAttrs
      (name: _: !(lib.elem name protectedEnvironment))
      cfg.extraEnvironment));

  needsWritableStateDir = cfg.stateDir != defaultStateDir;
  tmpfilesRules = lib.optionals needsWritableStateDir [
    "d ${cfg.stateDir} 0700 ${cfg.user} ${cfg.group} - -"
  ];
in
{
  options.services.hermes-webui = {
    enable = lib.mkEnableOption "Hermes WebUI service";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "self.packages.${pkgs.stdenv.hostPlatform.system}.default";
      description = "Package that provides the `bin/hermes-webui` executable.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = defaultUser;
      description = "User that runs the Hermes WebUI service.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = defaultGroup;
      description = "Group that runs the Hermes WebUI service.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Value for HERMES_WEBUI_HOST.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8787;
      description = "Value for HERMES_WEBUI_PORT.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the configured TCP port in the NixOS firewall.";
    };

    stateDir = lib.mkOption {
      type = lib.types.strMatching "^/.+";
      default = defaultStateDir;
      defaultText = lib.literalExpression ''"/var/lib/hermes-webui"'';
      description = "Value for HERMES_WEBUI_STATE_DIR.";
    };

    hermesHome = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "^/.+");
      default = null;
      description = "Optional value for HERMES_HOME.";
    };

    agent = {
      package = lib.mkOption {
        type = lib.types.nullOr lib.types.package;
        default = null;
        description = "Package to derive HERMES_WEBUI_PYTHON from passthru.hermesVenv and optionally HERMES_WEBUI_AGENT_DIR from passthru.hermesAgentDir.";
      };

      dir = lib.mkOption {
        type = lib.types.nullOr (lib.types.strMatching "^/.+");
        default = null;
        description = "Explicit path for HERMES_WEBUI_AGENT_DIR.";
      };

      python = lib.mkOption {
        type = lib.types.nullOr (lib.types.strMatching "^/.+");
        default = null;
        description = "Explicit path for HERMES_WEBUI_PYTHON when the service must run with agent dependencies from a separately managed environment.";
      };
    };

    environmentFiles = lib.mkOption {
      type = lib.types.listOf (lib.types.strMatching "^/.+");
      default = [ ];
      description = "Paths with extra environment variables for the service, including API keys. Protected WebUI runtime keys from module options are rejected here.";
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Additional environment entries for the service. Required WebUI variables remain enforced.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.hermes-webui = {
      description = "Hermes Web UI service";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig =
        {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          ExecStartPre = lib.optional (cfg.environmentFiles != [ ]) "+${protectedEnvironmentFileCheck} ${lib.escapeShellArgs (map builtins.toString cfg.environmentFiles)}";
          ExecStart = "${cfg.package}/bin/hermes-webui";
          Restart = "on-failure";
          Environment = mappedEnvironment;
          EnvironmentFile = map builtins.toString cfg.environmentFiles;
          StateDirectoryMode = "0700";
          UMask = "0077";
        }
        // lib.optionalAttrs (cfg.stateDir == defaultStateDir) {
          StateDirectory = "hermes-webui";
        };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    users.groups = lib.mkIf (cfg.group == defaultGroup) {
      ${cfg.group} = { };
    };

    users.users = lib.mkIf (cfg.user == defaultUser) {
      ${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.stateDir;
      };
    };

    systemd.tmpfiles.rules = tmpfilesRules;
  };
}
