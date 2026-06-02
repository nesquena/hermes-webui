from pathlib import Path

STREAMING = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTES = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")


def test_workspace_context_helper_exists():
    assert "def _workspace_title_context(" in STREAMING
    assert "Workspace context:" in STREAMING
    assert "- Name:" in STREAMING
    assert "- Path:" in STREAMING


def test_title_prompts_accept_workspace_context():
    assert "def _title_prompts(user_text: str, assistant_text: str, workspace_context: str = '')" in STREAMING
    assert "if workspace_context:" in STREAMING
    assert "qa += f\"\\n\\n{workspace_context[:600]}\"" in STREAMING


def test_regenerate_title_passes_session_workspace_context():
    idx = ROUTES.find('if parsed.path == "/api/session/regenerate_title":')
    assert idx >= 0
    block = ROUTES[idx:idx + 1800]
    assert "workspace_context = _workspace_title_context(getattr(s, 'workspace', '') or '')" in block
    assert "_generate_llm_session_title_via_aux(" in block
