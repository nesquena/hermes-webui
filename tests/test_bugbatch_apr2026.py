"""
Bug batch fixes — April 2026.

Covers:
- #594: .app-dialog and .file-rename-input have light theme overrides in style.css
- #576: workspace panel localStorage restore is gated on session.workspace presence (boot.js)
- #585: get_available_models() calls reload_config() before reading config cache
- #567: docker-compose.yml comment mentions macOS UID mismatch
- #590: _transcribeBlob already calls setComposerStatus('Transcribing…') — confirmed present
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS   = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
COMPOSE   = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


# ── #594: light theme dialog overrides ───────────────────────────────────────

def test_594_app_dialog_has_light_mode_override():
    """style.css must have a light mode rule targeting .app-dialog background."""
    assert ':root:not(.dark) .app-dialog{' in STYLE_CSS, (
        "Missing light mode override for .app-dialog — dialogs appear dark on light theme"
    )


def test_594_app_dialog_input_has_light_mode_override():
    """style.css must have a light mode rule for .app-dialog-input."""
    assert ":root:not(.dark) .app-dialog-input{" in STYLE_CSS, (
        "Missing light mode override for .app-dialog-input"
    )


def test_594_app_dialog_btn_has_light_mode_override():
    """style.css must have a light mode rule for .app-dialog-btn."""
    assert ":root:not(.dark) .app-dialog-btn{" in STYLE_CSS, (
        "Missing light mode override for .app-dialog-btn"
    )


def test_594_app_dialog_close_has_light_mode_override():
    """style.css must have a light mode rule for .app-dialog-close."""
    assert ":root:not(.dark) .app-dialog-close{" in STYLE_CSS, (
        "Missing light mode override for .app-dialog-close"
    )


def test_594_file_rename_input_has_light_mode_override():
    """style.css must have a light mode rule for .file-rename-input."""
    assert ":root:not(.dark) .file-rename-input{" in STYLE_CSS, (
        "Missing light mode override for .file-rename-input"
    )


# ── dark-mode user bubble semantics ──────────────────────────────────────────

def test_dark_user_bubbles_use_dark_tinted_surface():
    """Dark mode should keep user bubbles dark, with skin only tinting the bubble."""
    assert "--user-bubble-bg: var(--accent-bg-strong);" in STYLE_CSS, (
        "Dark mode user bubbles should use the dark accent tint, not the full bright accent fill"
    )
    assert "--user-bubble-border: var(--accent-bg-strong);" in STYLE_CSS, (
        "Dark mode user bubble borders should match the quieter thinking-card border intensity"
    )
    assert "--user-bubble-text: var(--text);" in STYLE_CSS, (
        "Dark mode user bubble text should inherit the theme text color"
    )


def test_dark_user_bubbles_do_not_need_per_skin_text_hacks():
    """Dark-mode user bubble contrast should not rely on per-skin text overrides."""
    assert re.search(r':root\.dark\[data-skin="[^"]+"\]\s*\{\s*--user-bubble-text:', STYLE_CSS) is None, (
        "Dark-mode user bubble contrast should come from shared theme tokens, not per-skin text hacks"
    )


# ── #576: workspace panel snap fix ───────────────────────────────────────────

def test_576_panel_restore_gated_on_workspace():
    """boot.js: localStorage panel restore must be gated on session.workspace."""
    # The guard must appear: session.workspace check before _workspacePanelMode='browse'
    assert "S.session&&S.session.workspace&&localStorage.getItem('hermes-webui-workspace-panel')" in BOOT_JS, (
        "Workspace panel localStorage restore must be gated on S.session.workspace "
        "to prevent snap-open-then-closed on sessions without a workspace (#576)"
    )


def test_576_restore_happens_after_load_session():
    """boot.js: loadSession() must come before the panel restore guard."""
    load_pos    = BOOT_JS.find("await loadSession(saved)")
    restore_pos = BOOT_JS.find("S.session&&S.session.workspace&&localStorage")
    assert load_pos != -1, "loadSession call not found in boot.js"
    assert restore_pos != -1, "workspace panel restore guard not found"
    assert load_pos < restore_pos, (
        "loadSession() must run before the panel restore guard "
        "so S.session.workspace is known at restore time"
    )


# ── #585: get_available_models reloads config ─────────────────────────────────

def test_585_get_available_models_calls_reload_config():
    """api/config.py: get_available_models() must do a mtime-based reload check."""
    config_src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    fn_start = config_src.find("def get_available_models()")
    assert fn_start != -1, "get_available_models not found"
    fn_body_end = config_src.find('"""', config_src.find('"""', fn_start + 30) + 3) + 3
    # Must check mtime before reading config
    mtime_pos    = config_src.find("_current_mtime", fn_body_end)
    active_prov_pos = config_src.find("active_provider = None", fn_body_end)
    assert mtime_pos != -1, (
        "get_available_models() must check config file mtime before reading cache (#585)"
    )
    assert mtime_pos < active_prov_pos, (
        "mtime check must come before active_provider = None in get_available_models()"
    )


# ── #567: docker-compose UID note ─────────────────────────────────────────────

def test_567_compose_mentions_macos_uid():
    """docker-compose.yml must mention macOS UID / id -u to help macOS users."""
    assert "macOS" in COMPOSE or "macos" in COMPOSE.lower(), (
        "docker-compose.yml should mention macOS UID issue (#567)"
    )
    assert "id -u" in COMPOSE, (
        "docker-compose.yml should tell users to run 'id -u' to find their UID (#567)"
    )


# ── #590: transcription spinner already present ───────────────────────────────

def test_590_transcribing_status_shown_before_fetch():
    """boot.js: setComposerStatus('Transcribing…') must fire before the fetch call."""
    transcribe_fn_start = BOOT_JS.find("async function _transcribeBlob(")
    assert transcribe_fn_start != -1, "_transcribeBlob not found in boot.js"
    fn_body = BOOT_JS[transcribe_fn_start:transcribe_fn_start + 600]
    status_pos = fn_body.find("setComposerStatus('Transcribing")
    fetch_pos  = fn_body.find("await fetch(")
    assert status_pos != -1, (
        "setComposerStatus('Transcribing…') must be called before the fetch in _transcribeBlob"
    )
    assert fetch_pos != -1, "await fetch not found in _transcribeBlob"
    assert status_pos < fetch_pos, (
        "setComposerStatus('Transcribing…') must appear before 'await fetch' "
        "so the UI shows a spinner immediately on stop (#590)"
    )


def test_590_recording_stops_before_transcribe():
    """boot.js: _setRecording(false) must fire in onstop before _transcribeBlob."""
    onstop_start = BOOT_JS.find("mediaRecorder.onstop")
    assert onstop_start != -1, "mediaRecorder.onstop not found"
    onstop_body = BOOT_JS[onstop_start:onstop_start + 400]
    rec_pos = onstop_body.find("_setRecording(false)")
    blob_pos = onstop_body.find("_transcribeBlob(")
    assert rec_pos != -1 and blob_pos != -1
    assert rec_pos < blob_pos, (
        "_setRecording(false) must come before _transcribeBlob so mic icon clears immediately"
    )
