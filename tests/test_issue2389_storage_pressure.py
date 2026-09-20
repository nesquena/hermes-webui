"""Regression coverage for storage-pressure cleanup from issue #2389."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SW_SRC = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str, window: int = 1600) -> str:
    idx = src.find(f"function {name}(")
    assert idx != -1, f"missing function {name}"
    return src[idx : idx + window]


def test_service_worker_install_stages_before_activation_cleanup():
    """A failed precache must leave the previous version available."""
    install_idx = SW_SRC.find("self.addEventListener('install'")
    assert install_idx != -1, "service worker must define an install handler"
    install_block = SW_SRC[install_idx : SW_SRC.find("self.addEventListener('activate'", install_idx)]
    precache_idx = install_block.find("precacheShell()")
    skip_idx = install_block.find("self.skipWaiting()")
    assert precache_idx != -1, "install must stage the current shell before activation"
    assert skip_idx > precache_idx, "the worker must not skip waiting before precache succeeds"
    assert "deleteOldShellCaches().then" not in install_block, (
        "install must retain the previous cache until the staged worker activates"
    )

    precache_idx = SW_SRC.find("async function precacheShell()")
    precache_block = SW_SRC[precache_idx:install_idx]
    staging_idx = precache_block.find("caches.open(STAGING_CACHE_NAME)")
    current_idx = precache_block.find("caches.open(CACHE_NAME)")
    assert staging_idx != -1 and current_idx != -1
    assert staging_idx < current_idx, "current cache may only be populated after staging succeeds"
    assert "await stagingCache.addAll(SHELL_ASSETS)" in precache_block
    assert "await caches.delete(STAGING_CACHE_NAME)" in precache_block


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
