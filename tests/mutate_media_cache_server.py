#!/usr/bin/env python3
"""Mutation gate for server-owned persistent media cache eligibility/integrity."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS = {
    "trust-digest-shaped-snapshot-name": (
        "api/media_snapshots.py",
        "return candidate if _snapshot_bytes_match_digest(candidate, digest) else None",
        "return candidate if candidate.is_file() else None",
        "tests/test_media_message_snapshots.py::test_snapshot_path_for_digest_rejects_tampered_named_object",
    ),
    "trust-corrupt-existing-object": (
        "api/media_snapshots.py",
        "if _snapshot_bytes_match_digest(final_path, hex_digest):",
        "if final_path.exists():",
        "tests/test_media_message_snapshots.py::test_capture_snapshot_repairs_corrupt_existing_object",
    ),
    "drop-cache-scope-hard-deny": (
        "api/routes.py",
        "if _media_deny_reason(canonical_target):",
        "if False:",
        "tests/test_persistent_video_cache_scope.py::test_media_cache_scope_rejects_server_denied_snapshot",
    ),
    "drop-canonical-path-binding": (
        "api/routes.py",
        "if not snapshot_servable_for_path(digest, canonical_target):",
        "if snapshot_file is None:",
        "tests/test_persistent_video_cache_scope.py::test_media_cache_scope_rejects_retargeted_path_without_digest_binding",
    ),
    "drop-canonical-target-from-resource": (
        "api/auth.py",
        'material = f"media-cache-resource:v2:{canonical}\\0{str(digest).lower()}".encode("utf-8")',
        'material = f"media-cache-resource:v2:{str(digest).lower()}".encode("utf-8")',
        "tests/test_persistent_video_cache_scope.py::test_media_cache_resource_is_opaque_and_binds_canonical_target",
    ),
    "re-read-attested-inode-after-verification": (
        "api/routes.py",
        "                opened_snapshot=snapshot_bytes,",
        "                opened_snapshot=None,",
        "tests/test_media_message_snapshots.py::test_handle_media_serves_the_bytes_verified_before_attestation",
    ),
    "rehash-scope-snapshot": (
        "api/routes.py",
        "snapshot_file = snapshot_candidate_for_digest(digest)",
        "snapshot_file = __import__('api.media_snapshots', fromlist=['snapshot_path_for_digest']).snapshot_path_for_digest(digest)",
        "tests/test_persistent_video_cache_scope.py::test_persistent_video_scope_does_not_rehash_eligible_body",
    ),
    "rehash-native-range-snapshot": (
        "api/routes.py",
        "snapshot_file = snapshot_candidate_for_digest(snap_digest)",
        "snapshot_file = snapshot_path_for_digest(snap_digest)",
        "tests/test_persistent_video_cache_scope.py::test_oversize_video_snapshot_range_streams_without_hashing",
    ),
    "drop-persistent-size-bound": (
        "api/routes.py",
        "if snapshot_size < 0 or snapshot_size > _PERSISTENT_VIDEO_CACHE_MAX_BYTES:",
        "if snapshot_size < 0:",
        "tests/test_persistent_video_cache_scope.py::test_persistent_video_scope_rejects_oversize_before_hashing",
    ),
    "rehash-preverified-etag": (
        "api/routes.py",
        "etag = opened_etag or _bytes_etag(snapshot)",
        "etag = _bytes_etag(snapshot)",
        "tests/test_media_message_snapshots.py::test_preverified_snapshot_reuses_digest_etag_without_second_hash",
    ),
    "hash-native-small-range": (
        "api/routes.py",
        "verify_snapshot_body = cache_fetch_requested or not handler.headers.get(\"Range\")\n        if verify_snapshot_body and 0 <= snapshot_size <= _PERSISTENT_VIDEO_CACHE_MAX_BYTES:",
        "verify_snapshot_body = True\n        if verify_snapshot_body and 0 <= snapshot_size <= _PERSISTENT_VIDEO_CACHE_MAX_BYTES:",
        "tests/test_persistent_video_cache_scope.py::test_native_snapshot_tiny_range_does_not_hash_full_object",
    ),
    "mark-unverified-native-snapshot-immutable": (
        "api/routes.py",
        '                    "private, no-store",\n                    csp=csp,\n                    download_name=target.name,\n                    anchor_root=snap_dir,\n                    opened_fd=snapshot_fd,',
        '                    "private, max-age=31536000, immutable",\n                    csp=csp,\n                    download_name=target.name,\n                    anchor_root=snap_dir,\n                    opened_fd=snapshot_fd,',
        "tests/test_persistent_video_cache_scope.py::test_large_tampered_snapshot_range_is_not_immutable",
    ),
    "stream-native-range-after-headers": (
        "api/routes.py",
        "                    opened_fd=snapshot_fd,\n                    bind_range_before_headers=True,",
        "                    opened_fd=snapshot_fd,",
        "tests/test_persistent_video_cache_scope.py::test_large_snapshot_range_binds_body_before_committing_headers",
    ),
    "drop-native-snapshot-range-cap": (
        "api/routes.py",
        "        if bind_range_before_headers and byte_range:\n            end = min(end, start + _NATIVE_SNAPSHOT_RANGE_MAX_BYTES - 1)",
        "        if False:\n            end = min(end, start + _NATIVE_SNAPSHOT_RANGE_MAX_BYTES - 1)",
        "tests/test_persistent_video_cache_scope.py::test_large_snapshot_open_ended_range_is_bounded",
    ),
    "trust-opened-nonregular-snapshot": (
        "api/routes.py",
        "        if not stat_mod.S_ISREG(os.fstat(fd).st_mode):",
        "        if False:",
        "tests/test_media_message_snapshots.py::test_open_verified_snapshot_fd_rejects_non_regular_opened_fd",
    ),
}

if getattr(os, "O_NONBLOCK", 0):
    MUTATIONS["drop-snapshot-nonblocking-open"] = (
        "api/routes.py",
        '        | getattr(os, "O_NONBLOCK", 0)',
        "        | 0",
        "tests/test_media_message_snapshots.py::test_snapshot_fifo_open_never_blocks_worker",
    )


def _ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache"}
    return {name for name in names if name in ignored or name.startswith("video-cache-evidence-")}


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="hermes-media-server-mutants-") as temp:
        sandbox = Path(temp) / "repo"
        shutil.copytree(ROOT, sandbox, ignore=_ignore)
        baselines: dict[str, str] = {}
        for name, (relative, old, new, nodeid) in MUTATIONS.items():
            target = sandbox / relative
            baseline = baselines.setdefault(relative, target.read_text(encoding="utf-8"))
            target.write_text(baseline, encoding="utf-8", newline="")
            if baseline.count(old) != 1:
                failures.append(f"{name}: expected one anchor in {relative}: {old!r}")
                continue
            target.write_text(baseline.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", nodeid, "-q"],
                    cwd=sandbox,
                    text=True,
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                failures.append(f"{name}: test timed out")
                continue
            finally:
                target.write_text(baseline, encoding="utf-8", newline="")
            if result.returncode == 0:
                failures.append(f"{name}: mutant unexpectedly passed")
            elif result.returncode in {2, 3, 4, 5}:
                failures.append(
                    f"{name}: test harness failed rc={result.returncode}\n"
                    f"{result.stdout[-1000:]}\n{result.stderr[-1000:]}"
                )
            else:
                print(f"MUTANT_RED {name} rc={result.returncode}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"PASS {len(MUTATIONS)} server media cache mutants rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
