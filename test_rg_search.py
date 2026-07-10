"""
Test bench: compare rg-backed search vs existing Python-only search.
Run from hermes-webui/ directory.
"""

from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "api")
sys.path.insert(0, ".")

# Patch before imports to avoid database conflicts in test mode
from api import config as cfg
cfg.STATE_DIR = cfg.STATE_DIR
# Ensure SESSION_DIR resolves properly

from api.models import SESSION_DIR, all_sessions
from api.rg_search import rg_search_sessions
from api.routes import _handle_sessions_search
from types import SimpleNamespace
from urllib.parse import urlparse


def benchmark_search(query: str, depth: int = 5, label: str = ""):
    """Run rg-based search and measure time."""
    t0 = time.time()
    results = rg_search_sessions(query, depth=depth, content=True)
    t1 = time.time()
    print(f"[rg{label}] '{query}' depth={depth}: {len(results)} results in {t1-t0:.3f}s")
    if results:
        # Show first few results
        for r in results[:3]:
            preview = r.get("match_preview", "(no preview)")
            preview_short = preview[:120] + "..." if len(preview) > 120 else preview
            print(f"  └─ {r['match_type']}: {r.get('session_id')} | {preview_short}")
    return results


# ── Test queries ────────────────────────────────────────────────────

queries = [
    ("rustdesk", 5, "Sparse term"),
    ("hello", 5, "Common term"),
    ("authentication", 5, "Technical term"),
    ("rustdesk", 0, "Sparse, full depth"),
    ("hello", 0, "Common, full depth"),
]

print("=" * 60)
print("RG-backed search benchmark")
print(f"Session dir: {SESSION_DIR}")
print(f"Total session files: {len(list(SESSION_DIR.glob('*.json')))}")
print("=" * 60)

for q, d, label in queries:
    benchmark_search(q, d, label=f" {label}")
    print()
