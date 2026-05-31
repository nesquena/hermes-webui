from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_workspace_artifacts_tab_has_real_run_graph_renderer():
    assert "function renderSessionRunGraph(" in WORKSPACE_JS
    assert "function _runGraphRunId(session)" in WORKSPACE_JS
    assert "function _runGraphCardHtml(graph)" in WORKSPACE_JS
    assert "workspace-run-graph" in WORKSPACE_JS
    assert "workspace-run-graph-card" in WORKSPACE_JS
    assert "data-run-graph-node-kind" in WORKSPACE_JS
    assert "data-run-graph-node-status" in WORKSPACE_JS
    assert "renderSessionRunGraph();" in WORKSPACE_JS


def test_run_graph_renderer_fetches_read_only_endpoint_with_real_identifiers():
    assert "/api/run/graph?session_id=${encodeURIComponent(sessionId)}&run_id=${encodeURIComponent(runId)}" in WORKSPACE_JS
    assert "api(`/api/run/graph?" in WORKSPACE_JS
    assert "S.activeStreamId" in WORKSPACE_JS
    assert "session.active_stream_id" in WORKSPACE_JS
    assert "INFLIGHT[sid]" in WORKSPACE_JS
    assert "hermes-webui-last-run-id-by-session" in WORKSPACE_JS
    lowered = WORKSPACE_JS.lower()
    assert "demo" not in lowered
    assert "mock" not in lowered
    assert "sample" not in lowered


def test_run_graph_renderer_falls_back_to_backend_latest_run_for_hard_refresh():
    assert "function _fetchLatestRunGraphRunId(sessionId)" in WORKSPACE_JS
    assert "/api/run/latest?session_id=${encodeURIComponent(sessionId)}" in WORKSPACE_JS
    assert "Looking up latest run graph…" in WORKSPACE_JS
    assert "if(!runId){" in WORKSPACE_JS
    assert "runId=await _fetchLatestRunGraphRunId(sessionId);" in WORKSPACE_JS
    assert "_setRunGraphRememberedRun(sessionId,runId);" in WORKSPACE_JS


def test_live_streams_remember_run_id_for_settled_graph_fetches():
    assert "function rememberSessionRunGraphRun(sessionId, runId)" in WORKSPACE_JS
    assert "rememberSessionRunGraphRun(activeSid, streamId);" in MESSAGES_JS
    assert "rememberSessionRunGraphRun(activeSid,r.stream_id);" in COMMANDS_JS


def test_run_graph_cards_have_dedicated_styles():
    for selector in (
        ".workspace-run-graph",
        ".workspace-run-graph-card",
        ".workspace-run-graph-card.status-failed",
        ".workspace-run-graph-card.status-running",
        ".workspace-run-graph-edge",
    ):
        assert selector in STYLE_CSS
