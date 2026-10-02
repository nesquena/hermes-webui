#!/usr/bin/env python3
"""Real-Chromium behavior gate for the persistent snapshot-video cache.

The fixture serves the production ``static/media-cache.js`` unchanged and uses
real Cache Storage, ReadableStream, MutationObserver, IntersectionObserver,
AbortController, Blob URLs, reloads, and network requests. No provider or agent
credentials are used.
"""
from __future__ import annotations

import json
import hashlib
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
MP4 = (ROOT / "tests" / "fixtures" / "persistent_video_cache.mp4").read_bytes()
DIGEST = hashlib.sha256(MP4).hexdigest()


class State:
    lock = threading.Lock()
    requests: Counter[str] = Counter()
    scope_requests: Counter[str] = Counter()
    aborted: Counter[str] = Counter()
    native: Counter[str] = Counter()
    ranges: dict[str, list[str]] = {}
    authority = "scope-a"
    retarget_mode = "first"
    scope_owner_a_started = threading.Event()
    scope_owner_a_release = threading.Event()
    scope_owner_b_started = threading.Event()
    scope_owner_b_release = threading.Event()

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.requests.clear()
            cls.scope_requests.clear()
            cls.aborted.clear()
            cls.native.clear()
            cls.ranges.clear()
            cls.authority = "scope-a"
            cls.retarget_mode = "first"
            cls.scope_owner_a_started.clear()
            cls.scope_owner_a_release.clear()
            cls.scope_owner_b_started.clear()
            cls.scope_owner_b_release.clear()


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
            prefix = "window.__HERMES_VIDEO_CACHE_TEST__={perFileBytes:4096,totalBytes:5000,cacheOpTimeoutMs:100,scopeRequestFinalized:[],forceCacheUnavailable:true};" if no_cache else "window.__HERMES_VIDEO_CACHE_TEST__={perFileBytes:4096,totalBytes:5000,cacheOpTimeoutMs:100,scopeRequestFinalized:[]};"
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
                retarget_mode = State.retarget_mode
            session_id = query.get("session_id", [""])[0]
            media_path = query.get("path", [""])[0]
            snap_digest = query.get("snap", [""])[0].lower()
            if (
                session_id not in {"session-a", "session-b"}
                or not media_path.endswith(".mp4")
                or len(snap_digest) != 64
            ):
                self._send(404, b'{"error":"session not found"}', "application/json")
                return
            if media_path.endswith("retarget.mp4") and retarget_mode != "first":
                self._send(404, b'{"error":"immutable binding not found"}', "application/json")
                return
            with State.lock:
                State.scope_requests[media_path] += 1
            if media_path.endswith("scope-owner-c.mp4"):
                self._send(404, b'{"error":"immutable binding not found"}', "application/json")
                return
            if media_path.endswith("scope-owner-a.mp4"):
                State.scope_owner_a_started.set()
                State.scope_owner_a_release.wait(timeout=5)
            if media_path.endswith("scope-owner-b.mp4"):
                State.scope_owner_b_started.set()
                State.scope_owner_b_release.wait(timeout=5)
            if media_path.endswith("slow-scope-left.mp4"):
                time.sleep(0.35)
            if (
                media_path.endswith("scope-race.mp4")
                and snap_digest
                == hashlib.sha256((MP4 + b"\0" * (1800 - len(MP4)))[:1800]).hexdigest()
            ):
                time.sleep(0.35)
            canonical_target = "/canonical/first.mp4" if media_path.endswith("retarget.mp4") else media_path
            resource = hashlib.sha256(
                (canonical_target + "\0" + snap_digest).encode("utf-8")
            ).hexdigest()
            self._send(
                200,
                json.dumps({"scope": scope, "resource": resource, "schema": 2}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/test/retarget":
            value = query.get("value", [""])[0]
            if value not in {"first", "denied", "mismatch"}:
                self._send(400, b"bad mode", "text/plain")
                return
            with State.lock:
                State.retarget_mode = value
            self._send(200, b"ok", "text/plain")
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
        requested_digest = query.get("snap", [""])[0].lower()
        with State.lock:
            retarget_mode = State.retarget_mode
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
        status = (
            403
            if case == "retarget" and retarget_mode == "denied"
            else (503 if case == "slow-reject-http" else (206 if partial else 200))
        )
        self.send_response(status)
        content_type = "text/plain" if case == "slow-reject-wrong-mime" else "video/mp4"
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        if (
            not case.startswith("live-fallback")
            and case != "slow-reject-unattested"
            and not (case == "retarget" and retarget_mode != "first")
        ):
            self.send_header("X-Hermes-Media-Snapshot", requested_digest)
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
    body = (MP4 + (b"\0" * max(0, size - len(MP4))))[:size]
    digest = hashlib.sha256(body).hexdigest()
    if case == "wrong-body-right-header":
        digest = hashlib.sha256(b"different-expected-bytes").hexdigest()
    return f"/api/media?path=%2Ftmp%2F{case}.mp4&inline=1&session_id={session_id}&snap={digest}&case={case}&size={size}"


def resource_fingerprint(case: str, size: int = 1800) -> str:
    parsed = urllib.parse.urlsplit(media_url(case, size))
    query = urllib.parse.parse_qs(parsed.query)
    material = query["path"][0] + "\0" + query["snap"][0]
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def same_path_media_url(case: str, size: int) -> str:
    body = (MP4 + (b"\0" * max(0, size - len(MP4))))[:size]
    digest = hashlib.sha256(body).hexdigest()
    return (
        "/api/media?path=%2Ftmp%2Fscope-race.mp4&inline=1"
        f"&session_id=session-a&snap={digest}&case={case}&size={size}"
    )


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
        page.evaluate("v => { v.muted=true; return v.play(); }", production)
        page.wait_for_function("v => v.currentTime > 0", arg=production, timeout=5000)
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
        page.evaluate("v => v.closest('.msg-media-editor').remove()", bfcache_video)
        page.wait_for_function("() => HermesPersistentVideoCache.debugSnapshot().consumers === 0")

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

        lock_ready = page.evaluate_handle(production_video_script(media_url("lock-ready", 1800)))
        wait_state(page, lock_ready, "ready")
        lock_ready_blob = page.evaluate("v => v.dataset.cacheBlobUrl", lock_ready)
        page.evaluate("""() => {
          window.__quotaLockHeld=false;
          window.__quotaLockReleased=false;
          navigator.locks.request('hermes-snapshot-video-vquota-lock',async()=>{
            window.__quotaLockHeld=true;
            await new Promise(resolve=>{window.__releaseQuotaLock=resolve;});
            window.__quotaLockHeld=false;
            window.__quotaLockReleased=true;
          });
        }""")
        page.wait_for_function("window.__quotaLockHeld === true")
        bounded_cleanup = page.evaluate("""async () => Promise.race([
          HermesPersistentVideoCache.prepareAuthorityChange().then(()=> 'done'),
          new Promise(resolve=>setTimeout(()=>resolve('timeout'),400))
        ])""")
        page.evaluate("v => v.scrollIntoView({block:'center'})", lock_ready)
        wait_state(page, lock_ready, "fallback", timeout=3000)
        require(
            page.evaluate("([v,old]) => !v.src.startsWith('blob:') && v.src.includes('lock-ready') && window.__lifecycleRevoked.includes(old)", [lock_ready, lock_ready_blob]),
            "a held quota lock must not strand a re-observed mounted player in loading",
        )
        page.evaluate("window.__releaseQuotaLock()")
        page.wait_for_function("window.__quotaLockReleased === true")
        require(bounded_cleanup == "done", "a never-granted quota lock must not gate authority mutation")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", lock_ready)

        ready_clear = page.evaluate_handle(production_video_script(media_url("ready-clear", 1800)))
        wait_state(page, ready_clear, "ready")
        ready_clear_blob = page.evaluate("v => v.dataset.cacheBlobUrl", ready_clear)
        page.evaluate("() => HermesPersistentVideoCache.clearAll(false)")
        page.evaluate("v => v.scrollIntoView({block:'center'})", ready_clear)
        wait_state(page, ready_clear, "ready", timeout=10000)
        require(page.evaluate("([v,old]) => v.src.startsWith('blob:') && v.dataset.cacheBlobUrl !== old", [ready_clear, ready_clear_blob]), "clearAll must re-observe and recover a mounted ready player")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", ready_clear)

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

        # A chat/session switch inside the same authenticated profile/workspace
        # must reauthorize the path but reuse the immutable path+snapshot bytes.
        scope_requests_before_switch = counts(page)["scope_requests"].get("/tmp/first.mp4", 0)
        session_b_url = media_url("first", 1800, session_id="session-b")
        session_b = page.evaluate_handle(video_script(session_b_url, offscreen=True, activate=True))
        wait_state(page, session_b, "ready")
        require(counts(page)["requests"].get("first") == 1, "session B must reuse session A's authorized immutable snapshot")
        session_a_again = page.evaluate_handle(video_script(url, offscreen=True, activate=True))
        wait_state(page, session_a_again, "ready")
        require(counts(page)["requests"].get("first") == 1, "A to B to A must not evict the same authority cache")
        require(counts(page)["scope_requests"].get("/tmp/first.mp4", 0) == scope_requests_before_switch + 2, "both B and the return to A must independently reauthorize the exact path")

        # A server-side authority rotation is detected even without an explicit
        # profile/logout hook; old cached bytes are not reused after the server
        # would deny their former session.
        page.evaluate("fetch('/test/scope?value=scope-rotated')")
        rotated = page.evaluate_handle(video_script(url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", rotated)
        wait_state(page, rotated, "ready")
        require(counts(page)["requests"].get("first") == 2, "authority rotation must force a new authorized media fetch")
        require(page.evaluate("HermesPersistentVideoCache.debugSnapshot().scope") == "scope-rotated", "authority scope must refresh before cache read")

        print("PHASE persistent-hit-integrity", flush=True)
        page.evaluate("() => { document.getElementById('host').replaceChildren(); return HermesPersistentVideoCache.clearAll(false); }")
        cache_integrity_url = media_url("cache-hit-integrity", 1800)
        page.evaluate("""async url => {
          const source=new URL(url,location.href);
          const endpoint=new URL('/api/media-cache/scope',location.origin);
          for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
          const authorization=await fetch(endpoint).then(r=>r.json());
          const key=location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          const cache=await caches.open('hermes-snapshot-video-v2-'+authorization.scope);
          const response=await fetch(url);
          const bytes=new Uint8Array(await response.arrayBuffer());
          bytes[bytes.length-1]^=1;
          await cache.put(key,new Response(bytes,{headers:{'Content-Type':'video/mp4','Content-Length':String(bytes.byteLength)}}));
          const entries={}; entries[key]={size:bytes.byteLength,at:Date.now()};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
        }""", cache_integrity_url)
        cache_integrity = page.evaluate_handle(video_script(cache_integrity_url, offscreen=True, activate=True))
        wait_state(page, cache_integrity, "ready", timeout=10000)
        require(counts(page)["requests"].get("cache-hit-integrity") == 1, "a digest-mismatched persistent hit must be evicted and refetched")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", cache_integrity)

        cache_mime_url = media_url("cache-hit-wrong-mime", 1800)
        page.evaluate("""async url => {
          const source=new URL(url,location.href);
          const endpoint=new URL('/api/media-cache/scope',location.origin);
          for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
          const authorization=await fetch(endpoint).then(r=>r.json());
          const key=location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          const cache=await caches.open('hermes-snapshot-video-v2-'+authorization.scope);
          const bytes=await fetch(url).then(r=>r.arrayBuffer());
          await cache.put(key,new Response(bytes,{headers:{'Content-Type':'text/plain','Content-Length':String(bytes.byteLength)}}));
          const entries={}; entries[key]={size:bytes.byteLength,at:Date.now()};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
        }""", cache_mime_url)
        cache_mime = page.evaluate_handle(video_script(cache_mime_url, offscreen=True, activate=True))
        wait_state(page, cache_mime, "ready", timeout=10000)
        require(counts(page)["requests"].get("cache-hit-wrong-mime") == 1, "a non-video persistent hit must be evicted and refetched even when its bytes and digest are valid")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", cache_mime)

        cache_size_url = media_url("cache-hit-size-mismatch", 1800)
        page.evaluate("""async url => {
          const source=new URL(url,location.href);
          const endpoint=new URL('/api/media-cache/scope',location.origin);
          for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
          const authorization=await fetch(endpoint).then(r=>r.json());
          const key=location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          const cache=await caches.open('hermes-snapshot-video-v2-'+authorization.scope);
          const bytes=await fetch(url).then(r=>r.arrayBuffer());
          const declared=bytes.byteLength-1;
          await cache.put(key,new Response(bytes,{headers:{'Content-Type':'video/mp4','Content-Length':String(declared)}}));
          const entries={}; entries[key]={size:declared,at:Date.now()};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
        }""", cache_size_url)
        cache_size = page.evaluate_handle(video_script(cache_size_url, offscreen=True, activate=True))
        wait_state(page, cache_size, "ready", timeout=10000)
        require(counts(page)["requests"].get("cache-hit-size-mismatch") == 1, "an in-cap persistent hit whose actual size differs from its declaration must be evicted and refetched")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", cache_size)

        cache_oversize_url = media_url("cache-hit-oversize", 4200)
        page.evaluate("""async url => {
          const source=new URL(url,location.href);
          const endpoint=new URL('/api/media-cache/scope',location.origin);
          for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
          const authorization=await fetch(endpoint).then(r=>r.json());
          const key=location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          const cache=await caches.open('hermes-snapshot-video-v2-'+authorization.scope);
          const response=await fetch(url);
          const bytes=new Uint8Array(await response.arrayBuffer());
          const declared=bytes.byteLength-200;
          await cache.put(key,new Response(bytes,{headers:{'Content-Type':'video/mp4','Content-Length':String(declared)}}));
          const entries={}; entries[key]={size:declared,at:Date.now()};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
        }""", cache_oversize_url)
        cache_oversize = page.evaluate_handle(video_script(cache_oversize_url, offscreen=True, activate=True))
        wait_state(page, cache_oversize, "fallback", timeout=10000)
        require(counts(page)["requests"].get("cache-hit-oversize") == 1, "an over-cap persistent hit must be evicted instead of becoming ready")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", cache_oversize)

        cache_stream_url = media_url("cache-hit-stream-cap", 1800)
        page.evaluate("""async url => {
          const source=new URL(url,location.href);
          const endpoint=new URL('/api/media-cache/scope',location.origin);
          for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
          const authorization=await fetch(endpoint).then(r=>r.json());
          const key=location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          const cache=await caches.open('hermes-snapshot-video-v2-'+authorization.scope);
          const bytes=await fetch(url).then(r=>r.arrayBuffer());
          await cache.put(key,new Response(bytes,{headers:{'Content-Type':'video/mp4','Content-Length':'4096'}}));
          const entries={}; entries[key]={size:4096,at:Date.now()};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
          const proto=Object.getPrototypeOf(cache);
          const original=proto.match;
          window.__boundedCachePulls=0;
          window.__boundedCacheCancelled=false;
          window.__restoreBoundedCacheMatch=()=>{proto.match=original;};
          proto.match=function(request){
            const requested=String(request&&request.url||request);
            if(requested!==key) return original.call(this,request);
            let emitted=0;
            const body=new ReadableStream({
              pull(controller){
                emitted++;
                window.__boundedCachePulls++;
                if(emitted>100){controller.close();return;}
                controller.enqueue(new Uint8Array(1024));
              },
              cancel(){window.__boundedCacheCancelled=true;},
            },{highWaterMark:0});
            return Promise.resolve(new Response(body,{headers:{'Content-Type':'video/mp4','Content-Length':'4096'}}));
          };
        }""", cache_stream_url)
        cache_stream = page.evaluate_handle(video_script(cache_stream_url, offscreen=True, activate=True))
        wait_state(page, cache_stream, "ready", timeout=10000)
        stream_cap = page.evaluate("() => ({pulls:window.__boundedCachePulls,cancelled:window.__boundedCacheCancelled})")
        page.evaluate("window.__restoreBoundedCacheMatch()")
        require(stream_cap["cancelled"] and stream_cap["pulls"] < 20, f"cached body must stop near the byte cap instead of being fully materialized: {stream_cap}")
        require(counts(page)["requests"].get("cache-hit-stream-cap") == 1, "an over-cap cached stream must be evicted and refetched")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", cache_stream)

        page.evaluate("""() => {
          const proto=Object.getPrototypeOf(caches);
          const originalOpen=proto.open;
          let wedged=false;
          window.__cacheQueueWedgeStarted=false;
          window.__restoreCacheQueueWedge=()=>{proto.open=originalOpen;};
          proto.open=async function(...args){
            const cache=await originalOpen.apply(this,args);
            if(wedged) return cache;
            wedged=true;
            const cacheProto=Object.getPrototypeOf(cache);
            const originalMatch=cacheProto.match;
            cacheProto.match=function(request){
              if(window.__cacheQueueWedgeStarted) return originalMatch.call(this,request);
              window.__cacheQueueWedgeStarted=true;
              return new Promise(resolve=>{window.__releaseCacheQueueWedge=()=>resolve(originalMatch.call(this,request));});
            };
            window.__restoreCacheQueueWedge=()=>{cacheProto.match=originalMatch;proto.open=originalOpen;};
            return cache;
          };
        }""")
        queue_wedge = page.evaluate_handle(video_script(media_url("cache-queue-wedge", 1800), offscreen=True, activate=True))
        page.wait_for_function("window.__cacheQueueWedgeStarted === true")
        queue_successors = [
            page.evaluate_handle(video_script(media_url(f"cache-queue-successor-{index}", 1800), offscreen=True, activate=True))
            for index in range(6)
        ]
        queue_wait_started = time.monotonic()
        wait_state(page, queue_successors[-1], "fallback", timeout=3000)
        queue_elapsed = time.monotonic() - queue_wait_started
        require(queue_elapsed < 0.45, f"queued cache operations must share enqueue-time deadlines, elapsed={queue_elapsed:.3f}s")
        require(
            all(
                page.evaluate(
                    "([v,index]) => v.src.includes(`cache-queue-successor-${index}`) && !v.src.startsWith('blob:')",
                    [video, index],
                )
                for index, video in enumerate(queue_successors)
            ),
            "every successor queued behind a permanently pending cache callback must reach native fallback by its own enqueue-time deadline",
        )
        page.evaluate("window.__releaseCacheQueueWedge(); window.__restoreCacheQueueWedge()")
        for video in [queue_wedge, *queue_successors]:
            page.evaluate("v => v.closest('.msg-media-editor').remove()", video)

        print("PHASE canonical-retarget", flush=True)
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
        page.evaluate("() => fetch('/test/retarget?value=first').then(r=>r.text())")
        retarget_url = media_url("retarget", 1800)
        retarget_first = page.evaluate_handle(
            video_script(retarget_url, offscreen=True, activate=True)
        )
        wait_state(page, retarget_first, "ready")
        require(
            counts(page)["requests"].get("retarget") == 1,
            "initial canonical target A must populate one persistent entry",
        )
        page.evaluate("v => v.closest('.msg-media-editor').remove()", retarget_first)

        page.evaluate("() => fetch('/test/retarget?value=denied').then(r=>r.text())")
        retarget_denied = page.evaluate_handle(
            video_script(retarget_url, offscreen=True, activate=True)
        )
        wait_state(page, retarget_denied, "fallback")
        require(
            not page.evaluate("v => v.src.startsWith('blob:')", retarget_denied),
            "retargeting the raw path to denied B must not attach cached A",
        )
        require(
            page.evaluate("HermesPersistentVideoCache.debugSnapshot().entries.length") == 0,
            "denied retarget must invalidate the former resource partition",
        )
        page.evaluate("v => v.closest('.msg-media-editor').remove()", retarget_denied)

        page.evaluate("() => fetch('/test/retarget?value=first').then(r=>r.text())")
        retarget_again = page.evaluate_handle(
            video_script(retarget_url, offscreen=True, activate=True)
        )
        wait_state(page, retarget_again, "ready")
        require(
            counts(page)["requests"].get("retarget") == 2,
            "A must be fetched again after the denied retarget invalidated its partition",
        )
        page.evaluate("v => v.closest('.msg-media-editor').remove()", retarget_again)

        page.evaluate("() => fetch('/test/retarget?value=mismatch').then(r=>r.text())")
        retarget_mismatch = page.evaluate_handle(
            video_script(retarget_url, offscreen=True, activate=True)
        )
        wait_state(page, retarget_mismatch, "fallback")
        require(
            not page.evaluate("v => v.src.startsWith('blob:')", retarget_mismatch),
            "allowed B without A's digest binding must not attach cached A",
        )
        require(
            page.evaluate("HermesPersistentVideoCache.debugSnapshot().entries.length") == 0,
            "binding-mismatch retarget must leave no reusable A entry",
        )
        page.evaluate("v => v.closest('.msg-media-editor').remove()", retarget_mismatch)
        page.evaluate("() => fetch('/test/retarget?value=first').then(r=>r.text())")

        print("PHASE concurrent-digest-scope", flush=True)
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
        scope_race_path = "/tmp/scope-race.mp4"
        scope_race_before = counts(page)["scope_requests"].get(scope_race_path, 0)
        scope_race_a = page.evaluate_handle(
            video_script(same_path_media_url("scope-race-a", 1800), offscreen=True, activate=True)
        )
        scope_race_b = page.evaluate_handle(
            video_script(same_path_media_url("scope-race-b", 2200), offscreen=True, activate=True)
        )
        wait_state(page, scope_race_a, "ready")
        wait_state(page, scope_race_b, "ready")
        scope_race_counts = counts(page)
        require(
            scope_race_counts["scope_requests"].get(scope_race_path, 0)
            == scope_race_before + 2,
            "same path with different digests must receive independent scope authorization",
        )
        require(
            scope_race_counts["requests"].get("scope-race-a") == 1
            and scope_race_counts["requests"].get("scope-race-b") == 1,
            "same-path distinct digests must not share the first resource task",
        )
        require(
            len(page.evaluate("HermesPersistentVideoCache.debugSnapshot().entries")) == 2,
            "same-path distinct digests must persist as separate resource fingerprints",
        )
        page.evaluate("([a,b]) => { a.closest('.msg-media-editor').remove(); b.closest('.msg-media-editor').remove(); }", [scope_race_a, scope_race_b])

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
        page.evaluate("v => v.closest('.msg-media-editor').remove()", reused)
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        # Unknown length is counted while streaming; oversize never enters cache.
        unknown_case = "slow-unknown-oversize"
        unknown = page.evaluate_handle(video_script(media_url(unknown_case, 5000), offscreen=True, activate=True))
        wait_state(page, unknown, "fallback")
        wait_aborted(page, unknown_case, timeout=10000)
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["entries"] == [], "unknown oversize response must not be cached")
        declared = page.evaluate_handle(video_script(media_url("declared-oversize", 5000)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", declared)
        wait_state(page, declared, "fallback")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["entries"] == [], "declared oversize response must not be cached")
        live = page.evaluate_handle(video_script(media_url("live-fallback", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", live)
        wait_state(page, live, "fallback")
        live2 = page.evaluate_handle(video_script(media_url("live-fallback", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", live2)
        wait_state(page, live2, "fallback")
        require(counts(page)["requests"].get("live-fallback") == 2, "unattested live fallback bytes must never enter persistent cache")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["tasks"] == 0, f"error/fallback must not retain a task: {snap}")

        print("PHASE body-attestation", flush=True)
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
        wrong_body = page.evaluate_handle(
            video_script(media_url("wrong-body-right-header", 1800), offscreen=True, activate=True)
        )
        page.wait_for_function(
            "v => ['fallback','integrity-error'].includes(v.dataset.persistentVideoState)",
            arg=wrong_body,
        )
        wrong_snapshot = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(wrong_snapshot["entries"] == [], f"wrong body/right header entered Cache Storage: {wrong_snapshot}")
        require(
            page.evaluate("v => v.dataset.persistentVideoState === 'integrity-error'", wrong_body),
            "wrong body/right header must fail closed instead of entering native fallback",
        )
        require(
            page.evaluate("v => { const label=v.closest('.msg-media-editor').querySelector('.msg-media-cache-progress'); return !label.hidden&&label.getAttribute('role')==='alert'&&label.textContent===t('file_open_failed'); }", wrong_body),
            "integrity failure must expose a localized accessible error state",
        )
        require(
            page.evaluate("v => !v.hasAttribute('src') && !v.src.startsWith('blob:')", wrong_body),
            "wrong body/right header must never be attached or played",
        )

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
            require(snapshot["entries"] == [], f"rejected response entered Cache Storage: {case} {snapshot}")
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
        require(
            len(snap["entries"]) == 1
            and snap["entries"][0].endswith(resource_fingerprint("lru-c", 2700)),
            "LRU must retain the newest fitting entry",
        )

        # A real QuotaExceededError evicts LRU data and retries exactly once.
        page.evaluate("HermesPersistentVideoCache.clearAll()")
        old = page.evaluate_handle(video_script(media_url("quota-old", 1800)))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", old)
        wait_state(page, old, "ready")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", old)
        page.evaluate("""async () => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v2-'+s.scope);
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
        require(
            len(snap["entries"]) == 1
            and snap["entries"][0].endswith(resource_fingerprint("quota-new", 1800)),
            f"quota retry must retain only the new entry: {snap}",
        )

        # Crash reconciliation repairs an orphan body plus dangling metadata.
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
        prime = page.evaluate_handle(video_script(media_url("crash-prime", 1800), offscreen=True, activate=True))
        wait_state(page, prime, "ready")
        orphan_url = media_url("crash-orphan", 1800)
        page.evaluate("""async (url) => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v2-'+s.scope);
          for(const request of await cache.keys()) await cache.delete(request);
          const source=new URL(url,location.href);
          const endpoint=new URL('/api/media-cache/scope',location.origin);
          for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
          const authorization=await fetch(endpoint).then(r=>r.json());
          const key=location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          const response=await fetch(url);
          await cache.put(key,response);
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries:{[location.origin+'/dangling']:{size:99,at:1}}}),{headers:{'Content-Type':'application/json'}}));
        }""", orphan_url)
        orphan = page.evaluate_handle(video_script(orphan_url))
        page.evaluate("v => v.dispatchEvent(new Event('play'))", orphan)
        wait_state(page, orphan, "ready")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(len(snap["entries"]) == 1, f"crash reconciliation must retain exactly the authorized orphan body: {snap}")
        require(not counts(page)["requests"].get("crash-orphan"), "reconciled orphan body must be a cache hit")

        # A crash can leave an existing metadata row with the previous body's
        # smaller size. Reconciliation must re-read Content-Length for every
        # body, not only metadata-less orphans, before enforcing global quota.
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
        stale_prime = page.evaluate_handle(video_script(media_url("stale-prime", 1800), offscreen=True, activate=True))
        wait_state(page, stale_prime, "ready")
        stale_a = media_url("stale-size-a", 3000)
        stale_b = media_url("stale-size-b", 3000)
        page.evaluate("""async ([a,b]) => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v2-'+s.scope);
          for(const request of await cache.keys()) await cache.delete(request);
          const keyFor=async url=>{
            const source=new URL(url,location.href);
            const endpoint=new URL('/api/media-cache/scope',location.origin);
            for(const name of ['session_id','path','snap']) endpoint.searchParams.set(name,source.searchParams.get(name));
            const authorization=await fetch(endpoint).then(r=>r.json());
            return location.origin+'/__hermes_snapshot_video_cache_resource__/'+authorization.resource;
          };
          const [ra,rb,ca,cb]=await Promise.all([fetch(a),fetch(b),keyFor(a),keyFor(b)]);
          await cache.put(ca,ra);
          await cache.put(cb,rb);
          window.__staleExpected=cb;
          const entries={};
          entries[ca]={size:1,at:1};
          entries[cb]={size:1,at:2};
          await cache.put(location.origin+'/__hermes_snapshot_video_cache_meta__',new Response(JSON.stringify({entries}),{headers:{'Content-Type':'application/json'}}));
        }""", [stale_a, stale_b])
        stale = page.evaluate_handle(video_script(stale_b, offscreen=True, activate=True))
        wait_state(page, stale, "ready")
        actual = page.evaluate("""async () => {
          const s=HermesPersistentVideoCache.debugSnapshot();
          const cache=await caches.open('hermes-snapshot-video-v2-'+s.scope);
          const bodies=(await cache.keys()).map(r=>r.url).filter(k=>!k.includes('__hermes_snapshot_video_cache_meta__'));
          return {bodies,snapshot:s,expected:window.__staleExpected};
        }""")
        require(actual["snapshot"]["totalBytes"] <= 5000, f"stale metadata size bypassed global quota: {actual}")
        require(len(actual["bodies"]) == 1 and actual["bodies"][0] == actual["expected"], f"stale-size reconciliation must evict the older body: {actual}")

        print("PHASE scope-request-ownership", flush=True)
        page.evaluate("document.getElementById('host').replaceChildren()")
        page.evaluate("() => HermesPersistentVideoCache.clearAll(false)")
        page.evaluate(video_script(media_url("scope-owner-a", 1800), offscreen=True, activate=True))
        require(State.scope_owner_a_started.wait(timeout=5), "scope owner A never reached the server barrier")
        page.evaluate("() => HermesPersistentVideoCache.clearAll(false)")
        owner_b = page.evaluate_handle(video_script(media_url("scope-owner-b", 1800), activate=True))
        require(State.scope_owner_b_started.wait(timeout=5), "scope owner B never reached the server barrier")
        owner_b_loading = page.evaluate("v => { const label=v.closest('.msg-media-editor').querySelector('.msg-media-cache-progress'); return {state:v.dataset.persistentVideoState||'',hidden:label.hidden,role:label.getAttribute('role')||'',text:label.textContent||'',expected:t('loading'),src:v.getAttribute('src')||''}; }", owner_b)
        State.scope_owner_a_release.set()
        page.wait_for_function("window.__HERMES_VIDEO_CACHE_TEST__.scopeRequestFinalized.some(value => value.includes('scope-owner-a.mp4'))", timeout=5000)
        owner_c = page.evaluate_handle(video_script(media_url("scope-owner-c", 1800), offscreen=True, activate=True))
        page.wait_for_timeout(100)
        with State.lock:
            owner_c_started_early = State.scope_requests.get("/tmp/scope-owner-c.mp4", 0) != 0
        State.scope_owner_b_release.set()
        require(
            owner_b_loading["state"] == "loading" and not owner_b_loading["hidden"] and owner_b_loading["role"] == "status" and owner_b_loading["text"] == owner_b_loading["expected"],
            f"scope wait must show a localized accessible loading affordance: {owner_b_loading}",
        )
        require(
            not owner_c_started_early,
            "a stale scope finalizer must not let a third request bypass its pending successor",
        )
        wait_state(page, owner_c, "fallback", timeout=10000)
        page.evaluate("v => v.scrollIntoView({block:'center'})", owner_b)
        wait_state(page, owner_b, "ready", timeout=10000)
        require(page.evaluate("v => v.src.startsWith('blob:')", owner_b), "a stale scope finalizer must not strand its successor's mounted video")
        page.evaluate("document.getElementById('host').replaceChildren()")

        print("PHASE authority", flush=True)
        # Authority transition clears old bytes before new-scope reads.
        page.evaluate("fetch('/test/scope?value=scope-b').then(() => HermesPersistentVideoCache.authorityChanged())")
        snap = page.evaluate("HermesPersistentVideoCache.debugSnapshot()")
        require(snap["scope"] == "" and snap["entries"] == [], "authority change must clear old-scope entries before another read")
        authority_probe = page.evaluate_handle(video_script(media_url("authority-probe", 1800), offscreen=True, activate=True))
        wait_state(page, authority_probe, "ready")
        require(page.evaluate("HermesPersistentVideoCache.debugSnapshot().scope") == "scope-b", "next read must enter the new authority scope")
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
        page.evaluate("document.getElementById('host').replaceChildren(); HermesPersistentVideoCache.clearAll()")
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
          const cache=await caches.open('hermes-snapshot-video-v2-'+s.scope);
          const keys=(await cache.keys()).map(r=>r.url);
          const meta=await cache.match(location.origin+'/__hermes_snapshot_video_cache_meta__').then(r=>r.json());
          const bodies=keys.filter(k=>!k.includes('__hermes_snapshot_video_cache_meta__'));
          return {bodies,meta,total:Object.values(meta.entries).reduce((n,v)=>n+v.size,0)};
        }""")
        require(actual["total"] <= 5000, f"cross-tab global quota exceeded: {actual}")
        require(sorted(actual["bodies"]) == sorted(actual["meta"]["entries"].keys()), f"cross-tab metadata/body mismatch: {actual}")
        require(len(actual["bodies"]) == 1, f"cross-tab LRU must evict one 3000-byte body: {actual}")
        peer.evaluate("v => { v.closest('.msg-media-editor').style.marginTop='0'; v.scrollIntoView({block:'center'}); }", right)
        right_blob = peer.evaluate("v => v.dataset.cacheBlobUrl", right)
        page.evaluate("() => HermesPersistentVideoCache.clearAll()")
        wait_state(peer, right, "ready", timeout=10000)
        require(peer.evaluate("([v,old]) => v.src.startsWith('blob:') && v.dataset.cacheBlobUrl !== old", [right, right_blob]), "cross-tab authority clear must recover a mounted ready player")
        page.evaluate("v => v.closest('.msg-media-editor').remove()", left)
        peer.evaluate("v => v.closest('.msg-media-editor').remove()", right)
        page.evaluate("() => HermesPersistentVideoCache.clearAll()")
        peer.wait_for_function("HermesPersistentVideoCache.debugSnapshot().consumers === 0")
        remaining = page.evaluate("caches.keys().then(keys => keys.filter(k => k.startsWith('hermes-snapshot-video-v')))")
        require(remaining == [], f"authority clear must remove every tab's persistent cache after consumers detach: {remaining}")

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
        deadline=time.time()+3
        while time.time()<deadline and not counts(page)["aborted"].get("slow-authority-race",0):
            time.sleep(0.1)
        require(counts(page)["aborted"].get("slow-authority-race",0) >= 1, "post-switch refresh must abort cross-tab work started after pre-clear")
        peer.evaluate("v => { v.closest('.msg-media-editor').style.marginTop='0'; v.scrollIntoView({block:'center'}); }", raced)
        wait_state(peer, raced, "ready", timeout=10000)
        require(peer.evaluate("() => HermesPersistentVideoCache.debugSnapshot().scope") == "scope-race-new", "mounted player must recover only under the post-switch authority")
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
        require(page.evaluate("v => { const label=v.closest('.msg-media-editor').querySelector('.msg-media-cache-progress'); return !label.hidden&&label.getAttribute('role')==='status'&&label.getAttribute('aria-live')==='polite'; }", visual), "loading progress must be exposed as an accessible status")
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

        reduced_data = browser.new_context(base_url=base)
        reduced_data.add_init_script("Object.defineProperty(navigator,'connection',{value:{saveData:true},configurable:true})")
        rpage = reduced_data.new_page()
        rpage.goto("/", wait_until="domcontentloaded")
        rpage.wait_for_function("window.HermesPersistentVideoCache && HermesPersistentVideoCache.ready")
        rv = rpage.evaluate_handle(video_script(media_url("reduced-data", 1800)))
        rpage.evaluate("v => v.dispatchEvent(new Event('play'))", rv)
        wait_state(rpage, rv, "fallback")
        require(not counts(rpage)["requests"].get("reduced-data"), "saveData must choose native playback before the application cache fetch")
        reduced_data.close()

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
