"""
Sprint 11 Tests: multi-provider model support, streaming smoothness, routes extraction.
"""

import json
import pathlib
import urllib.error
import urllib.request
import urllib.parse

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── /api/models endpoint ──────────────────────────────────────────────────


def test_models_endpoint_returns_200():
    """GET /api/models returns a valid response."""
    d, status = get("/api/models")
    assert status == 200


def test_models_has_required_fields():
    """Response includes groups, default_model, and active_provider."""
    d, _ = get("/api/models")
    assert "groups" in d
    assert "default_model" in d
    assert "active_provider" in d


def test_models_groups_structure():
    """Each group has provider name and models list."""
    d, _ = get("/api/models")
    assert isinstance(d["groups"], list)
    assert len(d["groups"]) > 0
    for group in d["groups"]:
        assert "provider" in group
        assert "models" in group
        assert isinstance(group["models"], list)
        assert len(group["models"]) > 0


def test_models_model_structure():
    """Each model has id and label."""
    d, _ = get("/api/models")
    for group in d["groups"]:
        for model in group["models"]:
            assert "id" in model
            assert "label" in model
            assert isinstance(model["id"], str)
            assert isinstance(model["label"], str)
            assert len(model["id"]) > 0
            assert len(model["label"]) > 0


def test_models_default_model_not_empty():
    """When HERMES_WEBUI_DEFAULT_MODEL env var is set (as in conftest), the
    /api/models response includes a non-empty default_model string."""
    d, _ = get("/api/models")
    assert isinstance(d["default_model"], str)
    # conftest sets HERMES_WEBUI_DEFAULT_MODEL to "openai/gpt-5.4-mini", so
    # this value should be non-empty in the test environment.
    # When no env var is set (production with empty default), default_model
    # can be "" — that is intentional (see PR #649).
    assert len(d["default_model"]) > 0  # only holds because conftest sets the env var


def test_models_at_least_one_provider():
    """At least one provider group should exist (fallback list at minimum)."""
    d, _ = get("/api/models")
    providers = [g["provider"] for g in d["groups"]]
    assert len(providers) >= 1


def test_models_no_duplicate_ids():
    """Model IDs should not be duplicated within a single group."""
    d, _ = get("/api/models")
    for group in d["groups"]:
        ids = [m["id"] for m in group["models"]]
        assert len(ids) == len(set(ids)), (
            f"Duplicate model IDs in {group['provider']}: {ids}"
        )


def test_session_preserves_unlisted_model():
    """A session with a model not in the dropdown should still load correctly."""
    # Create a session with a custom model string
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    try:
        custom_model = "custom-provider/test-model-999"
        post(
            "/api/session/update",
            {
                "session_id": sid,
                "model": custom_model,
                "workspace": d["session"]["workspace"],
            },
        )
        # Reload and verify model persisted
        d2, _ = get(f"/api/session?session_id={sid}")
        assert d2["session"]["model"] == custom_model
    finally:
        post("/api/session/delete", {"session_id": sid})
