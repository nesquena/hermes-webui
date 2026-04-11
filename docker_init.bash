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

whoami=`whoami`
script_dir=$(dirname $0)
script_name=$(basename $0)
echo ""; echo ""
echo "======================================"
echo "=================== Starting script (ID: $$)"
echo "== Running ${script_name} in ${script_dir} as ${whoami}"
script_fullname=$0
echo "  - script_fullname: ${script_fullname}"
ignore_value="VALUE_TO_IGNORE"

# everyone can read our files by default
umask 0022

# Write a world-writeable file (preferably inside /tmp -- ie within the container)
write_worldtmpfile() {
  tmpfile=$1
  if [ -z "${tmpfile}" ]; then error_exit "write_worldfile: missing argument"; fi
  if [ -f $tmpfile ]; then rm -f $tmpfile; fi
  echo -n $2 > ${tmpfile}
  chmod 777 ${tmpfile}
}

itdir=/tmp/hermeswebui_init
if [ ! -d $itdir ]; then mkdir $itdir; chmod 777 $itdir; fi
if [ ! -d $itdir ]; then error_exit "Failed to create $itdir"; fi

# Set user and group id
# logic: if not set and file exists, use file value, else use default. Create file for persistence when the container is re-run
# reasoning: needed when using docker compose as the file will exist in the stopped container, and changing the value from environment variables or configuration file must be propagated from hermeswebuitoo to hermeswebuitoo transition (those values are the only ones loaded before the environment variables dump file are loaded)
it=$itdir/hermeswebui_user_uid
if [ -z "${WANTED_UID+x}" ]; then
  if [ -f $it ]; then WANTED_UID=$(cat $it); fi
fi
WANTED_UID=${WANTED_UID:-1024}
write_worldtmpfile $it "$WANTED_UID"
echo "-- WANTED_UID: \"${WANTED_UID}\""

it=$itdir/hermeswebui_user_gid
if [ -z "${WANTED_GID+x}" ]; then
  if [ -f $it ]; then WANTED_GID=$(cat $it); fi
fi
WANTED_GID=${WANTED_GID:-1024}
write_worldtmpfile $it "$WANTED_GID"
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

# hermeswebuitoo is a specfiic user not existing by default on ubuntu, we can check its whomai
if [ "A${whoami}" == "Ahermeswebuitoo" ]; then 
  echo "-- Running as hermeswebuitoo, will switch hermeswebui to the desired UID/GID"
  # The script is started as hermeswebuitoo -- UID/GID 1025/1025

  # We are altering the UID/GID of the hermeswebui user to the desired ones and restarting as that user
  # using usermod for the already create hermeswebui user, knowing it is not already in use
  # per usermod manual: "You must make certain that the named user is not executing any processes when this command is being executed"
  sudo groupmod -o -g ${WANTED_GID} hermeswebui || error_exit "Failed to set GID of hermeswebui user"
  sudo usermod -o -u ${WANTED_UID} hermeswebui || error_exit "Failed to set UID of hermeswebui user"
  sudo chown -R ${WANTED_UID}:${WANTED_GID} /home/hermeswebui || error_exit "Failed to set owner of /home/hermeswebui"
  save_env /tmp/hermeswebuitoo_env.txt  
  # restart the script as hermeswebui set with the correct UID/GID this time
  echo "-- Restarting as hermeswebui user with UID ${WANTED_UID} GID ${WANTED_GID}"
  sudo su hermeswebui $script_fullname || error_exit "subscript failed"
  ok_exit "Clean exit"
fi

# If we are here, the script is started as another user than hermeswebuitoo
# because the whoami value for the hermeswebui user can be any existing user, we can not check against it
# instead we check if the UID/GID are the expected ones
if [ "$WANTED_GID" != "$new_gid" ]; then error_exit "hermeswebui MUST be running as UID ${WANTED_UID} GID ${WANTED_GID}, current UID ${new_uid} GID ${new_gid}"; fi
if [ "$WANTED_UID" != "$new_uid" ]; then error_exit "hermeswebui MUST be running as UID ${WANTED_UID} GID ${WANTED_GID}, current UID ${new_uid} GID ${new_gid}"; fi

########## 'hermeswebui' specific section below

# We are therefore running as hermeswebui
echo ""; echo "== Running as hermeswebui"

# Load environment variables one by one if they do not exist from /tmp/hermeswebuitoo_env.txt
it=/tmp/hermeswebuitoo_env.txt
if [ -f $it ]; then
  echo "-- Loading not already set environment variables from $it"
  load_env $it true
fi

##
echo ""; echo "-- Making sure /app is owned by the hermeswebui user to avoid permission issues when running the server "
sudo mkdir -p /app || error_exit "Failed to create /app directory"
sudo chown hermeswebui:hermeswebui /app || error_exit "Failed to set owner of /app to hermeswebui user"
sudo rsync -av --chown=hermeswebui:hermeswebui /apptoo/ /app/ || error_exit "Failed to sync /apptoo to /app with correct ownership"
it=/app/.testfile; touch $it || error_exit "Failed to verify /app directory"
rm -f $it || error_exit "Failed to delete test file in /app"

######## Environment variables (consume AFTER the load_env)

echo ""; echo "== Checking required environment variables for hermes-webui"

echo ""; echo "-- HERMES_WEBUI_VERSION: Where to store sessions, workspaces, and other state (default: ~/.hermes/webui-mvp)"
if [ -z "${HERMES_WEBUI_STATE_DIR+x}" ]; then error_exit "HERMES_WEBUI_STATE_DIR not set"; fi; 
echo "-- HERMES_WEBUI_STATE_DIR: $HERMES_WEBUI_STATE_DIR"
if [ ! -d "$HERMES_WEBUI_STATE_DIR" ]; then mkdir -p $HERMES_WEBUI_STATE_DIR || error_exit "Failed to create state directory at $HERMES_WEBUI_STATE_DIR"; fi
if [ ! -d "$HERMES_WEBUI_STATE_DIR" ]; then error_exit "HERMES_WEBUI_STATE_DIR directory does not exist at $HERMES_WEBUI_STATE_DIR"; fi
it="$HERMES_WEBUI_STATE_DIR/.testfile"; touch $it || error_exit "Failed to verify state directory at $HERMES_WEBUI_STATE_DIR"
rm -f $it || error_exit "Failed to delete test file in $HERMES_WEBUI_STATE_DIR"

echo ""; echo "-- HERMES_WEBUI_DEFAULT_WORKSPACE: Default workspace directory shown on first launch"
if [ -z "${HERMES_WEBUI_DEFAULT_WORKSPACE+x}" ]; then echo "HERMES_WEBUI_DEFAULT_WORKSPACE not set, setting to /workspace"; export HERMES_WEBUI_DEFAULT_WORKSPACE="/workspace"; fi;
echo "-- HERMES_WEBUI_DEFAULT_WORKSPACE: $HERMES_WEBUI_DEFAULT_WORKSPACE"
if [ ! -d "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then mkdir -p $HERMES_WEBUI_DEFAULT_WORKSPACE || error_exit "Failed to create default workspace at $HERMES_WEBUI_DEFAULT_WORKSPACE"; fi
if [ ! -d "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]; then error_exit "HERMES_WEBUI_DEFAULT_WORKSPACE directory does not exist at $HERMES_WEBUI_DEFAULT_WORKSPACE"; fi
it="$HERMES_WEBUI_DEFAULT_WORKSPACE/.testfile"; touch $it || error_exit "Failed to verify default workspace at $HERMES_WEBUI_DEFAULT_WORKSPACE"
rm -f $it || error_exit "Failed to delete test file in $HERMES_WEBUI_DEFAULT_WORKSPACE"

echo ""; echo "==================="
echo ""; echo "== Installing uv and creating a new virtual environment for hermes-webui"

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/home/hermeswebui/.local/bin/:$PATH"
export UV_PROJECT_ENVIRONMENT=venv

export UV_CACHE_DIR=/uv_cache
sudo mkdir -p ${UV_CACHE_DIR} || error_exit "Failed to create /uv_cache directory"
sudo chown hermeswebui:hermeswebui ${UV_CACHE_DIR} || error_exit "Failed to set owner of ${UV_CACHE_DIR} to hermeswebui user"

cd /app
uv venv venv
export VIRTUAL_ENV=/app/venv
test -d /app/venv
test -f /app/venv/bin/activate

echo "";echo "== Activating hermes webui's virtual environment"
source /app/venv/bin/activate || error_exit "Failed to activate hermeswebui virtual environment"
test -x /app/venv/bin/python3

echo ""; echo "== Installing hermes-webui dependencies"
uv pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
uv pip install -U pip setuptools --trusted-host pypi.org --trusted-host files.pythonhosted.org
test -x /app/venv/bin/pip

echo ""; echo "== Adding hermes-agent's pyproject.toml base dependencies to the virtual environment"
uv pip install /home/hermeswebui/.hermes/hermes-agent --trusted-host pypi.org --trusted-host files.pythonhosted.org || error_exit "Failed to install hermes-agent's requirements"

echo ""; echo "== Running hermes-webui"
cd /app; python server.py || error_exit "hermes-webui failed or exited with an error"

# we should never be here because the server should be running indefinitely, but if we are, we exit safely
ok_exit "Clean exit"
