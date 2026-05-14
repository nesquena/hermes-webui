from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _route_block(route: str, lines: int = 140) -> str:
    src = _read("api/routes.py")
    start = src.find(f'if parsed.path == "{route}":')
    assert start != -1, f"{route} handler not found"
    return "\n".join(src[start:].splitlines()[:lines])


def test_session_model_persists_context_engine_metadata():
    src = _read("api/models.py")

    for field in (
        "context_engine",
        "compression_anchor_engine",
        "compression_anchor_mode",
        "compression_anchor_details",
        "context_engine_state",
    ):
        assert field in src, f"Session model should persist {field}"

    assert "self.context_engine =" in src
    assert "self.compression_anchor_engine =" in src
    assert "self.compression_anchor_mode =" in src
    assert "self.compression_anchor_details =" in src
    assert "self.context_engine_state =" in src


def test_manual_compress_records_engine_aware_metadata():
    src = _read("api/routes.py")
    start = src.find("def _handle_session_compress(handler, body)")
    assert start != -1, "manual compression handler not found"
    block = src[start:start + 15000]

    assert "_context_engine_metadata(" in block
    assert "agent.context_compressor" in block
    assert "s.context_engine" in block
    assert "s.compression_anchor_engine" in block
    assert "s.compression_anchor_mode" in block
    assert "s.compression_anchor_details" in block
    assert "lossless_retrieval" in src
    assert "lcm_grep" in block
    assert "lcm_expand" in block


def test_auto_compression_sse_payload_includes_engine_metadata():
    src = _read("api/streaming.py")
    start = src.find("put('compressed'")
    assert start != -1, "compressed SSE payload not found"
    block = src[start:start + 900]

    assert "'engine':" in block
    assert "'mode':" in block
    assert "'compression_count':" in block
    assert "'details':" in block
    assert "_context_engine_metadata(" in src


def test_frontend_renders_lcm_compression_card_copy_from_metadata():
    src = _read("static/ui.js")

    assert "function _compressionEngineForSession" in src
    assert "function _compressionModeForSession" in src
    assert "LCM indexed context" in src
    assert "retrievable with LCM tools" in src
    assert "lossless_retrieval" in src
    # Legacy text-marker support should remain for old built-in compressor sessions.
    assert "function _isContextCompactionMessage" in src
    assert "[context compaction" in src.lower()


def test_compressed_sse_preserves_engine_metadata_for_live_card():
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('compressed'")
    assert start != -1, "compressed SSE listener not found"
    end = src.find("source.addEventListener('metering'", start)
    assert end != -1, "metering listener not found after compressed listener"
    block = src[start:end]

    assert "engine:d.engine" in block
    assert "mode:d.mode" in block
    assert "details:d.details" in block


def test_duplicate_session_invokes_context_engine_clone_hook():
    block = _route_block("/api/session/duplicate", lines=120)

    assert "_clone_context_engine_session_state(" in block
    assert "mode=\"duplicate\"" in block
    assert "old_session_id=session.session_id" in block
    assert "new_session_id=copied_session.session_id" in block
    assert "copied_session.context_engine_state" in block
    assert "copied_session.context_engine" in block


def test_branch_session_invokes_context_engine_clone_hook_as_independent_fork():
    block = _route_block("/api/session/branch", lines=130)

    assert "_clone_context_engine_session_state(" in block
    assert "mode=\"branch\"" in block
    assert "keep_count=keep_count" in block
    assert "old_session_id=source.session_id" in block
    assert "new_session_id=branch.session_id" in block
    assert "branch.context_engine_state" in block
    assert "independent_fork" in block


def test_context_engine_clone_hook_failure_has_explicit_warning():
    src = _read("api/routes.py")

    assert "def _clone_context_engine_session_state" in src
    assert "copied_sidecar" in src
    assert "visible transcript only" in src
    assert "Context engine clone hook unavailable" in src
