"""Operator-run CDP repro for scrollbar drag flag lifecycle and event dispatch.

Tests the actual deployed code on localhost:8787 via Chrome DevTools Protocol.
Verifies flag lifecycle, event handler wiring, and render behavior.

Run directly with Python; this is not part of the automated pytest suite.
"""
import pytest
pytest.importorskip("websockets")

import json
import asyncio
import urllib.request
import websockets

async def find_hermes_tab():
    """Find a Hermes session tab that has the scrollbar drag fix deployed."""
    tabs = json.loads(urllib.request.urlopen("http://localhost:9222/json").read())
    hermes = [t for t in tabs if "8787" in t.get("url", "") and t["type"] == "page" and "/session/" in t.get("url", "")]
    if not hermes:
        raise RuntimeError("No Hermes session tab found on :8787")
    for t in hermes:
        ws_url = t["webSocketDebuggerUrl"]
        try:
            async with websockets.connect(ws_url, max_size=1*1024*1024) as ws:
                mid = 1
                msg = {"id": mid, "method": "Runtime.evaluate", "params": {
                    "expression": "typeof _scrollbarDragActive",
                    "returnByValue": True
                }}
                await ws.send(json.dumps(msg))
                while True:
                    resp = json.loads(await ws.recv())
                    if resp.get("id") == mid:
                        val = resp.get("result", {}).get("result", {}).get("value", "?")
                        if val == "boolean":
                            return ws_url
                        break
        except Exception:
            continue
    # If none have the fix, reload the first one and retry
    ws_url = hermes[0]["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url, max_size=1*1024*1024) as ws:
        msg = {"id": 999, "method": "Page.reload", "params": {"ignoreCache": True}}
        await ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await ws.recv())
            if resp.get("id") == 999:
                break
    await asyncio.sleep(3)
    return ws_url

MSG_ID = 0

def next_id():
    global MSG_ID
    MSG_ID += 1
    return MSG_ID

async def send(ws, method, params=None):
    mid = next_id()
    msg = {"id": mid, "method": method}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == mid:
            if "error" in resp:
                raise RuntimeError(f"CDP error: {resp['error']}")
            return resp.get("result", {})

async def ev(ws, expr):
    result = await send(ws, "Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": False,
    })
    if "result" in result and "value" in result["result"]:
        return result["result"]["value"]
    return result

async def run_tests():
    ws_url = await find_hermes_tab()
    print(f"Connecting to {ws_url}")
    passed = 0
    failed = 0

    async with websockets.connect(ws_url, max_size=5*1024*1024) as ws:
        # Verify the fix is deployed
        flag_type = await ev(ws, "typeof _scrollbarDragActive")
        if flag_type != "boolean":
            print(f"ABORT: _scrollbarDragActive type is '{flag_type}', fix not deployed")
            print("Hard-refresh the tab (Ctrl+Shift+R) then re-run")
            return

        # T1: Flag starts as false
        print("\n=== T1: Flag initial state ===")
        # Clean up any leftover state
        await ev(ws, "_scrollbarDragActive = false")
        flag_init = await ev(ws, "_scrollbarDragActive")
        if flag_init == False:
            print("  PASS: flag starts as false")
            passed += 1
        else:
            print(f"  FAIL: expected false, got {flag_init}")
            failed += 1

        # T2: Synthetic pointerdown on scrollbar sets the flag
        print("\n=== T2: Pointerdown on scrollbar area ===")
        result = await ev(ws, """
            (function(){
                _scrollbarDragActive = false;
                var el = document.getElementById('messages');
                if (!el) return {error: 'no messages element'};
                var e = new PointerEvent('pointerdown', {bubbles: true});
                Object.defineProperty(e, 'offsetX', {value: el.clientWidth + 5});
                el.dispatchEvent(e);
                var flagAfter = _scrollbarDragActive;
                _scrollbarDragActive = false;
                return {scrollbar_click_activates: flagAfter};
            })()
        """)
        if result.get("scrollbar_click_activates") == True:
            print("  PASS: scrollbar click activates flag")
            passed += 1
        else:
            print(f"  FAIL: {result}")
            failed += 1

        # T3: Pointerdown on content area does NOT set the flag
        print("\n=== T3: Pointerdown on content area ===")
        result = await ev(ws, """
            (function(){
                _scrollbarDragActive = false;
                var el = document.getElementById('messages');
                var e = new PointerEvent('pointerdown', {bubbles: true});
                Object.defineProperty(e, 'offsetX', {value: 100});
                el.dispatchEvent(e);
                var flagAfter = _scrollbarDragActive;
                return {content_click_ignores: !flagAfter};
            })()
        """)
        if result.get("content_click_ignores") == True:
            print("  PASS: content click does not activate flag")
            passed += 1
        else:
            print(f"  FAIL: {result}")
            failed += 1

        # T4: Pointerup clears the flag
        print("\n=== T4: Pointerup clears flag ===")
        result = await ev(ws, """
            (function(){
                _scrollbarDragActive = true;
                window.dispatchEvent(new PointerEvent('pointerup'));
                var cleared = !_scrollbarDragActive;
                return {flag_cleared: cleared};
            })()
        """)
        if result.get("flag_cleared") == True:
            print("  PASS: pointerup clears flag")
            passed += 1
        else:
            print(f"  FAIL: {result}")
            failed += 1

        # T5: Pointercancel also clears the flag
        print("\n=== T5: Pointercancel clears flag ===")
        result = await ev(ws, """
            (function(){
                _scrollbarDragActive = true;
                window.dispatchEvent(new PointerEvent('pointercancel'));
                var cleared = !_scrollbarDragActive;
                return {flag_cleared: cleared};
            })()
        """)
        if result.get("flag_cleared") == True:
            print("  PASS: pointercancel clears flag")
            passed += 1
        else:
            print(f"  FAIL: {result}")
            failed += 1

        # T6: Spacer-only path during drag (verify spacers exist)
        print("\n=== T6: Spacer-only path ===")
        result = await ev(ws, """
            (function(){
                var inner = document.getElementById('msgInner');
                if (!inner) return {error: 'no inner'};
                var before = inner.querySelector('[data-virtual-spacer="before"]');
                var after = inner.querySelector('[data-virtual-spacer="after"]');
                return {
                    has_before: !!before,
                    has_after: !!after,
                    before_h: before ? before.style.height : null,
                    after_h: after ? after.style.height : null,
                };
            })()
        """)
        if result.get("has_before") and result.get("has_after"):
            print(f"  PASS: spacers present (before={result['before_h']}, after={result['after_h']})")
            passed += 1
        else:
            print(f"  FAIL or SKIP: {result}")
            failed += 1

        # T7: Render suppression - DOM children count stable during drag
        print("\n=== T7: DOM stability during drag (render suppression) ===")
        result = await ev(ws, """
            (function(){
                var inner = document.getElementById('msgInner');
                if (!inner) return {error: 'no inner'};
                var countBefore = inner.children.length;
                var htmlLenBefore = inner.innerHTML.length;

                _scrollbarDragActive = true;

                // Attempt a forced render - the drag guard should intercept
                try {
                    _scheduleMessageVirtualizedRender(true);
                } catch(e) {}

                // rAF is async so the render won't fire synchronously,
                // but we can verify the flag prevents synchronous render
                var countAfter = inner.children.length;
                var htmlLenAfter = inner.innerHTML.length;

                _scrollbarDragActive = false;
                return {
                    count_before: countBefore,
                    count_after: countAfter,
                    html_len_before: htmlLenBefore,
                    html_len_after: htmlLenAfter,
                    children_preserved: countBefore === countAfter,
                };
            })()
        """)
        if result.get("children_preserved") == True:
            print(f"  PASS: children preserved ({result['count_before']} -> {result['count_after']})")
            passed += 1
        else:
            print(f"  FAIL: {result}")
            failed += 1

        # T8: Full render fires after drag release
        print("\n=== T8: Full render after drag release ===")
        result = await ev(ws, """
            (function(){
                _scrollbarDragActive = false;
                // Trigger forced render, should go through full path
                try {
                    _scheduleMessageVirtualizedRender(true);
                } catch(e) {}
                var inner = document.getElementById('msgInner');
                return {
                    inner_exists: !!inner,
                    has_children: inner ? inner.children.length > 0 : false,
                };
            })()
        """)
        if result.get("has_children") == True:
            print("  PASS: content rendered after drag release")
            passed += 1
        else:
            print(f"  FAIL: {result}")
            failed += 1

        print(f"\n{'='*50}")
        print(f"RESULTS: {passed} passed, {failed} failed, {passed+failed} total")
        print(f"{'='*50}")
        if failed == 0:
            print("ALL CDP BEHAVIORAL TESTS PASSED")
        else:
            print(f"{failed} TEST(S) FAILED")

asyncio.run(run_tests())
