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
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

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


# The real durable-cache writer, captured before the autouse fixture replaces it
# with a no-op. The concurrency tests below opt back in to the real one, because
# the durable file is part of what they assert (#7481 review).
_REAL_SAVE_MODELS_CACHE_TO_DISK = cfg._save_models_cache_to_disk
_REAL_LOAD_MODELS_CACHE_FROM_DISK = cfg._load_models_cache_from_disk
_REAL_DELETE_MODELS_CACHE_ON_DISK = cfg._delete_models_cache_on_disk


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
    # The durable-commit generation is process-wide state like the two above, so
    # it has to be reset per test: a sequence committed by an earlier test would
    # otherwise fence this test's own (lower-numbered) commits out of the file.
    monkeypatch.setattr(cfg, "_models_disk_committed_seq", 0, raising=False)
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

    yield {"tmp_path": tmp_path, "auth_store_path": auth_store_path}

    # Own every daemon worker this test started before the fixture tears its
    # monkeypatches down (#7481 review): a worker still parked inside a mocked
    # builder would otherwise wake up in the NEXT test and run against state that
    # is no longer patched, which reads as a random failure somewhere else. The
    # concurrency tests below release and join their own workers, so this is a
    # backstop that also proves the ownership claim.
    for thread in threading.enumerate():
        if thread.name == "models-catalog-rebuild" and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            assert not thread.is_alive(), (
                "a models-catalog-rebuild worker outlived its test"
            )


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


def _install_schedule_recorder(monkeypatch, seen):
    """Wrap ``_CustomProbeSchedule`` so a test can see what the rebuild hands it.

    Records what the PRODUCTION caller supplies — notably the absolute
    ``deadline`` — rather than what the schedule would compute on its own, which
    is what the "one window, not two" invariant is about.
    """
    real_schedule = cfg._CustomProbeSchedule

    class _RecordingSchedule(real_schedule):
        def __init__(self, endpoint_count, *, out_of_band=None, deadline=None):
            seen["predicate"] = out_of_band
            seen["deadline"] = deadline
            seen["endpoint_count"] = endpoint_count
            seen["constructed"] = seen.get("constructed", 0) + 1
            super().__init__(
                endpoint_count,
                out_of_band=out_of_band,
                deadline=deadline,
            )

    monkeypatch.setattr(cfg, "_CustomProbeSchedule", _RecordingSchedule)
    return _RecordingSchedule


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
    real_invoke = cfg._invoke_models_rebuild

    _install_schedule_recorder(monkeypatch, seen)

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


def _run_two_in_flight_rebuilds(monkeypatch, *, newer_builder):
    """Drive an invalidated rebuild N and a newer rebuild N+1, both in flight.

    ``invalidate_models_cache`` clears ``_cache_build_in_progress`` without
    cancelling a running worker, so a newer rebuild can be allocated while an
    older one is still running. Both workers here are blocked inside the mocked
    rebuild so the caller controls which one finishes first; the worker thread
    objects are captured from inside the builder (the builder runs on the
    worker), which lets the caller ``join`` a specific worker rather than sleep
    and hope.

    Returns ``(workers, release_older, release_newer, saves, older)`` where
    ``workers[0]`` is rebuild N (returns the ``older`` catalog when released) and
    ``workers[1]`` is N+1 (runs ``newer_builder``).
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    older = _catalog("older")
    release_older = threading.Event()
    release_newer = threading.Event()
    older_started = threading.Event()
    newer_started = threading.Event()
    saves: list = []
    monkeypatch.setattr(
        cfg,
        "_save_models_cache_to_disk",
        lambda result, *_a, **_k: saves.append(result),
    )

    workers: list = []
    calls = {"n": 0}

    def _builder(_builder):
        workers.append(threading.current_thread())
        calls["n"] += 1
        if calls["n"] == 1:
            older_started.set()
            release_older.wait(5.0)
            return copy.deepcopy(older)
        newer_started.set()
        release_newer.wait(5.0)
        return newer_builder()

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _builder)

    # Rebuild N: the foreground gives up at the budget, leaving its worker
    # blocked inside the builder — the invalidated-in-flight state.
    cfg.get_available_models()
    assert older_started.wait(2.0), "the first rebuild worker never started"

    # A config edit invalidates the cache; a newer rebuild N+1 is allocated while
    # N is still running.
    cfg.invalidate_models_cache()
    cfg.get_available_models()
    assert newer_started.wait(2.0), "the second rebuild worker never started"

    return workers, release_older, release_newer, saves, older


def test_invalidated_worker_cannot_publish_over_a_newer_in_flight_rebuild(
    monkeypatch, isolate_models_catalog_state
):
    """Invalidation must fence out an in-flight worker, not just lose the race.

    Maintainer review, 2026-09-11: ``invalidate_models_cache`` clears
    ``_cache_build_in_progress`` without cancelling a running worker, so a newer
    rebuild can be allocated while an older one is still in flight. If the older
    worker finishes first, comparing against the last *published* sequence lets
    it publish the invalidated catalog and release the build flag that now
    belongs to the newer rebuild. The fence has to be the latest *allocated*
    generation (``_models_rebuild_seq``).
    """
    newer = _catalog("newer")
    workers, release_older, release_newer, saves, older = _run_two_in_flight_rebuilds(
        monkeypatch, newer_builder=lambda: copy.deepcopy(newer)
    )

    # Release the OLDER worker first. It must not publish, must not touch the
    # disk cache, and must not release the flag now owned by N+1.
    release_older.set()
    workers[0].join(timeout=5.0)
    assert not workers[0].is_alive()
    assert cfg._available_models_cache is None
    assert older not in saves
    assert cfg._cache_build_in_progress is True

    # Release N+1: it alone publishes, and the disk cache records only it.
    release_newer.set()
    workers[1].join(timeout=5.0)
    assert not workers[1].is_alive()
    assert cfg._available_models_cache == newer
    assert saves == [newer]
    assert cfg._cache_build_in_progress is False


def test_superseded_worker_error_does_not_resurrect_its_catalog(
    monkeypatch, isolate_models_catalog_state
):
    """When the newer rebuild fails, the invalidated catalog stays gone.

    Companion to the previous test (maintainer review, 2026-09-11): N has to be
    rejected on allocation order, not merely overwritten by N+1. If N+1 raises,
    the invalidated N catalog must not be resurrected from the worker that was
    still running, and N+1 still owns (and releases) the build flag.
    """

    def _fail():
        raise RuntimeError("newer rebuild failed")

    workers, release_older, release_newer, saves, older = _run_two_in_flight_rebuilds(
        monkeypatch, newer_builder=_fail
    )

    release_older.set()
    workers[0].join(timeout=5.0)
    assert cfg._available_models_cache is None
    assert older not in saves

    release_newer.set()
    workers[1].join(timeout=5.0)
    assert not workers[1].is_alive()
    assert cfg._available_models_cache is None
    assert older not in saves
    assert cfg._cache_build_in_progress is False


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


# ── Review round 5 (2026-10-01): the remaining production races ──────────────
#
# The review found three states the allocated-generation guard did not cover:
#   1. invalidation without a successor never revoked the running worker, which
#      then republished the cleared catalog stamped with a NEW fingerprint;
#   2. a publisher could clear a newer rebuild's single-flight ownership (and
#      overwrite its durable result) from its post-lock exit path;
#   3. the probe schedule and the foreground wait still used two independent
#      windows, so pre-custom discovery work was granted to the chain twice.
# All of the tests below are event/barrier driven — no polling and no fixed
# sleeps — and they release and join the daemon workers they start.


def _written_active_provider(cache_path):
    """``active_provider`` in the durable catalog, or None when it is absent."""
    if not cache_path.exists():
        return None
    return json.loads(cache_path.read_text(encoding="utf-8"))["active_provider"]


def _glob_cache_files(cache_path):
    """Every durable-catalog artefact (the file plus any leftover temp file)."""
    return sorted(p.name for p in cache_path.parent.glob(cache_path.name + "*"))


def test_probe_schedule_spends_the_callers_window_not_a_fresh_one(
    monkeypatch, isolate_models_catalog_state
):
    """The probe chain and the foreground wait must share ONE absolute deadline.

    Maintainer review, 2026-10-01. ``_CustomProbeSchedule`` used to mint its own
    window when it was constructed — deep inside the worker, after provider
    detection and the live id lookups — while the foreground started a fresh full
    ``Event.wait`` after starting that worker. Any discovery work before the
    custom-probe phase was therefore granted to the chain a second time, so the
    chain could still be probing after the caller had been served the over-budget
    fallback: the reachable provider landed out-of-band at best.

    The deadline handed to the schedule must be the caller's (captured before the
    worker started), so it reads ``budget`` past the call — not
    ``budget + discovery delay`` — and it must coincide with the instant the
    caller stopped waiting on.
    """
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        custom_providers=[
            {
                "name": "My Gateway",
                "base_url": "https://gw-live.example/v1",
                "api_key": "sk-live",
            }
        ],
    )
    budget = 1.0
    discovery_delay = 0.6
    cap = 0.3
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", budget, raising=False)
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", cap, raising=False)
    observed = _install_urlopen(
        monkeypatch, dead_hosts=["lan-dead.example"], live_hosts=["gw-live.example"]
    )

    seen: dict = {}
    recorder = _install_schedule_recorder(monkeypatch, seen)

    class _DelayedRecordingSchedule(recorder):
        def __init__(self, endpoint_count, *, out_of_band=None, deadline=None):
            # Stands in for the pre-custom discovery work: by the time the chain
            # is built, part of the caller's window is already spent. The old
            # construction-time deadline would start counting HERE.
            time.sleep(discovery_delay)
            super().__init__(
                endpoint_count, out_of_band=out_of_band, deadline=deadline
            )

    monkeypatch.setattr(cfg, "_CustomProbeSchedule", _DelayedRecordingSchedule)

    started_at = time.monotonic()
    cfg.get_available_models()
    stopped_waiting = time.monotonic()

    assert seen.get("constructed"), "the custom-probe chain was never scheduled"
    deadline = seen["deadline"]
    assert deadline is not None, (
        "the rebuild handed the schedule no deadline, so the chain minted its own "
        "window"
    )

    # One window, not two: the discovery delay is NOT added to it.
    assert deadline - started_at <= budget + 0.25, (
        deadline - started_at,
        budget,
        discovery_delay,
    )
    # ... and it is the very instant the caller stopped waiting on.
    assert stopped_waiting - deadline <= 0.25, (stopped_waiting - deadline)

    # The probes drew from that same window: every one of them was bounded
    # (positive, never above the per-endpoint cap).
    for url, timeout in observed["dead"] + observed["live"]:
        assert timeout is not None and 0 < timeout <= cap, (url, timeout)


def test_invalidated_worker_cannot_restore_the_catalog_without_a_successor(
    monkeypatch, isolate_models_catalog_state
):
    """Invalidation alone must revoke a rebuild that is already in flight.

    Maintainer review, 2026-10-01. ``invalidate_models_cache()`` cleared memory
    and the in-progress flag but never advanced the allocated generation, so a
    delayed worker stayed eligible and republished the catalog that had just been
    cleared — memory, provenance and the durable file — stamped with a NEW source
    fingerprint at publication, which is exactly how stale data acquires
    fresh-looking provenance. No successor rebuild is started here: the fence has
    to hold on its own.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    cache_path = cfg._get_models_cache_path()
    older = _catalog("older")
    started = threading.Event()
    release = threading.Event()
    workers: list = []
    saves: list = []

    def _builder(_builder):
        workers.append(threading.current_thread())
        started.set()
        assert release.wait(5.0)
        return copy.deepcopy(older)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _builder)
    monkeypatch.setattr(
        cfg,
        "_save_models_cache_to_disk",
        lambda result, **kwargs: (
            saves.append(result),
            _REAL_SAVE_MODELS_CACHE_TO_DISK(result, **kwargs),
        ),
    )

    # Rebuild N: the foreground gives up at the budget and leaves its worker
    # blocked inside the builder — the in-flight state invalidation must fence.
    cfg.get_available_models()
    assert started.wait(2.0), "the rebuild worker never started"
    assert cfg._cache_build_in_progress is True

    # The config edit. Nothing else is started — no successor rebuild at all.
    cfg.invalidate_models_cache()
    assert cfg._cache_build_in_progress is False

    release.set()
    workers[0].join(timeout=5.0)
    assert not workers[0].is_alive()

    assert cfg._available_models_cache is None, (
        "the invalidated worker restored the cleared catalog"
    )
    assert cfg._models_cache_provenance is None
    assert cfg._available_models_cache_source_fingerprint is None
    assert saves == [], "the invalidated worker wrote a durable catalog"
    assert _written_active_provider(cache_path) is None
    assert _glob_cache_files(cache_path) == [], (
        "a superseded publisher left a temp file behind"
    )
    assert cfg._cache_build_in_progress is False


def test_invalidated_worker_cannot_overwrite_a_newer_published_catalog(
    monkeypatch, isolate_models_catalog_state
):
    """The other completion order: the invalidated worker is released LAST.

    Companion to ``test_invalidated_worker_cannot_publish_over_a_newer_in_flight_rebuild``
    (maintainer review, 2026-10-01, "cover both completion orders"). Here the
    newer rebuild has already published to memory AND committed its durable
    catalog before the older worker is released, so the older worker has to be
    rejected on generation alone — the durable file is checked too, because the
    shared per-pid temp name used to let the older writer revert it.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    cache_path = cfg._get_models_cache_path()
    older = _catalog("older")
    newer = _catalog("newer")
    older_started = threading.Event()
    release_older = threading.Event()
    workers: list = []
    calls = {"n": 0}

    def _builder(_builder):
        workers.append(threading.current_thread())
        calls["n"] += 1
        if calls["n"] == 1:
            older_started.set()
            assert release_older.wait(5.0)
            return copy.deepcopy(older)
        return copy.deepcopy(newer)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _builder)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _REAL_SAVE_MODELS_CACHE_TO_DISK)

    # Rebuild N goes out of budget with its worker blocked (out-of-band).
    cfg.get_available_models()
    assert older_started.wait(2.0), "the first rebuild worker never started"

    # Invalidate, then let the newer rebuild run to completion — it publishes and
    # commits its catalog while N is still parked.
    cfg.invalidate_models_cache()
    result = cfg.get_available_models()
    assert result["active_provider"] == "newer"
    assert cfg._available_models_cache == newer
    assert _written_active_provider(cache_path) == "newer"

    release_older.set()
    workers[0].join(timeout=5.0)
    assert not workers[0].is_alive()

    assert cfg._available_models_cache == newer, (
        "the older worker reverted the newer in-memory catalog"
    )
    assert _written_active_provider(cache_path) == "newer", (
        "the older worker reverted the newer durable catalog"
    )
    assert _glob_cache_files(cache_path) == [cache_path.name], (
        "a superseded publisher left a temp file behind"
    )
    assert cfg._cache_build_in_progress is False


def test_superseded_publish_does_not_release_the_newer_rebuilds_ownership(
    monkeypatch, isolate_models_catalog_state
):
    """A publisher must not clear the single-flight slot a newer build now owns.

    Maintainer review, 2026-10-01: ``_publish_models_result`` checked the
    generation under the cache lock, saved the file with that lock released, and
    then cleared ``_cache_build_in_progress`` unconditionally. So A could pass its
    check, B could be allocated while A was in its disk window, and A's exit path
    would then release B's ownership — admitting a third rebuild C beside B and
    letting A's stale payload land in the durable file. This test holds A inside
    its own disk commit (a barrier) while the newer rebuild B is admitted, then
    releases it and asserts:

      * A's payload never reaches the durable catalog;
      * B keeps the single-flight slot, so C is NOT admitted (C gets B's catalog);
      * B, when it finally publishes, is recorded in the file.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    cache_path = cfg._get_models_cache_path()
    older = _catalog("older")
    newer = _catalog("newer")
    a_started = threading.Event()
    release_a = threading.Event()
    a_in_save = threading.Event()
    b_started = threading.Event()
    release_b = threading.Event()
    b_in_save = threading.Event()
    release_b_save = threading.Event()
    workers: list = []
    calls = {"n": 0}

    def _builder(_builder):
        workers.append(threading.current_thread())
        calls["n"] += 1
        if calls["n"] == 1:
            a_started.set()
            assert release_a.wait(5.0)
            return copy.deepcopy(older)
        b_started.set()
        assert release_b.wait(5.0)
        return copy.deepcopy(newer)

    def _gated_save(result, **kwargs):
        if result.get("active_provider") == "older":
            a_in_save.set()
            # Stay inside the publisher's disk window until the newer rebuild has
            # been allocated and has reached its own commit.
            assert b_started.wait(5.0)
            assert b_in_save.wait(5.0)
        else:
            b_in_save.set()
            # Hold B in its disk window too, so the flag it owns is still set
            # while A's exit path runs — that is the state A must not release.
            assert release_b_save.wait(5.0)
        return _REAL_SAVE_MODELS_CACHE_TO_DISK(result, **kwargs)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _builder)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _gated_save)

    # Rebuild A: out of budget, worker blocked inside the builder.
    cfg.get_available_models()
    assert a_started.wait(2.0), "the first rebuild worker never started"

    # Release A: it is out-of-band now, publishes to memory and enters its disk
    # commit — where the gate holds it.
    release_a.set()
    assert a_in_save.wait(5.0), "the older rebuild never reached its disk commit"

    # A config edit invalidates, and the newer rebuild B is admitted while A is
    # still inside that commit. Releasing B's builder lets B publish to memory and
    # enter its own commit, which is where B now owns the single-flight slot.
    cfg.invalidate_models_cache()
    cfg.get_available_models()
    assert b_started.wait(2.0), "the newer rebuild never started"
    release_b.set()
    assert b_in_save.wait(5.0), "the newer rebuild never reached its disk commit"
    assert cfg._available_models_cache == newer
    assert cfg._cache_build_in_progress is True, "B does not own the build slot"

    # Now let the older publisher out of its disk window. It is superseded.
    release_a.set()
    workers[0].join(timeout=5.0)
    assert not workers[0].is_alive()

    assert cfg._available_models_cache == newer, (
        "the superseded publisher reverted the newer in-memory catalog"
    )
    assert _written_active_provider(cache_path) != "older", (
        "the superseded publisher wrote its stale payload to the durable catalog"
    )
    assert cfg._cache_build_in_progress is True, (
        "the superseded publisher released the newer rebuild's single-flight slot"
    )

    # C must not be admitted while B owns the slot: it waits for B and is served
    # B's catalog instead of starting a third rebuild.
    seq_before_c = cfg._models_rebuild_seq
    c_result: dict = {}

    def _caller_c():
        c_result["catalog"] = cfg.get_available_models()

    caller_c = threading.Thread(target=_caller_c, name="issue7481-caller-c")
    caller_c.start()
    caller_c.join(timeout=0.3)
    assert caller_c.is_alive(), "the third caller did not wait for B"
    assert calls["n"] == 2, "a third rebuild was admitted while the newer one lived"
    assert cfg._models_rebuild_seq == seq_before_c

    release_b_save.set()
    workers[1].join(timeout=5.0)
    caller_c.join(timeout=5.0)
    assert not caller_c.is_alive()
    assert calls["n"] == 2

    assert c_result["catalog"]["active_provider"] == "newer"
    assert cfg._available_models_cache == newer
    assert _written_active_provider(cache_path) == "newer"
    assert _glob_cache_files(cache_path) == [cache_path.name]
    assert cfg._cache_build_in_progress is False


def test_source_edit_during_a_rebuild_discards_the_stale_result(
    monkeypatch, isolate_models_catalog_state
):
    """A catalog built from sources that changed must not be published as fresh.

    Maintainer review, 2026-10-01 ("capture and validate the build's
    source/profile identity at publication"). A build that outlives a config edit
    used to publish its stale catalog with the CURRENT source fingerprint — the
    data was never built from those sources, but it carried their provenance, in
    memory and on disk. The captured identity is re-validated at publication and
    at the durable commit instead; here nothing is invalidated, so the fence has
    to come from the identity check alone.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    cache_path = cfg._get_models_cache_path()
    fingerprint = {"value": "fp-before-edit"}
    monkeypatch.setattr(
        cfg, "_models_cache_source_fingerprint", lambda: fingerprint["value"]
    )

    older = _catalog("older")
    started = threading.Event()
    release = threading.Event()
    workers: list = []
    saves: list = []

    def _builder(_builder):
        workers.append(threading.current_thread())
        started.set()
        assert release.wait(5.0)
        return copy.deepcopy(older)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _builder)
    monkeypatch.setattr(
        cfg,
        "_save_models_cache_to_disk",
        lambda result, **kwargs: (
            saves.append(result),
            _REAL_SAVE_MODELS_CACHE_TO_DISK(result, **kwargs),
        ),
    )

    cfg.get_available_models()
    assert started.wait(2.0), "the rebuild worker never started"

    # The user edits the config while the build is running. No invalidation call:
    # the sources simply change under the build.
    fingerprint["value"] = "fp-after-edit"

    release.set()
    workers[0].join(timeout=5.0)
    assert not workers[0].is_alive()

    assert cfg._available_models_cache is None, (
        "a catalog built from the previous sources was published under the new "
        "fingerprint"
    )
    assert cfg._available_models_cache_source_fingerprint is None
    assert cfg._models_cache_provenance is None
    assert saves == [], "a catalog built from the previous sources reached the disk"
    assert _written_active_provider(cache_path) is None
    assert _glob_cache_files(cache_path) == []
    # The build owned the slot and nothing superseded it, so it must release it:
    # otherwise the next caller waits out the whole budget for nothing.
    assert cfg._cache_build_in_progress is False


class _FakeRouteHandler:
    """Transport-less ``BaseHTTPRequestHandler`` stand-in for ``routes.handle_post``.

    Exposes ``wfile``/``headers``/``rfile`` so the real router can read the JSON
    body and write its response. ``headers`` carries no ``Origin``/``Referer``, so
    ``_check_csrf`` treats the caller as a non-browser API client (the same
    contract curl/MCP use) instead of demanding a session CSRF token.
    """

    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
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


def _refresh_provider_via_route(provider_id: str) -> None:
    """Drive ``POST /api/models/refresh`` through the real router.

    The route is the production entry point (``api/routes.py``, reached from
    ``static/panels.js``); it is a thin wrapper over
    ``invalidate_provider_models_cache``, so asserting the fence at the route is
    what proves the endpoint the browser actually calls is fenced (#7481 review).
    """
    from api.routes import handle_post

    handler = _FakeRouteHandler(json.dumps({"provider": provider_id}).encode("utf-8"))
    handle_post(handler, urlparse("http://example.com/api/models/refresh"))
    # The branch answers with ``return j(handler, ...)`` — i.e. it returns the
    # router's response writer value, not the ``True`` the dispatcher docstring
    # describes — so the observable contract is the response itself.
    assert handler.status == 200, (handler.status, bytes(handler.body))
    assert handler.json_body() == {"ok": True, "provider": provider_id}


@pytest.mark.parametrize("entry", ["function", "route"], ids=["direct_call", "http_route"])
@pytest.mark.parametrize("start_successor", [False, True], ids=["a_only", "a_then_b"])
def test_provider_refresh_revokes_in_flight_catalog(
    monkeypatch, isolate_models_catalog_state, start_successor, entry
):
    """Provider-scoped invalidation fences A, with or without a successor B.

    ``entry`` selects the entry point: the invalidator function directly, or the
    ``POST /api/models/refresh`` route that calls it (``api/routes.py``). Both must
    revoke an in-flight rebuild, and neither may let it restore memory, provenance
    or the durable JSON afterwards.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05)
    old, new = _catalog("older"), _catalog("newer")
    cache_path = cfg._get_models_cache_path()
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", _REAL_DELETE_MODELS_CACHE_ON_DISK)
    a_started, release_a = threading.Event(), threading.Event()
    b_started, release_b = threading.Event(), threading.Event()
    workers, saves = [], []

    def refresh_provider() -> None:
        if entry == "route":
            _refresh_provider_via_route("openai")
        else:
            cfg.invalidate_provider_models_cache("openai")

    def builder(_builder):
        workers.append(threading.current_thread())
        if len(workers) == 1:
            a_started.set()
            assert release_a.wait(5)
            return copy.deepcopy(old)
        b_started.set()
        assert release_b.wait(5)
        return copy.deepcopy(new)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", builder)
    def record_save(value, **kwargs):
        saves.append(value)
        _REAL_SAVE_MODELS_CACHE_TO_DISK(value, **kwargs)

    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", record_save)
    try:
        cfg.get_available_models()
        assert a_started.wait(2)
        refresh_provider()
        if start_successor:
            cfg.get_available_models()
            assert b_started.wait(2)
        release_a.set()
        workers[0].join(5)
        assert not workers[0].is_alive()
        assert cfg._available_models_cache is None
        assert cfg._models_cache_provenance is None
        assert old not in saves
        assert _written_active_provider(cache_path) is None
        assert cfg._cache_build_in_progress is start_successor
        if start_successor:
            release_b.set()
            workers[1].join(5)
            assert not workers[1].is_alive()
            assert cfg._available_models_cache == new
            assert saves == [new]
            assert _written_active_provider(cache_path) == "newer"
        else:
            assert saves == []
            assert _glob_cache_files(cache_path) == []
    finally:
        release_a.set()
        release_b.set()
        for worker in workers:
            worker.join(5)
        assert all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize("start_successor", [False, True], ids=["delete_only", "newer_commit"])
def test_invalidation_between_final_disk_check_and_rename_cannot_restore_stale_file(
    monkeypatch, isolate_models_catalog_state, start_successor
):
    """Pause the real disk writer at its final rename, not at a mocked save."""
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _REAL_SAVE_MODELS_CACHE_TO_DISK)
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", _REAL_DELETE_MODELS_CACHE_ON_DISK)
    path = cfg._get_models_cache_path()
    old, new = _catalog("older"), _catalog("newer")
    at_rename, allow_rename = threading.Event(), threading.Event()
    a_started, release_a = threading.Event(), threading.Event()
    workers = []
    real_replace = cfg.os.replace

    def gated_replace(src, dst):
        if workers and threading.current_thread() is workers[0] and str(dst) == str(path):
            at_rename.set()
            assert allow_rename.wait(5)
        return real_replace(src, dst)

    def builder(_builder):
        workers.append(threading.current_thread())
        if len(workers) == 1:
            a_started.set()
            assert release_a.wait(5)
            return copy.deepcopy(old)
        return copy.deepcopy(new)

    monkeypatch.setattr(cfg.os, "replace", gated_replace)
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", builder)
    try:
        cfg.get_available_models()
        assert a_started.wait(2)
        release_a.set()
        assert at_rename.wait(5), "A never reached its final disk rename"
        invalidated = threading.Event()
        def invalidate_later():
            cfg.invalidate_models_cache()
            invalidated.set()
        invalidator = threading.Thread(target=invalidate_later)
        invalidator.start()
        # Invalidation must not return while A holds the commit through rename.
        invalidator.join(0.1)
        assert invalidator.is_alive()
        allow_rename.set()
        invalidator.join(5)
        assert invalidated.is_set()
        workers[0].join(5)
        if start_successor:
            cfg.get_available_models()
            assert cfg._available_models_cache == new
        assert not workers[0].is_alive()
        for worker in workers[1:]:
            worker.join(5)
            assert not worker.is_alive()
        assert _written_active_provider(path) == ("newer" if start_successor else None)
        assert _glob_cache_files(path) == ([path.name] if start_successor else [])
    finally:
        release_a.set()
        allow_rename.set()
        for worker in workers:
            worker.join(5)
        assert all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize("session_visit", [False, True], ids=["normal_cold_path", "session_visit"])
def test_preloaded_disk_snapshot_is_not_published_after_invalidation(
    monkeypatch, isolate_models_catalog_state, session_visit
):
    """An already-read disk value must not cross the post-invalidation memory lock."""
    _configure(monkeypatch, active_base_url=None)
    path = cfg._get_models_cache_path()
    stale = _catalog("older")
    _REAL_SAVE_MODELS_CACHE_TO_DISK(stale)
    assert _written_active_provider(path) == "older"
    loaded, resume = threading.Event(), threading.Event()
    reader = None
    outcome = {}

    def gated_load():
        snapshot = _REAL_LOAD_MODELS_CACHE_FROM_DISK()
        if snapshot is not None:
            loaded.set()
            assert resume.wait(5)
        return snapshot

    def read_catalog():
        try:
            outcome["result"] = (
                cfg.get_available_models_for_session_visit()
                if session_visit else cfg.get_available_models(prefer_cache=True)
            )
        except BaseException as exc:
            outcome["error"] = exc

    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", gated_load)
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", _REAL_DELETE_MODELS_CACHE_ON_DISK)
    try:
        reader = threading.Thread(target=read_catalog, name="issue7481-disk-reader")
        reader.start()
        assert loaded.wait(5), "the disk read was not intercepted"
        cfg.invalidate_models_cache()
        assert not path.exists()
        resume.set()
        reader.join(5)
        assert not reader.is_alive()
        assert "error" not in outcome, outcome
        assert cfg._available_models_cache != stale
        assert _written_active_provider(path) != "older"
    finally:
        resume.set()
        if reader is not None:
            reader.join(5)
            assert not reader.is_alive()


def _start_foreground_publisher(monkeypatch, modes, *, fresh):
    """Drive one real foreground cold-path publication and hold its commit.

    Returns ``(publisher, entered_commit, outcome, commit_threads)`` — the caller
    owns releasing the commit mutex and joining the thread.

    ``modes`` selects which foreground winner is exercised:
      * ``"sync"`` — the legacy unbounded path (``budget <= 0``), which publishes
        on the calling thread;
      * ``"within_budget"`` — the bounded path whose worker finishes inside the
        window, so the foreground publishes synchronously.

    The caller must acquire ``cfg._models_cache_disk_commit_lock`` *before*
    starting the publisher: that is what pins it inside its durable commit, the
    window the lock-order assertion needs.
    """
    _configure(monkeypatch, active_base_url=None)
    monkeypatch.setattr(
        cfg, "_save_models_cache_to_disk", _REAL_SAVE_MODELS_CACHE_TO_DISK
    )
    def builder(_builder):
        return copy.deepcopy(fresh)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", builder)
    monkeypatch.setattr(
        cfg,
        "_LIVE_REBUILD_BUDGET_SECONDS",
        0.0 if modes == "sync" else _BUDGET,
        raising=False,
    )

    outcome: dict = {}

    def _publish() -> None:
        # Mirror ``read_catalog``: a failure inside the foreground call is captured so
        # the caller's ``"error" not in outcome`` assertion can actually fire instead of
        # passing vacuously and then raising a confusing KeyError below.
        try:
            outcome["catalog"] = cfg.get_available_models()
        except Exception as exc:  # noqa: BLE001 - re-asserted by the caller
            outcome["error"] = exc

    publisher = threading.Thread(
        target=_publish, name="issue7481-foreground-publisher"
    )

    entered_commit = threading.Event()
    commit_threads: list = []

    def recording_save(cache, **kwargs):
        commit_threads.append(threading.current_thread())
        entered_commit.set()
        return _REAL_SAVE_MODELS_CACHE_TO_DISK(cache, **kwargs)

    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", recording_save)
    return publisher, entered_commit, outcome, commit_threads


def _assert_no_catalog_lock_taken_for_commit(cfg_module, entered_commit):
    """The publisher must own no catalog lock while it commits to disk.

    An invalidator takes the commit mutex and *then* the catalog lock
    (``_invalidate_models_catalog_epoch``), and the writer itself takes
    commit -> catalog. A foreground publisher that still owns the catalog lock
    inside its commit therefore acquires the two in the opposite order, and with
    both acquisitions unbounded in production the two threads wait on each other
    forever. This is the deterministic witness for that: prove the catalog lock
    is free while the publisher sits in its durable commit.
    """
    assert entered_commit.wait(5.0), "the foreground never reached its durable commit"
    acquired = cfg_module._available_models_cache_lock.acquire(timeout=2.0)
    try:
        assert acquired, (
            "the foreground publisher still owns the catalog lock while blocked "
            "on the commit mutex — a foreground load and an invalidation "
            "deadlock against each other"
        )
    finally:
        if acquired:
            cfg_module._available_models_cache_lock.release()


@pytest.mark.parametrize(
    "modes", ["sync", "within_budget"]
)
def test_foreground_publication_never_holds_the_catalog_lock_across_the_durable_commit(
    monkeypatch, isolate_models_catalog_state, modes
):
    """Every foreground publisher commits to disk with no catalog lock held.

    Maintainer re-gate, 2026-10-03: ``get_available_models`` owns the outer
    ``_available_models_cache_lock`` RLock across its whole cold path, and its
    synchronous writer / within-budget / budget-boundary publishers called
    ``_save_models_cache_to_disk`` — which takes commit-lock then catalog-lock —
    from inside that ownership. ``_invalidate_models_catalog_epoch`` takes the
    same two in that order, so a publisher blocked on a commit mutex an
    invalidator already holds, with that invalidator blocked on the catalog lock
    the publisher owns, is an unbounded cycle: neither the ``/api/models`` load
    nor the invalidation can finish. The fix allocates/reads the owner, source
    identity and deadline and updates memory in short catalog critical sections,
    and runs the durable publication after that critical section has fully
    ended.
    """
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", _REAL_DELETE_MODELS_CACHE_ON_DISK)
    cache_path = cfg._get_models_cache_path()
    fresh = _catalog("fresh")
    cfg._models_cache_disk_commit_lock.acquire()
    try:
        publisher, entered_commit, outcome, commit_threads = _start_foreground_publisher(
            monkeypatch, modes, fresh=fresh
        )
        publisher.start()
        _assert_no_catalog_lock_taken_for_commit(cfg, entered_commit)
    finally:
        cfg._models_cache_disk_commit_lock.release()
        if "publisher" in locals():
            publisher.join(5.0)

    assert not publisher.is_alive()
    assert "error" not in outcome, outcome
    # The foreground winner is the publisher: the durable commit must have been
    # queued by it, never performed by an out-of-band worker. The budget-boundary
    # winner runs the same ``with`` block and the same queued commit, so the
    # property under test belongs to the deferred publication, not to the branch.
    assert commit_threads == [publisher], commit_threads
    # The foreground still honours the pre-existing contract: the caller gets the
    # fresh catalog and the durable file is written by the time the call returns.
    assert outcome["catalog"]["active_provider"] == "fresh"
    assert cfg._available_models_cache == fresh
    assert _written_active_provider(cache_path) == "fresh"
    assert _glob_cache_files(cache_path) == [cache_path.name]
    assert cfg._cache_build_in_progress is False


@pytest.mark.parametrize("entry", ["function", "route"], ids=["full_invalidate", "post_refresh_route"])
@pytest.mark.parametrize("modes", ["sync", "within_budget"])
def test_foreground_publication_and_invalidation_complete_without_a_lock_cycle(
    monkeypatch, isolate_models_catalog_state, modes, entry
):
    """A foreground publish and a real invalidation must both finish, no cycle.

    The four combinations the re-gate asked for: the synchronous foreground
    winner and the within-budget one, each against the full invalidator and the
    actual ``POST /api/models/refresh`` entry point. The test holds the commit
    mutex so both operations queue behind one real mutex in the documented order,
    asserts the publisher owns no catalog lock while it waits there (in the
    broken shape the invalidator would now be blocked on that lock forever),
    then releases it and requires *bounded* completion of both — no diagnostic
    escape, no timeout papering over the cycle.

    Afterwards the invalidation must be observable: nothing stale in memory, in
    provenance, or on disk, and the single-flight slot still released.
    """
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", _REAL_DELETE_MODELS_CACHE_ON_DISK)
    cache_path = cfg._get_models_cache_path()
    seed = _catalog("seed")
    _REAL_SAVE_MODELS_CACHE_TO_DISK(seed)
    assert _written_active_provider(cache_path) == "seed"

    fresh = _catalog("fresh")
    cfg._models_cache_disk_commit_lock.acquire()
    started = False
    try:
        publisher, entered_commit, outcome, _commit_threads = _start_foreground_publisher(
            monkeypatch, modes, fresh=fresh
        )
        publisher.start()
        started = True
        _assert_no_catalog_lock_taken_for_commit(cfg, entered_commit)

        invalidated = threading.Event()

        def invalidate() -> None:
            if entry == "route":
                _refresh_provider_via_route("openai")
            else:
                cfg.invalidate_models_cache()
            invalidated.set()

        invalidator = threading.Thread(target=invalidate, name="issue7481-invalidator")
        invalidator.start()
        # It is queued behind the commit mutex this test holds — bounded, not
        # deadlocked.
        invalidator.join(0.2)
        assert invalidator.is_alive(), "the invalidation did not queue behind the commit"
    finally:
        cfg._models_cache_disk_commit_lock.release()
        if started:
            publisher.join(5.0)
        if "invalidator" in locals():
            invalidator.join(5.0)

    assert not publisher.is_alive(), "the foreground publication never completed"
    assert not invalidator.is_alive(), "the invalidation never completed"
    assert invalidated.is_set()
    assert "error" not in outcome, outcome

    assert cfg._available_models_cache is None, (
        "the foreground publisher restored a catalog after invalidation"
    )
    assert cfg._models_cache_provenance is None
    assert cfg._available_models_cache_source_fingerprint is None
    assert _written_active_provider(cache_path) is None
    assert _glob_cache_files(cache_path) == []
    assert cfg._cache_build_in_progress is False
