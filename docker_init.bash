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
export ENV_OBFUSCATE_PART="TOKEN API KEY PASSWORD SECRET CREDENTIAL COOKIE SESSION"

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
# Remember where the value came from. Auto-detection must never overwrite an
# identity the operator chose explicitly — including the legitimate value 1024,
# which is also the built-in default and was therefore indistinguishable from
# "unset" (#7027). The source is persisted next to the value because `su` drops
# the environment when this script re-enters as the runtime user, so an explicit
# choice would otherwise look like a detected one on the second pass.
it_source=$itdir/hermeswebui_user_uid_source
_wanted_uid_source=""
if [ -n "${WANTED_UID+x}" ]; then
  _wanted_uid_source=explicit
elif [ -f $it ]; then
  WANTED_UID=$(cat $it)
  if [ -f $it_source ]; then _wanted_uid_source=$(cat $it_source); fi
fi
# Auto-detect from mounted volumes if still unset (#569, #668, #7027).
# On macOS, host UIDs start at 501. Using the wrong UID means the container
# user cannot read the bind-mounted files, making the workspace appear empty.
# In two-container setups (hermes-agent + hermes-webui), the shared hermes-home
# volume may be owned by the agent container's UID — detect from there first.
if [ "$_wanted_uid_source" != "explicit" ] && { [ -z "${WANTED_UID+x}" ] || [ "${WANTED_UID}" = "1024" ]; }; then
  # Priority 1: the configured state dir first (#7027) — in a single-container
  # deploy it is a bind mount by definition, so its owner is the host identity
  # the container has to match — then the hermes-home shared volume, which
  # covers two-container Zeabur/Compose setups (#668)
  for _probe_dir in "${HERMES_WEBUI_STATE_DIR:-/app/data}" "/home/hermeswebui/.hermes" "$HERMES_HOME" "/opt/data"; do
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
if [ "$_wanted_uid_source" != "explicit" ] && { [ -z "${WANTED_UID+x}" ] || [ "${WANTED_UID}" = "1024" ]; }; then
  # Priority 2: /workspace bind-mount — a useful signal when it is actually
  # bind-mounted, but a stock image ships /workspace owned by the build-time
  # 1024, which says nothing about the host, so it stays below the state dir (#7027)
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
write_privtmpfile $it_source "${_wanted_uid_source:-detected}"
echo "-- WANTED_UID: \"${WANTED_UID}\""

it=$itdir/hermeswebui_user_gid
it_source=$itdir/hermeswebui_user_gid_source
_wanted_gid_source=""
if [ -n "${WANTED_GID+x}" ]; then
  _wanted_gid_source=explicit
elif [ -f $it ]; then
  WANTED_GID=$(cat $it)
  if [ -f $it_source ]; then _wanted_gid_source=$(cat $it_source); fi
fi
# Auto-detect GID from mounted volumes to match (#569, #668, #7027)
if [ "$_wanted_gid_source" != "explicit" ] && { [ -z "${WANTED_GID+x}" ] || [ "${WANTED_GID}" = "1024" ]; }; then
  # Priority 1: configured state dir first, then the hermes-home shared volume
  for _probe_dir in "${HERMES_WEBUI_STATE_DIR:-/app/data}" "/home/hermeswebui/.hermes" "$HERMES_HOME" "/opt/data"; do
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
if [ "$_wanted_gid_source" != "explicit" ] && { [ -z "${WANTED_GID+x}" ] || [ "${WANTED_GID}" = "1024" ]; }; then
  # Priority 2: /workspace bind-mount — lower priority for the same reason as UID (#7027)
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
write_privtmpfile $it_source "${_wanted_gid_source:-detected}"
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
    # Read whole lines, then split at the FIRST '=' only. Splitting on every
    # '=' (IFS='=' read -r key value) makes read drop a single trailing '='
    # from values, corrupting secrets such as openssl rand -base64 32 output
    # (44 chars ending in '=').
    while IFS= read -r key; do
      # Skip empty lines and comment lines.
      case "$key" in
        ''|\#*) continue ;;
      esac
      if [[ "$key" == *=* ]]; then
        # Split at the FIRST '=' only. Derive value before shortening key,
        # since ${key%%=*} mutates key. Reusing the existing 'key' read
        # target (rather than a new 'line' var) avoids silently clobbering a
        # user env var named 'line' on the post-su reload.
        value=${key#*=}
        key=${key%%=*}
      else
        # Line without '=': whole line is the key with an empty value.
        value=
      fi
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

# Inline helper for `find ... -exec sh -c "$_chown_tolerating_vanished" ...
# OWNER {} +`. Two-container compose shares ~/.hermes with a running agent
# that replaces files atomically (write temp + rename), so an entry listed by
# find can be gone before chown reaches it (#8106). Only chown's ENOENT for
# that vanished entry is tolerated: any other chown failure (EACCES, EROFS
# outside a pruned path, EIO) still fails the walk.
_chown_tolerating_vanished='
  owner=$1; shift
  chown -h "$owner" "$@" 2>/dev/null && exit 0
  for f in "$@"; do
    err=$(LC_ALL=C chown -h "$owner" "$f" 2>&1) && continue
    case "$err" in *": No such file or directory") continue ;; esac
    printf "%s\n" "$err" >&2
    exit 1
  done
'

# Run find, tolerating only entries that vanish mid-walk.
# -ignore_readdir_race covers entries that vanish before find stats them, but
# find still reports a directory it can no longer open and exits non-zero
# (#8106). So find's own "No such file or directory" diagnostics for entries
# below the root are accepted; any other diagnostic, including the chown
# helper's or one for the root itself, fails the walk.
find_tolerating_vanished() {
  local root="$1" err rc=0 line
  shift
  err=$(LC_ALL=C find "$root" -ignore_readdir_race "$@" 2>&1 >/dev/null) || rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ -n "$err" ] || { echo "find exited with status $rc" >&2; return 1; }
  while IFS= read -r line; do
    case "$line" in
      "find: '$root/"*"': No such file or directory") ;;
      *) printf "%s\n" "$err" >&2; return 1 ;;
    esac
  done <<< "$err"
}

chown_home_hermeswebui() {
  # macOS Docker bind mounts can expose hermes-agent git object packs as
  # read-only host files. The runtime only needs to read those existing objects;
  # requiring chown on them makes startup fail before WebUI can run (#2237).
  #
  # Multi-container compose (#2470) additionally mounts the entire
  # hermes-agent-src volume read-only on the WebUI side because the WebUI only
  # reads it for `uv pip install`. On a :ro mount, chown returns EROFS for any
  # file inside the subtree, which would propagate to `set -e` and kill startup
  # before the WebUI can run. Either way, the WebUI never writes to the agent
  # source — prune the entire hermes-agent path from the chown walk so a
  # read-only or partially-read-only mount doesn't break the rest of the home
  # ownership alignment.
  find_tolerating_vanished /home/hermeswebui \
    -path "/home/hermeswebui/.hermes/hermes-agent" -prune \
    -o -name ".git" -prune \
    -o -exec sh -c "$_chown_tolerating_vanished" chown_home_hermeswebui "${WANTED_UID}:${WANTED_GID}" {} +
}

# usermod -u used to move the old UID's files to the new UID anywhere under
# the home. Its walk is skipped (#8106), so this gives the subtrees that
# chown_home_hermeswebui prunes the same old-UID-only migration: every .git
# directory (git refuses repositories owned by another user), and the
# hermes-agent checkout when it is writable (a local development checkout the
# runtime user keeps working in). Like usermod, only the UID changes. A
# read-only agent source mount is left untouched.
migrate_home_old_uid() {
  local old_uid="$1"
  local agent="/home/hermeswebui/.hermes/hermes-agent"
  local skip_agent=()
  [ -w "$agent" ] || skip_agent=(-path "$agent" -prune -o)
  find_tolerating_vanished /home/hermeswebui "${skip_agent[@]}" \
    -uid "$old_uid" -exec sh -c "$_chown_tolerating_vanished" migrate_home_old_uid "${WANTED_UID}" {} +
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
    # usermod -u also walks the home directory to chown the old UID's files,
    # and aborts on the first entry that vanishes mid-walk — which happens
    # when a running agent replaces files in the shared ~/.hermes (#8106).
    # Pointing the home at a path that does not exist for that one call skips
    # usermod's walk; the real home is restored right after, and
    # chown_home_hermeswebui and migrate_home_old_uid below perform the
    # ownership walks with race-tolerant error handling.
    _usermod_nohome="/nonexistent-hermeswebui-home.$$"
    [ ! -e "$_usermod_nohome" ] || error_exit "Unexpected path $_usermod_nohome exists"
    _old_hermeswebui_uid=$(id -u hermeswebui)
    usermod -o -u "${WANTED_UID}" -d "$_usermod_nohome" hermeswebui || error_exit "Failed to set UID of hermeswebui user"
    usermod -d /home/hermeswebui hermeswebui || error_exit "Failed to restore home directory of hermeswebui user"
  fi

  chown_home_hermeswebui || error_exit "Failed to set owner of /home/hermeswebui"
  if [ -n "${_old_hermeswebui_uid:-}" ] && [ "A${_old_hermeswebui_uid}" != "A${WANTED_UID}" ]; then
    migrate_home_old_uid "$_old_hermeswebui_uid" || error_exit "Failed to move files of UID ${_old_hermeswebui_uid} under /home/hermeswebui to UID ${WANTED_UID}"
  fi

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

  # Preserve Docker --group-add supplemental groups (for example render/video
  # for /dev/dri GPU access) when dropping privileges. `su` rebuilds the target
  # user's groups from /etc/group, so host-passed numeric groups must be made
  # visible to hermeswebui before re-entering as the runtime user.
  for gid in $(id -G); do
    if [ "$gid" = "0" ] || [ "$gid" = "$WANTED_GID" ]; then
      continue
    fi
    group_name="$(getent group "$gid" | cut -d: -f1 || true)"
    if [ -z "$group_name" ]; then
      group_name="hostgpu${gid}"
      groupadd -g "$gid" "$group_name" 2>/dev/null || true
      group_name="$(getent group "$gid" | cut -d: -f1 || true)"
    fi
    if [ -z "$group_name" ]; then
      echo "!! WARNING: Could not create supplemental group for GID $gid; GPU device access may be unavailable"
      continue
    fi
    if [ -n "$group_name" ]; then
      usermod -a -G "$group_name" hermeswebui 2>/dev/null || echo "!! WARNING: Could not add hermeswebui to supplemental group $group_name ($gid)"
    fi
  done

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

# The staged agent source decides which interpreter the venv needs (#8113), so
# it is discovered before the venv is created or reused. Previously this lookup
# lived inside the dependency-install branch below, which meant the venv was
# already built on the base image's interpreter by the time we knew the agent
# required a newer one.
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

# `auto_install_agent_deps()` (api/startup.py) installs from the writable staged
# copy rather than the read-only agent mount, which cannot satisfy setuptools'
# egg_info step under the documented `:ro` compose mounts (#8113). Exported here,
# before the fast-restart guard, so `server.py` inherits it on the recovery path
# even when the staging block below is skipped and the copy already exists.
export HERMES_WEBUI_AGENT_STAGE_DIR="/app/hermes-agent-src"

# Bind a published stage to the Agent it was copied from (#8113). The recovery
# path (`_agent_install_dir()` in api/startup.py) substitutes the staged copy
# for the authorized Agent only when this manifest names that Agent's resolved
# path and the install inputs of both trees still hash to what was recorded
# here — an unrelated or stale directory that merely passes the ownership/mode
# check is ignored. Format: `source=<path>` then `<file>=<sha256|->` lines.
write_agent_stage_manifest() {
  local _src="$1" _stage="$2" _real _name _sum
  _real="$(cd "$_src" && pwd -P)" || return 1
  # The stage is a verbatim copy of the source tree: never write through
  # anything (e.g. a symlink of the same name) the copy brought along.
  rm -f "$_stage/.hermes-webui-stage-source" || return 1
  {
    printf 'source=%s\n' "$_real"
    for _name in pyproject.toml requirements.txt; do
      if [ -f "$_src/$_name" ]; then
        _sum="$(sha256sum < "$_src/$_name")" || return 1
        printf '%s=%s\n' "$_name" "${_sum%% *}"
      else
        printf '%s=-\n' "$_name"
      fi
    done
  } > "$_stage/.hermes-webui-stage-source" || return 1
  chmod 0644 "$_stage/.hermes-webui-stage-source"
}

# ── Rebuilding the agent venv without ever losing a working one (#8113) ─────
# A rebuild is a transaction. The current venv and the staged agent source are
# moved aside; the replacement is created, checked against the staged agent's
# interpreter range and given every dependency; only then are the old copies
# deleted. Any failure puts them back and startup continues on the old venv,
# which is fully installed, exactly as it would have without the rebuild.
_agent_venv=/app/venv
_agent_venv_prev=/app/venv.previous
_agent_stage=/app/hermes-agent-src
_agent_stage_prev=/app/hermes-agent-src.previous
_agent_venv_journal=/app/.agent-venv-rebuild
_venv_helper=/app/scripts/docker_venv_python.py

# Install the staged hermes-agent (editable, every extra) into $VIRTUAL_ENV with
# the agent's own `[tool.uv] override-dependencies` (#8524). `uv pip install`
# never reads a project's [tool.uv] table, and since Agent v0.22.5 the agent's
# requirements only resolve with those overrides (they lift a dependency's old
# Pillow pin to take Pillow 13's security fixes), so a plain install exits with
# "requirements are unsatisfiable". Anything that keeps the overrides file from
# being produced falls back to that plain install, exactly as before.
install_staged_agent() {
  _isa_src="$1"
  _isa_overrides=""
  if [ -f "$_venv_helper" ] && [ -f "$_isa_src/pyproject.toml" ]; then
    # TMPDIR first; then the stage's parent (/app, already written to here), for
    # runtimes whose /tmp is not writable (#1470 read_only, #2010 userns).
    _isa_overrides="$(mktemp "${TMPDIR:-/tmp}/hermes-agent-uv-overrides.XXXXXX" 2>/dev/null)" \
      || _isa_overrides="$(mktemp "$(dirname "$_isa_src")/.hermes-agent-uv-overrides.XXXXXX" 2>/dev/null)" \
      || _isa_overrides=""
    if [ -z "$_isa_overrides" ]; then
      echo "!! Could not create a temporary file for hermes-agent's uv overrides; installing without them"
    elif ! python3 "$_venv_helper" --uv-overrides "$_isa_src/pyproject.toml" "$_isa_overrides"; then
      echo "!! Could not read hermes-agent's [tool.uv] override-dependencies; installing without them"
      rm -f "$_isa_overrides"
      _isa_overrides=""
    fi
    if [ -n "$_isa_overrides" ] && [ ! -s "$_isa_overrides" ]; then
      rm -f "$_isa_overrides"
      _isa_overrides=""
    fi
  fi
  if [ -n "$_isa_overrides" ]; then
    echo "-- Applying hermes-agent's [tool.uv] override-dependencies ($(grep -c . "$_isa_overrides") entries)"
    _isa_rc=0
    uv pip install -e "$_isa_src[all]" --overrides "$_isa_overrides" \
      --trusted-host pypi.org --trusted-host files.pythonhosted.org || _isa_rc=$?
    rm -f "$_isa_overrides"
    return "$_isa_rc"
  fi
  uv pip install -e "$_isa_src[all]" --trusted-host pypi.org --trusted-host files.pythonhosted.org
}

# Install WebUI's requirements and the staged agent into $VIRTUAL_ENV. Returns
# non-zero on any failure instead of exiting, so a rebuild can roll back (a
# normal install below still treats a failure as fatal). set -e does not apply
# inside a function called as a condition, hence the explicit returns.
install_venv_dependencies() {
  echo ""; echo "== Installing hermes-webui dependencies"
  uv pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org || return 1
  uv pip install -U pip setuptools --trusted-host pypi.org --trusted-host files.pythonhosted.org || return 1
  test -x /app/venv/bin/pip || return 1

  echo ""; echo "== Adding hermes-agent's pyproject.toml base dependencies to the virtual environment"
  # `$_agent_src` was already resolved above the venv decision (#8113) — that
  # lookup is what selects the interpreter, so it must not be repeated here.
  if [ -n "$_agent_src" ]; then
    if [ -w "$_agent_src" ]; then
      echo ""
      echo "!! WARNING: hermes-agent source mount is writable from the WebUI container."
      echo "!!   Path: $_agent_src"
      echo "!! The multi-container compose defaults use a read-only mount for defence-in-depth."
      echo "!! If this is not an intentional local development checkout, switch the WebUI"
      echo "!! agent source volume/bind mount to read-only. See docs/rfcs/agent-source-boundary.md."
      echo ""
    fi
    # The agent source can be mounted read-only (see docker-compose.two-container.yml
    # / docker-compose.three-container.yml — the WebUI only reads this volume to
    # install the agent's Python dependencies and never writes to it). setuptools'
    # `egg_info` build step, however, touches `hermes_agent.egg-info/` inside the
    # source tree even under PEP 517 build isolation, which `EROFS`-fails on a
    # `:ro` mount and (under `set -e`) kills startup of every multi-container
    # deploy. Stage the source into a writable directory so the editable install
    # can write its metadata without touching the underlying mount.
    #
    # The staged copy lives under /app/ (persistent across container restarts)
    # because the editable install records this path in a .pth file — Python
    # imports resolve through it at runtime. Placing it alongside the venv gives
    # it the same lifecycle as .deps_installed: both survive restarts and both
    # are lost on container recreation (fresh boot re-stages automatically).
    #
    # The copy excludes any pre-baked `*.egg-info` / `build` / `dist` artifacts
    # to avoid the timestamp-update path setuptools takes when one is present.
    #
    # NB: `rsync -a` / `cp -a` preserve the source tree's mode bits, so a `:ro`
    # source mounted mode 555 leaves the staged copy also mode 555. setuptools
    # then can't create `hermes_agent.egg-info/` next to the package — it dies
    # with "Permission denied" even though `_stage_src` itself was created
    # writable by hermeswebui. Re-add owner-write on the staged tree after the
    # copy so the build dir is genuinely writable, not just owned by us.
    # (HERMES_WEBUI_AGENT_STAGE_DIR is exported above the venv decision so the
    # recovery path inherits it on a fast restart too.)
    _stage_src="/app/hermes-agent-src"
    rm -rf "$_stage_src" || return 1
    mkdir -p "$_stage_src" || return 1
    if command -v rsync >/dev/null 2>&1; then
      rsync -a \
        --exclude='*.egg-info' --exclude='build' --exclude='dist' \
        --exclude='__pycache__' --exclude='.git' \
        --exclude='.playwright' \
        --exclude='.install.lock' \
        "$_agent_src"/ "$_stage_src"/ \
        || { echo "!! Failed to stage hermes-agent source to writable build dir"; return 1; }
    else
      # Fallback when rsync isn't in the image — straight cp -a, then drop
      # the build artifacts that would trip setuptools.
      cp -a "$_agent_src"/. "$_stage_src"/ \
        || { echo "!! Failed to copy hermes-agent source to writable build dir"; return 1; }
      rm -rf "$_stage_src"/*.egg-info "$_stage_src"/build "$_stage_src"/dist 2>/dev/null || true
      rm -rf "$_stage_src"/.playwright 2>/dev/null || true
      rm -rf "$_stage_src"/.install.lock "$_stage_src"/tools/.install.lock 2>/dev/null || true
      find "$_stage_src" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    fi
    chmod -R u+w "$_stage_src" \
      || { echo "!! Failed to make staged hermes-agent source writable (rsync/cp preserved :ro mount perms)"; return 1; }
    # rsync excluded .git; the cp -a fallback copied it, and only now (after the
    # chmod: cp -a keeps a :ro mount's 0555 directories) can it be removed. A
    # stage with .git is never run by select_staged_agent_for_git_checkout.
    rm -rf "$_stage_src/.git" \
      || echo "!! WARNING: could not drop .git from the staged hermes-agent copy; the mounted Agent will be run instead"
    # Never fatal: without the manifest the recovery path simply ignores the
    # stage and falls back to the authorized Agent directory.
    write_agent_stage_manifest "$_agent_src" "$_stage_src" \
      || echo "!! WARNING: could not record which hermes-agent source was staged; the startup recovery installer will not use the staged copy"
    install_staged_agent "$_stage_src" \
      || { echo "!! Failed to install hermes-agent's requirements"; return 1; }
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
  touch /app/venv/.deps_installed || return 1
}

ensure_hindsight_client_docker_dependency() {
  # Keep this outside the .deps_installed fast-restart guard so existing
  # two-container Docker venvs self-heal after this dependency was added.
  # With "soft", a failed install returns non-zero instead of exiting (a
  # venv rebuild rolls back on it).
  _hindsight_client_requirement="hindsight-client>=0.4.22"
  echo ""; echo "== Checking Hindsight memory provider dependency"
  if uv pip show hindsight-client >/dev/null 2>&1; then
    echo "-- hindsight-client already installed"
  else
    echo "-- Installing ${_hindsight_client_requirement} for Hindsight memory provider support"
    if ! uv pip install "${_hindsight_client_requirement}" --trusted-host pypi.org --trusted-host files.pythonhosted.org; then
      if [ "${1:-}" = soft ]; then
        echo "!! Failed to install hindsight-client"
        return 1
      fi
      error_exit "Failed to install hindsight-client"
    fi
  fi
}

# The journal is the transaction's provenance: it is written before anything is
# moved ("pending") and flipped to "committed" before the old copies are
# deleted. Recovery acts only on what a journal says this script did; a
# .previous directory it did not create is never trusted or deleted.
write_agent_venv_journal() {
  printf '%s\n' "$1" > "$_agent_venv_journal.tmp" && mv -f "$_agent_venv_journal.tmp" "$_agent_venv_journal"
}

# Put the moved-aside venv and staged source back. Non-zero only when the
# previous venv could not be restored; the current venv is never swapped for a
# copy that is not a usable venv.
restore_previous_agent_venv() {
  if [ -e "$_agent_venv_prev" ]; then
    [ -e "$_agent_venv_prev/bin/python3" ] || return 1
    rm -rf "$_agent_venv" || return 1
    mv -T "$_agent_venv_prev" "$_agent_venv" || return 1
  fi
  if [ -e "$_agent_stage_prev" ]; then
    # A half-copied replacement can still carry the :ro source's mode 555.
    if [ -e "$_agent_stage" ]; then
      chmod -R u+w "$_agent_stage" 2>/dev/null
      rm -rf "$_agent_stage"
    fi
    if [ -e "$_agent_stage" ] || ! mv -T "$_agent_stage_prev" "$_agent_stage"; then
      # The restored venv's editable install points here: keep the journal so
      # the next start retries, and boot on the venv meanwhile.
      echo "!! WARNING: could not restore the previous staged hermes-agent source at $_agent_stage;"
      echo "!! the agent may not import until the next start retries it."
      return 0
    fi
  fi
  rm -f "$_agent_venv_journal"
  return 0
}

# Delete the old copies. Flipping the journal first makes this restartable: a
# container stopped after it finishes the commit on the next boot instead of
# rolling back onto the old venv.
commit_agent_venv_rebuild() {
  write_agent_venv_journal committed || return 1
  rm -rf "$_agent_stage_prev" "$_agent_venv_prev" || return 0
  rm -f "$_agent_venv_journal"
  return 0
}

# A container stopped mid-rebuild leaves its journal behind: roll back a pending
# rebuild, finish a committed one. Leftovers without a journal are not ours.
recover_interrupted_agent_venv_rebuild() {
  _rj_state=""
  if [ -f "$_agent_venv_journal" ]; then
    _rj_state="$(cat "$_agent_venv_journal" 2>/dev/null || true)"
  fi
  case "$_rj_state" in
    committed)
      echo ""; echo "== Finishing an interrupted virtual environment rebuild"
      { rm -rf "$_agent_stage_prev" "$_agent_venv_prev" && rm -f "$_agent_venv_journal"; } \
        || echo "!! WARNING: could not remove the previous virtual environment at $_agent_venv_prev"
      ;;
    pending)
      if [ -e "$_agent_venv_prev" ] && [ ! -e "$_agent_venv_prev/bin/python3" ]; then
        echo ""; echo "!! WARNING: the virtual environment an interrupted rebuild moved aside is not usable; keeping the current one"
        rm -f "$_agent_venv_journal"
      else
        echo ""; echo "== Restoring the virtual environment an interrupted rebuild moved aside"
        restore_previous_agent_venv || error_exit "Failed to restore the virtual environment at $_agent_venv_prev"
      fi
      ;;
    *)
      if [ -e "$_agent_venv_prev" ] || [ -e "$_agent_stage_prev" ]; then
        echo ""
        echo "!! WARNING: $_agent_venv_prev or $_agent_stage_prev exists but no rebuild of this script left it;"
        echo "!! the current virtual environment is kept as is and will not be rebuilt until it is removed."
      fi
      [ -z "$_rj_state" ] || rm -f "$_agent_venv_journal"
      ;;
  esac
}

# Rebuild the venv on <python> ("-" = uv's default) for the staged agent whose
# pyproject.toml is <pyproject> (may be empty). Returns 0 when the replacement is
# created, admitted and fully installed; 1 when the existing venv was kept or
# put back untouched; 2 when it could not be put back.
rebuild_agent_venv() {
  _rv_py="$1"
  _rv_pyproject="$2"
  if [ -e "$_agent_venv_prev" ] || [ -e "$_agent_stage_prev" ] || [ -e "$_agent_venv_journal" ]; then
    echo "!! A leftover $_agent_venv_prev, $_agent_stage_prev or rebuild journal exists; keeping $_agent_venv unchanged."
    return 1
  fi
  if ! write_agent_venv_journal pending; then
    rm -f "$_agent_venv_journal" "$_agent_venv_journal.tmp"
    echo "!! Could not record the rebuild in $_agent_venv_journal; keeping $_agent_venv unchanged."
    return 1
  fi
  if ! mv -T "$_agent_venv" "$_agent_venv_prev" 2>/dev/null; then
    rm -f "$_agent_venv_journal"
    echo "!! Could not move $_agent_venv aside (a mount point?); keeping it unchanged."
    return 1
  fi
  if [ -e "$_agent_stage" ] && ! mv -T "$_agent_stage" "$_agent_stage_prev" 2>/dev/null; then
    echo "!! Could not move the staged hermes-agent source aside; keeping the existing virtual environment."
    restore_previous_agent_venv || return 2
    return 1
  fi
  export VIRTUAL_ENV="$_agent_venv"
  _rv_ok=0
  if [ "$_rv_py" != "-" ]; then
    UV_PYTHON="$_rv_py" uv venv "$_agent_venv" && _rv_ok=1
  else
    uv venv "$_agent_venv" && _rv_ok=1
  fi
  if [ "$_rv_ok" = 1 ] && [ -n "$_rv_pyproject" ] && [ -f "$_venv_helper" ] \
      && ! python3 "$_venv_helper" --admits "$_rv_pyproject" "$_agent_venv"; then
    echo "!! uv installed an interpreter the staged agent does not accept."
    _rv_ok=0
  fi
  if [ "$_rv_ok" = 1 ]; then
    echo "-- Installing dependencies into the replacement virtual environment"
    if install_venv_dependencies && ensure_hindsight_client_docker_dependency soft && commit_agent_venv_rebuild; then
      return 0
    fi
    echo "!! Could not finish installing the replacement virtual environment."
  fi
  restore_previous_agent_venv || return 2
  return 1
}

recover_interrupted_agent_venv_rebuild

# Ask the staged agent which interpreter it actually requires instead of
# inheriting the base image's. A bare `uv venv` pins whatever /app/venv ends up
# with, and every dependency gated on a newer `python_version` is then silently
# dropped from the install — the container still starts healthy while
# `run_agent` fails to import.
_venv_action="reuse"
_venv_python="-"
if [ -f /app/scripts/docker_venv_python.py ] && command -v python3 >/dev/null 2>&1; then
  _agent_pyproject=""
  if [ -n "$_agent_src" ]; then
    _agent_pyproject="$_agent_src/pyproject.toml"
  fi
  _base_py="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "0.0")"
  # Default to today's behaviour ("reuse") if the helper is unreadable, so a
  # packaging problem can never be what stops the container from booting.
  _venv_plan="$(python3 /app/scripts/docker_venv_python.py "$_agent_pyproject" /app/venv "$_base_py" 2>/dev/null || echo "reuse -")"
  _venv_action="${_venv_plan%% *}"
  _venv_python="${_venv_plan##* }"
  case "$_venv_action" in
    create|reuse|rebuild) ;;
    *) _venv_action="reuse"; _venv_python="-" ;;
  esac
fi

# A venv built on an interpreter the staged agent cannot use can never import
# it, and `.deps_installed` would make the install below skip and pin the
# broken environment across every subsequent restart. Rebuild it as a
# transaction (above), so a failed interpreter fetch or dependency install
# still boots WebUI on the old venv (the readiness check below then reports the
# agent).
if [ "$_venv_action" = "rebuild" ]; then
  echo ""; echo "== Existing virtual environment cannot satisfy the staged agent — rebuilding"
  _rv_rc=0
  rebuild_agent_venv "$_venv_python" "${_agent_pyproject:-}" || _rv_rc=$?
  case "$_rv_rc" in
    0) _venv_action="rebuilt" ;;
    1)
      echo "!! Kept the existing virtual environment: a venv on Python $_venv_python could not be built and installed"
      echo "!! (no network, or uv downloads disabled?). The agent will not import until it can be."
      _venv_action="reuse"
      ;;
    *) error_exit "Failed to restore the previous virtual environment after a failed rebuild" ;;
  esac
fi

if [ "$_venv_action" = "rebuilt" ]; then
  echo "-- Rebuilt the virtual environment on Python $_venv_python"
elif [ "$_venv_action" = "reuse" ] && [ -f /app/venv/bin/python3 ]; then
  echo ""; echo "== Existing virtual environment found — reusing (fast restart)"
else
  echo ""; echo "== Creating new virtual environment"
  if [ "$_venv_python" != "-" ]; then
    # UV_PYTHON makes uv fetch/select that interpreter rather than inheriting
    # the base image's.
    UV_PYTHON="$_venv_python" uv venv venv \
      || error_exit "Failed to create the virtual environment on Python $_venv_python"
  else
    uv venv venv
  fi
fi
export VIRTUAL_ENV=/app/venv
test -d /app/venv
test -f /app/venv/bin/activate

echo "";echo "== Activating hermes webui's virtual environment"
source /app/venv/bin/activate || error_exit "Failed to activate hermeswebui virtual environment"
test -x /app/venv/bin/python3

if [ -f /app/venv/.deps_installed ]; then
  if [ "$_venv_action" = "rebuilt" ]; then
    echo ""; echo "== Dependencies were installed into the rebuilt virtual environment"
  else
    echo ""; echo "== Dependencies already installed — skipping (fast restart)"
  fi
else
  install_venv_dependencies || error_exit "Failed to install hermes-webui dependencies"
fi

ensure_hindsight_client_docker_dependency

# #8113 readiness: before #8113 the container reached "listening on
# https://0.0.0.0:8787" and /health 200 while the agent was unimportable, so a
# dropped core dependency was effectively silent. Import the agent's critical
# dependency with the venv's own interpreter and say so loudly on failure.
# This never aborts startup — the WebUI still serves without the agent.
if [ -n "$_agent_src" ] && [ -f /app/scripts/docker_venv_python.py ] && command -v python3 >/dev/null 2>&1; then
  echo ""; echo "== Verifying the Hermes agent's critical imports"
  _verify_expected="${_venv_python:-0.0}"
  if [ "$_verify_expected" = "-" ]; then _verify_expected="0.0"; fi
  if _verify_detail="$(python3 /app/scripts/docker_venv_python.py --verify /app/venv "$_verify_expected" 2>&1)"; then
    echo "-- Hermes agent critical imports verified"
  else
    echo ""
    echo "!! WARNING: the Hermes agent is installed but is NOT importable."
    echo "!!   ${_verify_detail}"
    echo "!! Agent features (model auto-detection, personality routing, CLI session"
    echo "!! imports) will be unavailable even though this container is healthy."
    echo "!! The usual cause is a dependency set resolved for the wrong Python"
    echo "!! version — see https://github.com/nesquena/hermes-webui/issues/8113"
    echo ""
  fi
fi

# #8529: server.py activates the Agent it discovers before importing anything
# else. In the single-container stack that is the host's own checkout, mounted
# at ~/.hermes/hermes-agent. A git checkout there is what a PM-managed Agent
# (v0.21.6+) treats as a self-managed install: its bootstrap would sync
# dependencies into the host's ~/.hermes/installs and re-exec WebUI into the
# host's store Python, a binary built for the host OS. Run the staged copy
# instead: it is the tree /app/venv's dependencies were installed from, and it
# has no .git, so the Agent's bootstrap leaves the process alone. Agent images
# ship without .git, so the two- and three-container stacks keep the mounted
# Agent; a pre-PM Agent (no hermes_cli/venv_sync.py + pm/environments.py beside
# its hermes_bootstrap.py) never relaunches, so it keeps the mounted checkout too; and an operator-set HERMES_WEBUI_AGENT_DIR
# always wins. A blank or whitespace-only value is unset, as server.py and
# api/config.py treat it.
select_staged_agent_for_git_checkout() {
  local _src="$1" _stage="$2" _override="${HERMES_WEBUI_AGENT_DIR:-}"
  [ -z "${_override//[[:space:]]/}" ] || return 0
  # Only a PM launcher (Agent v0.21.6+) re-execs WebUI: hermes_bootstrap.py alone
  # is also in pre-PM Agents. managed_agent_startup._PM_LAUNCH_MARKERS lists the same files.
  [ -n "$_src" ] && [ -e "$_src/.git" ] || return 0
  [ -f "$_src/hermes_bootstrap.py" ] && [ -f "$_src/hermes_cli/venv_sync.py" ] \
    && [ -f "$_src/pm/environments.py" ] || return 0
  [ -f "$_stage/run_agent.py" ] && [ ! -e "$_stage/.git" ] || return 0
  export HERMES_WEBUI_AGENT_DIR="$_stage"
  echo "-- $_src is a git checkout; running the staged copy at $_stage"
}
select_staged_agent_for_git_checkout "$_agent_src" "$_agent_stage"

echo ""; echo "== Running hermes-webui"
cd /app; python server.py || error_exit "hermes-webui failed or exited with an error"

# we should never be here because the server should be running indefinitely, but if we are, we exit safely
ok_exit "Clean exit"
