"""#5311 / #5611: OpenCode Go's generic live probe used to return public-catalog
models not enabled on the Go tier, so the picker intentionally falls back to a curated
static _PROVIDER_MODELS list. Keep that curated list in sync with the public Go docs
and documented Go models endpoint while excluding preview/free-only Zen models.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "api" / "config.py"
CONFIG = CONFIG_PATH.read_text(encoding="utf-8")

EXPECTED_OPENCODE_GO_MODEL_IDS = [
    "minimax-m3",
    "minimax-m2.7",
    "minimax-m2.5",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "kimi-k2.5",
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "mimo-v2-pro",
    "mimo-v2-omni",
    "mimo-v2.5-pro",
    "mimo-v2.5",
]


def _opencode_go_static_models():
    tree = ast.parse(CONFIG, filename=str(CONFIG_PATH))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_PROVIDER_MODELS" for target in node.targets):
            continue
        provider_models = ast.literal_eval(node.value)
        return provider_models["opencode-go"]
    raise AssertionError("_PROVIDER_MODELS assignment not found")


def test_opencode_go_uses_live_models_probe():
    # #7166: OpenCode Go must use the shared live /v1/models discovery path
    # like every other provider (stale static catalog was the bug). The
    # curated static _PROVIDER_MODELS list stays as the fallback when the
    # live probe yields nothing (#8084).
    body = CONFIG[CONFIG.index("def get_available_models"):]
    body = body[: body.index("\ndef ", 1)]
    # The opencode-go special-case that skipped the live probe is gone.
    assert 'elif pid == "opencode-go":' not in body
    # opencode-go now falls through to the shared live discovery branch.
    assert "_models_from_live_provider_ids" in body
    # Static list remains as the fallback for empty live results.
    assert "_PROVIDER_MODELS.get(pid, [])" in body


def test_opencode_go_static_models_match_documented_endpoint_snapshot():
    # Snapshot from https://opencode.ai/zen/go/v1/models on 2026-07-07.
    # This catches stale-list regressions such as missing GLM-5.2 / MiniMax M3.
    models = _opencode_go_static_models()
    assert [model["id"] for model in models] == EXPECTED_OPENCODE_GO_MODEL_IDS


def test_opencode_go_recent_additions_have_human_labels():
    labels = {model["id"]: model["label"] for model in _opencode_go_static_models()}
    assert labels["glm-5.2"] == "GLM-5.2"
    assert labels["minimax-m3"] == "MiniMax M3"
    assert labels["kimi-k2.7-code"] == "Kimi K2.7 Code"
    assert labels["qwen3.7-max"] == "Qwen3.7 Max"
