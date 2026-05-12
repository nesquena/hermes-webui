#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/kei/kei-jarvis"
cd "$ROOT/tools/cocoindex_secondbrain"
export COCOINDEX_DB="$ROOT/.cocoindex-secondbrain/cocoindex.db"
uv run cocoindex update main.py
python verify_index.py
