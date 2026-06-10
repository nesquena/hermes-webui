from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_error_toast_renders_explicit_dismiss_button():
    ui = read("static/ui.js")

    assert 'class="toast-dismiss"' in ui, (
        "Error toasts must render an explicit dismiss button so users can clear "
        "blocking toasts immediately instead of waiting for the timeout (#3842)."
    )
    assert 'data-toast-dismiss="1"' in ui, (
        "Dismiss button should expose a stable hook for future DOM/runtime tests."
    )
    assert "dismissToast(this)" in ui, (
        "Error toast dismiss control must call a dedicated dismiss helper."
    )


def test_error_toast_dismiss_helper_clears_show_state_and_timer():
    ui = read("static/ui.js")

    assert "function dismissToast(btnOrEl)" in ui, "Dismiss helper missing from static/ui.js"
    assert "clearToastDismissTimer(el);" in ui, (
        "Dismiss helper must clear the auto-dismiss timer before hiding the toast."
    )
    assert "el.classList.remove('show');" in ui, (
        "Dismiss helper must hide the toast immediately by removing the show class."
    )


def test_toast_styles_define_dismiss_button_layout():
    style = read("static/style.css")

    assert ".toast-dismiss" in style, (
        "Toast stylesheet must define the dismiss button so it matches the existing "
        "copy-button affordance without adding layout regressions."
    )
    assert ".toast-dismiss:hover,.toast-dismiss:focus-visible" in style, (
        "Dismiss button should have explicit hover/focus-visible styling for keyboard "
        "and pointer accessibility."
    )
