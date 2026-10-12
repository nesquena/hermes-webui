"""Regression tests for the retained ``models_endpoint_error`` lifetime (#8158).

A named custom provider whose ``GET <base_url>/v1/models`` probe fails once for a
transport-level reason (DNS blip, timeout past the 5s cap, throttled retry) gets
``models_endpoint_error`` written into the model catalog, and that diagnostic is
then served from the cached catalog for the full 24h
``_AVAILABLE_MODELS_CACHE_TTL`` — none of the five source-fingerprint axes can
observe that the endpoint *recovered*. The user keeps reading "Models endpoint
unreachable for <provider>; verify base_url." against a base_url that is
byte-for-byte correct.

The fix keeps the healthy catalog cache and gives the *diagnostic* a short,
bounded retry lifetime: while a group carries an expired diagnostic, a warm read
schedules one coalesced re-probe per (profile, provider); its result is published
only when the catalog generation and sources it was probed for are still current.

The reproduction test below is bound to the reporter's A/B/C shape
(``repro_stale_endpoint_error.py``): the failing probe stores the diagnostic
(stage A), the endpoint is restored *in memory only* — no source-fingerprint axis
changes, which is the same shape as the network merely recovering (stage B), and
the per-provider refresh force-rebuild remains the manual exit (stage C).

Covered shapes:
  * reporter A/B/C reproduction: transient failure → recovery → warm read
  * an unexpired diagnostic is not re-probed (no probe on every warm read)
  * a warm read never blocks on the retry probe
  * concurrent warm readers coalesce into exactly one re-probe
  * a failed re-probe re-stamps so reads do not retry on every call
  * a late result from a superseded catalog generation is discarded
  * the retry is scoped per provider, and the coalescing key includes the profile
  * a diagnostic without a stamp (pre-upgrade cache) is treated as expired
  * a successful same-owner ``/api/models/live`` probe clears the diagnostic
  * clearing keeps the zero-model error group itself (#7521 contract)
"""

import io
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest

from api import config

_UNRESOLVABLE = "custom:broken-proxy"
"""Slug the reporter's shape produces for ``name: Broken Proxy``."""


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class _ConfigState:
    """Isolate the catalog cache globals for one test (see test_issue2540)."""

    def __enter__(self):
        self.old_cfg = config.cfg
        self.old_mtime = config._cfg_mtime
        self.old_cache = config._available_models_cache
        self.old_cache_ts = config._available_models_cache_ts
        self.old_cache_fp = config._available_models_cache_source_fingerprint
        self.old_live_budget = config._LIVE_REBUILD_BUDGET_SECONDS
        self.old_provenance = config._models_cache_provenance
        config._cfg_mtime = 0.0
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_cache_source_fingerprint = None
        config._sync_models_cache_provenance()
        # Synchronous rebuild path: keeps stages A/C deterministic instead of
        # racing the bounded out-of-band worker.
        config._LIVE_REBUILD_BUDGET_SECONDS = 0.0
        _reset_reprobe_registry()
        return self

    def __exit__(self, exc_type, exc, tb):
        _wait_for_reprobe_idle(5.0)
        _reset_reprobe_registry()
        config.cfg = self.old_cfg
        config._cfg_mtime = self.old_mtime
        config._available_models_cache = self.old_cache
        config._available_models_cache_ts = self.old_cache_ts
        config._available_models_cache_source_fingerprint = self.old_cache_fp
        config._models_cache_provenance = self.old_provenance
        config._LIVE_REBUILD_BUDGET_SECONDS = self.old_live_budget
        return False


def _reset_reprobe_registry() -> None:
    registry = getattr(config, "_models_endpoint_reprobe_inflight", None)
    if isinstance(registry, dict):
        registry.clear()
    elif isinstance(registry, set):
        registry.clear()


def _reprobe_inflight() -> object:
    return getattr(config, "_models_endpoint_reprobe_inflight", {})


def _wait_for_reprobe_idle(timeout: float = 5.0) -> bool:
    """Wait until no re-probe worker is registered as in flight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _reprobe_inflight():
            return True
        time.sleep(0.01)
    return not _reprobe_inflight()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _configure_named_custom_provider(tmp_path, monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    config.cfg = {
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
        "providers": {},
        "fallback_providers": [],
        "custom_providers": [
            {
                "name": "Broken Proxy",
                "base_url": base_url,
                "api_key": "test-key",
            }
        ],
    }


class _Probe:
    """Fake ``urlopen``: healthy/unhealthy answers for the endpoint under test.

    The endpoint under test is any ``*.example`` host (the reporter's shape
    repoints the same custom provider between an unresolvable host and the real
    one, so both are the *same* provider's endpoint). Every other URL belongs to
    unrelated provider discovery (Copilot token exchange, OpenRouter free-tier
    fetch, ...) and must not pollute the counter — those answer as transport
    failures so they take their own fallbacks.
    """

    def __init__(self, *, healthy: bool = True, catalog=("proxy-model-a", "proxy-model-b")):
        self.healthy = healthy
        self.catalog = list(catalog)
        self.count = 0
        self.entered = threading.Event()
        self.gate: threading.Event | None = None
        self.lock = threading.Lock()

    @staticmethod
    def _is_endpoint_under_test(url: str) -> bool:
        try:
            hostname = urlparse(url).hostname or ""
        except ValueError:
            return False
        return hostname.endswith(".example")

    def __call__(self, req, timeout=None):
        url = str(getattr(req, "full_url", req))
        if not self._is_endpoint_under_test(url):
            raise urllib.error.URLError("unrelated endpoint")
        with self.lock:
            self.count += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5.0)
        if not self.healthy:
            raise urllib.error.URLError("Name or service not known")
        body = {"data": [{"id": mid} for mid in self.catalog]}
        return _FakeResponse(json.dumps(body).encode())


def _probe_count(probe: _Probe) -> int:
    with probe.lock:
        return probe.count


def _groups_by_provider(data: dict) -> dict:
    return {
        str(group.get("provider_id") or ""): group
        for group in (data.get("groups") or [])
        if isinstance(group, dict)
    }


def _endpoint_error(data: dict, provider_id: str = _UNRESOLVABLE):
    group = _groups_by_provider(data).get(provider_id)
    return None if group is None else group.get("models_endpoint_error")


def _expire_retries(monkeypatch, seconds: float = 0.0) -> None:
    monkeypatch.setattr(
        config, "_MODELS_ENDPOINT_ERROR_RETRY_SECONDS", seconds, raising=False
    )


def _age_cached_diagnostic(seconds: float, provider_id: str = _UNRESOLVABLE) -> bool:
    """Backdate the published diagnostic's stamp so it reads as expired.

    Bumping the stamp is what the wall-clock lifetime measures, so this is the
    deterministic way to put the catalog into the "expired diagnostic" state
    without sleeping for the window.
    """
    cache = config._available_models_cache
    if not isinstance(cache, dict):
        return False
    for group in cache.get("groups") or []:
        if not isinstance(group, dict):
            continue
        if str(group.get("provider_id") or "").strip().lower() != provider_id:
            continue
        error = group.get("models_endpoint_error")
        if isinstance(error, dict):
            error["checked_at"] = time.time() - seconds
            return True
    return False


# ── Reporter reproduction (script ``repro_stale_endpoint_error.py``) ──────────


@pytest.mark.reproduction
def test_issue8158_transient_probe_failure_then_recovery_on_warm_read(monkeypatch, tmp_path):
    """A/B/C: one transient probe failure must not be served for 24h.

    Stage A stores the diagnostic from an unresolvable host. Stage B restores the
    real ``base_url`` in memory — on-disk config untouched, so no source
    fingerprint axis changes, exactly like the network simply recovering — and
    reads the catalog the way the picker does. Stage C is the manual
    per-provider refresh (a forced rebuild), which is expected to keep working.
    """
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState():
        # ── Stage A: unresolvable host, in memory only ────────────────────────
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        data = config.get_available_models()
        error = _endpoint_error(data)
        assert error is not None, "stage A must store the probe diagnostic"
        assert error["kind"] == "network"
        assert error["code"] is None
        assert "verify base_url" in error["message"]
        assert config._models_cache_source_fingerprint is not None
        fingerprint_before = config._available_models_cache_source_fingerprint

        # ── Stage B: endpoint healthy again; config on disk unchanged ─────────
        probe.healthy = True
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://healthy.example/v1"
        )
        # The in-memory config edit is invisible to every fingerprint axis: this
        # is the shape of the network recovering, not of a config change.
        assert config._models_cache_source_fingerprint() == fingerprint_before

        probes_before = _probe_count(probe)
        served = config.get_available_models()
        assert _probe_count(probe) <= probes_before + 1, "the warm read must not fan out"

        # The diagnostic is expired, so a coalesced re-probe is scheduled and its
        # result replaces the stale diagnostic without waiting out the 24h TTL.
        assert _wait_for(lambda: _endpoint_error(config.get_available_models()) is None), (
            "a reachable endpoint must stop reporting 'unreachable'; "
            f"warm read still served {_endpoint_error(served)!r}"
        )
        assert _probe_count(probe) > probes_before, "recovery requires a re-probe"

        # ── Stage C: the manual refresh exit still works ──────────────────────
        refreshed = config.get_available_models(force_refresh=True)
        assert _endpoint_error(refreshed) is None
        assert _groups_by_provider(refreshed)[_UNRESOLVABLE]["models"], (
            "stage C must surface the recovered catalog"
        )


@pytest.mark.reproduction
def test_issue8158_error_lifetime_is_short_and_bounded():
    """The diagnostic's retry lifetime must be seconds-to-minutes, not the 24h TTL."""
    lifetime = getattr(config, "_MODELS_ENDPOINT_ERROR_RETRY_SECONDS", None)
    assert lifetime is not None, "the diagnostic needs its own bounded lifetime"
    assert 0 < lifetime <= 300, (
        "a transient probe diagnostic must expire quickly (issue #8158); "
        f"got {lifetime!r}"
    )
    assert lifetime < config._AVAILABLE_MODELS_CACHE_TTL


# ── The diagnostic must not be re-probed on every warm read ──────────────────


def test_unexpired_diagnostic_is_served_without_reprobing(monkeypatch, tmp_path):
    """A fresh diagnostic is still current — warm reads must not probe for it.

    The pickup notes are explicit that the healthy catalog keeps its cache and
    that warm reads must not grow a synchronous network probe; the same applies
    to scheduling one for every read while a recent diagnostic is still valid.
    """
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    _expire_retries(monkeypatch, 3600.0)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        first = config.get_available_models()
        assert _endpoint_error(first) is not None

        probes_before = _probe_count(probe)
        for _ in range(5):
            served = config.get_available_models()
            assert _endpoint_error(served) is not None
        assert _probe_count(probe) == probes_before, (
            "an unexpired diagnostic must not be re-probed on warm reads"
        )
        assert not _reprobe_inflight(), "no re-probe may be scheduled while unexpired"


def test_warm_read_serves_immediately_while_retry_probe_is_in_flight(monkeypatch, tmp_path):
    """The retry must not block the read path it was scheduled from.

    A hung endpoint is exactly the case that produced the diagnostic, so a warm
    read that waited on the re-probe would turn a cached picker payload into a
    request that stalls for the endpoint cap.
    """
    probe = _Probe(healthy=True)
    probe.gate = threading.Event()
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        # Seed the diagnostic directly: the healthy probe would not produce one.
        probe.healthy = False
        config.get_available_models()
        probe.healthy = True

        started = time.monotonic()
        served = config.get_available_models()
        elapsed = time.monotonic() - started
        assert served is not None
        # The read returns the warm payload while the gated probe is parked.
        assert elapsed < 1.0, f"warm read blocked for {elapsed:.2f}s on the re-probe"
        assert probe.entered.wait(5.0), "the retry probe should have started"
        assert _endpoint_error(served) is not None, "the still-in-flight probe cannot clear yet"
        probe.gate.set()
        assert _wait_for(lambda: _endpoint_error(config.get_available_models()) is None)


def test_concurrent_readers_coalesce_into_one_reprobe(monkeypatch, tmp_path):
    """N warm readers of one provider produce exactly one re-probe."""
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        config.get_available_models()
        assert _endpoint_error(config.get_available_models()) is not None

        # Park the probe so every reader observes the in-flight state.
        probe.gate = threading.Event()
        probes_before = _probe_count(probe)
        barrier = threading.Barrier(8)

        def _read():
            barrier.wait(5.0)
            config.get_available_models()

        readers = [threading.Thread(target=_read) for _ in range(8)]
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join(10.0)
        assert probe.entered.wait(5.0)
        assert _probe_count(probe) == probes_before + 1, (
            "concurrent readers must coalesce onto one re-probe per provider; "
            f"observed {_probe_count(probe) - probes_before}"
        )
        probe.gate.set()
        _wait_for_reprobe_idle(5.0)


def test_failed_reprobe_restamps_instead_of_retrying_on_every_read(monkeypatch, tmp_path):
    """A still-unreachable endpoint is retried once per lifetime, not per read."""
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    # A real window (not 0): the point under test is that the *re-stamp* from the
    # failed retry restores a current diagnostic, so reads stop retrying.
    _expire_retries(monkeypatch, 60.0)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        config.get_available_models()
        # Age the diagnostic past the window so the next read retries it.
        _age_cached_diagnostic(3600.0)
        first_stamp = _endpoint_error(config.get_available_models())["checked_at"]

        assert _wait_for(
            lambda: _endpoint_error(config.get_available_models())["checked_at"] != first_stamp
        ), "the failed re-probe must re-stamp so the diagnostic is current again"
        probes_after_restamp = _probe_count(probe)
        for _ in range(5):
            config.get_available_models()
        assert _probe_count(probe) == probes_after_restamp, (
            "after re-stamping, further reads must not probe again"
        )


# ── Isolation and fencing ────────────────────────────────────────────────────


def test_reprobe_scope_is_per_provider(monkeypatch, tmp_path):
    """Two errored providers are probed independently, not one shared probe."""
    probed: list[str] = []
    lock = threading.Lock()

    def _fake_reprobe(provider_id: str):
        with lock:
            probed.append(provider_id)
        return [{"id": f"{provider_id}-model", "label": f"{provider_id}-model"}], None

    monkeypatch.setattr(config, "_reprobe_provider_endpoint", _fake_reprobe)
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState():
        monkeypatch.setattr(
            config, "_models_cache_path", tmp_path / "models_cache.json"
        )
        monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
        config.cfg = {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "providers": {},
            "fallback_providers": [],
            "custom_providers": [
                {"name": "Alpha", "base_url": "https://alpha.example/v1", "api_key": "k"},
                {"name": "Beta", "base_url": "https://beta.example/v1", "api_key": "k"},
            ],
        }
        # Seed both groups with an expired diagnostic directly so the test does
        # not depend on the probe order of one rebuild.
        expired = config._custom_endpoint_error("x")
        expired["checked_at"] = time.time() - 3600
        groups = [
            {"provider": "Alpha", "provider_id": "custom:alpha", "models": [],
             "models_endpoint_error": dict(expired)},
            {"provider": "Beta", "provider_id": "custom:beta", "models": [],
             "models_endpoint_error": dict(expired)},
        ]
        config._available_models_cache = {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.5",
            "configured_model_badges": {},
            "groups": groups,
        }
        config._available_models_cache_ts = time.monotonic()
        config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()
        config._sync_models_cache_provenance()

        config.get_available_models()
        assert _wait_for(lambda: len(probed) >= 2), f"both providers must be retried; got {probed}"
        assert sorted(set(probed)) == ["custom:alpha", "custom:beta"]
        for provider_id in ("custom:alpha", "custom:beta"):
            assert _wait_for(
                lambda pid=provider_id: _endpoint_error(config.get_available_models(), pid) is None
            ), f"{provider_id} must be cleared by its own re-probe"


def test_reprobe_coalescing_key_includes_the_profile(monkeypatch):
    """The in-flight key is (profile, provider), so profiles cannot share a probe."""
    monkeypatch.setattr(
        config, "_reprobe_scope_key", config._reprobe_scope_key, raising=False
    )
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-a")
    scope_a = config._reprobe_scope_key("custom:shared-slug")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-b")
    scope_b = config._reprobe_scope_key("custom:shared-slug")

    assert scope_a == ("profile-a", "custom:shared-slug")
    assert scope_b == ("profile-b", "custom:shared-slug")
    assert scope_a != scope_b, (
        "two profiles exposing the same provider slug must not coalesce onto "
        "one probe: their credentials and endpoints are per-profile"
    )


def test_late_result_from_superseded_generation_is_discarded(monkeypatch, tmp_path):
    """A probe that finishes after the catalog was rebuilt must not be applied.

    The reporter's stage C (the manual per-provider refresh) allocates a new
    catalog generation. A retry probe that was already in flight belongs to the
    generation it was scheduled for, so its late result must be dropped rather
    than merged into the catalog that replaced it.
    """
    probe_started = threading.Event()
    release = threading.Event()

    def _slow_reprobe(provider_id: str):
        probe_started.set()
        release.wait(5.0)
        # Deliberately "recovered": if the fence were missing, this would clear
        # the diagnostic the newer generation established.
        return [{"id": "stale-model", "label": "stale-model"}], None

    monkeypatch.setattr(config, "_reprobe_provider_endpoint", _slow_reprobe)
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        monkeypatch.setattr(urllib.request, "urlopen", _Probe(healthy=False))
        _expire_retries(monkeypatch, 0.0)

        # Build the catalog (unreachable → diagnostic), then let the next read
        # schedule the slow re-probe against that expired state.
        config.get_available_models()
        assert _age_cached_diagnostic(3600.0)
        config.get_available_models()
        assert probe_started.wait(5.0), "the retry probe must have started"

        # A newer generation: the manual refresh (stage C) — still unreachable, so
        # the new catalog legitimately carries a fresh diagnostic.
        refreshed = config.get_available_models(force_refresh=True)
        assert _endpoint_error(refreshed) is not None
        fresh_stamp = _endpoint_error(refreshed)["checked_at"]

        release.set()
        assert _wait_for_reprobe_idle(5.0)
        # Give the (fenced) late result a moment to be dropped.
        time.sleep(0.2)
        after = _endpoint_error(config.get_available_models())
        assert after is not None, (
            "a late result from a superseded generation must not clear the "
            "diagnostic the newer catalog established"
        )
        assert after["checked_at"] == fresh_stamp


# ── Clearing on a successful same-owner live probe ───────────────────────────


def _seed_expired_diagnostic(*, with_models=False, stamp_age=3600.0) -> None:
    expired = config._custom_endpoint_error("broken-proxy")
    expired["checked_at"] = time.time() - stamp_age
    groups = [
        {
            "provider": "Broken Proxy",
            "provider_id": _UNRESOLVABLE,
            "models": ([{"id": "@custom:broken-proxy:sticky", "label": "sticky"}] if with_models else []),
            "models_endpoint_error": expired,
        },
        {"provider": "Other", "provider_id": "custom:other", "models": []},
    ]
    config._available_models_cache = {
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": groups,
    }
    config._available_models_cache_ts = time.monotonic()
    config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()
    config._sync_models_cache_provenance()


def test_live_probe_success_clears_the_cached_diagnostic(monkeypatch, tmp_path):
    """A successful /api/models/live probe clears the same provider's diagnostic.

    This is the reporter's exact situation: ``/api/models/live`` already
    succeeded while the catalog kept reporting "unreachable", because the live
    path never touched the group's cached diagnostic.
    """
    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=True)
        assert _endpoint_error(config.get_available_models()) is not None

        assert config.clear_models_endpoint_error_for_provider("custom:broken-proxy") is True
        assert _endpoint_error(config.get_available_models()) is None

        # The zero-model error group itself is the #7521 contract: keep it.
        group = _groups_by_provider(config.get_available_models())[_UNRESOLVABLE]
        assert group is not None, "clearing the diagnostic must not drop the group"


def test_clearing_keeps_the_zero_model_error_group(monkeypatch, tmp_path):
    """Clearing must not silently drop an empty provider group (#7521)."""
    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=False)
        assert _groups_by_provider(config.get_available_models())[_UNRESOLVABLE]["models"] == []

        assert config.clear_models_endpoint_error_for_provider(_UNRESOLVABLE) is True
        served = config.get_available_models()
        assert _UNRESOLVABLE in _groups_by_provider(served), (
            "the intentionally-retained zero-model error group must survive the clear"
        )


def test_clearing_an_unaffected_other_provider_is_a_noop(monkeypatch, tmp_path):
    """The clear is scoped: a sibling provider's diagnostic is untouched."""
    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=True)

        assert config.clear_models_endpoint_error_for_provider("custom:unrelated") is False
        assert _endpoint_error(config.get_available_models()) is not None
        # And the failed lookup must not have rewritten the snapshot.
        assert config._available_models_cache_ts > 0.0


def test_missing_stamp_counts_as_expired(monkeypatch, tmp_path):
    """A pre-upgrade cache has no stamp, so its first read must re-probe.

    The declared default for the open schema question: a diagnostic written
    before this field existed cannot be trusted to be current.
    """
    assert config._models_endpoint_error_retry_expired(
        {"kind": "network", "code": None, "message": "old"}
    ), "a diagnostic without a stamp must be treated as expired"
    assert config._models_endpoint_error_retry_expired(
        {"kind": "network", "checked_at": "not-a-number"}
    ), "a non-numeric stamp must be treated as expired"
    assert config._models_endpoint_error_retry_expired(
        {"kind": "network", "checked_at": time.time() + 3600}
    ), "a future stamp (clock step) must fail closed toward re-probing"
    assert not config._models_endpoint_error_retry_expired(
        {"kind": "network", "checked_at": time.time()}
    ), "a fresh stamp is current"
    assert config._models_endpoint_error_retry_expired(
        {"kind": "network", "checked_at": time.time() - 3600}
    ), "an hour-old stamp is expired"


def test_transport_kinds_carry_a_stamp(monkeypatch):
    """Every diagnostic the rebuild can record is stampable and dated."""
    for exc, code, kind in (
        (urllib.error.URLError("dns"), None, "network"),
        (None, 401, "auth"),
        (None, 502, "http"),
    ):
        error = config._custom_endpoint_error("broken-proxy", exc, code=code)
        assert error["kind"] == kind
        assert isinstance(error["checked_at"], float), kind
        assert not config._models_endpoint_error_retry_expired(error), kind
