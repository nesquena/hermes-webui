"""Regression coverage for #7481 — serial custom-provider probes starving.

During a cold model-catalog rebuild the WebUI probes the active endpoint first
(``model.base_url``) and then each named ``custom_providers`` entry, serially,
off one shared ``_LIVE_REBUILD_BUDGET_SECONDS`` budget while each probe is
individually capped at ``CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS``.

Before the fix one unreachable endpoint — the common case being a LAN
LM Studio/Ollama host the webui container cannot route to, because the probe runs
server-side — spent the whole budget on its own connect timeout, so every
reachable provider scheduled behind it never got an in-band ``/v1/models``
probe: its group rendered from a stale disk cache or stayed empty, and every
cold load paid the full stall.

The fix (``_CustomProbeSchedule``) gives every probe a fair slice of the
remaining window instead of the whole cap, and the LM Studio provider-group
fallback — a second consumer of the same dead endpoint, previously on a
hardcoded 5s timeout outside any budget — now draws from the same schedule.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import api.config as cfg
import api.profiles as profiles


# Models the reachable gateway advertises — what the picker must show in-band.
_GATEWAY_MODELS = ["gateway-model-a", "gateway-model-b"]

# The issue's own numbers, scaled down only where noted.
_BUDGET = 4.0
_CAP = 1.5

# Deliberately widened budget/cap pair for the end-to-end publication assertion
# (the repro test). The rebuild worker's deadline is a *real* OS-clock wait —
# ``build_done.wait(timeout=_LIVE_REBUILD_BUDGET_SECONDS)`` in
# ``get_available_models`` — which the injected ``_FakeClock`` cannot
# virtualise, so on a slow runner the issue's 4s window can be crossed mid-build
# and the over-budget fallback served without the gateway. Widening the window
# makes the assertion depend on the fix rather than on the runner: the repro's
# locally-served, non-sleeping probes finish in milliseconds against 40s, while
# the "slice < cap" rule still holds — its three scheduled slots get
# ``40 / (3 + 1) = 10 < 20``.
_WIDE_BUDGET = 40.0
_WIDE_CAP = 20.0


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _install_urlopen(monkeypatch, *, dead_hosts, live_hosts, clock=None):
    """Route probes by host and record the timeout each one was handed.

    A "dead" host behaves the way an unreachable LAN endpoint does: it burns the
    entire timeout it was given and then fails — exactly the behaviour that used
    to eat the shared rebuild budget.

    ``clock`` (a ``_FakeClock`` already installed as ``cfg.time``) makes that burn
    *virtual* instead of real. A real sleep ties the budget arithmetic to how fast
    the runner is: on a slower box the active endpoint's sleep crosses the 4s
    deadline before the later endpoints are reached, so the chain is cut off in a
    way these tests were not written to describe. Advancing the injected clock
    keeps the budget exact and the ordering assertions meaningful on any runner.

    Returns ``{"dead": [(url, timeout)], "live": [(url, timeout)],
    "order": [url, ...]}`` — ``order`` is the flat probe sequence across both
    categories, so a test can assert the visit order rather than only per-host.
    """
    observed: dict[str, list] = {"dead": [], "live": [], "order": []}

    def fake_urlopen(req, timeout=None):
        url = str(getattr(req, "full_url", ""))
        if any(host in url for host in dead_hosts):
            observed["dead"].append((url, timeout))
            observed["order"].append(url)
            if clock is not None:
                clock.now += float(timeout or 0.0)
            else:
                time.sleep(timeout if timeout is not None else 10)
            raise urllib.error.URLError("timed out")
        if any(host in url for host in live_hosts):
            observed["live"].append((url, timeout))
            observed["order"].append(url)
            return _FakeResponse({"data": [{"id": mid} for mid in _GATEWAY_MODELS]})
        raise urllib.error.URLError(f"unexpected probe: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return observed


@pytest.fixture(autouse=True)
def isolate_models_catalog_state(monkeypatch, tmp_path):
    """Hermetic catalog state, mirroring the #3928 budget-fallback fixture."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    auth_store_path = tmp_path / "auth.json"
    auth_store_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(cfg, "_get_auth_store_path", lambda: auth_store_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: "issue-7481-fp")
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(cfg, "_models_rebuild_seq", 0, raising=False)
    monkeypatch.setattr(cfg, "_models_published_seq", 0, raising=False)
    monkeypatch.setattr(cfg, "cfg", {}, raising=False)
    # Any provider left in the catalog would otherwise shell out to the Hermes
    # CLI for a live id list; the rebuild must stay network-free apart from the
    # custom endpoints under test.
    monkeypatch.setattr(cfg, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path / "hermes-home")
    monkeypatch.setattr(cfg.os, "getenv", lambda key, default=None: default or "")
    # The probe path resolves the endpoint hostname for its SSRF guard. A real
    # resolver makes these tests depend on the host's DNS behaviour (and this
    # container takes seconds to answer NXDOMAIN), so pin it to an immediate
    # failure: the guard treats that as "not resolvable" and lets the probe
    # through, which is exactly what the fake urlopen above is standing in for.
    def _unresolvable(host, port, *args, **kwargs):
        raise socket.gaierror("hermetic test resolver")

    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)

    return {"tmp_path": tmp_path, "auth_store_path": auth_store_path}


def _configure(monkeypatch, *, active_base_url, provider_base_url=None, custom_providers=None):
    cfg.cfg = {
        "model": {
            "provider": "lmstudio",
            "default": "some-local-model",
            "base_url": active_base_url,
        },
        "providers": (
            {"lmstudio": {"base_url": provider_base_url}} if provider_base_url else {}
        ),
        "fallback_providers": [],
        "custom_providers": custom_providers or [],
    }
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", _BUDGET, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", _CAP, raising=False)


def _bare_id(model_id: str) -> str:
    """``@custom:my-gateway:model-a`` -> ``model-a``."""
    return str(model_id).split(":")[-1]


def _models_by_provider(catalog: dict) -> dict[str, list[str]]:
    return {
        group["provider_id"]: [_bare_id(m.get("id")) for m in group.get("models", [])]
        for group in catalog["groups"]
    }


class _FakeClock:
    """Stand-in for the ``time`` module so a schedule can be advanced by hand.

    Only ``monotonic()`` is virtual — that is the clock ``_CustomProbeSchedule``
    measures the rebuild window with, and the one a mock probe advances when it
    "burns" its slice. ``time()`` stays real so the epoch-based comparisons
    elsewhere in the module (cache file ages, credential-pool TTLs) keep their
    meaning; nothing in ``api/config.py`` calls ``time.sleep``.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def _catalog(name: str) -> dict:
    """A minimal catalog tagged so a test can tell which build produced it."""
    return {
        "active_provider": name,
        "default_model": f"{name}/model",
        "configured_model_badges": {},
        "groups": [{"provider": name.title(), "provider_id": name, "models": []}],
        "aliases": {},
    }


def test_unreachable_lan_active_endpoint_does_not_starve_the_gateway_behind_it(
    monkeypatch, isolate_models_catalog_state
):
    """The #7481 repro, using the issue's own config shape.

    A dead LAN endpoint is configured both as the active provider and as
    ``providers.lmstudio`` (so the provider-group fallback probes it a second
    time). The reachable named gateway behind it must still get its in-band
    probe — not merely be deferred to an out-of-band refresh after a fallback
    was served.

    Machine-speed independence (maintainer review, 2026-09-11). Installing
    ``_FakeClock`` makes the *schedule's* window arithmetic exact, but the
    budget is ultimately enforced by the rebuild worker's
    ``build_done.wait(timeout=_LIVE_REBUILD_BUDGET_SECONDS)``, a real OS-clock
    wait the injected clock cannot virtualise. On a slow runner the build can
    therefore cross the 4s deadline mid-call, the foreground serves the
    over-budget fallback, ``observed["live"]`` is empty and the gateway never
    reaches the caller — a property of the runner, not of the fix. So this test
    asserts the probe/scheduling invariants that the fake clock makes exact,
    and takes the one end-to-end "lands in the caller's catalog" assertion under
    a deliberately widened budget/cap pair (see ``_WIDE_BUDGET`` / ``_WIDE_CAP``)
    whose real work finishes with a large margin.

    The in-band publication itself is also covered, machine-independently, by
    ``test_every_dead_endpoint_in_the_chain_still_lets_the_live_one_through``
    and ``test_static_allowlist_provider_is_never_probed_and_does_not_dilute_the_schedule``;
    if the widened pair ever proves flaky on a runner, drop the final catalog
    assertion and keep the four invariants, relying on those two for
    end-to-end coverage.
    """
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        provider_base_url="http://lan-dead.example:1234/v1",
        custom_providers=[
            {
                "name": "My Gateway",
                "base_url": "https://gw-live.example/v1",
                "api_key": "sk-live",
            }
        ],
    )
    # Widen the window for this test only: the schedule then computes its slices
    # from a 40s budget while the per-endpoint cap is 20s, so the repro's three
    # slots each get 10s (still strictly below the cap) and the call phase has an
    # ≥8x real-time margin instead of racing the 4s deadline.
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", _WIDE_BUDGET, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", _WIDE_CAP, raising=False)
    clock = _FakeClock()
    monkeypatch.setattr(cfg, "time", clock, raising=False)
    observed = _install_urlopen(
        monkeypatch,
        dead_hosts=["lan-dead.example"],
        live_hosts=["gw-live.example"],
        clock=clock,
    )

    catalog = cfg.get_available_models()

    # (1) The dead endpoint is configured twice (active `model.base_url` and
    # `providers.lmstudio.base_url`), but it is now probed ONCE per rebuild: the
    # second consumer reuses the first outcome instead of paying the connect
    # timeout again.
    assert len(observed["dead"]) == 1

    # (2) Probes are walked in config order — the active (`lan-dead`) endpoint
    # before the named gateway — and the gateway really was probed in-band.
    assert observed["live"], "the reachable named provider was never probed"
    probed_hosts = [url.split("://", 1)[1].split("/", 1)[0] for url in observed["order"]]
    assert probed_hosts == ["lan-dead.example:1234", "gw-live.example"], probed_hosts

    # (3) Every probe was handed a bounded slice of the window — positive and
    # strictly below the per-endpoint cap — rather than the whole cap.
    for url, timeout in observed["dead"] + observed["live"]:
        assert timeout is not None and 0 < timeout < _WIDE_CAP, (url, timeout)

    # (4) The schedule's own arithmetic stayed inside the window. This is the
    # virtual clock, so it states "the chain fitted inside the window" on any
    # runner — the invariant the slow-runner failure violated via the real-clock
    # fallback. (The catalogue the caller actually receives is asserted next,
    # under the widened budget that removes the real-clock race.)
    assert clock.now < _WIDE_BUDGET, clock.now

    # (5) End-to-end: the reachable gateway lands in the catalog the caller
    # receives rather than only in an out-of-band refresh after a fallback.
    assert _models_by_provider(catalog).get("custom:my-gateway") == _GATEWAY_MODELS


def test_every_dead_endpoint_in_the_chain_still_lets_the_live_one_through(
    monkeypatch, isolate_models_catalog_state
):
    """Two dead named providers in front of a reachable one must not starve it.

    Also on the injected clock (maintainer review, 2026-09-10): with real sleeps
    the active endpoint's sleep could cross the 4s deadline before ``dead-one`` /
    ``dead-two`` were reached on a slower box, so the ordering assertion this test
    exists for was decided by the runner's speed.
    """
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        custom_providers=[
            {"name": "Dead One", "base_url": "https://dead-one.example/v1", "api_key": "k1"},
            {"name": "Dead Two", "base_url": "https://dead-two.example/v1", "api_key": "k2"},
            {"name": "My Gateway", "base_url": "https://gw-live.example/v1", "api_key": "k3"},
        ],
    )
    clock = _FakeClock()
    monkeypatch.setattr(cfg, "time", clock, raising=False)
    observed = _install_urlopen(
        monkeypatch,
        dead_hosts=["lan-dead.example", "dead-one.example", "dead-two.example"],
        live_hosts=["gw-live.example"],
        clock=clock,
    )

    catalog = cfg.get_available_models()

    # Every endpoint was attempted, in config order, before the budget ran out:
    # the active endpoint first, then the named entries. The LM Studio
    # provider-group fallback resolves to the active endpoint's URL here and
    # reuses that probe's outcome (same URL, same credential) rather than
    # repeating it, so it adds no third probe of the dead host.
    probed_hosts = [url.split("://", 1)[1].split("/", 1)[0] for url, _ in observed["dead"]]
    assert probed_hosts == [
        "lan-dead.example:1234",
        "dead-one.example",
        "dead-two.example",
    ], probed_hosts
    # …and the chain still finished inside the window, so the live provider behind
    # the dead ones was probed in-band and published.
    assert clock.now < _BUDGET, clock.now
    assert _models_by_provider(catalog).get("custom:my-gateway") == _GATEWAY_MODELS


def test_static_allowlist_provider_is_never_probed_and_does_not_dilute_the_schedule(
    monkeypatch, isolate_models_catalog_state
):
    """A provider with a static ``models:`` allowlist consumes no probe slot."""
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        custom_providers=[
            {
                "name": "Static Co",
                "base_url": "https://static-never-probed.example/v1",
                "api_key": "k",
                "models": ["static-a", "static-b"],
            },
            {"name": "My Gateway", "base_url": "https://gw-live.example/v1", "api_key": "k"},
        ],
    )
    observed = _install_urlopen(
        monkeypatch,
        dead_hosts=["lan-dead.example"],
        live_hosts=["gw-live.example", "static-never-probed.example"],
    )

    catalog = cfg.get_available_models()

    # The allowlist provider rendered from config and was never probed…
    assert _models_by_provider(catalog).get("custom:static-co") == ["static-a", "static-b"]
    assert not [
        url
        for url, _ in observed["live"] + observed["dead"]
        if "static-never-probed.example" in url
    ]
    # …while the live provider behind the dead endpoint still made it in-band.
    assert _models_by_provider(catalog).get("custom:my-gateway") == _GATEWAY_MODELS


def test_probe_schedule_keeps_the_documented_cap_when_the_budget_is_disabled(monkeypatch):
    """Legacy synchronous path (budget <= 0) has no window to share."""
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0, raising=False)

    schedule = cfg._CustomProbeSchedule(3)

    assert [schedule.next_timeout() for _ in range(3)] == [5.0, 5.0, 5.0]


def test_probe_schedule_shares_the_window_and_reserves_headroom(monkeypatch):
    """Every probe gets a slice, and a fully-burned chain cannot drain the window."""
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 4.0, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0, raising=False)

    clock = _FakeClock()
    monkeypatch.setattr(cfg, "time", clock, raising=False)

    schedule = cfg._CustomProbeSchedule(4)
    timeouts = []
    for _ in range(4):
        timeout = schedule.next_timeout()
        timeouts.append(timeout)
        clock.now += timeout  # the probe burns its whole slice

    assert all(t < 5.0 for t in timeouts), timeouts
    # A fully-burned chain still finishes inside the window, so the foreground
    # caller gets a published catalog instead of the over-budget fallback.
    assert clock.now < 4.0, timeouts


@pytest.mark.parametrize("endpoint_count", [1, 2, 8, 24, 401, 1000])
def test_probe_schedule_cannot_outspend_the_window_at_any_chain_length(
    endpoint_count, monkeypatch
):
    """The headroom must survive long chains — `custom_providers` is unbounded.

    Regression guard for both earlier revisions. A fixed 0.5s per-probe floor let
    eight timeouts spend the whole four-second window and nine spend past it. The
    0.01s "arithmetic guard" that replaced it was still a floor: with a computed
    share below it, roughly the first 400 probes of a thousand could drain the
    window and every probe after that was handed the full per-endpoint cap. Either
    way a long chain of dead endpoints could push a reachable provider out of the
    in-band rebuild — the reported defect — so the counts here run past both
    thresholds, and the assertion is made after *every* allocation rather than
    only at the end of the chain.
    """
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 4.0, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0, raising=False)

    clock = _FakeClock()
    monkeypatch.setattr(cfg, "time", clock, raising=False)

    schedule = cfg._CustomProbeSchedule(endpoint_count)
    for probe in range(endpoint_count):
        timeout = schedule.next_timeout()
        # Every slot is still attempted — including the last of a very long chain
        # — and none of them is handed the full cap out of the shared window.
        assert 0 < timeout < 5.0, (endpoint_count, probe, timeout)
        clock.now += timeout  # every probe burns its slice
        # Cumulative spend never reaches the window, so the chain always finishes
        # in-band and the foreground caller gets a published catalog.
        assert clock.now < 4.0, (endpoint_count, probe, clock.now)


def test_probe_schedule_restores_the_cap_once_the_foreground_gives_up(monkeypatch):
    """The out-of-band continuation still gets a full attempt to refresh."""
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0, raising=False)

    abandoned = threading.Event()
    schedule = cfg._CustomProbeSchedule(3, out_of_band=abandoned.is_set)

    assert schedule.next_timeout() < 5.0
    time.sleep(0.1)  # budget now spent
    abandoned.set()  # the foreground caller has stopped waiting
    assert schedule.next_timeout() == 5.0


def test_probe_schedule_stays_bounded_in_band_after_the_window_is_spent(monkeypatch):
    """A spent window is not the same state as an out-of-band continuation.

    Regression guard for the review finding on the second revision: the deadline
    being spent said nothing about whether the foreground caller had given up, so
    an in-band chain that had already outspent the window was handed the full
    per-endpoint cap for each of its remaining probes — the window was bypassed
    and whatever reachable provider sat behind it landed out-of-band (or not at
    all), which is the reported defect. Only a caller that has actually stopped
    waiting may spend past the window.
    """
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0, raising=False)

    # No out-of-band signal: the caller is still waiting on this chain.
    schedule = cfg._CustomProbeSchedule(3)

    assert schedule.next_timeout() < 5.0
    time.sleep(0.1)  # window spent while the foreground is still waiting
    for _ in range(3):
        timeout = schedule.next_timeout()
        assert 0 < timeout < 5.0, timeout  # attempted, but never the full cap


def test_probe_schedule_is_wired_to_the_foreground_giving_up(
    monkeypatch, isolate_models_catalog_state
):
    """The schedule must see the real hand-off, not infer it from the deadline.

    The over-budget continuation is recognised through an explicit signal; if a
    rebuild never passed one, its probes would stay pinned to the window that the
    caller already walked away from, and the out-of-band refresh could not
    complete. This asserts the schedule is handed a signal, and that the signal
    reports out-of-band once the foreground caller has stopped waiting.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    seen: dict = {}
    real_schedule = cfg._CustomProbeSchedule
    real_invoke = cfg._invoke_models_rebuild

    class _RecordingSchedule(real_schedule):
        def __init__(self, endpoint_count, *, out_of_band=None):
            seen["predicate"] = out_of_band
            super().__init__(endpoint_count, out_of_band=out_of_band)

    monkeypatch.setattr(cfg, "_CustomProbeSchedule", _RecordingSchedule)

    def _slow_builder(builder):
        # Hold the worker past the budget so the foreground gives up before the
        # probe chain is scheduled — the state the signal has to report.
        time.sleep(0.15)
        return real_invoke(builder)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_builder)

    cfg.get_available_models()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "predicate" not in seen:
        time.sleep(0.01)

    assert seen.get("predicate") is not None, (
        "the probe schedule was never given a foreground/out-of-band signal"
    )
    assert seen["predicate"]() is True, (
        "the foreground gave up but the schedule still reads as in-band"
    )


def test_late_out_of_band_result_cannot_overwrite_a_newer_rebuild(
    monkeypatch, isolate_models_catalog_state
):
    """A superseded rebuild must not resurrect its catalog over a newer one."""
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    newer = _catalog("newer")
    older = _catalog("older")
    finished = {"value": False}

    def _slow_builder(_builder):
        # Still running when the foreground gives up — and by now a NEWER
        # rebuild has been allocated and published its catalog (the out-of-band
        # race the guard exists for).
        time.sleep(0.15)
        cfg._models_rebuild_seq += 1
        cfg._models_published_seq = cfg._models_rebuild_seq
        cfg._available_models_cache = newer
        cfg._available_models_cache_ts = time.monotonic()
        cfg._available_models_live_rebuild_ts = time.monotonic()
        finished["value"] = True
        return copy.deepcopy(older)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_builder)

    cfg.get_available_models()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not finished["value"]:
        time.sleep(0.01)
    # Give the worker's finally-block publisher a moment to (not) clobber.
    time.sleep(0.15)

    assert finished["value"] is True
    assert cfg._available_models_cache is newer, (
        "the superseded out-of-band rebuild overwrote a newer catalog"
    )


def test_older_publish_does_not_cost_a_newer_rebuild_its_result(
    monkeypatch, isolate_models_catalog_state
):
    """An older build publishing late must not suppress the newer build's publish.

    Regression guard for the review finding on the first revision: ordering by
    wall-clock stamp meant an OLDER rebuild that published *after* a newer one
    had started read as the newer generation, so the newer build's correct result
    was discarded — leaving a stale catalog in the cache and disk while its
    caller received a catalog that was never published.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 5.0, raising=False)

    newer = _catalog("newer")
    older = _catalog("older")

    def _builder(_builder):
        # This run is rebuild N; simulate rebuild N-1 publishing its older
        # catalog late, while we are still building.
        cfg._models_published_seq = cfg._models_rebuild_seq - 1
        cfg._available_models_cache = older
        cfg._available_models_cache_ts = time.monotonic()
        cfg._available_models_live_rebuild_ts = time.monotonic()
        return copy.deepcopy(newer)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _builder)

    result = cfg.get_available_models()

    assert result["active_provider"] == "newer"
    assert cfg._available_models_cache is not older, (
        "an older publish suppressed the newer rebuild's result"
    )
    assert cfg._available_models_cache["active_provider"] == "newer"
    assert cfg._models_published_seq == cfg._models_rebuild_seq
