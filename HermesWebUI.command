#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
chmod +x install.sh start.sh ctl.sh bootstrap.py 2>/dev/null || true
./install.sh
