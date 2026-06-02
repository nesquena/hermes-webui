#!/bin/bash
# Hermes WebUI watchdog loop — runs forever, checks every 5 minutes
# Launched via setsid so it survives terminal session
# Logs to /tmp/hermes-webui-watchdog.log

HOST="127.0.0.1"
PORT="8787"
WEBUI_DIR="/root/hermes-webui"
LOG_FILE="/tmp/hermes-webui-watchdog.log"
PID_FILE="/tmp/hermes-webui-watchdog.pid"

echo $$ > "$PID_FILE"

while true; do
    if ! curl -sf "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WebUI DOWN — restarting..." >> "$LOG_FILE"
        cd "$WEBUI_DIR"
        bash ctl.sh start >> "$LOG_FILE" 2>&1
        sleep 20  # 8-10s for PRoot bind + buffer
        if curl -sf "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] WebUI restarted successfully" >> "$LOG_FILE"
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] WebUI restart FAILED" >> "$LOG_FILE"
        fi
    fi
    sleep 300
done
