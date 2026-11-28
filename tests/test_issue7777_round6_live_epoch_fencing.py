"""#7777 round 6 — the live-response policy fence must actually fence.

Review 5489844033 (2026-10-12) listed, still open at the 2026-11-23 re-gate:

    Live-response race (``api/routes.py``): the epoch check in
    ``_handle_live_models``'s ``_finish`` compares the old key with itself.
    Compare against the current ``_picker_excludes_epoch()``.

The pre-fix code compared::

    if payload.get("_picker_excludes_epoch", cache_key[2]) != cache_key[2]:

``payload`` is the freshly assembled live model list. It never carries an
``_picker_excludes_epoch`` key — only :func:`_set_cached_live_models` stamps
one, and that happens AFTER this check. So the ``get`` fell back to
``cache_key[2]`` and the comparison was ``cache_key[2] != cache_key[2]`` — always
False — and the stale guard never fired.

Consequence: an in-flight ``/api/models/live`` request that started BEFORE a
``picker_excludes`` save still publishes its pre-save model list, so the very
next request re-receives the model the user just hid.

These tests pin the fence:

1. A live payload assembled under an OLD policy must be dropped when it
   lands after the policy changed.
2. A live payload assembled under the CURRENT policy must be published.
3. The fence must not fire for an unrelated payload shape (a payload with no
   epoch stamp keeps the historical behaviour of the request's own epoch).
"""

from __future__ import annotations

import io
import json

import pytest

from api import routes


@pytest.fixture(autouse=True)
def _isolated_live_cache(monkeypatch):
    """Isolate the process-global live cache between tests."""
    monkeypatch.setattr(routes, "_LIVE_MODELS_CACHE", {})
    yield


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in for the ``j()`` response path.

    Mirrors the shape the repo's other route tests use (``wfile``/``headers``/
    ``rfile``), so the real ``api.helpers.j`` can write to it.
    """

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
def _published(monkeypatch):
    """Capture what the publish path hands to the cache."""
    published: list = []
    monkeypatch.setattr(
        routes,
        "_set_cached_live_models",
        lambda key, payload: published.append((key, payload)),
    )
    return published


def test_finish_drops_a_payload_built_under_a_superseded_policy(
    monkeypatch, _published
):
    """The core race: the fetch straddles a ``picker_excludes`` save.

    The request started under epoch A. The user saved a new policy, so the
    CURRENT epoch is now B. The payload that lands was built under A and must
    be discarded — not published to the cache.
    """
    epoch_at_request = "epoch:old-policy"
    current_epoch = "epoch:new-policy"

    cache_key = ("default", "zai", epoch_at_request)
    payload = {
        "provider": "zai",
        "models": [{"id": "gpt-x"}],
        "count": 1,
        "_picker_excludes_epoch": epoch_at_request,
    }

    monkeypatch.setattr(routes, "_picker_excludes_epoch", lambda: current_epoch)

    routes._publish_live_models_result(_Handler(), "zai", cache_key, payload)

    assert not _published, (
        "a live payload built under a superseded policy was published to the "
        "cache, so the excluded model comes back on the next request"
    )


def test_finish_publishes_a_payload_built_under_the_current_policy(
    monkeypatch, _published
):
    """The control: a payload built under the CURRENT epoch is published."""
    epoch = "epoch:current"
    cache_key = ("default", "zai", epoch)
    payload = {
        "provider": "zai",
        "models": [{"id": "gpt-keep"}],
        "count": 1,
        "_picker_excludes_epoch": epoch,
    }

    monkeypatch.setattr(routes, "_picker_excludes_epoch", lambda: epoch)

    routes._publish_live_models_result(_Handler(), "zai", cache_key, payload)

    assert len(_published) == 1, "a current-policy payload was dropped"
    assert _published[0][0] == cache_key


def test_finish_drops_a_payload_whose_stamp_is_absent_when_policy_moved(
    monkeypatch, _published
):
    """An unstamped payload under a moved policy is also stale.

    The pre-fix ``.get(key, cache_key[2])`` fallback made an unstamped
    payload look current. With the fence comparing against the CURRENT epoch,
    an unstamped payload under a superseded policy is dropped rather than
    published.
    """
    epoch_at_request = "epoch:old-policy"
    current_epoch = "epoch:new-policy"
    cache_key = ("default", "zai", epoch_at_request)
    payload = {"provider": "zai", "models": [{"id": "gpt-x"}], "count": 1}

    monkeypatch.setattr(routes, "_picker_excludes_epoch", lambda: current_epoch)

    routes._publish_live_models_result(_Handler(), "zai", cache_key, payload)

    assert not _published, (
        "an unstamped payload was published although the policy moved on"
    )
