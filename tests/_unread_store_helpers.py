"""Node-harness scaffolding shared by the unread-store persistence tests.

``static/sessions.js`` persists two unread stores (``hermes-session-viewed-counts``
and ``hermes-session-completion-unread``) through a small set of pure helpers:
reading a stored map, asking whether a session still exists, and merging a cache
into a store under the monotonic / tombstone rules. The node harnesses in this
suite work by extracting individual functions from the module by name, so a
harness that drives a persistence path must also provide those helpers.

Keeping the list here means every harness injects the same set from the same
source, and the store keys / tombstone cap always match the module.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

_HELPER_NAMES = (
    "_readStoredJsonMap",
    "_sessionExistsForUnreadState",
    "_sessionListLoaded",
    "_sessionViewedCountRecord",
    "_sessionViewedCountValue",
    "_sessionViewedRecordWins",
    "_sessionTranscriptGenerationForUnread",
    "_mergeSessionViewedCounts",
    "_sessionViewedCountDeletedKey",
    "_readSessionViewedCountDeletions",
    "_recordSessionViewedCountDeleted",
    "_markerLosesToUnreadClear",
    "_sessionCompletionUnreadOrder",
    "_nextSessionCompletionUnreadOrder",
    "_sessionCompletionUnreadClearedKey",
    "_parseSessionCompletionUnreadClearedKey",
    "_readPersistedSessionCompletionUnreadCleared",
    "_readSessionCompletionUnreadCleared",
    "_mergeSessionCompletionUnread",
    "_writeSessionCompletionUnreadCleared",
)

# Declared from the module source so a harness cannot drift from the values the
# module uses. Harnesses keep their own stub keys for the two older stores.
_CONST_NAMES = (
    "SESSION_VIEWED_COUNTS_DELETED_PREFIX",
    "SESSION_VIEWED_COUNTS_DELETED_TTL_MS",
    "SESSION_COMPLETION_UNREAD_CLEARED_KEY",
    "SESSION_COMPLETION_UNREAD_CLEARED_PREFIX",
    "SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS",
)

_CONST_FALLBACKS = {
    "SESSION_COMPLETION_UNREAD_CLEARED_KEY": (
        "const SESSION_COMPLETION_UNREAD_CLEARED_KEY = "
        "'hermes-session-completion-unread-cleared';"
    ),
    "SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS": (
        "const SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS = 7 * 24 * 60 * 60 * 1000;"
    ),
}


def _function_source(name: str) -> str:
    """Brace-match a top-level ``function name(...) { ... }`` definition."""
    marker = f"function {name}("
    if marker not in SESSIONS_JS:
        return ""
    start = SESSIONS_JS.index(marker)
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    for i in range(brace, len(SESSIONS_JS)):
        ch = SESSIONS_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start:i + 1]
    raise AssertionError(f"could not brace-match {name}")


def _const_source(name: str) -> str:
    for line in SESSIONS_JS.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"const {name} ="):
            return stripped
    return _CONST_FALLBACKS.get(name, "")


# Ready to interpolate into a node harness.
BLOCK = "\n".join(
    ["var _sessionCompletionUnreadClearedMemory = {};\n"]
    + [_const_source(name) for name in _CONST_NAMES]
    + [_function_source(name) for name in _HELPER_NAMES if f"function {name}(" in SESSIONS_JS]
)
