"""
Sprint 9 Tests: app.js module split verification, tool cards, todo panel.
Run: python -m pytest tests/test_sprint9.py -v
"""
import json, pathlib, urllib.error, urllib.request

from tests._pytest_port import BASE

def get_text(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode()

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

# ── Module split: all 6 files served ──────────────────────────────────────

def test_ui_js_served(cleanup_test_sessions):
    src = get_text("/static/ui.js")
    assert len(src) > 1000
    assert "function setBusy" in src
    assert "function syncTopbar" in src
    assert "const S=" in src or "const S =" in src

def test_workspace_js_served(cleanup_test_sessions):
    src = get_text("/static/workspace.js")
    assert "async function api(" in src
    assert "async function loadDir(" in src
    assert "async function openFile(" in src  # renderFileTree is in ui.js

def test_sessions_js_served(cleanup_test_sessions):
    src = get_text("/static/sessions.js")
    assert "async function newSession(" in src
    assert "async function loadSession(" in src
    assert "async function renderSessionList(" in src

def test_messages_js_served(cleanup_test_sessions):
    src = get_text("/static/messages.js")
    assert "async function send(" in src
    assert "function transcript(" in src

def test_panels_js_served(cleanup_test_sessions):
    src = get_text("/static/panels.js")
    assert "async function switchPanel(" in src
    assert "async function loadCrons(" in src
    assert "async function loadSkills(" in src
    assert "async function loadMemory(" in src

def test_boot_js_served(cleanup_test_sessions):
    src = get_text("/static/boot.js")
    assert "btnSend" in src
    assert "btnNewChat" in src
    # boot IIFE
    assert "(async()=>{" in src or "(async () => {" in src

def test_app_js_no_longer_referenced_in_html(cleanup_test_sessions):
    """index.html must not reference the old monolithic app.js."""
    html = get_text("/")
    assert 'src="static/app.js"' not in html
    # All split modules must be present with the server-injected cache-busting version query.
    for module in ["ui.js", "workspace.js", "sessions.js", "messages.js", "panels.js", "boot.js"]:
        assert f'src="static/{module}?v=' in html, f"Missing versioned {module} in index.html"

def test_module_load_order_correct(cleanup_test_sessions):
    """ui.js must appear before sessions.js which must appear before boot.js."""
    html = get_text("/")
    ui_pos = html.find('src="static/ui.js?v=')
    ws_pos = html.find('src="static/workspace.js?v=')
    sess_pos = html.find('src="static/sessions.js?v=')
    msg_pos = html.find('src="static/messages.js?v=')
    panels_pos = html.find('src="static/panels.js?v=')
    boot_pos = html.find('src="static/boot.js?v=')
    assert ui_pos < ws_pos < sess_pos < msg_pos < panels_pos < boot_pos

def test_no_duplicate_function_definitions(cleanup_test_sessions):
    """No function name should appear in more than one module."""
    import re
    modules = ["ui.js", "workspace.js", "sessions.js", "messages.js", "panels.js", "boot.js"]
    seen = {}
    for m in modules:
        src = get_text(f"/static/{m}")
        fns = re.findall(r'(?:async )?function ([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(', src)
        for fn in fns:
            if fn in seen:
                assert False, f"Duplicate function {fn} in both {seen[fn]} and {m}"
            seen[fn] = m
    assert len(seen) > 50, f"Expected 50+ functions, got {len(seen)}"

def test_all_functions_present_across_modules(cleanup_test_sessions):
    """Key functions must be present somewhere in the split modules."""
    import re
    modules = ["ui.js", "workspace.js", "sessions.js", "messages.js", "panels.js", "boot.js"]
    all_src = ""
    for m in modules:
        all_src += get_text(f"/static/{m}")
    required = [
        "setBusy", "syncTopbar", "renderMessages", "send", "loadSession",
        "newSession", "renderSessionList", "loadDir", "switchPanel",
        "loadCrons", "loadSkills", "loadMemory", "editMessage",
        "regenerateResponse", "clearConversation", "highlightCode",
        "toggleSkillForm", "submitSkillSave", "toggleMemoryEdit",
    ]
    for fn in required:
        assert fn in all_src, f"Function {fn} missing from all modules"
