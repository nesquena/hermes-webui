"""Static coverage for the session "Export to PDF" feature (#3381).

The feature is intentionally dependency-free: it renders an export-only
transcript into an off-screen container with a dedicated `@media print`
stylesheet and calls the browser's print-to-PDF, reusing the existing markdown
and KaTeX rendering. These tests pin that contract so a regression (a removed
hook, or sneaking in a PDF rasterizing dependency) is caught.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PACKAGE_JSON = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def test_export_action_is_wired_into_the_session_menu():
    # The helper exists and the menu builder calls it.
    assert "function _appendSessionExportPdfAction(menu, session)" in SESSIONS_JS
    assert "_appendSessionExportPdfAction(menu, session);" in SESSIONS_JS
    # Core flow + the browser print call that produces the PDF.
    assert "function _openSessionPdfExportDialog(session)" in SESSIONS_JS
    assert "function _buildPdfExportRoot(session, messages, opts)" in SESSIONS_JS
    assert "function _exportSessionToPdf(session, opts)" in SESSIONS_JS
    assert "window.print(" in SESSIONS_JS


def test_export_reuses_existing_render_pipeline_for_fidelity():
    # Reuses renderMd + KaTeX rather than a new renderer.
    assert "renderMd(" in SESSIONS_JS
    assert "renderKatexBlocks(" in SESSIONS_JS


def test_export_is_dependency_free():
    # No PDF rasterizing library may be introduced anywhere in the frontend.
    static_dir = ROOT / "static"
    banned = ("jspdf", "html2pdf", "html2canvas")
    for js in static_dir.glob("*.js"):
        text = js.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in text, f"{js.name} references banned PDF dependency '{token}'"
    deps = {**PACKAGE_JSON.get("dependencies", {}), **PACKAGE_JSON.get("devDependencies", {})}
    for token in banned:
        assert not any(token in name.lower() for name in deps), f"package.json adds banned PDF dependency '{token}'"


def test_print_stylesheet_present():
    assert "@media print" in STYLE_CSS
    assert "#pdfExportRoot" in STYLE_CSS
    assert "pdf-exporting" in STYLE_CSS
    # Colours must survive print so the chosen theme renders.
    assert "print-color-adjust" in STYLE_CSS


def test_export_i18n_keys_present():
    for key in [
        "session_export_pdf",
        "session_export_pdf_desc",
        "export_pdf_title",
        "export_pdf_theme",
        "export_pdf_theme_light",
        "export_pdf_theme_dark",
        "export_pdf_include_timestamps",
        "export_pdf_confirm",
        "export_pdf_role_user",
        "export_pdf_role_assistant",
        "export_pdf_default_title",
        "export_pdf_empty",
        "export_pdf_failed",
    ]:
        assert f"{key}:" in I18N_JS


def test_changelog_entry_exists():
    assert "#3381" in CHANGELOG
    assert "export the current conversation to PDF" in CHANGELOG
