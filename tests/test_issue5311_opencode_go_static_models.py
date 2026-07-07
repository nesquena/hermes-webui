"""#5311 / #5611: OpenCode Go's live /v1/models probe returns public-catalog
models not enabled on the Go tier, so selecting one 404s on send. get_available_models
must skip the live probe for opencode-go and fall through to the curated static
_PROVIDER_MODELS list instead.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = (ROOT / "api" / "config.py").read_text(encoding="utf-8")


def test_opencode_go_skips_live_models_probe():
    # The provider-loop must special-case opencode-go to skip the live probe.
    body = CONFIG[CONFIG.index("def get_available_models"):]
    body = body[: body.index("\ndef ", 1)]
    assert 'elif pid == "opencode-go":' in body
    # It must NOT call the live-provider probe on the opencode-go branch — the
    # branch is a bare skip so the next `if not raw_models` falls back to static.
    idx = body.index('elif pid == "opencode-go":')
    branch = body[idx: idx + 400]
    assert "_models_from_live_provider_ids" not in branch.split("else:")[0]


def test_opencode_go_has_curated_static_models():
    # The static fallback the skip relies on must exist and be non-empty.
    assert '"opencode-go": [' in CONFIG
    block = CONFIG[CONFIG.index('"opencode-go": ['):]
    block = block[: block.index("],") + 1]
    assert block.count('"id":') >= 5
