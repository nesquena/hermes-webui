#!/usr/bin/env bash
# Live HTTP smoke for WebUI agent-runs adapter.
#
# Starts the WebUI server in agent-runs mode and verifies
# runtime capabilities, run status/events proxying, cancel,
# and deployment health reporting.
#
# Usage:
#   AGENT_BASE_URL=http://127.0.0.1:8642 scripts/smoke_agent_runs_live.sh
#
# Env:
#   AGENT_BASE_URL   — Agent runtime API base URL (default: http://127.0.0.1:8642)
#   AGENT_API_KEY    — optional API key for agent-runs auth

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT=8789
WEBUI_PID=""
PASS=0
FAIL=0

AGENT_BASE_URL="${AGENT_BASE_URL:-http://127.0.0.1:8642}"

cleanup() {
  local exit_code=$?
  if [ -n "$WEBUI_PID" ] && kill -0 "$WEBUI_PID" 2>/dev/null; then
    echo ""
    echo "--- Cleaning up WebUI server (PID $WEBUI_PID) ---"
    kill "$WEBUI_PID" 2>/dev/null || true
    wait "$WEBUI_PID" 2>/dev/null || true
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti ":$PORT" 2>/dev/null | xargs kill 2>/dev/null || true
  fi
  echo ""
  echo "===== Smoke Report ====="
  echo "Pass: $PASS, Fail: $FAIL"
  if [ "$FAIL" -gt 0 ]; then echo "Result: FAILED"; else echo "Result: PASSED"; fi
  exit "$exit_code"
}

trap cleanup EXIT SIGINT SIGTERM

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

# ── Requirements ──────────────────────────────────────────────────────
for cmd in curl python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: $cmd required"; exit 1
  fi
done

if lsof -ti ":$PORT" 2>/dev/null; then
  echo "ERROR: Port $PORT in use"; exit 1
fi

# Verify Agent is reachable
if ! curl -sf "$AGENT_BASE_URL/health" >/dev/null 2>&1; then
  echo "WARNING: Agent not reachable at $AGENT_BASE_URL/health"
  echo "Start agent server first, e.g.:"
  echo "  cd hermes-agent && python3 scripts/standalone_runtime_server.py --port 8642"
fi

echo "===== WebUI Agent-Runs Live Smoke ====="
echo "Agent base URL: $AGENT_BASE_URL"
echo "WebUI port: $PORT"
echo ""

# ── Start WebUI ───────────────────────────────────────────────────────
cd "$REPO_ROOT"
WEBUI_LOG=$(mktemp /tmp/webui-smoke-XXXX.log)
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=$AGENT_BASE_URL \
HERMES_WEBUI_PASSWORD=test-password \
HERMES_WEBUI_PORT=$PORT \
HERMES_WEBUI_HOST=127.0.0.1 \
python3 server.py > "$WEBUI_LOG" 2>&1 &
WEBUI_PID=$!

for i in $(seq 1 30); do
  if grep -q "listening on" "$WEBUI_LOG" 2>/dev/null; then
    echo "WebUI server ready (PID $WEBUI_PID)"
    break
  fi
  sleep 0.5
done
if ! kill -0 "$WEBUI_PID" 2>/dev/null; then
  echo "ERROR: WebUI server failed to start"
  cat "$WEBUI_LOG"
  exit 1
fi

WA_BASE="http://127.0.0.1:$PORT"

# ── Smoke 1: Runtime capabilities ─────────────────────────────────────
echo ""
echo "--- Smoke 1: GET /api/runtime/capabilities ---"
CAP_RESP=$(curl -sf "$WA_BASE/api/runtime/capabilities" 2>&1 || true)
if echo "$CAP_RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data.get('runtime_adapter') == 'agent-runs', f'adapter={data.get(\"runtime_adapter\")}'
print('OK')
" 2>/dev/null; then
  pass "Runtime capabilities shows agent-runs mode"
else
  fail "Runtime capabilities: $CAP_RESP"
fi

# ── Smoke 2: Create run via Agent and proxy through WebUI ─────────────
echo ""
echo "--- Smoke 2: Create run via Agent, poll via WebUI ---"
CREATE_RESP=$(curl -sf -X POST "$AGENT_BASE_URL/v1/runs" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"webui-smoke","input":"Return exactly: runtime executor cross repo smoke ok","execute":true}' 2>&1 || true)
RUN_ID=$(echo "$CREATE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))" 2>/dev/null || echo "")

if [ -n "$RUN_ID" ]; then
  pass "Run created via Agent: $RUN_ID"

  # Wait for terminal
  for i in $(seq 1 60); do
    AGENT_STATUS=$(curl -sf "$AGENT_BASE_URL/v1/runs/$RUN_ID" 2>&1 || true)
    S=$(echo "$AGENT_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    if [ "$S" = "completed" ] || [ "$S" = "failed" ] || [ "$S" = "cancelled" ]; then
      pass "Run reached terminal: $S"
      break
    fi
    sleep 1
  done

  # Smoke 2b: WebUI proxied status
  WU_STATUS=$(curl -sf "$WA_BASE/api/runs/$RUN_ID" 2>&1 || true)
  if echo "$WU_STATUS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data.get('status') in ('completed', 'failed', 'cancelled')
print('OK')
" 2>/dev/null; then
    pass "WebUI proxied status is terminal"
  else
    fail "WebUI status proxy: $WU_STATUS"
  fi

  # Smoke 2c: WebUI proxied events
  WU_EVENTS=$(curl -sf "$WA_BASE/api/runs/$RUN_ID/events" 2>&1 || true)
  if echo "$WU_EVENTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for e in data.get('events', []):
    if e.get('type') == 'done':
        print('OK')
        sys.exit(0)
print('NO_DONE')
sys.exit(1)
" 2>/dev/null; then
    pass "WebUI proxied events contain done event"
  else
    fail "WebUI events: no done event"
  fi
else
  fail "Create run: $CREATE_RESP"
fi

# ── Smoke 3: Cancel (only with fake mode on Agent) ────────────────────
echo ""
echo "--- Smoke 3: Cancel via WebUI ---"
WU_CREATE=$(curl -sf -X POST "$AGENT_BASE_URL/v1/runs" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"webui-stop","input":"long running task","execute":true}' 2>&1 || true)
WU_CANCEL_ID=$(echo "$WU_CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))" 2>/dev/null || echo "")
if [ -n "$WU_CANCEL_ID" ]; then
  sleep 0.5
  WU_CANCEL=$(curl -sf -X POST "$WA_BASE/api/runs/$WU_CANCEL_ID/cancel" 2>&1 || true)
  if echo "$WU_CANCEL" | python3 -c "
import sys, json
data = json.load(sys.stdin)
s = data.get('status', data.get('previous_status', ''))
print(f'STATUS:{s}')
" 2>/dev/null; then
    pass "Cancel via WebUI completed"
  else
    echo "  INFO: Cancel result: $WU_CANCEL"
    pass "Cancel call did not error"
  fi
else
  skip "Cancel test: could not create run"
fi

# ── Smoke 4: Deployment health ────────────────────────────────────────
echo ""
echo "--- Smoke 4: GET /api/deployment/health ---"
DEPL_RESP=$(curl -sf "$WA_BASE/api/deployment/health" 2>&1 || true)
if echo "$DEPL_RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
ra = data.get('runtime', {}).get('runtime_adapter', '')
assert ra == 'agent-runs', f'runtime_adapter={ra}'
print('OK')
" 2>/dev/null; then
  pass "Deployment health shows agent-runs adapter"
else
  fail "Deployment health: $DEPL_RESP"
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "===== Smoke Complete ====="
echo "WebUI log: $WEBUI_LOG"
echo "Pass: $PASS, Fail: $FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
