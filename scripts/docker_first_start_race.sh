#!/usr/bin/env bash
# Disposable Docker proof for first-start ownership alignment (#8106).
#
# Two-container compose shares ~/.hermes with an agent that replaces files
# atomically (temp file + rename) while the WebUI container is still running
# its root init. The init's ownership walks must survive entries vanishing
# mid-walk, while still failing on real chown errors.
#
# Usage: scripts/docker_first_start_race.sh IMAGE [RACE_ROUNDS]
#
# Everything lives in named Docker volumes and is inspected with
# `docker exec`, so the script also works against a remote Docker daemon.
# Exit code 0 means every case matched its expectation; anything else fails.
set -euo pipefail

IMAGE="${1:?usage: $0 IMAGE [RACE_ROUNDS]}"
RACE_ROUNDS="${2:-8}"
SEED_FILES="${SEED_FILES:-20000}"
OLD_ID=1024
NEW_ID=1002
PREFIX="hermes-race-$$"

# Shared across cases so each first start does not redownload dependencies.
CACHE_VOL="${PREFIX}-uvcache"
CREATED_CONTAINERS=()
CREATED_VOLUMES=("$CACHE_VOL")

cleanup() {
  local rc=$?
  for c in "${CREATED_CONTAINERS[@]}"; do
    docker container rm -f "$c" >/dev/null 2>&1 || true
  done
  for v in "${CREATED_VOLUMES[@]}"; do
    docker volume rm -f "$v" >/dev/null 2>&1 || true
  done
  return $rc
}
trap cleanup EXIT

docker volume create "$CACHE_VOL" >/dev/null

# Create a hermes-home volume holding SEED_FILES files owned by the image's
# build-time UID, so both ownership walks have real work to do.
new_home_volume() {
  local vol="$1"
  CREATED_VOLUMES+=("$vol")
  docker volume create "$vol" >/dev/null
  docker container run --rm -v "$vol":/h --entrypoint bash "$IMAGE" -c \
    "mkdir -p /h/seed /h/churn && cd /h/seed && seq 1 $SEED_FILES | xargs touch && chown -R $OLD_ID:$OLD_ID /h"
}

# Keep replacing files in the shared home the way the agent gateway does:
# write a temp file, then rename it over the final name. Runs as root so the
# writes keep going after the WebUI init has re-owned the directory.
start_churn() {
  local name="$1" vol="$2"
  CREATED_CONTAINERS+=("$name")
  docker container run -d --name "$name" -v "$vol":/h \
    --entrypoint bash "$IMAGE" -c \
    'i=0; while :; do i=$(( (i + 1) % 200 )); echo x > /h/churn/.tmp.$i; mv -f /h/churn/.tmp.$i /h/churn/f$i; done' \
    >/dev/null
}

start_webui() {
  local name="$1"; shift
  CREATED_CONTAINERS+=("$name")
  docker container run -d --name "$name" \
    -e WANTED_UID="$NEW_ID" -e WANTED_GID="$NEW_ID" \
    -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui \
    -v "$CACHE_VOL":/uv_cache \
    "$@" "$IMAGE" >/dev/null
}

# Wait for /health or for the container to exit. Returns 0 when healthy.
wait_ready() {
  local name="$1" attempts=0
  while [ "$attempts" -lt 120 ]; do
    if [ "$(docker container inspect -f '{{.State.Running}}' "$name")" != "true" ]; then
      return 1
    fi
    if docker container exec "$name" bash /apptoo/scripts/lib/health_probe.sh localhost 8787 /health 2 >/dev/null 2>&1; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 5
  done
  return 1
}

fail() {
  echo "FAIL: $*"
  exit 1
}

# Readiness plus the identity and ownership the init promised.
assert_ready() {
  local name="$1"
  if ! wait_ready "$name"; then
    docker container logs --tail 80 "$name" 2>&1 || true
    fail "$name did not reach /health"
  fi
  local uid gid home stale
  uid="$(docker container exec "$name" id -u hermeswebui)"
  gid="$(docker container exec "$name" id -g hermeswebui)"
  home="$(docker container exec "$name" getent passwd hermeswebui | cut -d: -f6)"
  [ "$uid:$gid" = "$NEW_ID:$NEW_ID" ] || fail "$name runs as $uid:$gid, expected $NEW_ID:$NEW_ID"
  [ "$home" = "/home/hermeswebui" ] || fail "$name has home $home, expected /home/hermeswebui"
  stale="$(docker container exec "$name" find /home/hermeswebui/.hermes/seed ! -uid "$NEW_ID" -print -quit)"
  [ -z "$stale" ] || fail "$name left $stale with the old owner"
  if docker container logs "$name" 2>&1 | grep -E '!! ERROR|!! Exiting script'; then
    fail "$name logged an init error"
  fi
}

echo "== normal first start"
new_home_volume "${PREFIX}-normal"
start_webui "${PREFIX}-normal" -v "${PREFIX}-normal":/home/hermeswebui/.hermes
assert_ready "${PREFIX}-normal"
echo "ok"

echo "== restart keeps the aligned identity"
docker container restart "${PREFIX}-normal" >/dev/null
assert_ready "${PREFIX}-normal"
docker container rm -f "${PREFIX}-normal" >/dev/null
echo "ok"

for round in $(seq 1 "$RACE_ROUNDS"); do
  echo "== first start racing atomic renames ($round/$RACE_ROUNDS)"
  vol="${PREFIX}-race$round"
  new_home_volume "$vol"
  start_churn "${vol}-churn" "$vol"
  sleep 2
  start_webui "$vol" -v "$vol":/home/hermeswebui/.hermes
  assert_ready "$vol"
  docker container rm -f "$vol" "${vol}-churn" >/dev/null
  echo "ok"
done

echo "== read-only agent source stays pruned"
new_home_volume "${PREFIX}-ro"
agent_vol="${PREFIX}-ro-agent"
CREATED_VOLUMES+=("$agent_vol")
docker volume create "$agent_vol" >/dev/null
docker container run --rm -v "$agent_vol":/a --entrypoint bash "$IMAGE" -c \
  "echo src > /a/README && chown -R $OLD_ID:$OLD_ID /a"
start_webui "${PREFIX}-ro" -v "${PREFIX}-ro":/home/hermeswebui/.hermes \
  -v "$agent_vol":/home/hermeswebui/.hermes/hermes-agent:ro
assert_ready "${PREFIX}-ro"
docker container rm -f "${PREFIX}-ro" >/dev/null
echo "ok"

echo "== a real chown failure still stops startup"
new_home_volume "${PREFIX}-denied"
denied_vol="${PREFIX}-denied-extra"
CREATED_VOLUMES+=("$denied_vol")
docker volume create "$denied_vol" >/dev/null
docker container run --rm -v "$denied_vol":/a --entrypoint bash "$IMAGE" -c \
  "echo x > /a/file && chown -R $OLD_ID:$OLD_ID /a"
start_webui "${PREFIX}-denied" -v "${PREFIX}-denied":/home/hermeswebui/.hermes \
  -v "$denied_vol":/home/hermeswebui/readonly-extra:ro
if wait_ready "${PREFIX}-denied"; then
  fail "startup succeeded although /home/hermeswebui/readonly-extra cannot be chowned"
fi
docker container logs "${PREFIX}-denied" 2>&1 | grep -q 'Failed to set owner of /home/hermeswebui' \
  || fail "startup stopped for a reason other than the chown failure"
echo "ok"

echo "All first-start ownership cases passed."
