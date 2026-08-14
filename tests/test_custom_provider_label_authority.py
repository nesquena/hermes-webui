"""Label authority for configured custom_provider models (deep-review 2026-08-13).

PR #6657: `custom_providers[].models[].label` must be authoritative over the
endpoint-derived label on BOTH catalog paths — the cold path
(`_static_models_catalog_without_live_probes`) and the hot path
(`get_available_models` with a prewarmed/probed live row).

Before the fix, a prewarmed live row duplicating a configured model entered
`_seen_custom_ids` first, so the configured duplicate was skipped and the
operator label never won on the active-base-URL path.

The hot-path tests use a real loopback HTTP server as the custom endpoint:
`_read_custom_endpoint_models` is a nested function (not monkeypatchable), and
the conftest's network isolation permits loopback — so the probe runs for real
and the prewarm map is populated through the production path.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import api.config as config


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the models TTL cache before and after every test."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


class _ModelsEndpoint(BaseHTTPRequestHandler):
    """Serves a fixed /v1/models payload on any path (loopback, test-only)."""

    payload = {"data": []}

    def do_GET(self):  # noqa: N802 (http.server API)
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output quiet
        pass


@pytest.fixture
def live_endpoint():
    """A loopback /v1/models endpoint; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsEndpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _patch_cfg(model_cfg, custom_providers):
    """Patch config.cfg (and pin mtime) for the duration of a call.

    The mtime pin stops get_available_models()'s reload_config() guard from
    overwriting the patched cfg with the real on-disk values (same trick as
    test_custom_provider_display_name.py).
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    if model_cfg:
        config.cfg["model"] = model_cfg
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    return old_cfg, old_mtime


def _restore_cfg(old_cfg, old_mtime):
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


def _models_with_cfg(model_cfg=None, custom_providers=None):
    """Call get_available_models() with a patched cfg."""
    old_cfg, old_mtime = _patch_cfg(model_cfg, custom_providers)
    try:
        return config.get_available_models()
    finally:
        _restore_cfg(old_cfg, old_mtime)


def _cold_catalog_with_cfg(model_cfg=None, custom_providers=None):
    """Call _static_models_catalog_without_live_probes() with a patched cfg."""
    old_cfg, old_mtime = _patch_cfg(model_cfg, custom_providers)
    try:
        return config._static_models_catalog_without_live_probes()
    finally:
        _restore_cfg(old_cfg, old_mtime)


def _row_by_model_id(groups, provider_id, model_id):
    group = next((g for g in groups if g.get("provider_id") == provider_id), None)
    if group is None:
        return None
    return next(
        (m for m in group.get("models", []) if m["id"].endswith(model_id)),
        None,
    )


def _gateway_cfg(base_url):
    return {"name": "MyGateway", "base_url": base_url}


def _active_cfg(base_url):
    return {"provider": "custom:mygateway", "base_url": base_url}


# ── Hot path: prewarmed live row duplicating a configured model ──────────────

@pytest.fixture
def _sync_rebuild(monkeypatch):
    """Let the live catalog rebuild finish inside the caller's wait.

    get_available_models() waits _LIVE_REBUILD_BUDGET_SECONDS for the rebuild
    worker, then serves the cold-path fallback. The first call in a pytest
    process never completes in the default 4s (the rebuild probes every
    credentialed provider), so without a longer budget the tests would assert
    against the fallback — which applies the label map too, masking the
    hot-path fix this file exists to protect (deep-review defect 1).
    """
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 60.0)


def test_prewarmed_row_takes_configured_label(live_endpoint, _sync_rebuild):
    """Deep-review defect 1: the prewarmed row must render the operator label,
    not the endpoint's — the configured duplicate used to be skipped."""
    # The probe extracts the label from the payload's `name` field
    # (_extract_model_entries_from_payload reads name/model, never label).
    _ModelsEndpoint.payload = {"data": [{"id": "model-a", "name": "Endpoint Label"}]}
    result = _models_with_cfg(
        model_cfg=_active_cfg(live_endpoint),
        custom_providers=[
            {**_gateway_cfg(live_endpoint), "models": [{"id": "model-a", "label": "Operator Label"}]}
        ],
    )
    row = _row_by_model_id(result.get("groups", []), "custom:mygateway", "model-a")
    assert row is not None, "prewarmed model-a row must appear in the gateway group"
    assert row["label"] == "Operator Label", (
        f"operator label must override the endpoint label, got {row['label']!r}"
    )


def test_prewarmed_row_keeps_endpoint_label_without_config_label(live_endpoint, _sync_rebuild):
    """Without an operator label, the endpoint label survives unchanged."""
    _ModelsEndpoint.payload = {"data": [{"id": "model-a", "name": "Endpoint Label"}]}
    result = _models_with_cfg(
        model_cfg=_active_cfg(live_endpoint),
        custom_providers=[
            {**_gateway_cfg(live_endpoint), "models": ["model-a"]}  # bare id: no label supplied
        ],
    )
    row = _row_by_model_id(result.get("groups", []), "custom:mygateway", "model-a")
    assert row is not None
    assert row["label"] == "Endpoint Label"


# ── Cold path: network-free catalog ──────────────────────────────────────────

def test_cold_catalog_takes_configured_label():
    """The network-free catalog must render the operator label."""
    result = _cold_catalog_with_cfg(
        model_cfg=_active_cfg("https://gw.example.com/v1"),
        custom_providers=[
            {**_gateway_cfg("https://gw.example.com/v1"), "models": [{"id": "model-a", "label": "Operator Label"}]}
        ],
    )
    row = _row_by_model_id(result.get("groups", []), "custom:mygateway", "model-a")
    assert row is not None, "configured model-a must appear in the cold catalog"
    assert row["label"] == "Operator Label"


def test_cold_catalog_derives_label_without_config_label():
    """Without an operator label the cold catalog falls back to the derived
    label (title-cased id), never the raw id."""
    result = _cold_catalog_with_cfg(
        model_cfg=_active_cfg("https://gw.example.com/v1"),
        custom_providers=[{**_gateway_cfg("https://gw.example.com/v1"), "models": ["model-a"]}],
    )
    row = _row_by_model_id(result.get("groups", []), "custom:mygateway", "model-a")
    assert row is not None
    assert row["label"] != "model-a", "bare id must not render as its own label"
