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
import socket
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

    def __init__(self, join_reprobes):
        self.join_reprobes = join_reprobes

    def __enter__(self):
        self.old_cfg = config.cfg
        self.old_path = config._cfg_path
        self.old_mtime = config._cfg_mtime
        self.old_cache = config._available_models_cache
        self.old_cache_ts = config._available_models_cache_ts
        self.old_cache_fp = config._available_models_cache_source_fingerprint
        self.old_live_budget = config._LIVE_REBUILD_BUDGET_SECONDS
        self.old_provenance = config._models_cache_provenance
        # Every seeded snapshot must match disk identity without loading over cfg.
        config._cfg_path = config._get_config_path()
        try:
            config._cfg_mtime = config._cfg_path.stat().st_mtime
        except OSError:
            config._cfg_mtime = 0.0
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_cache_source_fingerprint = None
        config._sync_models_cache_provenance()
        # Synchronous rebuild path: keeps stages A/C deterministic instead of
        # racing the bounded out-of-band worker.
        config._LIVE_REBUILD_BUDGET_SECONDS = 0.0
        assert not _reprobe_inflight(), "a previous test left a re-probe registered"
        return self

    def __exit__(self, exc_type, exc, tb):
        self.join_reprobes()
        config.cfg = self.old_cfg
        config._cfg_path = self.old_path
        config._cfg_mtime = self.old_mtime
        config._available_models_cache = self.old_cache
        config._available_models_cache_ts = self.old_cache_ts
        config._available_models_cache_source_fingerprint = self.old_cache_fp
        config._models_cache_provenance = self.old_provenance
        config._LIVE_REBUILD_BUDGET_SECONDS = self.old_live_budget
        return False


def _reprobe_inflight() -> object:
    with config._models_endpoint_reprobe_lock:
        return dict(config._models_endpoint_reprobe_inflight)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _wait_for_reprobe_idle(timeout: float = 5.0) -> bool:
    """True once no re-probe worker is registered as in flight.

    This observes the registry; it does NOT clean it up. A test-owned worker that
    never finishes must fail loudly in the fixture teardown, not be papered over
    by clearing state here.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _reprobe_inflight():
            return True
        time.sleep(0.01)
    return not _reprobe_inflight()


def _sync_config_version() -> None:
    """Stamp the loaded-config identity so a seeded snapshot matches production.

    Production discards a warm catalog whose ``config.yaml`` mtime differs from
    the one the running config was loaded with. ``_ConfigState`` clears
    ``_cfg_mtime``, so any test that seeds a catalog into the cache directly must
    re-stamp both the path and the mtime afterwards — otherwise the first read
    sees a "changed" config, invalidates the seeded snapshot, and cold-rebuilds
    over the state the test just set up. Every manual seeding helper goes
    through here so the invariant has exactly one home.
    """
    config_path = config._get_config_path()
    config._cfg_path = config_path
    try:
        config._cfg_mtime = config_path.stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0


def _publish_test_catalog(cache: dict) -> None:
    """Publish a hand-built catalog the way a real rebuild would.

    Shared by every test that seeds a snapshot directly, so the config-version
    stamp, the source fingerprint, and the provenance pair always move together.
    """
    _sync_config_version()
    config._available_models_cache = cache
    config._available_models_cache_ts = time.monotonic()
    config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()
    config._sync_models_cache_provenance()


@pytest.fixture(autouse=True)
def config_exists(request, monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    if getattr(request, "param", False):
        config_path.write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)


@pytest.fixture(autouse=True)
def _reprobe_threads(monkeypatch):
    """Keep fake network and test state installed until owned workers finish."""
    monkeypatch.setattr(urllib.request, "urlopen", _Probe(healthy=False))
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port or 0))
        ],
    )
    workers = []
    lock = threading.Lock()
    real_start = threading.Thread.start

    def _start(thread, *args, **kwargs):
        if thread.name != "models-endpoint-error-reprobe":
            return real_start(thread, *args, **kwargs)
        with lock:
            result = real_start(thread, *args, **kwargs)
            workers.append(thread)
        return result

    def _join(timeout=5.0):
        deadline = time.monotonic() + timeout
        with lock:
            owned = tuple(workers)
        for worker in owned:
            worker.join(max(0.0, deadline - time.monotonic()))
        assert not any(worker.is_alive() for worker in owned), "a test-owned re-probe did not finish"
        assert not _reprobe_inflight(), "finished re-probes must remove their registry entries"

    monkeypatch.setattr(threading.Thread, "start", _start)
    try:
        yield _join
    finally:
        _join()


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


def _seed_expired_diagnostic_catalog(monkeypatch, tmp_path, *, base_url="https://broken.example/v1"):
    """Publish a warm catalog whose named custom provider carries an expired error.

    Seeds the state directly (expired stamp on a real rebuild's diagnostic) rather
    than forcing the expiry window open and re-reading, so tests that need "an
    expired diagnostic, nothing in flight" start from a known quiet state: no
    probe is scheduled by this helper, and no rebuild is triggered by it.
    """
    _configure_named_custom_provider(tmp_path, monkeypatch, base_url=base_url)
    # A cheap real rebuild would probe; go straight to the published shape instead.
    error = config._custom_endpoint_error(_UNRESOLVABLE)
    error["checked_at"] = time.time() - 3600
    _publish_test_catalog({
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "Broken Proxy",
                "provider_id": _UNRESOLVABLE,
                "models": [],
                "models_endpoint_error": error,
            }
        ],
    })
    # Verify by inspecting the published snapshot directly. Calling
    # get_available_models() here would itself serve the expired diagnostic and
    # therefore schedule the very probe these tests want to observe starting, so
    # the helper must stay read-free to leave the registry quiet.
    published = config._available_models_cache
    assert isinstance(published, dict) and published["groups"][0]["models_endpoint_error"]
    assert not _reprobe_inflight(), "seeding must not leave a probe in flight"


# ── Reporter reproduction (script ``repro_stale_endpoint_error.py``) ──────────


@pytest.mark.reproduction
def test_issue8158_transient_probe_failure_then_recovery_on_warm_read(monkeypatch, tmp_path, _reprobe_threads):
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

    with _ConfigState(_reprobe_threads):
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


def test_unexpired_diagnostic_is_served_without_reprobing(monkeypatch, tmp_path, _reprobe_threads):
    """A fresh diagnostic is still current — warm reads must not probe for it.

    The pickup notes are explicit that the healthy catalog keeps its cache and
    that warm reads must not grow a synchronous network probe; the same applies
    to scheduling one for every read while a recent diagnostic is still valid.
    """
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    _expire_retries(monkeypatch, 3600.0)

    with _ConfigState(_reprobe_threads):
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


@pytest.mark.parametrize("config_exists", [False, True], ids=["missing-config", "existing-config"])
def test_warm_read_serves_immediately_while_retry_probe_is_in_flight(
    monkeypatch, tmp_path, config_exists, _reprobe_threads
):
    """The retry must not block the read path it was scheduled from.

    A hung endpoint is exactly the case that produced the diagnostic, so a warm
    read that waited on the re-probe would turn a cached picker payload into a
    request that stalls for the endpoint cap.
    """
    config_path = tmp_path / "config.yaml"
    if config_exists:
        config_path.write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "_cfg_path", config_path)
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)

    with _ConfigState(_reprobe_threads):
        _seed_expired_diagnostic_catalog(monkeypatch, tmp_path)
        # Only now park the endpoint: the seeded snapshot must not be produced
        # through a read that could block on the gate.
        probe.healthy = True
        probe.gate = threading.Event()

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


def test_concurrent_readers_coalesce_into_one_reprobe(monkeypatch, tmp_path, _reprobe_threads):
    """N warm readers of one provider produce exactly one re-probe."""
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)

    with _ConfigState(_reprobe_threads):
        _seed_expired_diagnostic_catalog(monkeypatch, tmp_path)

        # Park the endpoint so the single coalesced probe stays in flight and is
        # countable. Starting from an idle registry with a parked gate makes the
        # expected count exactly one new probe for all eight readers.
        assert _wait_for_reprobe_idle(5.0), "no probe may be in flight before the readers"
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
        assert probe.entered.wait(5.0), "the coalesced re-probe must start"
        assert _probe_count(probe) - probes_before == 1, (
            "concurrent readers must coalesce onto one re-probe per provider; "
            f"observed {_probe_count(probe) - probes_before}"
        )
        probe.gate.set()
        _wait_for_reprobe_idle(5.0)


def test_failed_reprobe_restamps_instead_of_retrying_on_every_read(monkeypatch, tmp_path, _reprobe_threads):
    """A still-unreachable endpoint is retried once per lifetime, not per read."""
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    # A real window (not 0): the point under test is that the *re-stamp* from the
    # failed retry restores a current diagnostic, so reads stop retrying.
    _expire_retries(monkeypatch, 60.0)

    with _ConfigState(_reprobe_threads):
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


def test_unresolvable_provider_does_not_clear_its_diagnostic(monkeypatch, tmp_path, _reprobe_threads):
    """A provider that no longer resolves to a configured entry keeps its error.

    "No entry to probe" is not evidence that the endpoint recovered, so it must
    not be reported as an empty success — that would erase a real diagnostic on
    the strength of a failed lookup.
    """
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState(_reprobe_threads):
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        # A diagnostic for a provider that is not in custom_providers at all.
        expired = config._custom_endpoint_error("ghost")
        expired["checked_at"] = time.time() - 3600
        _publish_test_catalog({
            "active_provider": "openai-codex",
            "default_model": "gpt-5.5",
            "configured_model_badges": {},
            "groups": [
                {"provider": "Ghost", "provider_id": "custom:ghost", "models": [],
                 "models_endpoint_error": expired}
            ],
        })

        assert config._resolve_endpoint_probe_request("custom:ghost") is None, (
            "an id with no configured entry must report 'nothing to probe'"
        )
        config.get_available_models()
        assert _wait_for_reprobe_idle(5.0)
        assert _endpoint_error(config.get_available_models(), "custom:ghost") is not None, (
            "a failed lookup must leave the diagnostic intact"
        )


def test_reprobe_scope_is_per_provider(monkeypatch, tmp_path, _reprobe_threads):
    """Two errored providers are probed independently, not one shared probe."""
    probed: list[str] = []
    lock = threading.Lock()

    def _fake_reprobe(request):
        with lock:
            probed.append(request.provider_id)
        return [{"id": f"{request.provider_id}-model", "label": "m"}], None

    monkeypatch.setattr(config, "_reprobe_provider_endpoint", _fake_reprobe)
    _expire_retries(monkeypatch, 0.0)

    with _ConfigState(_reprobe_threads):
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
        _publish_test_catalog({
            "active_provider": "openai-codex",
            "default_model": "gpt-5.5",
            "configured_model_badges": {},
            "groups": [
                {"provider": "Alpha", "provider_id": "custom:alpha", "models": [],
                 "models_endpoint_error": dict(expired)},
                {"provider": "Beta", "provider_id": "custom:beta", "models": [],
                 "models_endpoint_error": dict(expired)},
            ],
        })

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


def test_late_result_from_superseded_generation_is_discarded(monkeypatch, tmp_path, _reprobe_threads):
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

    with _ConfigState(_reprobe_threads):
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
    _publish_test_catalog({
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "Broken Proxy",
                "provider_id": _UNRESOLVABLE,
                "models": ([{"id": "@custom:broken-proxy:sticky", "label": "sticky"}] if with_models else []),
                "models_endpoint_error": expired,
            },
            {"provider": "Other", "provider_id": "custom:other", "models": []},
        ],
    })


def _run_live_probe(monkeypatch, provider_id: str, *, body=None, raises=None):
    """Drive the real ``/api/models/live`` handler for one custom provider.

    Going through the handler (rather than calling the clear helper) is the point:
    it is the handler that captures the probe identity before the request and
    decides what counts as success evidence, so a helper-only test would stay green
    even if the handler wiring were removed.
    """
    import api.routes as routes
    from tests.test_issue3718_live_models_custom_probe import _install_provider_model_ids

    routes._clear_live_models_cache()
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, **_kw: payload
    )
    _install_provider_model_ids(monkeypatch, lambda _provider: [])

    def _urlopen(_req, **_kwargs):
        if raises is not None:
            raise raises
        return _FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return routes._handle_live_models(
        object(), urlparse(f"/api/models/live?provider={provider_id}")
    )


def test_live_probe_success_clears_the_cached_diagnostic(monkeypatch, tmp_path, _reprobe_threads):
    """A successful /api/models/live probe clears the same provider's diagnostic.

    This is the reporter's exact situation: ``/api/models/live`` already
    succeeded while the catalog kept reporting "unreachable", because the live
    path never touched the group's cached diagnostic.
    """
    with _ConfigState(_reprobe_threads):
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=True)
        assert _endpoint_error(config.get_available_models()) is not None

        _run_live_probe(
            monkeypatch, _UNRESOLVABLE, body={"data": [{"id": "broken-proxy-model"}]}
        )
        assert _endpoint_error(config.get_available_models()) is None

        # The zero-model error group itself is the #7521 contract: keep it.
        group = _groups_by_provider(config.get_available_models())[_UNRESOLVABLE]
        assert group is not None, "clearing the diagnostic must not drop the group"


def test_clearing_keeps_the_zero_model_error_group(monkeypatch, tmp_path, _reprobe_threads):
    """Clearing must not silently drop an empty provider group (#7521)."""
    with _ConfigState(_reprobe_threads):
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=False)
        assert _groups_by_provider(config.get_available_models())[_UNRESOLVABLE]["models"] == []

        # A well-formed but empty answer: the endpoint replied, so it is not
        # unreachable — yet the group has no models to show.
        _run_live_probe(monkeypatch, _UNRESOLVABLE, body={"data": []})
        served = config.get_available_models()
        assert _UNRESOLVABLE in _groups_by_provider(served), (
            "the intentionally-retained zero-model error group must survive the clear"
        )


def test_failed_live_probe_keeps_the_diagnostic(monkeypatch, tmp_path, _reprobe_threads):
    """A transport failure is not recovery evidence — the diagnostic stays."""
    with _ConfigState(_reprobe_threads):
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=True)
        before = _endpoint_error(config.get_available_models())

        _run_live_probe(
            monkeypatch, _UNRESOLVABLE, raises=urllib.error.URLError("still down")
        )
        after = _endpoint_error(config.get_available_models())
        assert after is not None, "a failed live probe must not clear the diagnostic"
        # The stamp may legitimately advance: the expired diagnostic also has a
        # background re-probe in flight, and a failed re-probe re-stamps. What
        # must not change is that the diagnostic still describes the failure.
        assert (after["kind"], after["code"], after["message"]) == (
            before["kind"], before["code"], before["message"]
        )


def test_clearing_an_unaffected_other_provider_is_a_noop(monkeypatch, tmp_path, _reprobe_threads):
    """The clear is scoped: a sibling provider's diagnostic is untouched."""
    with _ConfigState(_reprobe_threads):
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        _seed_expired_diagnostic(with_models=True)

        identity = config.capture_live_probe_identity()
        assert config.clear_models_endpoint_error_for_provider("custom:unrelated", identity) is False
        assert _endpoint_error(config.get_available_models()) is not None
        # And the failed lookup must not have rewritten the snapshot.
        assert config._available_models_cache_ts > 0.0


def test_disk_borne_diagnostic_is_retried_from_the_read_that_serves_it(monkeypatch, tmp_path, _reprobe_threads):
    """A diagnostic restored from the durable catalog is retried immediately.

    After a restart the picker is served straight from ``models_cache.json``
    without going through the memory read path, so the retry has to be scheduled
    where that payload is published — otherwise the first read of every boot
    re-serves the stale message.
    """
    probe = _Probe(healthy=False)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    _expire_retries(monkeypatch, 3600.0)

    with _ConfigState(_reprobe_threads):
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://broken.example/v1"
        )
        configured = config.get_available_models()
        assert _endpoint_error(configured) is not None
        disk_path = tmp_path / "models_cache.json"
        assert disk_path.exists(), "the catalog must have reached the durable cache"

        # Simulate a restart: process-global memory cache gone, disk cache intact.
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_cache_source_fingerprint = None
        config._sync_models_cache_provenance()
        # Age the durable diagnostic past its window (the stamp is what expires).
        payload = json.loads(disk_path.read_text(encoding="utf-8"))
        for group in payload["groups"]:
            if group.get("models_endpoint_error"):
                group["models_endpoint_error"]["checked_at"] = time.time() - 3600
        disk_path.write_text(json.dumps(payload), encoding="utf-8")

        probe.healthy = True
        probes_before = _probe_count(probe)
        started = time.monotonic()
        first_read = config.get_available_models()
        elapsed = time.monotonic() - started
        assert _endpoint_error(first_read) is not None, "the durable payload is served as-is"
        assert elapsed < 1.0, f"the read must not wait on the retry ({elapsed:.2f}s)"
        assert _wait_for(
            lambda: _probe_count(probe) > probes_before
        ), "the read that served a durable diagnostic must start the retry"


def test_missing_stamp_counts_as_expired(monkeypatch, tmp_path, _reprobe_threads):
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
