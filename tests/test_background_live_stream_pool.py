"""Background live-stream pool: keep N conversations streaming at once.

Historically ``closeOtherLiveStreams(activeSid)`` closed the EventSource of
EVERY session except the one being viewed. The stated reason (#2313) was the
browser connection pool: one EventSource per background conversation would
exhaust the HTTP/1.1 six-connection-per-origin budget and starve ordinary
XHR/fetch traffic.

That reasoning caps the number of *simultaneous* streams; it does not require
the number to be one. Closing every background stream means switching away from
a running conversation drops its live token feed: coming back re-fetches and
replays the run journal instead of having simply kept receiving. For an
operator piloting several conversations at once (the multi-tab workflow) that is
the dominant source of perceived slowness.

These tests lock the bounded-pool behaviour:

* the active session's stream is never closed by the pool;
* up to ``LIVE_STREAM_POOL_MAX`` streams stay open simultaneously;
* beyond that, the LEAST-RECENTLY-USED background stream is evicted, so the
  connection budget stays bounded exactly as #2313 required;
* eviction goes through ``closeLiveStream``, preserving its snapshot + reattach
  bookkeeping rather than dropping the transport silently.

The function under test is the real region extracted from static/messages.js and
executed under node, matching tests/test_5306_subagent_sidebar_flicker.py.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This module only reads static source; it does not need the HTTP fixture."""


def _source_const(name: str) -> int:
    """Read an integer constant's value straight from static/messages.js.

    Tests assert the pool's INVARIANTS (a multiplexed transport gets a wider
    budget; the cap is enforced; unknown fails closed). Pinning the literal
    numbers here would turn a deliberate capacity change into a test failure
    that says nothing about correctness, so the numbers are sourced from the
    implementation and only their relationships are asserted.
    """
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    match = re.search(r"^const\s+" + re.escape(name) + r"\s*=\s*(\d+)\s*;", src, re.M)
    if not match:
        raise AssertionError(f"{name} not found in static/messages.js")
    return int(match.group(1))


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


_PREAMBLE = """
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
// `eval('const X=…')` scopes the binding to the eval itself and does NOT leak
// it to the global scope (function declarations do, which is why extractFunc
// works). Extract the right-hand side instead and bind it explicitly.
function extractConstValue(name) {{
  const re = new RegExp('const\\\\s+' + name + '\\\\s*=\\\\s*([^;]*);');
  const m = src.match(re);
  if (!m) throw new Error(name + ' not found');
  return eval(m[1]);
}}
// Module-level state the extracted pool functions close over. Declared on
// globalThis (not with `const`/`let`) so the eval'd regions resolve them as
// bare identifiers, exactly as they do inside the real module scope.
globalThis._LIVE_STREAM_USE = {{}};
globalThis._liveStreamUseClock = 0;
// Record which sessions were torn down, so a test can assert the pool evicted
// the right ones without needing the whole closeLiveStream machinery.
global.closed = [];
global.LIVE_STREAMS = {{}};
global.closeLiveStream = function(sid){{
  if (!global.LIVE_STREAMS[sid]) return;
  global.closed.push(sid);
  delete global.LIVE_STREAMS[sid];
  delete globalThis._LIVE_STREAM_USE[sid];
}};
"""


def _preamble() -> str:
    js = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    return _PREAMBLE.format(js=js)


def _pool_source(scenario: str, next_hop_protocol: str | None = "http/1.1") -> str:
    """Build a Node program exercising the real pool functions.

    ``next_hop_protocol`` fakes what the browser negotiated with the FIRST hop
    (tailscale serve), which is what actually spends the per-origin connection
    budget. ``None`` simulates navigation timing being unavailable.
    """
    if next_hop_protocol is None:
        perf = "globalThis.performance = { getEntriesByType: () => [] };\n"
    else:
        perf = (
            "globalThis.performance = { getEntriesByType: (t) => "
            "t === 'navigation' ? [{ nextHopProtocol: %r }] : [] };\n"
            % (next_hop_protocol,)
        ).replace("'", '"')
    return (
        _preamble()
        + perf
        + "globalThis.LIVE_STREAM_POOL_MAX_HTTP1 = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_HTTP1');\n"
        + "globalThis.LIVE_STREAM_POOL_MAX_MULTIPLEXED = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_MULTIPLEXED');\n"
        + "globalThis._liveStreamTransportUncertain = false;\n"
        + "eval(extractFunc('_liveStreamPoolMax'));\n"
        + "eval(extractFunc('_markLiveStreamTransportUncertain'));\n"
        + "eval(extractFunc('_touchLiveStreamUse'));\n"
        + "eval(extractFunc('closeOtherLiveStreams'));\n"
        + scenario
    )


def test_active_session_stream_is_never_closed_by_the_pool():
    """The conversation on screen must keep its own transport."""
    out = json.loads(_run_node(_pool_source("""
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({ open:Object.keys(LIVE_STREAMS), closed }));
""")))
    assert out["open"] == ["a"]
    assert out["closed"] == []


def test_background_streams_below_the_cap_stay_open():
    """RED before the fix: every background stream was closed unconditionally.

    One background conversation plus the active one is inside the connection
    budget, so both must keep streaming.
    """
    out = json.loads(_run_node(_pool_source("""
LIVE_STREAMS['a'] = { streamId:'sa' };
LIVE_STREAMS['b'] = { streamId:'sb' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({ open:Object.keys(LIVE_STREAMS).sort(), closed }));
""")))
    assert out["open"] == ["a", "b"]
    assert out["closed"] == []


def test_pool_evicts_least_recently_used_background_stream_beyond_the_cap():
    """The connection budget stays bounded: the oldest background stream goes.

    'b' is touched (viewed) after 'c', so 'c' is the least-recently-used and is
    the one evicted when the pool overflows.
    """
    out = json.loads(_run_node(_pool_source("""
// Open order: b, then c.
LIVE_STREAMS['b'] = { streamId:'sb' }; _touchLiveStreamUse('b');
LIVE_STREAMS['c'] = { streamId:'sc' }; _touchLiveStreamUse('c');
// The user visited 'b' again, making 'c' the least-recently-used.
_touchLiveStreamUse('b');
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({
  open:Object.keys(LIVE_STREAMS).sort(), closed, cap:_liveStreamPoolMax(),
}));
""")))
    assert out["cap"] == 3
    # 3 streams total is exactly the cap: nothing is evicted yet.
    assert out["open"] == ["a", "b", "c"]
    assert out["closed"] == []


def test_pool_never_exceeds_the_connection_budget():
    """Six live conversations must collapse to the cap, active one retained."""
    out = json.loads(_run_node(_pool_source("""
for (const sid of ['b','c','d','e','f']) {
  LIVE_STREAMS[sid] = { streamId:'s'+sid };
  _touchLiveStreamUse(sid);
}
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({
  open:Object.keys(LIVE_STREAMS).sort(), closed:closed.slice().sort(),
  cap:_liveStreamPoolMax(),
}));
""")))
    assert len(out["open"]) == out["cap"]
    assert "a" in out["open"], "the viewed conversation must never be evicted"
    # Only the two most recently used background streams survive.
    assert out["closed"] == ["b", "c", "d"]


def test_multiplexed_transport_raises_the_cap():
    """On HTTP/2 the per-origin connection limit does not apply, so more
    conversations may stream at once. The browser reports the protocol it
    negotiated with the first hop; the pool must honour it.

    The expected cap is read from the source constant rather than hardcoded:
    the invariant under test is "a multiplexed transport uses the wider
    budget, and the pool never exceeds it", not one particular number.
    """
    multiplexed_cap = _source_const("LIVE_STREAM_POOL_MAX_MULTIPLEXED")
    http1_cap = _source_const("LIVE_STREAM_POOL_MAX_HTTP1")
    assert multiplexed_cap > http1_cap, (
        "a multiplexed transport must allow strictly more concurrent streams "
        "than the HTTP/1.1 connection budget"
    )
    # Offer more background streams than the cap so the assertion below proves
    # the cap is enforced, not merely that every stream survived.
    background = [f"b{i}" for i in range(multiplexed_cap + 3)]
    out = json.loads(_run_node(_pool_source("""
for (const sid of %s) {
  LIVE_STREAMS[sid] = { streamId:'s'+sid };
  _touchLiveStreamUse(sid);
}
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({
  open:Object.keys(LIVE_STREAMS).sort(), cap:_liveStreamPoolMax(),
}));
""" % json.dumps(background), next_hop_protocol="h2")))
    assert out["cap"] == multiplexed_cap
    assert len(out["open"]) == multiplexed_cap
    assert "a" in out["open"], "the viewed conversation must never be evicted"


def test_http3_is_treated_as_multiplexed():
    out = json.loads(_run_node(_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        next_hop_protocol="h3-29",
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_MULTIPLEXED")


def test_unknown_transport_fails_closed_to_the_http1_budget():
    """Navigation timing can be unavailable. Unknown must not be treated as
    multiplexed, and must not be memoised as a permanent answer."""
    out = json.loads(_run_node(_pool_source(
        "const first=_liveStreamPoolMax();\n"
        "globalThis.performance = { getEntriesByType: (t) => "
        "t === 'navigation' ? [{ nextHopProtocol: 'h2' }] : [] };\n"
        "console.log(JSON.stringify({ first, second:_liveStreamPoolMax() }));",
        next_hop_protocol=None,
    )))
    assert out["first"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1"), (
        "unknown transport must fail closed to the HTTP/1.1 budget"
    )
    assert out["second"] == _source_const("LIVE_STREAM_POOL_MAX_MULTIPLEXED"), (
        "an unknown transport must not be cached: once navigation timing "
        "reports h2 the pool must widen"
    )


def test_eviction_uses_close_live_stream_for_snapshot_and_reattach():
    """Evicted streams must go through closeLiveStream, not a raw .close().

    closeLiveStream snapshots the live-turn DOM and flags INFLIGHT.reattach so
    returning to the conversation restores it. Bypassing it would lose the
    streamed content the pool is meant to preserve.
    """
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    start = src.index("function closeOtherLiveStreams(")
    body = src[start:src.index("\n}", start)]
    assert "closeLiveStream(" in body
    assert ".close()" not in body, "must delegate teardown, not close the transport directly"


# ---------------------------------------------------------------------------
# Service-worker navigation entries report an EMPTY nextHopProtocol, which used
# to pin an h2 origin to the HTTP/1.1 budget on every visit after the first.
# ---------------------------------------------------------------------------

def _sw_pool_source(
    scenario: str,
    resources,
    origin: str = "https://webui.example.net",
    nav_protocol: str = "",
) -> str:
    """Pool harness where navigation timing is service-worker blanked.

    ``resources`` is a list of ``(url, nextHopProtocol)`` pairs, mirroring what
    Resource Timing exposes for subresources the worker did not intercept.
    """
    entries = json.dumps([{"name": u, "nextHopProtocol": p} for u, p in resources])
    perf = (
        "globalThis.location = { origin: %s };\n"
        "globalThis.performance = { getEntriesByType: (t) => "
        "t === 'navigation' ? [{ nextHopProtocol: %s }] : "
        "(t === 'resource' ? %s : []) };\n"
    ) % (json.dumps(origin), json.dumps(nav_protocol), entries)
    return (
        _preamble()
        + perf
        + "globalThis.LIVE_STREAM_POOL_MAX_HTTP1 = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_HTTP1');\n"
        + "globalThis.LIVE_STREAM_POOL_MAX_MULTIPLEXED = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_MULTIPLEXED');\n"
        + "globalThis._liveStreamTransportUncertain = false;\n"
        + "eval(extractFunc('_liveStreamPoolMax'));\n"
        + scenario
    )


def test_service_worker_blanked_navigation_falls_back_to_resource_timing():
    """The regression this fixes: an h2 origin behind a service worker.

    The navigation entry reports '', so the pool used to fail closed to the
    HTTP/1.1 budget forever -- which also shrank the nav cache sized off it.
    """
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[("https://webui.example.net/static/messages.js", "h2")],
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_MULTIPLEXED")


def test_resource_timing_fallback_respects_http1():
    """The fallback must report what was negotiated, not assume multiplexing."""
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[("https://webui.example.net/static/app.css", "http/1.1")],
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")


def test_multiplexed_decision_is_revalidated_after_transport_change():
    """A page-lifetime h2 verdict must not outlive a later HTTP/1.1 transport."""
    out = json.loads(_run_node(_sw_pool_source(
        "const first=_liveStreamPoolMax();\n"
        "globalThis.performance = { getEntriesByType: (t) => "
        "t === 'navigation' ? [{ nextHopProtocol: '' }] : "
        "(t === 'resource' ? [{ name: 'https://webui.example.net/new.js', "
        "nextHopProtocol: 'http/1.1' }] : []) };\n"
        "console.log(JSON.stringify({ first, second:_liveStreamPoolMax() }));",
        resources=[("https://webui.example.net/old.js", "h2")],
    )))
    assert out["first"] == _source_const("LIVE_STREAM_POOL_MAX_MULTIPLEXED")
    assert out["second"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")


def test_transport_uncertainty_guard_is_fail_closed():
    source = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    assert "let_liveStreamTransportUncertain=false" in compact
    assert "function_markLiveStreamTransportUncertain()" in compact
    assert "if(_liveStreamTransportUncertain)returnLIVE_STREAM_POOL_MAX_HTTP1" in compact
    assert "closeOtherLiveStreams(keepSid)" in compact
    assert "addEventListener('online',_markLiveStreamTransportUncertain)" in compact
    assert "event.persisted" in source


def test_marking_transport_uncertain_prunes_immediately_and_locks_budget():
    out = json.loads(_run_node(_pool_source("""
globalThis.S={sessionId:'a'};
for (const sid of ['a','b','c','d']) {
  LIVE_STREAMS[sid]={streamId:'s'+sid};
  _touchLiveStreamUse(sid);
}
_markLiveStreamTransportUncertain();
console.log(JSON.stringify({
  cap:_liveStreamPoolMax(), open:Object.keys(LIVE_STREAMS).sort(),
  closed:closed.slice().sort(), uncertain:_liveStreamTransportUncertain,
}));
""")))
    assert out["uncertain"] is True
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")
    assert "a" in out["open"]
    assert len(out["open"]) == out["cap"]
    assert len(out["closed"]) == 1


def test_newest_resource_overrides_stale_navigation_transport():
    """Fresh same-origin timing must override an old nonblank navigation h2."""
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[("https://webui.example.net/new.js", "http/1.1")],
        nav_protocol="h2",
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")


def test_cross_origin_resources_must_not_widen_the_pool():
    """A third-party CDN on h2 says nothing about this origin's connection.

    Trusting it would OVER-size the pool against an HTTP/1.1 origin, which is
    the failure mode the same-origin filter exists to prevent.
    """
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[
            ("https://cdn.example.com/lib.js", "h2"),
            ("https://fonts.example.org/f.woff2", "h3"),
        ],
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1"), (
        "cross-origin nextHopProtocol must never size this origin's pool"
    )


def test_origin_prefix_match_is_not_fooled_by_lookalike_host():
    """'https://webui.example.net.evil.com/...' must not count as same-origin."""
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[("https://webui.example.net.evil.com/x.js", "h2")],
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")


def test_blank_resource_protocols_still_fail_closed():
    """Cached/blocked resources report ''. No signal must stay fail-closed."""
    out = json.loads(_run_node(_sw_pool_source(
        "const first=_liveStreamPoolMax();\n"
        "console.log(JSON.stringify({ first }));",
        resources=[
            ("https://webui.example.net/a.js", ""),
            ("https://webui.example.net/b.js", ""),
        ],
    )))
    assert out["first"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")


def test_newest_blank_same_origin_resource_does_not_reuse_stale_h2():
    """A stale h2 entry must not widen the pool after the transport becomes unknown.

    Resource Timing entries are chronological. If the newest same-origin entry
    has no protocol, walking farther back to an old h2 observation would turn an
    uncertain current transport into a permanently cached multiplexed verdict.
    """
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[
            ("https://webui.example.net/old.js", "h2"),
            ("https://webui.example.net/new.js", ""),
        ],
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")


def test_navigation_protocol_is_fallback_without_same_origin_resource():
    """Navigation timing remains authoritative when no relevant resource exists."""
    out = json.loads(_run_node(_sw_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        resources=[("https://cdn.example.com/x.js", "http/1.1")],
        nav_protocol="h2",
    )))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_MULTIPLEXED")


def test_missing_location_object_fails_closed():
    """Without an origin there is no safe same-origin test: stay conservative."""
    src = (
        _preamble()
        + "globalThis.performance = { getEntriesByType: (t) => "
          "t === 'navigation' ? [{ nextHopProtocol: '' }] : "
          "[{ name:'https://x/y.js', nextHopProtocol:'h2' }] };\n"
        + "globalThis.LIVE_STREAM_POOL_MAX_HTTP1 = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_HTTP1');\n"
        + "globalThis.LIVE_STREAM_POOL_MAX_MULTIPLEXED = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_MULTIPLEXED');\n"
        + "globalThis._liveStreamTransportUncertain = false;\n"
        + "eval(extractFunc('_liveStreamPoolMax'));\n"
        + "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));"
    )
    out = json.loads(_run_node(src))
    assert out["cap"] == _source_const("LIVE_STREAM_POOL_MAX_HTTP1")
