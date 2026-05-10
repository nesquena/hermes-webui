from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text()
STYLE_CSS = (ROOT / "static" / "style.css").read_text()


def test_error_toast_default_duration_is_substantially_longer_than_info_toasts():
    assert "const TOAST_DEFAULT_MS=2800" in UI_JS
    assert "const TOAST_ERROR_DEFAULT_MS=20000" in UI_JS
    assert (
        "const duration=(ms==null)?(t==='error'?TOAST_ERROR_DEFAULT_MS:TOAST_DEFAULT_MS):ms"
        in UI_JS
    )
    assert "ms||2800" not in UI_JS


def test_error_toast_keeps_explicit_duration_override():
    show_toast = UI_JS[
        UI_JS.index("function showToast") : UI_JS.index("// ── Shared app dialogs")
    ]
    assert "ms==null" in show_toast
    assert "?TOAST_ERROR_DEFAULT_MS" in show_toast
    assert ":TOAST_DEFAULT_MS" in show_toast
    assert "setToastDismissTimer(el,duration)" in show_toast


def test_error_toast_has_copy_button_for_exact_error_text():
    show_toast = UI_JS[
        UI_JS.index("function showToast") : UI_JS.index("// ── Shared app dialogs")
    ]
    assert "toast-copy" in show_toast
    assert "data-toast-copy" in show_toast
    assert "copyToastText" in show_toast
    assert "const text=el?(el.dataset.toastMessage||el.textContent||''):''" in UI_JS
    assert "_copyText(text).then(done).catch(()=>{})" in UI_JS


def test_toast_dismissal_pauses_on_hover_and_keyboard_focus():
    assert "onmouseenter=()=>clearToastDismissTimer(el)" in UI_JS
    assert "onmouseleave=()=>setToastDismissTimer(el,duration)" in UI_JS
    assert "onfocusin=()=>clearToastDismissTimer(el)" in UI_JS
    assert "onfocusout=()=>setToastDismissTimer(el,duration)" in UI_JS
    assert ".toast{pointer-events:auto" in STYLE_CSS
    assert ".toast-copy" in STYLE_CSS
