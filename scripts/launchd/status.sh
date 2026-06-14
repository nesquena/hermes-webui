#!/usr/bin/env bash
set -euo pipefail

# status.sh — Show the status of the hermes-webui launchd job
#
# Runs launchctl print (read-only — no side effects) and curl health check.
#
# Usage: ./scripts/launchd/status.sh

LABEL="com.parantoux.hermes-webui"
UID_NOW="$(id -u)"
SERVICE="gui/${UID_NOW}/${LABEL}"

echo "=== hermes-webui launchd 상태 ==="
echo ""

# --- launchctl print ---
if ! launchd_out="$(launchctl print "${SERVICE}" 2>/dev/null)"; then
    echo "launchd 작업을 찾을 수 없습니다 (${SERVICE})"
    echo ""
    echo "plist가 설치되어 있고 load 되었는지 확인:"
    echo "  install:  ./scripts/launchd/install.sh"
    echo "  load:     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/${LABEL}.plist"
else
    # Parse key fields
    pid="$(printf '%s\n' "${launchd_out}" | awk '/^[[:space:]]*pid = / {print $3; exit}')"
    state="$(printf '%s\n' "${launchd_out}" | awk '/^[[:space:]]*state = / {print $3; exit}')"
    last_exit="$(printf '%s\n' "${launchd_out}" | awk '/^[[:space:]]*last exit code = / {print $5; exit}')"

    echo "● launchd 작업: ${SERVICE}"
    echo "  상태(state): ${state:-unknown}"
    if [[ -n "${pid}" && "${pid}" =~ ^[0-9]+$ ]]; then
        echo "  PID:         ${pid}"
        if uptime="$(ps -p "${pid}" -o etime= 2>/dev/null | sed 's/^ *//')"; then
            echo "  가동 시간:   ${uptime}"
        fi
    fi
    if [[ -n "${last_exit}" ]]; then
        echo "  마지막 종료 코드: ${last_exit}"
    fi

    # Full print output for debugging
    echo ""
    echo "--- launchctl print 전체 출력 ---"
    printf '%s\n' "${launchd_out}"
fi

echo ""

# --- Health check ---
HEALTH_URL="http://127.0.0.1:8788/health"
echo "--- 서버 health check (${HEALTH_URL}) ---"
if command -v curl >/dev/null 2>&1; then
    if result="$(curl -fsS --max-time 3 "${HEALTH_URL}" 2>/dev/null)"; then
        echo "● 서버 응답: OK"
        if command -v python3 >/dev/null 2>&1; then
            printf '%s' "${result}" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    sessions = data.get("sessions", data.get("session_count", "?"))
    active = data.get("active_streams", "?")
    status = data.get("status", "ok")
    print(f"  세션: {sessions}, 활성 스트림: {active}")
    print(f"  상태: {status}")
except Exception:
    print("  (JSON 파싱 실패 — 원시 응답 보존)")
    print(sys.stdin.read())
' 2>/dev/null || true
        fi
    else
        echo "● 서버 응답 없음 — 서버가 실행 중이 아닐 수 있습니다"
    fi
else
    echo "  curl이 설치되어 있지 않아 health check를 건너뜁니다"
fi

echo ""
echo "--- 수동 조작 ---"
echo "  재시작:  launchctl kickstart -k ${SERVICE}"
echo "  중지:    launchctl bootout ${SERVICE}"
echo "  ctl.sh:  ./ctl.sh status"
