from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _extract_function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{\n", start)
    depth = 0
    for idx in range(brace, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Function body not closed for {signature}")


def test_new_session_omits_implicit_restored_or_settings_workspace():
    fn = _extract_function(_read("static/sessions.js"), "async function newSession")
    compact = "".join(fn.split())

    assert "S.session?S.session.workspace" not in compact
    assert "S._profileDefaultWorkspace" not in fn

    req_start = fn.index("const reqBody={")
    req_end = fn.index("};", req_start)
    req_literal = fn[req_start:req_end]
    assert "workspace" not in req_literal

    assert re.search(r"if\s*\(\s*explicitWs\s*\)\s*reqBody\.workspace\s*=\s*explicitWs\s*;", fn)
    assert "S._profileSwitchWorkspace=null" in compact


def test_load_workspace_list_promotes_api_last_before_syncing_blank_display():
    fn = _extract_function(_read("static/panels.js"), "async function loadWorkspaceList")
    compact = "".join(fn.split())

    update = "if(data.last)S._profileDefaultWorkspace=data.last;"
    assert update in compact
    assert compact.index(update) < compact.index("syncWorkspaceDisplays();")


def test_worktree_action_passes_explicit_base_but_empty_base_still_omits_workspace():
    panels = _read("static/panels.js")
    assert "const worktreeBase=currentWs||((typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'');" in panels
    assert "newSession(false,{worktree:true,workspace:worktreeBase})" in panels

    fn = _extract_function(_read("static/sessions.js"), "async function newSession")
    compact = "".join(fn.split())
    assert "constexplicitWs=(options&&options.workspace)||switchWs||null;" in compact
    assert "if(explicitWs)reqBody.workspace=explicitWs;" in compact


def test_models_new_session_without_workspace_uses_get_last_workspace(tmp_path, monkeypatch):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    last_workspace = tmp_path / "last-workspace"
    last_workspace.mkdir()

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "get_last_workspace", lambda: str(last_workspace))
    monkeypatch.setattr(
        models,
        "_profile_default_model_state",
        lambda profile=None: ("test/default-model", None),
    )
    models.SESSIONS.clear()

    try:
        session = models.new_session(workspace=None, profile="default")
        assert session.workspace == str(last_workspace)
    finally:
        models.SESSIONS.clear()
