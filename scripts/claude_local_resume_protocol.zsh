#!/bin/zsh

# Deployment shim for claude-local. Task 6 wires this into the live wrapper.
emulate -L zsh
setopt errexit nounset pipefail

readonly task_script_dir="${0:A:h}"
readonly task_webui_root="${task_script_dir:h}"
exec "${task_webui_root}/.venv/bin/python" \
  "${task_webui_root}/api/claude_code_runner.py" --wrapper "$@"
