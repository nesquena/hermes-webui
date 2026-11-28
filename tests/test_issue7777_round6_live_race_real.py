"""#7777 round 6 — the live-response fence under the REAL race.

Review 5489844033 (2026-10-12), still open at the 2026-11-23 re-gate:

    Live-response race (``api/routes.py``): the epoch check in
    ``_handle_live_models``'s ``_finish`` compares the old key with itself.
    Compare against the current ``_picker_excludes_epoch()``.

This file reproduces the ACTUAL race with the real settings store (no mocked
epoch): a live request starts under one policy, the user saves a new policy
while the fetch is in flight, and the payload lands afterwards. The stale
payload must not reach the cache, and the response must not carry the model
the user just hid.
"""

from __future__ import annotations

import io
import json

import pytest

from api import config, routes


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in for the ``j()`` response path."""

    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(b"")
        self.headers = {}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture
def _settings(monkeypatch):
    """A live settings store the policy can move in."""
    store: dict = {"picker_excludes": {"zai": ["gpt-x"]}}
    monkeypatch.setattr(config, "load_settings", lambda: store)
    return store


@pytest.fixture
def _published(monkeypatch):
    published: list = []
    monkeypatch.setattr(
        routes,
        "_set_cached_live_models",
        lambda key, payload: published.append((key, payload)),
    )
    return published


def test_a_save_during_the_fetch_drops_the_stale_payload(
    _settings, _published
):
    """The real race: the fetch straddles a ``picker_excludes`` save.

    The request started under one policy; the user saved another before the
    payload landed. The stale payload must not be published, and the response
    must not carry the model the user just hid.
    """
    cache_key = routes._live_models_cache_key("zai")
    payload = {
        "provider": "zai",
        "models": [{"id": "gpt-x"}, {"id": "gpt-keep"}],
        "count": 2,
        "_picker_excludes_epoch": cache_key[2],
    }

    # …the user saves a new policy while the fetch is in flight.
    _settings["picker_excludes"] = {"zai": ["gpt-x"], "openai": ["o1"]}

    handler = _Handler()
    routes._publish_live_models_result(handler, "zai", cache_key, payload)

    assert not _published, (
        "the stale payload was published to the cache, so the next request "
        f"re-receives the hidden model: {_published!r}"
    )
    body = handler.json_body()
    assert not body.get("models"), (
        f"the stale response still carried the hidden model: {body!r}"
    )


def test_a_payload_under_the_current_policy_is_published(_settings, _published):
    """The control: nothing moved, so the payload is published."""
    cache_key = routes._live_models_cache_key("zai")
    payload = {
        "provider": "zai",
        "models": [{"id": "gpt-keep"}],
        "count": 1,
        "_picker_excludes_epoch": cache_key[2],
    }

    handler = _Handler()
    routes._publish_live_models_result(handler, "zai", cache_key, payload)

    assert len(_published) == 1, "a current-policy payload was dropped"
    assert _published[0][0] == cache_key
