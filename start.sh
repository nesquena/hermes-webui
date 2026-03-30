#!/bin/bash
# Start Hermes Co-Work MVP web server
# Source: /home/hermes/webui-mvp/ (canonical location)
# Symlink: /home/hermes/.hermes/hermes-agent/webui-mvp -> /home/hermes/webui-mvp
# Usage: ./start.sh [port]

PORT=${1:-8787}
VENV="/home/hermes/.hermes/hermes-agent/venv/bin/python"
SERVER="/home/hermes/webui-mvp/server.py"
LOG="/tmp/webui-mvp.log"

# Kill any existing instance
pkill -f "python.*webui-mvp/server.py" 2>/dev/null

# Start fresh -- cd into hermes-agent so sys.path imports work
export HERMES_WEBUI_PORT=$PORT
cd /home/hermes/.hermes/hermes-agent
nohup $VENV $SERVER > $LOG 2>&1 &
PID=$!
echo "Started PID $PID on port $PORT"
echo "Source: /home/hermes/webui-mvp/"
echo "Log: $LOG"
sleep 1
curl -s http://127.0.0.1:$PORT/health && echo "" || echo "Health check failed"
