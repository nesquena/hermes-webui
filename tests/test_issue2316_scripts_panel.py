"""Regression coverage for issue #2316: read-only Scripts subtab in Tasks."""
from pathlib import Path

import api.routes as routes

REPO = Path(__file__).resolve().parents[1]


def test_script_browser_lists_global_scripts_and_extracts_descriptions(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    py_script = scripts / "avatar_db.py"
    py_script.write_text('"""AVATAR DB — Interfaz PostgreSQL para Hermes."""\nprint("ok")\n', encoding="utf-8")
    sh_script = scripts / "sync.sh"
    sh_script.write_text("#!/bin/sh\n# Sync helper\n# for cron jobs\necho ok\n", encoding="utf-8")
    (scripts / "notes.txt").write_text("not a script", encoding="utf-8")

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

    roots = routes._script_browser_roots()
    entries = [
        routes._script_browser_entry(scope, root, path)
        for scope, root in roots.items()
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in routes._SCRIPT_BROWSER_EXTENSIONS
    ]

    assert [entry["name"] for entry in entries] == ["avatar_db.py", "sync.sh"]
    assert entries[0]["description"] == "AVATAR DB — Interfaz PostgreSQL para Hermes."
    assert entries[1]["description"] == "Sync helper for cron jobs"
    assert entries[0]["scope"] == "global"
    assert str(home / "scripts" / "avatar_db.py") == entries[0]["path"]


def test_script_browser_rejects_traversal_and_non_script_files(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "safe.py").write_text("print('ok')\n", encoding="utf-8")
    (home / "secret.py").write_text("print('secret')\n", encoding="utf-8")
    (scripts / "notes.txt").write_text("not script", encoding="utf-8")

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

    assert routes._resolve_script_browser_path("global", "safe.py") is not None
    assert routes._resolve_script_browser_path("global", "../secret.py") is None
    assert routes._resolve_script_browser_path("global", "notes.txt") is None
    assert routes._resolve_script_browser_path("unknown", "safe.py") is None


def test_tasks_panel_scripts_subtab_frontend_hooks_exist():
    index = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    style = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    routes_src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

    assert 'id="taskSubtabScripts"' in index
    assert "switchTasksSubtab('scripts')" in index
    assert "async function loadScripts" in panels
    assert "/api/scripts/list" in panels
    assert "/api/scripts/raw" in panels
    assert ".script-source" in style
    assert 'parsed.path == "/api/scripts/list"' in routes_src
    assert 'parsed.path == "/api/scripts/raw"' in routes_src


def test_scripts_i18n_keys_exist_in_english_locale():
    src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    en_block = src.split("  en: {", 1)[1].split("\n  },", 1)[0]
    for key in (
        "scripts_subtab",
        "scripts_empty_title",
        "scripts_no_scripts",
        "scripts_scope_global",
        "scripts_scope_profile",
        "scripts_detail_title",
        "scripts_path_label",
        "scripts_scope_label",
        "scripts_description_label",
        "scripts_source_label",
    ):
        assert f"{key}:" in en_block
