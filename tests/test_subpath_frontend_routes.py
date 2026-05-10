"""Regression tests for frontend routing under subpath mounts like /hermes/."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_workspace_api_401_redirect_uses_relative_login_path():
    src = read("static/workspace.js")
    assert "res.status===401" in src
    assert "window.location.href='login?next='" in src, (
        "workspace api() must redirect to relative login?next= so /hermes/ "
        "does not escape to the personal site root /login."
    )
    assert "window.location.href='/login?next='" not in src


def test_ui_401_redirect_helper_uses_relative_login_path():
    src = read("static/ui.js")
    assert "function _redirectIfUnauth" in src
    assert "window.location.href='login?next='" in src, (
        "UI auth-expiry redirect must stay under the current subpath mount."
    )
    assert "window.location.href='/login?next='" not in src


def test_server_auth_redirect_uses_relative_login_path_with_encoded_next():
    src = read("api/auth.py")
    assert "handler.send_header('Location', 'login?next=' + _next)" in src
    assert "handler.send_header('Location', '/login?next='" not in src
    assert "safe='/'" in src, (
        "the relative redirect must keep the existing next= encoding fix"
    )


def test_direct_frontend_fetches_are_relative_to_current_mount():
    for path in ("static/boot.js", "static/sessions.js", "static/ui.js"):
        src = read(path)
        assert "fetch('/api/" not in src, (
            f"{path} must not fetch root /api/* because /hermes/ is subpath mounted."
        )
        assert 'fetch("/api/' not in src
    assert "fetch('/health'" not in read("static/ui.js")
    assert "new URL('health'" in read("static/ui.js")


def test_direct_frontend_event_sources_are_relative_to_current_mount():
    src = read("static/messages.js")
    assert "EventSource('/api/" not in src
    assert 'EventSource("/api/' not in src
    for endpoint in ("api/approval/stream", "api/clarify/stream", "api/chat/stream"):
        assert endpoint in src
        assert "new URL(" in src


def test_static_vendor_import_is_relative_to_current_mount():
    """Import must use `./static/vendor/smd.min.js` form so the URL resolves
    relative to the document URL. Bare specifier (no leading `./` or `/`)
    is invalid per ES module spec and breaks markdown streaming silently
    (#1849). Root-absolute (`/static/...`) escapes subpath mounts like
    `/hermes/`. The `./` form satisfies both constraints.
    """
    src = read("static/index.html")
    assert "import * as smd from './static/vendor/smd.min.js'" in src
    # Bare specifier — broken per ES module spec (#1849)
    assert "import * as smd from 'static/vendor/smd.min.js'" not in src
    # Root-absolute — breaks /hermes/ subpath mounts
    assert "import * as smd from '/static/vendor/smd.min.js'" not in src
