"""Regression coverage for storage-pressure cleanup from issue #2389."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SW_SRC = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str, window: int = 1600) -> str:
    idx = src.find(f"function {name}(")
    assert idx != -1, f"missing function {name}"
    return src[idx : idx + window]


def test_service_worker_install_deletes_old_caches_before_opening_new_cache():
    install_idx = SW_SRC.find("self.addEventListener('install'")
    assert install_idx != -1, "service worker must define an install handler"
    install_block = SW_SRC[install_idx : SW_SRC.find("self.addEventListener('activate'", install_idx)]
    cleanup_idx = install_block.find("deleteOldShellCaches().then")
    open_idx = install_block.find("caches.open(CACHE_NAME)")
    assert cleanup_idx != -1, "install must delete stale shell caches before pre-cache"
    assert open_idx != -1, "install must still pre-cache the current shell cache"
    assert cleanup_idx < open_idx, (
        "opening the new shell cache before deleting old ones creates a temporary "
        "double-cache window that increases quota pressure"
    )


def test_service_worker_keeps_activate_cleanup_safety_net():
    activate_idx = SW_SRC.find("self.addEventListener('activate'")
    assert activate_idx != -1, "service worker must define an activate handler"
    activate_block = SW_SRC[activate_idx : activate_idx + 500]
    assert "event.waitUntil(deleteOldShellCaches())" in activate_block
    assert "self.clients.claim()" in activate_block


def test_deleted_sessions_prune_all_session_tracking_maps():
    assert "const SESSION_VIEWED_COUNTS_KEY = 'hermes-session-viewed-counts';" in SESSIONS_SRC
    assert "const SESSION_COMPLETION_UNREAD_KEY = 'hermes-session-completion-unread';" in SESSIONS_SRC
    assert "const SESSION_OBSERVED_STREAMING_KEY = 'hermes-session-observed-streaming';" in SESSIONS_SRC
    assert "function _clearSessionViewedCount(sid)" in SESSIONS_SRC

    clear_block = _function_block(SESSIONS_SRC, "_clearHandoffStorageForSession")
    assert "_clearSessionViewedCount(sid)" in clear_block
    assert "_clearSessionCompletionUnread(sid)" in clear_block
    assert "_forgetObservedStreamingSession(sid)" in clear_block


def test_session_viewed_count_prune_is_best_effort_and_persists_when_changed():
    viewed_block = _function_block(SESSIONS_SRC, "_clearSessionViewedCount")
    assert "Object.prototype.hasOwnProperty.call(counts, sid)" in viewed_block
    assert "delete counts[sid]" in viewed_block
    assert "_saveSessionViewedCounts()" in viewed_block

    clear_block = _function_block(SESSIONS_SRC, "_clearHandoffStorageForSession")
    assert "try { _clearSessionViewedCount(sid); } catch {}" in clear_block
    assert "try { _clearSessionCompletionUnread(sid); } catch {}" in clear_block
    assert "try { _forgetObservedStreamingSession(sid); } catch {}" in clear_block
