set dotenv-load := false

# List available commands
_default:
    @just --list

# Install Python and JS dev dependencies
install:
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
    npm ci

# Start a foreground dev WebUI on a non-service port
dev port="8788":
    python3 bootstrap.py --no-browser --foreground {{port}}

# Start/stop the repo-managed background daemon on a non-service port
start port="8788":
    ./ctl.sh start {{port}}

stop:
    ./ctl.sh stop

restart port="8788":
    ./ctl.sh restart {{port}}

status:
    ./ctl.sh status

logs:
    ./ctl.sh logs --no-follow

# JavaScript runtime guard
lint:
    npm run lint:runtime

# Python and app tests
test:
    python3 -m pytest

# Standard pre-handoff check
check: lint test
