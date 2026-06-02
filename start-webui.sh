#!/bin/bash
cd /root/hermes-webui
export HERMES_WEBUI_AGENT_DIR=/usr/local/lib/hermes-agent
export HERMES_WEBUI_HOST=127.0.0.1
export HERMES_WEBUI_PYTHON=/usr/local/lib/hermes-agent/venv/bin/python
exec /usr/local/lib/hermes-agent/venv/bin/python server.py >> /tmp/hermes-webui.log 2>&1
