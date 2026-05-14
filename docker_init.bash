#!/bin/bash

set -e

error_exit() {
  echo -n "!! ERROR: "
  echo $*
  echo "!! Exiting script (ID: $$)"
  exit 1
}

ok_exit() {
  echo $*
  echo "++ Exiting script (ID: $$)"
  exit 0
}

## Environment variables loaded when passing environment variables from user to user
# Ignore list: variables to ignore when loading environment variables from user to user
export ENV_IGNORELIST="HOME PWD USER SHLVL TERM OLDPWD SHELL _ SUDO_COMMAND HOSTNAME LOGNAME MAIL SUDO_GID SUDO_UID SUDO_USER CHECK_NV_CUDNN_VERSION VIRTUAL_ENV VIRTUAL_ENV_PROMPT ENV_IGNORELIST ENV_OBFUSCATE_PART"
# Obfuscate part: part of the key to obfuscate when loading environment variables from user to user, ex: HF_TOKEN, ...
export ENV_OBFUSCATE_PART="TOKEN API KEY"

# Check for ENV_IGNORELIST and ENV_OBFUSCATE_PART
if [ -z "${ENV_IGNORELIST+x}" ]; then error_exit "ENV_IGNORELIST not set"; fi
if [ -z "${ENV_OBFUSCATE_PART+x}" ]; then error_exit "ENV_OBFUSCATE_PART not set"; fi

# whoami fails under set -e if the UID has no /etc/passwd entry (k8s runAsUser).
whoami=$(whoami 2>/dev/null || echo "uid-$(id -u)")
script_dir=$(dirname $0)
script_name=$(basename $0)
echo ""; echo ""
echo "======================================"
echo "=================== Starting script (ID: $$)"
echo "== Running ${script_name} in ${script_dir} as ${whoami}"
script_fullname=$0
echo "  - script_fullname: ${script_fullname}"
ignore_value="VALUE_TO_IGNORE"

# Keep init scratch files private to the container user that owns them.
umask 0077

write_privtmpfile() {
  tmpfile=$1
  if [ -z "${tmpfile}" ]; then error_exit "write_privtmpfile: missing argument"; fi
  if [ -f "$tmpfile" ]; then rm -f "$tmpfile"; fi
  printf '%s' "$2" > "$tmpfile"
  chmod 600 "$tmpfile"
}

itdir=/tmp/hermeswebui_init
if [ ! -d "$itdir" ]; then mkdir -p "$itdir"; fi
chmod 700 "$itdir" || error_exit "Failed to secure $itdir"
if [ ! -d "$itdir" ]; then error_exit "Failed to create $itdir"; fi

# Set user and group id
# logic: if not set and file exists, use file value, else use default. Create file for persistence when the container is re-run
# reasoning: needed when using docker compose as the file will exist in the stopped container, and changing the value from environment variables or configuration file must be propagated from the root init phase to the hermeswebui runtime phase
it=$itdir/hermeswebui_user_uid
if [ -z "${WANTED_UID+x}" ]; then
  if [ -f $it ]; then WANTED_UID=$(cat $it); fi
fi
# Auto-detect from mounted volumes if still unset (#569, #668).
# On macOS, host UIDs start at 501. Using the wrong UID means the container
# user cannot read the bind-mounted files, making the workspace appear empty.
# In two-container setups (hermes-agent + hermes-webui), the shared hermes-home
# volume may be owned by the agent container's UID — detect from there first.
if [ -z "${WANTED_UID+x}" ] || [ "${WANTED_UID}" = "1024" ]; then
  # Priority 1: hermes-home shared volume — covers two-container Zeabur/Compose setups (#668)
  for _probe_dir in "/home/hermeswebui/.hermes" "$HERMES_HOME" "/opt/data"; do
    if [ -d "$_probe_dir" ]; then
      _detected_uid=$(stat -c '%u' "$_probe_dir" 2>/dev/null || echo "")
      if [ -n "$_detected_uid" ] && [ "$_detected_uid" != "0" ]; then
        echo "-- Auto-detected UID: $_detected_uid (from $_probe_dir)"
        WANTED_UID=$_detected_uid
        break
      fi
    fi
  done
fi
if [ -z "${WANTED_UID+x}" ] || [ "${WANTED_UID}" = "1024" ]; then
  # Priority 2: /workspace bind-mount — the standard single-container mount point
  if [ -d "/workspace" ]; then
    _detected_uid=$(stat -c '%u' "/workspace" 2>/dev/null || echo "")
    if [ -n "$_detected_uid" ] && [ "$_detected_uid" != "0" ]; then
      echo "-- Auto-detected workspace UID: $_detected_uid (from /workspace)"
      WANTED_UID=$_detected_uid
    fi
  fi
fi
WANTED_UID=${WANTED_UID:-1024}
write_privtmpfile $it "$WANTED_UID"
echo "-- WANTED_UID: \"${WANTED_UID}\""

it=$itdir/hermeswebui_user_gid
if [ -z "${WANTED_GID+x}" ]; then
  if [ -f $it ]; then WANTED_GID=$(cat $it); fi
fi
# Auto-detect GID from mounted volumes to match (#569, #668)
if [ -z "${WANTED_GID+x}" ] || [ "${WANTED_GID}" = "1024" ]; then
  # Priority 1: hermes-home shared volume
  for _probe_dir in "/home/hermeswebui/.hermes" "$HERMES_HOME" "/opt/data"; do
    if [ -d "$_probe_dir" ]; then
      _detected_gid=$(stat -c '%g' "$_probe_dir" 2>/dev/null || echo "")
      if [ -n "$_detected_gid" ] && [ "$_detected_gid" != "0" ]; then
        echo "-- Auto-detected GID: $_detected_gid (from $_probe_dir)"
        WANTED_GID=$_detected_gid
        break
      fi
    fi
  done
fi
if [ -z "${WANTED_GID+x}" ] || [ "${WANTED_GID}" = "1024" ]; then
  # Priority 2: /workspace bind-mount
  if [ -d "/workspace" ]; then
    _detected_gid=$(stat -c '%g' "/workspace" 2>/dev/null || echo "")
    if [ -n "$_detected_gid" ] && [ "$_detected_gid" != "0" ]; then
      echo "-- Auto-detected workspace GID: $_detected_gid (from /workspace)"
      WANTED_GID=$_detected_gid
    fi
  fi
fi
WANTED_GID=${WANTED_GID:-1024}
write_privtmpfile $it "$WANTED_GID"
echo "-- WANTED_GID: \"${WANTED_GID}\""

echo "== Most Environment variables set"

# Check user id and group id
new_gid=`id -g`
new_uid=`id -u`
echo "== user ($whoami)"
echo "  uid: $new_uid / WANTED_UID: $WANTED_UID"
echo "  gid: $new_gid / WANTED_GID: $WANTED_GID"

save_env() {
  tosave=$1
  echo "-- Saving environment variables to $tosave"
  env | sort > "$tosave"
}

load_env() {
  tocheck=$1
  overwrite_if_different=$2
  ignore_list="${ENV_IGNORELIST}"
  obfuscate_part="${ENV_OBFUSCATE_PART}"
  if [ -f "$tocheck" ]; then
    echo "-- Loading environment variables from $tocheck (overwrite existing: $overwrite_if_different) (ignorelist: $ignore_list) (obfuscate: $obfuscate_part)"
    while IFS='=' read -r key value; do
      doit=false
      # checking if the key is in the ignorelist
      for i in $ignore_list; do
        if [[ "A$key" ==  "A$i" ]]; then doit=ignore; break; fi
      done
      if [[ "A$doit" == "Aignore" ]]; then continue; fi
      rvalue=$value
      # checking if part of the key is in the obfuscate list
      doobs=false
      for i in $obfuscate_part; do
        if [[ "A$key" == *"$i"* ]]; then doobs=obfuscate; break; fi
      done
      if [[ "A$doobs" == "Aobfuscate" ]]; then rvalue="**OBFUSCATED**"; fi

      if [ -z "${!key}" ]; then
        echo "  ++ Setting environment variable $key [$rvalue]"
        doit=true
      elif [ "A$overwrite_if_different" == "Atrue" ]; then
        cvalue="${!key}"
        if [[ "A${doobs}" == "Aobfuscate" ]]; then cvalue="**OBFUSCATED**"; fi
        if [[ "A${!key}" != "A${value}" ]]; then
          echo "  @@ Overwriting environment variable $key [$cvalue] -> [$rvalue]"
          doit=true
        else
          echo "  == Environment variable $key [$rvalue] already set and value is unchanged"
        fi
      fi
      if [[ "A$doit" == "Atrue" ]]; then
        export "$key=$value"
      fi
    done < "$tocheck"
  fi
}

# The production image does not ship sudo. The entrypoint starts as root only
# long enough to align the hermeswebui UID/GID with mounted volumes, prepare
# root-owned paths, and then drop privileges for the server process.
if [ "A${whoami}" == "Aroot" ]; then
  echo "-- Running as root for one-time container init; will switch to hermeswebui"

  # We are altering the UID/GID of the hermeswebui user to the desired ones and restarting as that user
  # using usermod for the already created hermeswebui user, knowing it is not already in use
  # per usermod manual: "You must make certain that the named user is not executing any processes when this command is being executed"
  # Guard for read-only root filesystem (podman with read_only=true, issue #1470).
  _readonly_root=false
  if ! sh -c 'test -w /etc/group && test -w /etc/passwd' 2>/dev/null; then
    _readonly_root=true
    echo "  !! Detected read-only root filesystem — /etc/group or /etc/passwd is not writable"
  fi
  if [ "A${_readonly_root}" == "Atrue" ]; then
    _current_hermeswebui_gid=$(id -g hermeswebui 2>/dev/null || echo "")
    _current_hermeswebui_uid=$(id -u hermeswebui 2>/dev/null || echo "")
    if [ "A${_current_hermeswebui_gid}" == "A${WANTED_GID}" ] && [ "A${_current_hermeswebui_uid}" == "A${WANTED_UID}" ]; then
      echo "  -- Skipping groupmod/usermod — hermeswebui already has UID ${WANTED_UID} GID ${WANTED_GID} and root fs is read-only"
    else
      error_exit "Cannot modify /etc/group or /etc/passwd (read-only root fs). Set UID=${_current_hermeswebui_uid} and GID=${_current_hermeswebui_gid} to match, or run without read_only=true. See issue #1470."
    fi
  else
    groupmod -o -g "${WANTED_GID}" hermeswebui || error_exit "Failed to set GID of hermeswebui user"
    usermod -o -u "${WANTED_UID}" hermeswebui || error_exit "Failed to set UID of hermeswebui user"
  fi

  chown -R "${WANTED_UID}:${WANTED_GID}" /home/hermeswebui || error_exit "Failed to set owner of /home/hermeswebui"

  echo ""; echo "-- Preparing /app for the hermeswebui runtime user"
  mkdir -p /app || error_exit "Failed to create /app directory"
  chown hermeswebui:hermeswebui /app || error_exit "Failed to set owner of /app to hermeswebui user"
  rsync -av --chown=hermeswebui:hermeswebui /apptoo/ /app/ || error_exit "Failed to sync /apptoo to /app with correct ownership"

  if [ -z "${HERMES_WEBUI_DEFAULT_WORKSPACE+x}" ]; then export HERMES_WEBUI_DEFAULT_WORKSPACE="/workspace"; fi
  if [ ! -d "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then
    mkdir -p "$HERMES_WEBUI_DEFAULT_WORKSPACE" || error_exit "Failed to create default workspace at $HERMES_WEBUI_DEFAULT_WORKSPACE"
  fi
  if [ ! -d "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then error_exit "HERMES_WEBUI_DEFAULT_WORKSPACE directory does not exist at $HERMES_WEBUI_DEFAULT_WORKSPACE"; fi
  chown hermeswebui:hermeswebui "$HERMES_WEBUI_DEFAULT_WORKSPACE" 2>/dev/null || echo "!! WARNING: Could not chown $HERMES_WEBUI_DEFAULT_WORKSPACE (continuing)"

  export UV_CACHE_DIR=${UV_CACHE_DIR:-/uv_cache}
  mkdir -p "${UV_CACHE_DIR}" || error_exit "Failed to create ${UV_CACHE_DIR} directory"
  chown hermeswebui:hermeswebui "${UV_CACHE_DIR}" || error_exit "Failed to set owner of ${UV_CACHE_DIR} to hermeswebui user"

  chown -R "${WANTED_UID}:${WANTED_GID}" "$itdir" || error_exit "Failed to set owner of $itdir"
  # Issue #2010 — Railway / user-namespaced runtimes: in-container UID 0 may map
  # to a host UID outside the writable subuid range, so /tmp writes fail despite
  # id -u == 0. Probe writability and fall back through $itdir → /app.
  ENV_FILE="/tmp/hermeswebui_root_env.txt"
  if ! ( : > "$ENV_FILE" ) 2>/dev/null; then
    ENV_FILE="${itdir:-/tmp/hermeswebui_init}/hermeswebui_root_env.txt"
    mkdir -p "$(dirname "$ENV_FILE")" 2>/dev/null
    if ! ( : > "$ENV_FILE" ) 2>/dev/null; then
      ENV_FILE="/app/.hermeswebui_root_env"
    fi
    echo "  !! /tmp not writable by root — falling back to $ENV_FILE (user-namespaced runtime?)"
  fi
  save_env "$ENV_FILE"
  chown "${WANTED_UID}:${WANTED_GID}" "$ENV_FILE" || error_exit "Failed to set owner of $ENV_FILE"
  chmod 600 "$ENV_FILE" || error_exit "Failed to secure $ENV_FILE"
  export _HW_ROOT_ENV_PATH="$ENV_FILE"

  # restart the script as hermeswebui set with the correct UID/GID this time
  echo "-- Restarting as hermeswebui user with UID ${WANTED_UID} GID ${WANTED_GID}"
  exec su -s /bin/bash -c "exec \"${script_fullname}\"" hermeswebui || error_exit "subscript failed"
fi

# If we are here, the script is started as an unprivileged runtime user.
# Because the whoami value for the hermeswebui user can be any existing user, we cannot check against it;
# instead we check if the UID/GID are the expected ones.
if [ "$WANTED_GID" != "$new_gid" ]; then error_exit "hermeswebui MUST be running as UID ${WANTED_UID} GID ${WANTED_GID}, current UID ${new_uid} GID ${new_gid}"; fi
if [ "$WANTED_UID" != "$new_uid" ]; then error_exit "hermeswebui MUST be running as UID ${WANTED_UID} GID ${WANTED_GID}, current UID ${new_uid} GID ${new_gid}"; fi

########## 'hermeswebui' specific section below

# We are therefore running as hermeswebui
echo ""; echo "== Running as hermeswebui"

# Load environment variables one by one if they do not exist from the root init phase
tmp_root_env="${_HW_ROOT_ENV_PATH:-/tmp/hermeswebui_root_env.txt}"
if [ -f $tmp_root_env ]; then
  echo "-- Loading not already set environment variables from $tmp_root_env"
  load_env $tmp_root_env true
fi

##
if [ ! -f /app/server.py ] && [ -d /apptoo ]; then
  echo ""; echo "-- Seeding /app from /apptoo (rootless startup)"
  cp -a /apptoo/. /app/ || error_exit "Failed to seed /app from /apptoo (is /app writable by the runtime user?)"
fi

echo ""; echo "-- Verifying /app is writable by the hermeswebui runtime user"
if [ ! -d /app ]; then error_exit "/app directory does not exist"; fi
it=/app/.testfile; touch $it || error_exit "Failed to verify /app directory"
rm -f $it || error_exit "Failed to delete test file in /app"

######## Environment variables (consume AFTER the load_env)

echo ""; echo "== Checking required environment variables for hermes-webui"

echo ""; echo "-- HERMES_WEBUI_STATE_DIR: Where to store sessions, workspaces, and other state (default: ~/.hermes/webui)"
if [ -z "${HERMES_WEBUI_STATE_DIR+x}" ]; then error_exit "HERMES_WEBUI_STATE_DIR not set"; fi; 
echo "-- HERMES_WEBUI_STATE_DIR: $HERMES_WEBUI_STATE_DIR"
if [ ! -d "$HERMES_WEBUI_STATE_DIR" ]; then mkdir -p $HERMES_WEBUI_STATE_DIR || error_exit "Failed to create state directory at $HERMES_WEBUI_STATE_DIR"; fi
if [ ! -d "$HERMES_WEBUI_STATE_DIR" ]; then error_exit "HERMES_WEBUI_STATE_DIR directory does not exist at $HERMES_WEBUI_STATE_DIR"; fi
it="$HERMES_WEBUI_STATE_DIR/.testfile"; touch $it || error_exit "Failed to verify state directory at $HERMES_WEBUI_STATE_DIR"
rm -f $it || error_exit "Failed to delete test file in $HERMES_WEBUI_STATE_DIR"

echo ""; echo "-- HERMES_WEBUI_DEFAULT_WORKSPACE: Default workspace directory shown on first launch"
if [ -z "${HERMES_WEBUI_DEFAULT_WORKSPACE+x}" ]; then echo "HERMES_WEBUI_DEFAULT_WORKSPACE not set, setting to /workspace"; export HERMES_WEBUI_DEFAULT_WORKSPACE="/workspace"; fi;
echo "-- HERMES_WEBUI_DEFAULT_WORKSPACE: $HERMES_WEBUI_DEFAULT_WORKSPACE"
# The root init phase creates/chowns missing bind-mount directories before
# dropping privileges. After that, the runtime user only verifies access.
if [ ! -d "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then
  mkdir -p "$HERMES_WEBUI_DEFAULT_WORKSPACE" || error_exit "Failed to create default workspace at $HERMES_WEBUI_DEFAULT_WORKSPACE"
fi
if [ ! -d "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then error_exit "HERMES_WEBUI_DEFAULT_WORKSPACE directory does not exist at $HERMES_WEBUI_DEFAULT_WORKSPACE"; fi
# Only write-test if the workspace is writable. Read-only bind-mounts (:ro)
# are valid — the workspace is used for browsing, not writing by the server.
if [ -w "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then
  it="$HERMES_WEBUI_DEFAULT_WORKSPACE/.testfile"; touch $it && rm -f $it || echo "!! WARNING: Could not write to $HERMES_WEBUI_DEFAULT_WORKSPACE (continuing)"
else
  echo "-- HERMES_WEBUI_DEFAULT_WORKSPACE is read-only — skipping write check (read-only workspace is supported)"
fi

echo ""; echo "==================="
echo ""; echo "== Installing uv and creating a new virtual environment for hermes-webui"

export PATH="/home/hermeswebui/.local/bin/:$PATH"
if command -v uv &>/dev/null; then
  echo "-- uv already installed ($(uv --version)), skipping download"
else
  echo "-- uv not found, downloading..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || error_exit "Failed to install uv — check network connectivity"
fi
export UV_PROJECT_ENVIRONMENT=venv

export UV_CACHE_DIR=${UV_CACHE_DIR:-/uv_cache}
mkdir -p "${UV_CACHE_DIR}" || error_exit "Failed to create ${UV_CACHE_DIR} directory"
test -w "${UV_CACHE_DIR}" || error_exit "${UV_CACHE_DIR} is not writable by hermeswebui"

cd /app
if [ -f /app/venv/bin/python3 ]; then
  echo ""; echo "== Existing virtual environment found — reusing (fast restart)"
else
  echo ""; echo "== Creating new virtual environment"
  uv venv venv
fi
export VIRTUAL_ENV=/app/venv
test -d /app/venv
test -f /app/venv/bin/activate

echo "";echo "== Activating hermes webui's virtual environment"
source /app/venv/bin/activate || error_exit "Failed to activate hermeswebui virtual environment"
test -x /app/venv/bin/python3

ensure_hindsight_client_docker_dependency() {
  # Keep this outside the .deps_installed fast-restart guard so existing
  # two-container Docker venvs self-heal after this dependency was added.
  _hindsight_client_requirement="hindsight-client>=0.4.22"
  echo ""; echo "== Checking Hindsight memory provider dependency"
  if uv pip show hindsight-client >/dev/null 2>&1; then
    echo "-- hindsight-client already installed"
  else
    echo "-- Installing ${_hindsight_client_requirement} for Hindsight memory provider support"
    uv pip install "${_hindsight_client_requirement}" --trusted-host pypi.org --trusted-host files.pythonhosted.org || error_exit "Failed to install hindsight-client"
  fi
}

if [ -f /app/venv/.deps_installed ]; then
  echo ""; echo "== Dependencies already installed — skipping (fast restart)"
else
  echo ""; echo "== Installing hermes-webui dependencies"
  uv pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
  uv pip install -U pip setuptools --trusted-host pypi.org --trusted-host files.pythonhosted.org
  test -x /app/venv/bin/pip

  echo ""; echo "== Adding hermes-agent's pyproject.toml base dependencies to the virtual environment"
  _agent_paths=(
    "/home/hermeswebui/.hermes/hermes-agent"
    "/opt/hermes"
  )
  _agent_src=""
  for _p in "${_agent_paths[@]}"; do
    if [ -d "$_p" ] && [ -f "$_p/pyproject.toml" ]; then
      _agent_src="$_p"
      break
    fi
  done
  if [ -n "$_agent_src" ]; then
    uv pip install "$_agent_src[all]" --trusted-host pypi.org --trusted-host files.pythonhosted.org || error_exit "Failed to install hermes-agent's requirements"
  else
    echo ""
    echo "!! WARNING: hermes-agent source not found."
    echo "!!   Looked in: ${_agent_paths[0]}"
    echo "!!              ${_agent_paths[1]}"
    echo "!! The WebUI will start with reduced functionality (no model auto-detection,"
    echo "!! no personality routing, no CLI session imports)."
    echo "!! To fix: mount the agent source volume into the container:"
    echo "!!   -v /path/to/hermes-agent:/home/hermeswebui/.hermes/hermes-agent"
    echo "!! Or see the two-container compose example:"
    echo "!!   https://github.com/nesquena/hermes-webui/blob/master/docker-compose.two-container.yml"
    echo ""
  fi
  touch /app/venv/.deps_installed
fi

ensure_hindsight_client_docker_dependency

echo ""; echo "== Running hermes-webui"
cd /app; python server.py || error_exit "hermes-webui failed or exited with an error"

# we should never be here because the server should be running indefinitely, but if we are, we exit safely
ok_exit "Clean exit"
