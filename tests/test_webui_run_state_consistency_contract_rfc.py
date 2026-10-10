"""The run state consistency contract must document the client-side unread
persistence rules, and what it names has to match what `static/sessions.js`
implements.

The contract is prose, so it cannot be verified behaviourally. What rots silently
is the identifier set: the store keys the sidebar layer depends on and the
retention window that bounds the tombstone map. These assertions pin the
documented names to the implementation constants, so changing either one without
the other fails here instead of drifting.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RFC = ROOT / "docs" / "rfcs" / "webui-run-state-consistency-contract.md"
SESSIONS_JS = ROOT / "static" / "sessions.js"

# The stores the sidebar layer persists in localStorage, and the constant that
# bounds the tombstone map.
DOCUMENTED_CONSTANTS = [
    "SESSION_VIEWED_COUNTS_KEY",
    "SESSION_COMPLETION_UNREAD_KEY",
    "SESSION_COMPLETION_UNREAD_CLEARED_KEY",
]


def _sessions_js() -> str:
    return SESSIONS_JS.read_text(encoding="utf-8")


def _string_constant(source: str, name: str) -> str:
    match = re.search(rf"^const {name} = '([^']*)';", source, re.MULTILINE)
    assert match, f"{name} must be defined as a string constant in static/sessions.js"
    return match.group(1)


def test_contract_documents_the_client_side_unread_store_keys():
    text = RFC.read_text(encoding="utf-8")
    source = _sessions_js()

    for name in DOCUMENTED_CONSTANTS:
        key = _string_constant(source, name)
        assert key in text, (
            f"the contract must name the store behind {name} ({key})"
        )


def test_documented_retention_window_matches_the_implemented_cap():
    source = _sessions_js()
    match = re.search(
        r"^const SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS = (\d+) \*",
        source,
        re.MULTILINE,
    )
    assert match, (
        "SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS must be defined in days in "
        "static/sessions.js"
    )
    days = int(match.group(1))

    text = RFC.read_text(encoding="utf-8")
    assert f"{days}-day" in text, (
        f"the contract must state the {days}-day retention cap on the tombstone map"
    )
