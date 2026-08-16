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
    "drop-path-scope-key": [
        (
            "return String(url.searchParams.get('session_id')||'')+'\\n'+String(url.searchParams.get('path')||'');",
            "return String(url.searchParams.get('session_id')||'');",
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
            "async function prepareAuthorityChange(){\n  // Cache cleanup is best-effort plumbing, never the authority mutation itself.\n  // clearAll() invalidates this tab's scope/tasks/Blob URLs before its first\n  // await; if persistent deletion then fails, the new server-issued scope still\n  // makes the old partition unreadable and a later reconciliation can remove it.\n  try{await clearAll();}catch(_){}\n}",
            "async function prepareAuthorityChange(){\n  await clearAll();\n}",
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
    "drop-stream-byte-cap": [
        ("if(received>PER_FILE_BYTES){", "if(false){"),
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
