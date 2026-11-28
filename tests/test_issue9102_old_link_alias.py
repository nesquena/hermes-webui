"""Old-link aliasing for a compressed reset-successor tip (#9102 review).

#9102 marks a compressed reset-successor tip ``relationship_type='reset_successor'``
so the sidebar stops drawing "Forked from ...". That marker also switches off the
``parent_session_id`` redirect in ``_resolveSessionIdFromSidebarLineage``: an old
``/session/<pre-compression segment>`` link stops resolving to the imported tip and
opens the obsolete segment instead.

The review asks for the alias back, but only towards a tip that is *already imported*:
aliasing to a tip that still lives outside the WebUI store opens it read-only, which is
the path #9102 documents as breaking clarify cards and the YOLO pill.

These tests drive the production resolver extracted from ``static/sessions.js``:

- a compressed reset-successor tip that is imported answers for its own
  pre-compression segment id (the defect);
- the chain root link keeps resolving to the tip (control);
- an uncompressed reset successor never aliases its parent (control);
- a tip outside the WebUI store (CLI projection or messaging row) never becomes the
  target of an old link (control);
- ordinary compression keeps aliasing (control).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = REPO_ROOT / 'static' / 'sessions.js'
NODE = shutil.which('node')

# The resolver's own dependencies in static/sessions.js. Every name here is a
# function declaration in that file; the resolver is exercised as extracted
# production code, never re-implemented in the harness.
RESOLVER_DEPS = (
    '_sessionTimestampMs',
    '_isChildSession',
    '_isResetSuccessor',
    '_sessionLineageKey',
    '_sessionLineageContainsSession',
    '_authoritativeLineageTipId',
    '_sidebarLineageKeyForRow',
    '_collapseSessionLineageForSidebar',
    '_resolveSessionIdFromSidebarLineage',
    '_isMessagingSession',
    '_isWebUiSourceSession',
    '_isExternalSession',
)


def _module_state(js):
    """The module-level declarations the extracted predicates read.

    Without this the messaging fallback would throw a ReferenceError inside the
    harness and the resolver's own import test would be measured on a dead branch.
    """
    start = js.index('const _MESSAGING_RAW_SOURCES')
    return js[start:js.index('\n', start)]


def _resolve(rows, sid):
    if NODE is None:
        pytest.skip('node not on PATH')
    js = SESSIONS_JS_PATH.read_text(encoding='utf-8')
    program = _module_state(js)
    program += '\n' + '\n'.join(extract_function(js, name) for name in RESOLVER_DEPS)
    program += f"\nvar _allSessions = {json.dumps(rows)};"
    program += f"\nconsole.log(JSON.stringify({{resolved: _resolveSessionIdFromSidebarLineage({json.dumps(sid)})}}));"
    result = subprocess.run(
        [NODE],
        input=program,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding='utf-8',
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())['resolved']


def _compressed_reset_chain(tip_extra=None):
    """A -> reset B -> compressed B2 -> compressed B3, with B2 absent from the payload."""
    tip = {
        "session_id": "compressed-tip",
        "title": "Reset chat",
        "parent_session_id": "old-segment",
        "relationship_type": "reset_successor",
        "_lineage_root_id": "reset-root",
        "_lineage_tip_id": "compressed-tip",
        "_compression_segment_count": 3,
        "updated_at": 300,
        "last_message_at": 300,
    }
    if tip_extra:
        tip.update(tip_extra)
    return [
        {"session_id": "original", "title": "Original", "updated_at": 90, "last_message_at": 90},
        {
            "session_id": "reset-root",
            "title": "Reset chat",
            "parent_session_id": "original",
            "relationship_type": "reset_successor",
            "_lineage_root_id": "reset-root",
            "updated_at": 100,
            "last_message_at": 100,
        },
        tip,
    ]


def test_imported_compressed_reset_tip_answers_for_old_segment_link():
    rows = _compressed_reset_chain()
    assert _resolve(rows, "old-segment") == "compressed-tip"


def test_chain_root_link_still_resolves_to_the_tip():
    rows = _compressed_reset_chain()
    assert _resolve(rows, "reset-root") == "compressed-tip"


def test_uncompressed_reset_successor_does_not_alias_its_parent():
    rows = [
        {
            "session_id": "direct-reset",
            "title": "Direct reset",
            "parent_session_id": "hidden-parent",
            "relationship_type": "reset_successor",
            "_lineage_root_id": "direct-reset",
            "updated_at": 150,
            "last_message_at": 150,
        }
    ]
    assert _resolve(rows, "hidden-parent") == "hidden-parent"


def test_cli_projection_tip_never_becomes_the_old_link_target():
    rows = _compressed_reset_chain({"is_cli_session": True, "session_source": "cli"})
    assert _resolve(rows, "old-segment") == "old-segment"


def test_messaging_tip_never_becomes_the_old_link_target():
    rows = _compressed_reset_chain({"session_source": "messaging"})
    assert _resolve(rows, "old-segment") == "old-segment"


def test_ordinary_compression_keeps_aliasing_the_parent_link():
    rows = [
        {"session_id": "plain-parent", "title": "Plain", "updated_at": 90, "last_message_at": 90},
        {
            "session_id": "plain-tip",
            "title": "Plain",
            "parent_session_id": "old-plain-segment",
            "_lineage_root_id": "plain-parent",
            "_compression_segment_count": 2,
            "updated_at": 200,
            "last_message_at": 200,
        },
    ]
    assert _resolve(rows, "old-plain-segment") == "plain-tip"
