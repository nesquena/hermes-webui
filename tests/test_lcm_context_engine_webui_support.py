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


def test_context_engine_metadata_reports_lcm_runtime_shape():
    from api import routes

    class FakeLcmEngine:
        name = "lcm"
        compression_count = 3

    meta = routes._context_engine_metadata(FakeLcmEngine())

    assert meta == {
        "engine": "lcm",
        "mode": "lossless_retrieval",
        "details": {
            "engine": "lcm",
            "retrieval_tools": ["lcm_grep", "lcm_expand", "lcm_describe"],
            "compression_count": 3,
        },
    }


def test_manual_compression_metadata_is_read_after_compress():
    src = _read("api/routes.py")
    start = src.find("def _handle_session_compress(handler, body)")
    assert start != -1, "manual compression handler not found"
    block = src[start:start + 15000]

    compress_idx = block.find("compressed = agent.context_compressor.compress(")
    metadata_idx = block.find("engine_meta = _context_engine_metadata(agent.context_compressor)")
    assert compress_idx != -1
    assert metadata_idx != -1
    assert compress_idx < metadata_idx

    assert "s.context_engine" in block
    assert "s.compression_anchor_engine" in block
    assert "s.compression_anchor_mode" in block
    assert "s.compression_anchor_details" in block


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


def test_frontend_renders_retrieval_compression_card_copy_from_i18n_metadata():
    src = _read("static/ui.js")
    i18n = _read("static/i18n.js")

    assert "function _compressionEngineForSession" in src
    assert "function _compressionModeForSession" in src
    assert "t('retrieval_context_label')" in src
    assert "t('retrieval_context_preview')" in src
    assert "lossless_retrieval" in src
    assert "retrieval_context_label" in i18n
    assert "retrieval_context_preview" in i18n
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


def test_context_engine_clone_helper_calls_fake_agent_hook(monkeypatch):
    from api import routes

    calls = []

    class FakeEngine:
        name = "lcm"
        compression_count = 4

        def clone_session_state(self, old_session_id, new_session_id, **kwargs):
            calls.append((old_session_id, new_session_id, kwargs))
            return {"ok": True, "copied_sidecar": True}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.context_compressor = FakeEngine()

    monkeypatch.setattr(routes, "AIAgent", FakeAIAgent, raising=False)
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: ["web"])
    monkeypatch.setattr(routes, "get_config", lambda: {"context": {"engine": "lcm"}})

    result = routes._clone_context_engine_session_state(
        old_session_id="old-session",
        new_session_id="new-session",
        mode="branch",
        keep_count=2,
        messages=[{"role": "user", "content": "kept"}],
        context_messages=[{"role": "system", "content": "ctx"}],
        model="gpt-test",
        model_provider="test-provider",
    )

    assert result == {
        "ok": True,
        "copied_sidecar": True,
        "engine": "lcm",
        "mode": "branch",
        "fork_semantics": "independent_fork",
    }
    assert calls == [
        (
            "old-session",
            "new-session",
            {
                "mode": "branch",
                "keep_count": 2,
                "messages": [{"role": "user", "content": "kept"}],
                "context_messages": [{"role": "system", "content": "ctx"}],
            },
        )
    ]


def test_default_compressor_clone_path_does_not_instantiate_agent(monkeypatch):
    from api import routes

    class ExplodingAIAgent:
        def __init__(self, **kwargs):
            raise AssertionError("default compressor should not instantiate AIAgent")

    monkeypatch.setattr(routes, "AIAgent", ExplodingAIAgent, raising=False)

    result = routes._clone_context_engine_session_state(
        old_session_id="old-session",
        new_session_id="new-session",
        mode="duplicate",
    )

    assert result == {
        "ok": True,
        "engine": "compressor",
        "mode": "duplicate",
        "copied_sidecar": True,
        "warning": None,
    }


def test_context_engine_clone_hook_failure_has_explicit_warning(monkeypatch):
    from api import routes

    class FakeEngineWithoutHook:
        name = "lcm"

    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.context_compressor = FakeEngineWithoutHook()

    monkeypatch.setattr(routes, "AIAgent", FakeAIAgent, raising=False)
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: ["web"])
    monkeypatch.setattr(routes, "get_config", lambda: {"context": {"engine": "lcm"}})

    result = routes._clone_context_engine_session_state(
        old_session_id="old-session",
        new_session_id="new-session",
        mode="duplicate",
    )

    assert result["ok"] is False
    assert result["copied_sidecar"] is False
    assert result["engine"] == "lcm"
    assert "visible transcript only" in result["warning"]
