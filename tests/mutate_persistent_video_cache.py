#!/usr/bin/env python3
"""Mutation gate for the persistent snapshot-video Chromium behavior test."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "static" / "media-cache.js"
GATE = Path(__file__).with_name("browser_persistent_video_cache.py")

MUTATIONS = {
    "drop-snapshot-attestation": [
        (
            "if(!requestedDigest||servedDigest!==requestedDigest.toLowerCase()){",
            "if(false){",
        ),
    ],
    "reject-real-session-media": [
        (
            "if(!/^[0-9A-Za-z._-]{1,128}$/.test(sessionId)) return '';",
            "if(sessionId) return '';",
        ),
    ],
    "drop-snapshot-from-scope-key": [
        (
            "      String(url.searchParams.get('path')||'')+'\\n'+\n      String(url.searchParams.get('snap')||'').toLowerCase();",
            "      String(url.searchParams.get('path')||'');",
        ),
    ],
    "cache-by-raw-url": [
        (
            "const cacheKey=_cacheKey(authorization&&authorization.resource);",
            "const cacheKey=new URL(sourceUrl,document.baseURI||location.href).href;",
        ),
    ],
    "drop-resource-body-digest": [
        (
            "if(await _blobDigest(blob)!==requestedDigest.toLowerCase()){",
            "if(false){",
        ),
    ],
    "degrade-integrity-error-to-fallback": [
        (
            "throw new MediaCacheIntegrityError('media response bytes did not match snapshot digest');",
            "throw new Error('media response bytes did not match snapshot digest');",
        ),
    ],
    "drop-snapshot-from-scope-request": [
        (
            "endpoint.searchParams.set('snap',String(mediaUrl.searchParams.get('snap')||'').toLowerCase());",
            "void mediaUrl.searchParams.get('snap');",
        ),
    ],
    "release-scope-waiter-on-deny": [
        (
            "await clearAll(true,request.waiters);",
            "await clearAll();",
        ),
    ],
    "drop-rejected-response-cancel": [
        (
            "if(response&&response.body&&typeof response.body.cancel==='function'){",
            "if(false){",
        ),
        ("try{task.controller.abort();}catch(_){}", "try{}catch(_){}"),
    ],
    "let-prepare-cleanup-failure-escape": [
        (
            "function prepareAuthorityChange(){\n  void clearAll().catch(()=>{});\n  return Promise.resolve();\n}",
            "function prepareAuthorityChange(){\n  return clearAll();\n}",
        ),
        ("const CACHE_CLEANUP_TIMEOUT_MS=250;", "const CACHE_CLEANUP_TIMEOUT_MS=60000;"),
    ],
    "drop-cache-op-lock-deadline": [
        (
            "timer=setTimeout(()=>{\n      controller.abort();\n      reject(new DOMException('Cache operation deadline exceeded','TimeoutError'));\n    },CACHE_OPERATION_TIMEOUT_MS);",
            "timer=setTimeout(()=>{\n      controller.abort();\n      reject(new DOMException('Cache operation deadline exceeded','TimeoutError'));\n    },60000);",
        ),
    ],
    "start-cache-op-deadline-after-queue": [
        (
            """function _queueCacheOp(fn){
  // Start the deadline at enqueue time. A prior callback can retain the Web
  // Lock forever; successors must still leave this promise queue and fall back
  // to native playback instead of waiting forever before requesting the lock.
  const controller=new AbortController();
  let timer=null;
  const deadline=new Promise((_,reject)=>{
    timer=setTimeout(()=>{
      controller.abort();
      reject(new DOMException('Cache operation deadline exceeded','TimeoutError'));
    },CACHE_OPERATION_TIMEOUT_MS);
  });
  const locked=()=>navigator.locks.request(
    CACHE_FAMILY+'quota-lock',
    {mode:'exclusive',signal:controller.signal},
    fn,
  );
  const queued=cacheOps.then(locked,locked);
  const run=Promise.race([queued,deadline]).finally(()=>{if(timer!==null) clearTimeout(timer);});
  cacheOps=run.catch(()=>{});
  return run;
}""",
            """function _queueCacheOp(fn){
  const locked=()=>{
    const controller=new AbortController();
    let timer=null;
    const deadline=new Promise((_,reject)=>{
      timer=setTimeout(()=>{
        controller.abort();
        reject(new DOMException('Cache operation deadline exceeded','TimeoutError'));
      },CACHE_OPERATION_TIMEOUT_MS);
    });
    const operation=navigator.locks.request(
      CACHE_FAMILY+'quota-lock',
      {mode:'exclusive',signal:controller.signal},
      fn,
    );
    return Promise.race([operation,deadline]).finally(()=>{if(timer!==null) clearTimeout(timer);});
  };
  const run=cacheOps.then(locked,locked);
  cacheOps=run.catch(()=>{});
  return run;
}""",
        ),
    ],
    "drop-persistent-hit-digest": [
        (
            "await _blobDigest(blob)===requestedDigest;",
            "true;",
        ),
    ],
    "drop-persistent-hit-mime": [
        (
            "requestedDigest)&&contentType.startsWith('video/')&&",
            "requestedDigest)&&true&&",
        ),
        (
            "blob.size===declared&&blob.type.toLowerCase().startsWith('video/')&&",
            "blob.size===declared&&true&&",
        ),
    ],
    "stale-scope-finalizer-clears-successor": [
        (
            "if(scopeRequest===request) scopeRequest=null;",
            "scopeRequest=null;",
        ),
    ],
    "drop-mounted-video-reobserve": [
        (
            "if(reobserve) for(const video of mounted) _observe(video);",
            "if(false) for(const video of mounted) _observe(video);",
        ),
    ],
    "drop-reduced-data-fallback": [
        (
            "if(!_cacheStorage()||_prefersReducedData()){",
            "if(!_cacheStorage()){",
        ),
    ],
    "drop-loading-affordance": [
        (
            "  _showLoading(record);",
            "  void record;",
        ),
    ],
    "hide-integrity-error-status": [
        (
            "_showProgressLabel(record,typeof t==='function'?t('file_open_failed'):'Could not open file',{alert:true});",
            "void record;",
        ),
    ],
    "drop-pagehide-teardown": [
        ("window.addEventListener('pagehide',()=>_teardownActive());", "window.addEventListener('pagehide',()=>{});"),
    ],
    "drop-pageshow-reinit": [
        (
            "window.addEventListener('pageshow',event=>{\n    if(!event||!event.persisted) return;\n    document.querySelectorAll('.msg-media-video').forEach(_observe);\n  });",
            "window.addEventListener('pageshow',event=>{\n    if(!event||!event.persisted) return;\n  });",
        ),
    ],
    "drop-ready-progress-cleanup": [
        (
            "video.dataset.persistentVideoState='ready';\n    _clearProgress(record);",
            "video.dataset.persistentVideoState='ready';",
        ),
    ],
    "drop-blob-error-fallback": [
        ("if(current&&current.blobUrl) _fallback(current);", "if(false) _fallback(current);"),
    ],
    "drop-play-listener": [
        ("video.addEventListener('play',onPlay);", "void onPlay;"),
    ],
    "drop-native-preload-suppression": [
        (
            "video.preload='none';\n  video.removeAttribute('src');",
            "video.preload='metadata';",
        ),
    ],
    "drop-final-consumer-abort": [
        (
            "if(!record.task.settled&&record.task.consumers.size===0) record.task.controller.abort();",
            "if(false) record.task.controller.abort();",
        ),
    ],
    "drop-cached-read-byte-cap": [
        (
            "Number.isFinite(declared)&&declared>=0&&declared<=PER_FILE_BYTES&&",
            "Number.isFinite(declared)&&declared>=0&&",
        ),
        (
            "if(received>PER_FILE_BYTES){\n        controller.error(new MediaCacheLimitError('cached video exceeds persistent cache limit'));",
            "if(false){\n        controller.error(new MediaCacheLimitError('cached video exceeds persistent cache limit'));",
        ),
        (
            "if(blob.size!==received||blob.size>PER_FILE_BYTES) throw new MediaCacheLimitError('invalid cached video size');",
            "if(blob.size!==received) throw new MediaCacheLimitError('invalid cached video size');",
        ),
        ("blob.size===declared&&blob.type.toLowerCase().startsWith('video/')&&", "blob.type.toLowerCase().startsWith('video/')&&"),
    ],
    "drop-fresh-stream-byte-cap": [
        ("_broadcast(task,received,declared);\n      if(received>PER_FILE_BYTES){", "_broadcast(task,received,declared);\n      if(false){"),
        ("if(blob.size>PER_FILE_BYTES) throw new MediaCacheLimitError('video exceeds persistent cache limit');", "if(false) throw new MediaCacheLimitError('video exceeds persistent cache limit');"),
    ],
    "drop-global-lru": [
        (
            "while(_total(meta)>TOTAL_BYTES||Object.keys(meta.entries).length>MAX_ENTRIES){",
            "while(false){",
        ),
    ],
    "drop-concurrent-dedup": [
        ("if(task) return task;", "if(false) return task;"),
    ],
    "drop-source-replacement-observer": [
        (
            "attributes:true,attributeFilter:['src','data-media-source']",
            "attributes:false,attributeFilter:['src','data-media-source']",
        ),
    ],
    "drop-fallback-yield": [
        ("if(fallbackSource===sourceUrl) return false;", "if(false) return false;"),
    ],
    "drop-final-authority-broadcast": [
        ("return authorityChanged();", "return _ensureScope();"),
    ],
    "drop-stale-size-reconciliation": [
        ("}else if(prior.size!==size){", "}else if(false){"),
    ],
}


def main() -> int:
    source = SOURCE.read_text(encoding="utf-8")
    failures = []
    with tempfile.TemporaryDirectory(prefix="hermes-video-cache-mutants-") as tmp:
        tmp_path = Path(tmp)
        for name, replacements in MUTATIONS.items():
            mutant = source
            for old, new in replacements:
                if mutant.count(old) != 1:
                    failures.append(f"{name}: expected one anchor for {old!r}")
                    break
                mutant = mutant.replace(old, new, 1)
            else:
                mutant_path = tmp_path / f"{name}.js"
                mutant_path.write_text(mutant, encoding="utf-8")
                env = os.environ.copy()
                env["VIDEO_CACHE_SCRIPT"] = str(mutant_path)
                env["VIDEO_CACHE_ARTIFACT_DIR"] = str(tmp_path / name)
                result = subprocess.run(
                    [sys.executable, str(GATE)],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=240,
                    check=False,
                )
                if result.returncode == 0:
                    failures.append(f"{name}: mutant unexpectedly passed")
                elif result.returncode == 2:
                    failures.append(f"{name}: environment/setup failure\n{result.stderr[-1000:]}")
                else:
                    print(f"MUTANT_RED {name} rc={result.returncode}")
        if failures:
            print("\n".join(failures), file=sys.stderr)
            return 1
    print(f"PASS {len(MUTATIONS)} persistent video cache mutants rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
