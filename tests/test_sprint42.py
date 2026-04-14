"""
Tests for sprint-42: fix #427 - persist thinking/reasoning trace across page reload.

Three structural tests that verify the fix is present in the source files.
"""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_streaming_persists_reasoning_in_session():
    """streaming.py must accumulate reasoning_text and patch last assistant message."""
    src = (REPO / 'api' / 'streaming.py').read_text()

    # _reasoning_text must be initialised
    assert "_reasoning_text = ''" in src, \
        "_reasoning_text variable not initialised in streaming.py"

    # on_reasoning must accumulate into _reasoning_text
    assert '_reasoning_text += str(text)' in src, \
        "on_reasoning callback does not accumulate into _reasoning_text"

    # Persistence block must exist before raw_session is built
    assert "Persist reasoning trace in the session so it survives reload" in src, \
        "Reasoning persistence comment not found in streaming.py"

    assert "_rm['reasoning'] = _reasoning_text" in src, \
        "Code to set _rm['reasoning'] not found in streaming.py"

    # Persistence block must come BEFORE raw_session assignment
    persist_idx = src.index("Persist reasoning trace in the session")
    raw_session_idx = src.index("raw_session = s.compact()")
    assert persist_idx < raw_session_idx, \
        "Reasoning persistence block must appear before raw_session assignment"


def test_done_handler_patches_reasoning_field():
    """messages.js done SSE handler must patch reasoningText onto the last assistant message."""
    src = (REPO / 'static' / 'messages.js').read_text()

    # The persistence comment must be present inside the done handler
    assert "Persist reasoning trace so thinking card survives page reload" in src, \
        "Reasoning persistence comment not found in messages.js done handler"

    # The guard and assignment must be present
    assert "if(reasoningText){" in src, \
        "reasoningText guard not found in messages.js"

    assert "lastAsst.reasoning=reasoningText" in src, \
        "lastAsst.reasoning assignment not found in messages.js"

    # Verify the patch is inside the done handler (after 'source.addEventListener' for done)
    done_handler_idx = src.index("source.addEventListener('done'")
    persist_idx = src.index("Persist reasoning trace so thinking card survives page reload")
    assert done_handler_idx < persist_idx, \
        "Reasoning persistence patch must be inside the done SSE handler"

    # The guard must also check !lastAsst.reasoning to avoid overwriting server value
    assert "!lastAsst.reasoning" in src, \
        "Guard '!lastAsst.reasoning' missing — would overwrite server-persisted reasoning"


def test_rendermessages_reads_reasoning_from_messages():
    """ui.js renderMessages must read m.reasoning to display the thinking card."""
    src = (REPO / 'static' / 'ui.js').read_text()

    # m.reasoning must be read in the render path
    assert 'm.reasoning' in src, \
        "m.reasoning not referenced in ui.js — thinking card won't render on reload"

    # The thinking card rendering block must also be present
    assert 'thinking-card' in src, \
        "thinking-card CSS class not found in ui.js"

    # Specifically, the fallback that reads from top-level m.reasoning field
    assert 'thinkingText=m.reasoning' in src.replace(' ', ''), \
        "thinkingText=m.reasoning assignment not found in ui.js renderMessages"
