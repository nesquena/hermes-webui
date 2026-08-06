"""Regression test — the client rate-limit maps don't grow without bound.

`_csp_report_rate_limited` / `_client_event_rate_limited` key a timestamp list
by client IP and always re-store `[now]` for the calling key, so a key is only
ever revisited when that same IP calls again. An IP that hits the endpoint once
and never returns left its entry in the map forever. Behind a reverse proxy the
map holds a single key (the proxy IP), but a directly-exposed deployment would
accumulate one entry per distinct IP.

The functions now sweep keys whose newest timestamp has aged past the window,
size-gated so the common few-key path stays O(1).
"""
import api.routes as routes


def test_prune_drops_only_fully_stale_keys(monkeypatch):
    monkeypatch.setattr(routes, "_RATE_LIMIT_MAP_SWEEP_THRESHOLD", 2)
    now = 1_000_000.0
    window = 60.0
    cutoff = now - window
    mapping = {
        "stale-1": [now - 120.0],            # aged out
        "stale-2": [now - 90.0, now - 61.0],  # newest still older than cutoff
        "fresh": [now - 10.0],                # within window
        "empty": [],                          # degenerate, drop
    }

    routes._prune_stale_rate_limit_keys(mapping, cutoff)

    assert set(mapping) == {"fresh"}, "prune must keep only keys active in the window"


def test_prune_is_size_gated_noop_below_threshold(monkeypatch):
    monkeypatch.setattr(routes, "_RATE_LIMIT_MAP_SWEEP_THRESHOLD", 100)
    mapping = {"stale": [0.0], "also-stale": [1.0]}
    routes._prune_stale_rate_limit_keys(mapping, cutoff=1_000_000.0)
    # Under the threshold nothing is swept — keeps the hot path O(1).
    assert set(mapping) == {"stale", "also-stale"}


def test_map_bounded_across_many_one_shot_ips(monkeypatch):
    """End-to-end: many distinct IPs each calling once must not grow the map
    without bound once it crosses the sweep threshold."""
    routes._CSP_REPORT_RATE_LIMIT.clear()
    monkeypatch.setattr(routes, "_RATE_LIMIT_MAP_SWEEP_THRESHOLD", 50)

    base = 2_000_000.0
    # 200 distinct IPs, each a single hit far enough apart that older ones age out.
    for i in range(200):
        # Advance time by 2s per IP so the first entries fall outside the 60s window.
        routes._csp_report_rate_limited(
            _ip_handler(f"198.51.100.{i % 256}.{i}"), now=base + i * 2.0
        )

    # Without the sweep this would hold ~200 keys; with it, only the recent window.
    assert len(routes._CSP_REPORT_RATE_LIMIT) <= 60, (
        f"rate-limit map grew unbounded: {len(routes._CSP_REPORT_RATE_LIMIT)} keys"
    )
    routes._CSP_REPORT_RATE_LIMIT.clear()


class _IpHandler:
    def __init__(self, ip):
        self.client_address = (ip, 12345)
        self.headers = {}


def _ip_handler(ip):
    return _IpHandler(ip)
