"""Operator-run native scrollbar drag repro using Windows SendInput + CDP.

SendInput generates real OS-level mouse events that interact with the native
scrollbar thumb, unlike CDP's Input.dispatchMouseEvent which only goes through
the DOM hit-test and can't grab the OS-rendered scrollbar.

Flow:
1. CDP: get Chrome window position and scrollbar coordinates
2. SendInput: move real mouse cursor to scrollbar, click-and-drag
3. CDP: verify scrollTop changed, flag lifecycle, content integrity

Run directly with Python on Windows; this is not part of the automated pytest suite.
"""
import sys
import pytest
pytest.importorskip("websockets")
if sys.platform != "win32":
    pytest.skip("Windows-only (SendInput)", allow_module_level=True)

import ctypes
import ctypes.wintypes
import json
import time
import asyncio
import urllib.request
import websockets

# Windows SendInput structures
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000  # map to virtual screen, not primary monitor

user32 = ctypes.windll.user32

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("_input", _INPUT),
    ]

# Virtual screen origin and size (all monitors combined)
VS_X = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
VS_Y = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
VS_W = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
VS_H = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN

def screen_to_absolute(x, y):
    """Convert screen pixels to SendInput absolute coords (0-65535) on virtual desktop."""
    ax = int((x - VS_X) * 65535 / VS_W)
    ay = int((y - VS_Y) * 65535 / VS_H)
    return ax, ay

def send_mouse(x, y, flags):
    ax, ay = screen_to_absolute(x, y)
    mi = MOUSEINPUT(ax, ay, 0, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)
    inp = INPUT(type=0)  # INPUT_MOUSE = 0
    inp._input.mi = mi
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def mouse_move(x, y):
    send_mouse(x, y, MOUSEEVENTF_MOVE)

def mouse_down(x, y):
    send_mouse(x, y, MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN)

def mouse_up(x, y):
    send_mouse(x, y, MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP)


async def find_hermes_tab():
    tabs = json.loads(urllib.request.urlopen("http://localhost:9222/json").read())
    hermes = [t for t in tabs if "8787" in t.get("url", "") and t["type"] == "page" and "/session/" in t.get("url", "")]
    if not hermes:
        raise RuntimeError("No Hermes session tab found on :8787")
    for t in hermes:
        ws_url = t["webSocketDebuggerUrl"]
        try:
            async with websockets.connect(ws_url, max_size=1*1024*1024) as ws:
                msg = {"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": "typeof _scrollbarDragActive", "returnByValue": True
                }}
                await ws.send(json.dumps(msg))
                while True:
                    resp = json.loads(await ws.recv())
                    if resp.get("id") == 1:
                        val = resp.get("result", {}).get("result", {}).get("value", "?")
                        if val == "boolean":
                            return ws_url
                        break
        except Exception:
            continue
    raise RuntimeError("No tab with scrollbar fix deployed. Hard-refresh a Hermes session tab.")

MSG_ID = 0
def next_id():
    global MSG_ID
    MSG_ID += 1
    return MSG_ID

async def send_cdp(ws, method, params=None):
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
    result = await send_cdp(ws, "Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "awaitPromise": False,
    })
    if "result" in result and "value" in result["result"]:
        return result["result"]["value"]
    return result

async def run():
    ws_url = await find_hermes_tab()
    print(f"Connected to {ws_url}")

    async with websockets.connect(ws_url, max_size=5*1024*1024) as ws:
        # Calibrate viewport-to-screen mapping using a known-position probe.
        # CDP dispatched mouse events report CSS coordinates, so we can place a
        # probe click, read its screen-pixel position from the CDP target bounds,
        # and compute the offset.
        target_info = await send_cdp(ws, "Browser.getWindowForTarget")
        bounds = target_info.get("bounds", {})
        print(f"Window bounds: {bounds}")

        coords = await ev(ws, """
            (function(){
                var el = document.getElementById('messages');
                if (!el) return {error: 'no messages element'};
                var rect = el.getBoundingClientRect();
                return {
                    left: rect.left,
                    top: rect.top,
                    right: rect.right,
                    bottom: rect.bottom,
                    width: rect.width,
                    height: rect.height,
                    clientWidth: el.clientWidth,
                    scrollHeight: el.scrollHeight,
                    scrollTop: el.scrollTop,
                    scrollbarWidth: el.offsetWidth - el.clientWidth,
                    dpr: window.devicePixelRatio,
                    innerWidth: window.innerWidth,
                    innerHeight: window.innerHeight,
                };
            })()
        """)
        print(f"Messages element: {coords}")

        if coords.get("scrollbarWidth", 0) < 3:
            print("SKIP: No visible scrollbar")
            return

        dpr = coords.get("dpr", 1)
        inner_w = coords.get("innerWidth", 0)
        inner_h = coords.get("innerHeight", 0)

        # CDP window bounds are in CSS pixels (Chrome DPI-scales them).
        # innerWidth/innerHeight are also CSS pixels for the viewport.
        # Chrome header = window height - viewport height (all CSS px).
        chrome_header_css = bounds["height"] - inner_h
        chrome_left_css = bounds["width"] - inner_w

        # Viewport origin in CSS pixels from screen origin
        vp_css_x = bounds["left"] + chrome_left_css
        vp_css_y = bounds["top"] + chrome_header_css

        # Element scrollbar center in CSS pixels from screen origin
        scrollbar_css_x = vp_css_x + coords["left"] + coords["clientWidth"] + coords["scrollbarWidth"] / 2
        track_top_css = vp_css_y + coords["top"] + 20
        track_bottom_css = vp_css_y + coords["bottom"] - 20

        # Convert to physical screen pixels for SendInput
        scrollbar_screen_x = scrollbar_css_x * dpr
        track_top_screen = track_top_css * dpr
        track_bottom_screen = track_bottom_css * dpr

        # Clamp to the monitor containing the window center to avoid
        # overshoot into adjacent monitors (maximized windows extend
        # past screen edges for shadow/overshoot)
        pri_w = user32.GetSystemMetrics(0)
        pri_h = user32.GetSystemMetrics(1)
        scrollbar_screen_x = min(scrollbar_screen_x, pri_w - 3)
        track_top_screen = max(track_top_screen, 3)
        track_bottom_screen = min(track_bottom_screen, pri_h - 3)

        print(f"DPR={dpr}, chrome header: {chrome_header_css:.0f}px CSS")
        print(f"Viewport origin CSS: ({vp_css_x:.0f}, {vp_css_y:.0f})")
        print(f"Scrollbar screen X: {scrollbar_screen_x:.0f}, track Y: {track_top_screen:.0f}-{track_bottom_screen:.0f}")

        # Bring Chrome to foreground via CDP
        await send_cdp(ws, "Page.bringToFront")
        await asyncio.sleep(0.3)

        # Scroll to top first
        await ev(ws, "document.getElementById('messages').scrollTop = 0")
        await asyncio.sleep(0.5)

        pre_scroll = await ev(ws, "document.getElementById('messages').scrollTop")
        print(f"\nPre-drag scrollTop: {pre_scroll}")

        # === NATIVE DRAG TEST ===
        print("\n=== Native Scrollbar Drag via SendInput ===")

        start_y = track_top_screen + 30
        print(f"  Mouse down at ({scrollbar_screen_x:.0f}, {start_y:.0f})")
        mouse_down(int(scrollbar_screen_x), int(start_y))
        time.sleep(0.15)

        # Check flag immediately after native pointerdown
        flag_during = await ev(ws, "_scrollbarDragActive")
        print(f"  _scrollbarDragActive after native pointerdown: {flag_during}")

        # Drag down in steps
        positions = []
        steps = 12
        step_size = (track_bottom_screen - start_y) / steps
        for i in range(1, steps + 1):
            y = start_y + step_size * i
            mouse_move(int(scrollbar_screen_x), int(y))
            time.sleep(0.1)
            pos = await ev(ws, "document.getElementById('messages').scrollTop")
            positions.append(pos)
            print(f"  Step {i}/{steps}: screen_y={y:.0f}, scrollTop={pos}")

        # Release
        end_y = start_y + step_size * steps
        mouse_up(int(scrollbar_screen_x), int(end_y))
        time.sleep(0.3)

        flag_after = await ev(ws, "_scrollbarDragActive")
        print(f"  _scrollbarDragActive after release: {flag_after}")

        total_delta = positions[-1] - pre_scroll if positions else 0
        increasing = sum(1 for i in range(1, len(positions)) if positions[i] > positions[i-1])
        reversals = sum(1 for i in range(1, len(positions)) if positions[i] < positions[i-1])

        # Post-release content check
        post = await ev(ws, """
            (function(){
                var inner = document.getElementById('msgInner');
                if (!inner) return {error: 'no inner'};
                var kids = Array.from(inner.children);
                var content = kids.filter(function(c){
                    return c.classList.contains('msg-row') || c.classList.contains('assistant-turn');
                });
                var blanks = content.filter(function(c){
                    return c.offsetHeight > 0 && c.textContent.trim() === '';
                }).length;
                return {content: content.length, blanks: blanks};
            })()
        """)

        print("\n=== RESULTS ===")
        print(f"  Total scroll delta: {total_delta}px")
        print(f"  Monotonic steps: {increasing}/{steps-1}")
        print(f"  Reversals: {reversals}")
        print(f"  Flag set during drag: {flag_during}")
        print(f"  Flag cleared after release: {flag_after == False}")
        print(f"  Content nodes: {post.get('content', 0)}, blanks: {post.get('blanks', -1)}")

        drag_worked = total_delta > 100
        monotonic = increasing >= steps - 3
        no_blanks = post.get("blanks", -1) == 0
        flag_lifecycle = flag_during == True and flag_after == False

        verdicts = {
            "T1 Native drag moved scroll": drag_worked,
            "T2 Monotonic scrolling": monotonic,
            "T3 No blank content after release": no_blanks,
            "T4 Flag lifecycle (set during, cleared after)": flag_lifecycle,
        }

        print(f"\n{'='*50}")
        for name, v in verdicts.items():
            print(f"  {'PASS' if v else 'FAIL'}: {name}")
        all_pass = all(verdicts.values())
        print(f"\n  ALL PASS: {all_pass}")
        print(f"{'='*50}")

if __name__ == "__main__":
    asyncio.run(run())
