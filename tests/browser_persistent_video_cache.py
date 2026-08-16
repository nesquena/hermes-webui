#!/usr/bin/env python3
"""Real-Chromium behavior gate for the persistent snapshot-video cache.

The fixture serves the production ``static/media-cache.js`` unchanged and uses
real Cache Storage, ReadableStream, MutationObserver, IntersectionObserver,
AbortController, Blob URLs, reloads, and network requests. No provider or agent
credentials are used.
"""
from __future__ import annotations

import json
import base64
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.getenv("VIDEO_CACHE_SCRIPT") or (ROOT / "static" / "media-cache.js"))
UI_SCRIPT = ROOT / "static" / "ui.js"
STYLE = ROOT / "static" / "style.css"
DIGEST = "a" * 64
MP4 = base64.b64decode(
    "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAANcbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAHgAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAod0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAHgAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAABAAAAAQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAB4AAAEAAABAAAAAAH/bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAyAAAACABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABqm1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAWpzdGJsAAAAvnN0c2QAAAAAAAAAAQAAAK5hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAABAAEABIAAAASAAAAAAAAAABFUxhdmM2Mi4xMS4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANGF2Y0MBZAAK/+EAF2dkAAqs2V7ARAAAAwAEAAADAMg8SJZYAQAGaOvjyyLA/fj4AAAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAAL7iAAAAAAAAABhzdHRzAAAAAAAAAAEAAAADAAACAAAAABRzdHNzAAAAAAAAAAEAAAABAAAAKGN0dHMAAAAAAAAAAwAAAAEAAAQAAAAAAQAABgAAAAABAAACAAAAABxzdHNjAAAAAAAAAAEAAAABAAAAAwAAAAEAAAAgc3RzegAAAAAAAAAAAAAAAwAAAsUAAAAMAAAADAAAABRzdGNvAAAAAAAAAAEAAAOMAAAAYXVkdGEAAABZbWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAsaWxzdAAAACSpdG9vAAAAHGRhdGEAAAABAAAAAExhdmY2Mi4zLjEwMAAAAAhmcmVlAAAC5W1kYXQAAAKuBgX//6rcRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY1IHIzMjIzIDA0ODBjYjAgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDI1IC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRjdD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTMgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJfYmlhcz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MiBrZXlpbnQ9MjUwIGtleWludF9taW49MjUgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAPZYiEADP//vbsvgU2FMjBAAAACEGaImxCv/7AAAAACAGeQXkK/8SB"
)


class State:
    lock = threading.Lock()
    requests: Counter[str] = Counter()
    scope_requests: Counter[str] = Counter()
    aborted: Counter[str] = Counter()
    native: Counter[str] = Counter()
    ranges: dict[str, list[str]] = {}
    authority = "scope-a"

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.requests.clear()
            cls.scope_requests.clear()
            cls.aborted.clear()
            cls.native.clear()
            cls.ranges.clear()
            cls.authority = "scope-a"


class FixtureServer(ThreadingHTTPServer):
    def handle_error(self, _request, _client_address):
        # Client-side AbortController cancellation is an expected test event.
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, status: int, body: bytes, content_type="application/octet-stream", *, length=True):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if length:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if not length:
            self.close_connection = True

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            no_cache = query.get("nocache", [""])[0] == "1"
            prefix = "window.__HERMES_VIDEO_CACHE_TEST__={perFileBytes:4096,totalBytes:5000,forceCacheUnavailable:true};" if no_cache else "window.__HERMES_VIDEO_CACHE_TEST__={perFileBytes:4096,totalBytes:5000};"
            html = f"""<!doctype html><meta charset=utf-8><link rel=stylesheet href=/static/style.css><style>
body{{margin:0;background:var(--bg,#111);color:var(--text,#eee)}} .host{{padding:16px;width:min(520px,calc(100vw - 32px));box-sizing:border-box}}
</style><div class=host id=host></div><script>window.__HERMES_CONFIG__={{maxUploadBytes:20971520}};window.t=window.t||((key)=>key);{prefix}</script><script src=/static/media-cache.js></script><script src=/static/ui.js></script>"""
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/static/media-cache.js":
            if not SCRIPT.exists():
                self._send(404, b"missing media-cache.js", "text/plain")
            else:
                self._send(200, SCRIPT.read_bytes(), "text/javascript; charset=utf-8")
            return
        if parsed.path == "/static/ui.js":
            self._send(200, UI_SCRIPT.read_bytes(), "text/javascript; charset=utf-8")
            return
        if parsed.path == "/static/style.css":
            self._send(200, STYLE.read_bytes(), "text/css; charset=utf-8")
            return
        if parsed.path == "/api/media-cache/scope":
            with State.lock:
                scope = State.authority
            session_id = query.get("session_id", [""])[0]
            media_path = query.get("path", [""])[0]
            if session_id not in {"session-a", "session-b"} or not media_path.endswith(".mp4"):
                self._send(404, b'{"error":"session not found"}', "application/json")
                return
            with State.lock:
                State.scope_requests[media_path] += 1
            if media_path.endswith("slow-scope-left.mp4"):
                time.sleep(0.35)
            scoped = f"{scope}-{session_id}"
            self._send(200, json.dumps({"scope": scoped, "schema": 1}).encode(), "application/json")
            return
        if parsed.path == "/test/scope":
            value = query.get("value", [""])[0]
            with State.lock:
                State.authority = value
            self._send(200, b"ok", "text/plain")
            return
        if parsed.path == "/test/counts":
            with State.lock:
                payload = {
                    "requests": dict(State.requests),
                    "scope_requests": dict(State.scope_requests),
                    "native": dict(State.native),
                    "ranges": dict(State.ranges),
                    "aborted": dict(State.aborted),
                }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path != "/api/media":
            self._send(404, b"not found", "text/plain")
            return

        case = query.get("case", ["default"])[0]
        size = int(query.get("size", ["64"])[0])
        # Record application-owned full fetches separately from native media
        # requests so the production integration path proves it did not race a
        # browser-owned preload/Range request.
        if self.headers.get("X-Hermes-Video-Cache") == "1":
            with State.lock:
                State.requests[case] += 1
        else:
            with State.lock:
                State.native[case] += 1
                State.ranges.setdefault(case, []).append(str(self.headers.get("Range") or ""))
        body = MP4 + (b"\0" * max(0, size - len(MP4)))
        body = body[:size]
        range_header = str(self.headers.get("Range") or "")
        start, end = 0, max(0, len(body) - 1)
        partial = False
        if range_header.startswith("bytes=") and len(body):
            try:
                left, right = range_header[6:].split("-", 1)
                start = int(left or 0)
                end = min(end, int(right) if right else end)
                partial = 0 <= start <= end
            except (TypeError, ValueError):
                partial = False
        payload = body[start:end + 1] if partial else body
        status = 503 if case == "slow-reject-http" else (206 if partial else 200)
        self.send_response(status)
        content_type = "text/plain" if case == "slow-reject-wrong-mime" else "video/mp4"
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        if not case.startswith("live-fallback") and case != "slow-reject-unattested":
            self.send_header("X-Hermes-Media-Snapshot", DIGEST)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Accept-Ranges", "bytes")
        unknown_length = case in {"unknown", "slow-unknown-oversize"}
        if not unknown_length or partial:
            self.send_header("Content-Length", str(len(payload)))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        chunk_size = 128
        sent = 0
        try:
            while sent < len(payload):
                part = payload[sent:sent + chunk_size]
                self.wfile.write(part)
                self.wfile.flush()
                sent += len(part)
                if case.startswith("slow"):
                    delay = 0.35 if case == "slow-shared" else (0.03 if case == "slow-unknown-oversize" else 0.20)
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if self.headers.get("X-Hermes-Video-Cache") == "1":
                with State.lock:
                    State.aborted[case] += 1
        if unknown_length:
            self.close_connection = True


def media_url(case: str, size: int = 1800, session_id: str = "session-a") -> str:
    return f"/api/media?path=%2Ftmp%2F{case}.mp4&inline=1&session_id={session_id}&snap={DIGEST}&case={case}&size={size}"


def video_script(url: str, *, offscreen=False, activate=False) -> str:
    return f"""(() => {{
      const host=document.getElementById('host');
      const wrap=document.createElement('div'); wrap.className='msg-media-editor'; {"wrap.setAttribute('style','margin-top:3000px')" if offscreen else ''};
      wrap.innerHTML=`<video class="msg-media-video" src="{url}" preload="none"></video><div class="msg-media-meta"><span class="msg-media-name">fixture-video.mp4</span><span class="msg-media-cache-progress" hidden></span></div>`;
      host.appendChild(wrap);
      const video=wrap.querySelector('video');
      HermesPersistentVideoCache.observe(video);
      {"HermesPersistentVideoCache.detach(video); HermesPersistentVideoCache.observe(video); video.dispatchEvent(new Event('play'));" if activate else ''}
      return video;
    }})()"""


def production_video_script(url: str, *, offscreen=False) -> str:
    return f"""(() => {{
      const template=document.createElement('template');
      template.innerHTML=_mediaPlayerHtml('video',{json.dumps(url)},'fixture-video.mp4');
      const wrap=template.content.firstElementChild;
      {"wrap.style.marginTop='3000px';" if offscreen else ''}
      document.getElementById('host').appendChild(wrap);
      return wrap.querySelector('video');
    }})()"""


def wait_state(page, handle, state: str, timeout=5000):
    try:
        page.wait_for_function("([v,s]) => v.dataset.persistentVideoState === s", arg=[handle, state], timeout=timeout)
    except Exception as exc:
        current = page.evaluate("v => ({state:v.dataset.persistentVideoState,progress:v.dataset.cacheProgress,src:v.getAttribute('src')})", handle)
        snapshot = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        raise AssertionError(f"wait_state expected={state} current={current} cache={snapshot}") from exc


def counts(page):
    return page.evaluate("fetch('/test/counts').then(r=>r.json())")


def wait_progress(page, handle, timeout=5000):
    try:
        page.wait_for_function("v => Number(v.dataset.cacheProgress||0) > 0", arg=handle, timeout=timeout)
    except Exception as exc:
        current = page.evaluate("v => ({state:v.dataset.persistentVideoState,progress:v.dataset.cacheProgress,src:v.getAttribute('src')})", handle)
        snapshot = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        raise AssertionError(f"wait_progress current={current} cache={snapshot} counts={counts(page)}") from exc


def wait_aborted(page, case: str, timeout=5000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        value = counts(page)["aborted"].get(case, 0)
        if value >= 1:
            return value
        time.sleep(0.05)
    raise AssertionError(f"expected aborted application response for {case}: counts={counts(page)}")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(base: str, artifact_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(base_url=base, viewport={"width": 1280, "height": 800})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_function("window.HermesPersistentVideoCache && HermesPersistentVideoCache.ready")
        page.wait_for_function("typeof _mediaPlayerHtml === 'function'")
        require(page.evaluate("d => !!HermesPersistentVideoCache.eligibleUrl('/api/media?path=x.mp4&snap='+d+'&session_id=session-a')", DIGEST), "real session-authorized snapshot media must be cache-eligible")
        require(page.evaluate("d => HermesPersistentVideoCache.eligibleUrl('/api/media?path=x.mp4&snap='+d)", DIGEST) == "", "sessionless snapshot media must stay on the native path")

        print("PHASE production-integration", flush=True)
        production_url = media_url("production", 1800)
        production = page.evaluate_handle(production_video_script(production_url))
        wait_state(page, production, "ready")
        require(page.evaluate("v => !v.dataset.cacheProgress && v.closest('.msg-media-editor').querySelector('.msg-media-cache-progress').hidden", production), "ready production player must clear and hide cache progress")
        production_counts = counts(page)
        require(production_counts["requests"].get("production") == 1, "production renderer must issue one bounded application fetch")
        require(not production_counts["native"].get("production"), "production renderer must not issue a native request before cache activation")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", production)
        production_cached = page.evaluate_handle(production_video_script(production_url))
        wait_state(page, production_cached, "ready")
        production_counts = counts(page)
        require(production_counts["requests"].get("production") == 1, "production renderer replay must use Cache Storage")
        require(not production_counts["native"].get("production"), "cached production replay must not issue a native request")
        production_fallback = page.evaluate_handle(production_video_script(media_url("live-fallback-production", 1800)))
        wait_state(page, production_fallback, "fallback")
        page.wait_for_function("() => fetch('/test/counts').then(r=>r.json()).then(c => (c.native['live-fallback-production']||0)>0)")
        production_counts = counts(page)
        require(any(value.startswith("bytes=") for value in production_counts["ranges"].get("live-fallback-production", [])), "native fallback must preserve a Range request")
        page.evaluate("([cached,fallback]) => { cached.closest('.msg-media-editor').remove(); fallback.closest('.msg-media-editor').remove(); }", [production_cached, production_fallback])
        production_offscreen = page.evaluate_handle(production_video_script(media_url("production-offscreen", 1800), offscreen=True))
        page.wait_for_timeout(250)
        page.evaluate("v => v.closest('.msg-media-editor').remove()", production_offscreen)
        offscreen_counts = counts(page)
        require(not offscreen_counts["requests"].get("production-offscreen") and not offscreen_counts["native"].get("production-offscreen"), "off-screen production history must not download")

        print("PHASE bfcache-error-recovery", flush=True)
        page.evaluate("""() => {
          const original=URL.revokeObjectURL.bind(URL);
          window.__lifecycleRevoked=[];
          URL.revokeObjectURL=(url)=>{window.__lifecycleRevoked.push(url);original(url);};
        }""")
        bfcache_url = media_url("bfcache", 1800)
        bfcache_video = page.evaluate_handle(production_video_script(bfcache_url))
        wait_state(page, bfcache_video, "ready")
        old_bfcache_blob = page.evaluate("v => v.dataset.cacheBlobUrl", bfcache_video)
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
        page.wait_for_function("() => { const s=HermesPersistentVideoCache.debugSnapshot(); return s.tasks===0&&s.consumers===0; }")
        require(page.evaluate("url => window.__lifecycleRevoked.includes(url)", old_bfcache_blob), "pagehide must revoke the old Blob URL")
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        page.evaluate("v => v.scrollIntoView({block:'center'})", bfcache_video)
        wait_state(page, bfcache_video, "ready")
        require(page.evaluate("([v,old]) => v.dataset.cacheBlobUrl && v.dataset.cacheBlobUrl !== old", [bfcache_video, old_bfcache_blob]), "pageshow must install a fresh Blob URL")
        require(counts(page)["requests"].get("bfcache") == 1, "BFCache restore must reuse Cache Storage without another media request")

        # Persistent cleanup is optional plumbing: a Cache Storage/Web Lock
        # deletion failure must not suppress the profile/workspace mutation that
        # awaits prepareAuthorityChange(). Local authority state is still torn
        # down synchronously before the failing persistent delete.
        page.evaluate("""async () => {
          const proto=Object.getPrototypeOf(caches);
          const original=proto.delete;
          proto.delete=()=>Promise.reject(new DOMException('synthetic cleanup failure','UnknownError'));
          window.__authorityMutationSent=false;
          try{
            await HermesPersistentVideoCache.prepareAuthorityChange();
            window.__authorityMutationSent=true;
          }finally{
            proto.delete=original;
          }
        }""")
        require(page.evaluate("window.__authorityMutationSent") is True, "optional cache cleanup failure must not block authority mutation")
        require(page.evaluate("() => { const s=HermesPersistentVideoCache.debugSnapshot(); return s.scope===''&&s.tasks===0&&s.consumers===0; }"), "failed persistent cleanup must still invalidate in-memory authority state")

        playback_error = page.evaluate_handle(production_video_script(media_url("blob-error", 1800)))
        wait_state(page, playback_error, "ready")
        error_blob = page.evaluate("v => v.dataset.cacheBlobUrl", playback_error)
        page.evaluate("v => v.dispatchEvent(new Event('error'))", playback_error)
        wait_state(page, playback_error, "fallback")
        require(page.evaluate("([v,old]) => !v.src.startsWith('blob:') && window.__lifecycleRevoked.includes(old)", [playback_error, error_blob]), "Blob playback error must revoke and fall back to the native URL")
        page.evaluate("document.getElementById('host').replaceChildren()")
        page.wait_for_function("() => HermesPersistentVideoCache.debugSnapshot().consumers===0")

        print("PHASE first-cache-reload", flush=True)
        # First fetch, DOM teardown, second Cache Storage hit, and hard-reload reuse.
        url = media_url("first", 1800)
        v1 = page.evaluate_handle(video_script(url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", v1)
        wait_state(page, v1, "ready")
        require(counts(page)["requests"].get("first") == 1, "first player must issue one request")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", v1)
        page.wait_for_function("HermesPersistentVideoCache.debugSnapshot().consumers === 0")
        v2 = page.evaluate_handle(video_script(url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", v2)
        wait_state(page, v2, "ready")
        require(counts(page)["requests"].get("first") == 1, "second player must use Cache Storage")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("window.HermesPersistentVideoCache && HermesPersistentVideoCache.ready")
        v3 = page.evaluate_handle(video_script(url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", v3)
        wait_state(page, v3, "ready")
        require(counts(page)["requests"].get("first") == 1, "reload must reuse the same authority-scoped entry")

        # A server-side authority rotation is detected even without an explicit
        # profile/logout hook; old cached bytes are not reused after the server
        # would deny their former session.
        page.evaluate("fetch('/test/scope?value=scope-rotated')")
        rotated = page.evaluate_handle(video_script(url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", rotated)
        wait_state(page, rotated, "ready")
        require(counts(page)["requests"].get("first") == 2, "authority rotation must force a new authorized media fetch")
        require(page.evaluate("HermesPersistentVideoCache.debugSnapshot().scope") == "scope-rotated-session-a", "authority scope must refresh before cache read")

        print("PHASE concurrent", flush=True)
        # A late consumer joins the same in-flight task, immediately inherits
        # current progress, and releasing one must not abort the other.
        slow = media_url("slow-shared", 2400)
        a = page.evaluate_handle(video_script(slow, offscreen=True))
        b = page.evaluate_handle(video_script(slow, offscreen=True))
        page.evaluate("a => HermesPersistentVideoCache.detach(a)", a)
        page.evaluate("b => HermesPersistentVideoCache.detach(b)", b)
        page.evaluate("a => { HermesPersistentVideoCache.observe(a); a.dispatchEvent(new Event('play')); }", a)
        wait_progress(page, a)
        page.evaluate("b => { HermesPersistentVideoCache.observe(b); b.dispatchEvent(new Event('play')); b.closest('.msg-media-editor').style.marginTop='0'; }", b)
        wait_progress(page, b)
        page.evaluate("a => a.closest('.msg-media-editor').remove()", a)
        wait_state(page, b, "ready", timeout=10000)
        c = counts(page)
        require(c["requests"].get("slow-shared") == 1, "concurrent consumers must share one request")
        require(not c["aborted"].get("slow-shared"), "one consumer removal must not abort a shared fetch")

        # Scope validation deduplicates only identical session+path requests.
        # Different paths in the same session must each reach the server before
        # either cached body can be used.
        scope_left_url = media_url("slow-scope-left", 1800)
        scope_right_url = media_url("slow-scope-right", 1800)
        scope_left = page.evaluate_handle(video_script(scope_left_url, offscreen=True, activate=True))
        scope_right = page.evaluate_handle(video_script(scope_right_url, offscreen=True, activate=True))
        wait_state(page, scope_left, "ready", timeout=10000)
        wait_state(page, scope_right, "ready", timeout=10000)
        c = counts(page)
        require(c["scope_requests"].get("/tmp/slow-scope-left.mp4") == 1, "left path must receive its own scope validation")
        require(c["scope_requests"].get("/tmp/slow-scope-right.mp4") == 1, "right path must receive its own scope validation")

        print("PHASE replacement-pagehide", flush=True)
        # Replacing a downloading player releases the old consumer, aborts its
        # now-unowned task, and lets the replacement own a fresh lifecycle.
        replacing = page.evaluate_handle(video_script(media_url("slow-replaced", 3500), offscreen=True, activate=True))
        wait_progress(page, replacing)
        replacement = page.evaluate_handle("""([oldVideo,url]) => {
          const wrap=document.createElement('div');
          wrap.className='msg-media-editor';
          wrap.style.marginTop='3000px';
          wrap.innerHTML=`<video class="msg-media-video" src="${url}" preload="none"></video><div class="msg-media-meta"><span class="msg-media-name">replacement.mp4</span><span class="msg-media-cache-progress" hidden></span></div>`;
          oldVideo.closest('.msg-media-editor').replaceWith(wrap);
          return wrap.querySelector('video');
        }""", [replacing, media_url("replacement", 1800)])
        page.wait_for_function("v => v.dataset.persistentVideoState === 'observed'", arg=replacement)
        page.evaluate("v => v.dispatchEvent(new Event('play'))", replacement)
        wait_state(page, replacement, "ready")
        page.wait_for_function("() => HermesPersistentVideoCache.debugSnapshot().tasks === 0")
        deadline=time.time()+3
        while time.time()<deadline and not counts(page)["aborted"].get("slow-replaced",0):
            time.sleep(0.1)
        require(counts(page)["aborted"].get("slow-replaced",0) >= 1, "DOM replacement must abort the unowned fetch")

        # pagehide must release every consumer and task without receiving the
        # PageTransitionEvent as the internal preserve-set argument.
        pagehide = page.evaluate_handle(video_script(media_url("slow-pagehide", 3500), offscreen=True, activate=True))
        wait_progress(page, pagehide)
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        page.wait_for_function("() => { const s=HermesPersistentVideoCache.debugSnapshot(); return s.tasks===0&&s.consumers===0; }")
        deadline=time.time()+3
        while time.time()<deadline and not counts(page)["aborted"].get("slow-pagehide",0):
            time.sleep(0.1)
        require(counts(page)["aborted"].get("slow-pagehide",0) >= 1, "pagehide must abort the final active fetch")

        print("PHASE abort", flush=True)
        # Final consumer removal aborts the network task and releases registry state.
        aborting = page.evaluate_handle(video_script(media_url("slow-abort", 3500), offscreen=True, activate=True))
        wait_progress(page, aborting)
        page.evaluate("v => v.closest('.msg-media-editor').remove()", aborting)
        page.wait_for_function("HermesPersistentVideoCache.debugSnapshot().tasks === 0")
        deadline=time.time()+3
        aborted=0
        while time.time()<deadline:
            aborted=counts(page)["aborted"].get("slow-abort",0)
            if aborted>=1:
                break
            time.sleep(0.1)
        require(aborted >= 1, f"final consumer teardown must abort the response: aborted={aborted}")

        print("PHASE source-replacement", flush=True)
        # Reusing a connected <video> with a new native src must not keep the
        # old data-media-source or its Blob URL.
        reused = page.evaluate_handle(video_script(media_url("reuse-old", 1800), offscreen=True, activate=True))
        wait_state(page, reused, "ready")
        old_blob = page.evaluate("v => v.dataset.cacheBlobUrl", reused)
        page.evaluate("([v,url]) => { v.src=url; }", [reused, media_url("reuse-new", 1800)])
        page.wait_for_function("v => (v.dataset.mediaSource||'').includes('reuse-new')", arg=reused)
        page.evaluate("v => { v.closest('.msg-media-editor').style.marginTop='0'; v.scrollIntoView({block:'center'}); }", reused)
        wait_state(page, reused, "ready")
        require(counts(page)["requests"].get("reuse-new") == 1, "same-node src replacement must fetch the new snapshot exactly once")
        require(page.evaluate("([v,old]) => v.dataset.cacheBlobUrl !== old", [reused, old_blob]), "same-node replacement must own a new Blob URL")

        print("PHASE unknown-oversize", flush=True)
        # Unknown length is counted while streaming; oversize never enters cache.
        unknown_case = "slow-unknown-oversize"
        unknown = page.evaluate_handle(video_script(media_url(unknown_case, 5000), offscreen=True, activate=True))
        wait_state(page, unknown, "fallback")
        wait_aborted(page, unknown_case, timeout=10000)
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(all(unknown_case not in key for key in snap["entries"]), "unknown oversize response must not be cached")
        declared = page.evaluate_handle(video_script(media_url("declared-oversize", 5000)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", declared)
        wait_state(page, declared, "fallback")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(all("declared-oversize" not in key for key in snap["entries"]), "declared oversize response must not be cached")
        live = page.evaluate_handle(video_script(media_url("live-fallback", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", live)
        wait_state(page, live, "fallback")
        live2 = page.evaluate_handle(video_script(media_url("live-fallback", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", live2)
        wait_state(page, live2, "fallback")
        require(counts(page)["requests"].get("live-fallback") == 2, "unattested live fallback bytes must never enter persistent cache")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["tasks"] == 0, f"error/fallback must not retain a task: {snap}")

        print("PHASE header-rejection-abort", flush=True)
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
        rejection_cases = (
            "slow-reject-http",
            "slow-reject-unattested",
            "slow-reject-wrong-mime",
            "slow-reject-invalid-length",
            "slow-reject-declared-oversize",
            "slow-reject-no-stream",
        )
        for case in rejection_cases:
            page.evaluate("HermesPersistentVideoCache.clearAll()")
            if case == "slow-reject-invalid-length":
                page.evaluate("""() => {
                  const original=window.fetch.bind(window);
                  window.__restoreRejectFetch=()=>{window.fetch=original;};
                  window.fetch=async (...args)=>{
                    const response=await original(...args);
                    const url=String(args[0]&&args[0].url||args[0]||'');
                    if(!url.includes('slow-reject-invalid-length')) return response;
                    const headers=new Headers(response.headers);
                    headers.set('Content-Length','invalid');
                    return new Response(response.body,{status:response.status,statusText:response.statusText,headers});
                  };
                }""")
            if case == "slow-reject-no-stream":
                page.evaluate("window.__savedTransformStream=window.TransformStream; window.TransformStream=undefined")
            size = 5000 if case == "slow-reject-declared-oversize" else 3500
            rejected = page.evaluate_handle(video_script(media_url(case, size), offscreen=True, activate=True))
            wait_state(page, rejected, "fallback", timeout=10000)
            wait_aborted(page, case)
            snapshot = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
            require(snapshot["tasks"] == 0 and snapshot["consumers"] == 0, f"rejected response leaked registry state: {case} {snapshot}")
            require(all(case not in key for key in snapshot["entries"]), f"rejected response entered Cache Storage: {case} {snapshot}")
            page.evaluate("v => v.closest('.msg-media-editor').remove()", rejected)
            if case == "slow-reject-invalid-length":
                page.evaluate("window.__restoreRejectFetch()")
            if case == "slow-reject-no-stream":
                page.evaluate("window.TransformStream=window.__savedTransformStream; delete window.__savedTransformStream")

        print("PHASE quota-lru", flush=True)
        # Global quota is byte-based LRU, not an item count.
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        for case in ("lru-a", "lru-b", "lru-c"):
            v = page.evaluate_handle(video_script(media_url(case, 2700)))
            page.evaluate("v => v.dispatchEvent(new Event('play'))", v)
            wait_state(page, v, "ready")
            page.evaluate("v => v.closest('.msg-media-editor').remove()", v)
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["totalBytes"] <= 5000, f"global byte quota must be enforced: {snap}")
        require(len(snap["entries"]) == 1 and "lru-c" in snap["entries"][0], "LRU must retain the newest fitting entry")

        # A real QuotaExceededError evicts LRU data and retries exactly once.
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        old = page.evaluate_handle(video_script(media_url("quota-old", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", old)
        wait_state(page, old, "ready")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", old)
        page.evaluate("""async () => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v1-'+s.scope);
          const proto=Object.getPrototypeOf(cache);
          const original=proto.put;
          let thrown=false;
          proto.put=function(request,response){
            const key=String(request&&request.url||request);
            if(!thrown&&!key.includes('__hermes_snapshot_video_cache_meta__')){
              thrown=true;
              return Promise.reject(new DOMException('synthetic quota','QuotaExceededError'));
            }
            return original.call(this,request,response);
          };
          window.__restoreCachePut=()=>{proto.put=original;};
        }""")
        fresh = page.evaluate_handle(video_script(media_url("quota-new", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", fresh)
        wait_state(page, fresh, "ready")
        page.evaluate("window.__restoreCachePut()")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(len(snap["entries"]) == 1 and "quota-new" in snap["entries"][0], f"quota retry must retain only the new entry: {snap}")

        # Crash reconciliation repairs an orphan body plus dangling metadata.
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        prime = page.evaluate_handle(video_script(media_url("crash-prime", 1800), offscreen=True, activate=True))
        wait_state(page, prime, "ready")
        orphan_url = media_url("crash-orphan", 1800)
        page.evaluate("""async (url) => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v1-'+s.scope);
          for(const request of await cache.keys()) await cache.delete(request);
          const response=await fetch(url);
          await cache.put(url,response);
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries:{[location.origin+'/dangling']:{size:99,at:1}}}),{headers:{'Content-Type':'application/json'}}));
        }""", orphan_url)
        orphan = page.evaluate_handle(video_script(orphan_url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", orphan)
        wait_state(page, orphan, "ready")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(len(snap["entries"]) == 1 and "crash-orphan" in snap["entries"][0], f"crash reconciliation must match actual cache bodies: {snap}")
        require(not counts(page)["requests"].get("crash-orphan"), "reconciled orphan body must be a cache hit")

        # A crash can leave an existing metadata row with the previous body's
        # smaller size. Reconciliation must re-read Content-Length for every
        # body, not only metadata-less orphans, before enforcing global quota.
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        stale_prime = page.evaluate_handle(video_script(media_url("stale-prime", 1800), offscreen=True, activate=True))
        wait_state(page, stale_prime, "ready")
        stale_a = media_url("stale-size-a", 3000)
        stale_b = media_url("stale-size-b", 3000)
        page.evaluate("""async ([a,b]) => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v1-'+s.scope);
          for(const request of await cache.keys()) await cache.delete(request);
          const [ra,rb]=await Promise.all([fetch(a),fetch(b)]);
          await cache.put(a,ra);
          await cache.put(b,rb);
          const entries={};
          entries[new URL(a,location.href).href]={size:1,at:1};
          entries[new URL(b,location.href).href]={size:1,at:2};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
        }""", [stale_a, stale_b])
        stale = page.evaluate_handle(video_script(stale_b, offscreen=True, activate=True))
        wait_state(page, stale, "ready")
        actual = page.evaluate("""async () => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v1-'+s.scope);
          const bodies=(await cache.keys()).map(r=>r.url).filter(k=>!k.includes('__hermes_snapshot_video_cache_meta__'));
          return {bodies,snapshot:s};
        }""")
        require(actual["snapshot"]["totalBytes"] <= 5000, f"stale metadata size bypassed global quota: {actual}")
        require(len(actual["bodies"]) == 1 and "stale-size-b" in actual["bodies"][0], f"stale-size reconciliation must evict the older body: {actual}")

        print("PHASE authority", flush=True)
        # Authority transition clears old bytes before new-scope reads.
        page.evaluate("fetch('/test/scope?value=scope-b').then(() => HermesPersistentVideoCache.authorityChanged())")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["scope"] == "" and snap["entries"] == [], "authority change must clear old-scope entries before another read")
        authority_probe = page.evaluate_handle(video_script(media_url("authority-probe", 1800), offscreen=True, activate=True))
        wait_state(page, authority_probe, "ready")
        require(page.evaluate("HermesPersistentVideoCache.debugSnapshot().scope") == "scope-b-session-a", "next read must enter the new session-authority scope")
        page.evaluate("caches.open('hermes-snapshot-video-v0-stale').then(c => c.put('/stale',new Response('old')))")
        page.evaluate("HermesPersistentVideoCache.authorityChanged()")
        cache_names = page.evaluate("caches.keys()")
        require("hermes-snapshot-video-v0-stale" not in cache_names, "schema change must delete old cache versions")

        print("PHASE observer", flush=True)
        # Removed-before-intersection nodes are unobserved and never fetched.
        off = page.evaluate_handle(video_script(media_url("offscreen", 1800), offscreen=True))
        page.evaluate("v => v.closest('.msg-media-editor').remove()", off)
        page.wait_for_timeout(250)
        require(not counts(page)["requests"].get("offscreen"), "removed off-screen history must not download")
        observed = page.evaluate_handle(video_script(media_url("observer-once", 1800)))
        page.evaluate("v => v.scrollIntoView({block:'center'})", observed)
        wait_state(page, observed, "ready")
        page.evaluate("v => HermesPersistentVideoCache.observe(v)", observed)
        page.wait_for_timeout(150)
        require(counts(page)["requests"].get("observer-once") == 1, "intersection must activate an eligible video only once")

        print("PHASE cross-tab-quota", flush=True)
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        peer = context.new_page()
        peer.goto("/", wait_until="domcontentloaded")
        peer.wait_for_function("window.HermesPersistentVideoCache && HermesPersistentVideoCache.ready")
        left = page.evaluate_handle(video_script(media_url("tab-left", 3000), offscreen=True))
        right = peer.evaluate_handle(video_script(media_url("tab-right", 3000), offscreen=True))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", left)
        peer.evaluate("v => v.dispatchEvent(new Event('play'))", right)
        wait_state(page, left, "ready")
        wait_state(peer, right, "ready")
        actual = page.evaluate("""async () => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v1-'+s.scope);
          const keys=(await cache.keys()).map(r=>r.url);
          const meta=await cache.match(location.origin+'/__hermes_snapshot_video_cache_meta__').then(r=>r.json());
          const bodies=keys.filter(k=>!k.includes('__hermes_snapshot_video_cache_meta__'));
          return {bodies,meta,total:Object.values(meta.entries).reduce((n,v)=>n+v.size,0)};
        }""")
        require(actual["total"] <= 5000, f"cross-tab global quota exceeded: {actual}")
        require(sorted(actual["bodies"]) == sorted(actual["meta"]["entries"].keys()), f"cross-tab metadata/body mismatch: {actual}")
        require(len(actual["bodies"]) == 1, f"cross-tab LRU must evict one 3000-byte body: {actual}")
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        peer.wait_for_function("HermesPersistentVideoCache.debugSnapshot().scope === '' && HermesPersistentVideoCache.debugSnapshot().consumers === 0")
        remaining = page.evaluate("caches.keys().then(keys => keys.filter(k => k.startsWith('hermes-snapshot-video-v')))")
        require(remaining == [], f"authority clear must remove every tab's persistent cache: {remaining}")

        # Model the production two-phase transition: a pre-clear happens before
        # the server mutation, another tab starts old-scope work in that gap,
        # then post-switch refresh must broadcast a final abort/clear.
        page.evaluate("fetch('/test/scope?value=scope-race-old').then(() => HermesPersistentVideoCache.authorityChanged())")
        peer.evaluate("HermesPersistentVideoCache.refreshAuthority()")
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        peer.wait_for_function("HermesPersistentVideoCache.debugSnapshot().scope === ''")
        raced = peer.evaluate_handle(video_script(media_url("slow-authority-race", 3500), offscreen=True, activate=True))
        wait_progress(peer, raced)
        page.evaluate("fetch('/test/scope?value=scope-race-new')")
        page.evaluate("HermesPersistentVideoCache.refreshAuthority()")
        peer.wait_for_function("() => { const s=HermesPersistentVideoCache.debugSnapshot(); return s.scope===''&&s.tasks===0&&s.consumers===0; }")
        deadline=time.time()+3
        while time.time()<deadline and not counts(page)["aborted"].get("slow-authority-race",0):
            time.sleep(0.1)
        require(counts(page)["aborted"].get("slow-authority-race",0) >= 1, "post-switch refresh must abort cross-tab work started after pre-clear")
        stale_names = page.evaluate("caches.keys().then(keys => keys.filter(k => k.includes('scope-race-old')))")
        require(stale_names == [], f"old-authority cache survived final transition: {stale_names}")
        peer.close()

        print("PHASE object-url-cleanup", flush=True)
        page.evaluate("""() => { const original=URL.revokeObjectURL.bind(URL); window.__revoked=[]; URL.revokeObjectURL=(url)=>{window.__revoked.push(url); original(url);}; }""")
        cleanup = page.evaluate_handle(video_script(media_url("cleanup", 1800), offscreen=True))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", cleanup)
        wait_state(page, cleanup, "ready")
        blob_url = page.evaluate("v => v.dataset.cacheBlobUrl", cleanup)
        page.evaluate("v => v.closest('.msg-media-editor').remove()", cleanup)
        page.wait_for_function("url => window.__revoked.includes(url)", arg=blob_url)
        require(page.evaluate("url => fetch(url).then(()=>false,()=>true)", blob_url), "revoked object URL must no longer be readable")

        print("PHASE visual", flush=True)
        # Visible progress evidence using production markup/CSS at desktop and narrow widths.
        visual = page.evaluate_handle(production_video_script(media_url("slow-visual", 3500)))
        page.evaluate("v => v.scrollIntoView({block:'center'})", visual)
        wait_progress(page, visual)
        page.locator('.msg-media-editor').last.screenshot(path=str(artifact_dir / "persistent-video-cache-desktop.png"))
        mobile = browser.new_context(base_url=base, viewport={"width": 390, "height": 844}, is_mobile=True)
        mpage = mobile.new_page()
        mpage.goto("/", wait_until="domcontentloaded")
        mpage.wait_for_function("window.HermesPersistentVideoCache && HermesPersistentVideoCache.ready")
        mpage.wait_for_function("typeof _mediaPlayerHtml === 'function'")
        mv = mpage.evaluate_handle(production_video_script(media_url("slow-mobile", 3500)))
        mpage.evaluate("v => v.scrollIntoView({block:'center'})", mv)
        wait_progress(mpage, mv)
        mpage.locator('.msg-media-editor').last.screenshot(path=str(artifact_dir / "persistent-video-cache-mobile.png"))
        mobile.close()

        print("PHASE unavailable", flush=True)
        # CacheStorage-unavailable fallback does not start the application fetch.
        no_cache = browser.new_context(base_url=base)
        npage = no_cache.new_page()
        npage.goto("/?nocache=1", wait_until="domcontentloaded")
        npage.wait_for_function("window.HermesPersistentVideoCache && HermesPersistentVideoCache.ready")
        nv = npage.evaluate_handle(video_script(media_url("no-cache-storage", 1800)))
        npage.evaluate("v => v.dispatchEvent(new Event('play'))", nv)
        wait_state(npage, nv, "fallback")
        require(not counts(npage)["requests"].get("no-cache-storage"), "CacheStorage-unavailable path must fall back before app fetch")
        no_cache.close()

        require(not errors, f"uncaught browser errors: {errors}")
        context.close()
        browser.close()


def main() -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2
    State.reset()
    artifact_dir = Path(os.getenv("VIDEO_CACHE_ARTIFACT_DIR") or tempfile.mkdtemp(prefix="video-cache-evidence-"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server = FixtureServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        run(base, artifact_dir)
        print(f"PASS persistent video cache Chromium behavior; artifacts={artifact_dir}")
        return 0
    except Exception as exc:
        traceback.print_exc()
        print(f"FAIL persistent video cache Chromium behavior: {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
