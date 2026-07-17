"""Regression coverage for public-share local media isolation (#6126)."""

from io import BytesIO
from types import SimpleNamespace


OMITTED_ATTACHMENT = "[Local attachment omitted from public share]"


def test_public_share_snapshot_omits_local_media_references():
    import api.shares as shares

    class Session:
        pass

    session = Session()
    session.title = "Local media share"
    session.workspace = "/private/workspace"
    session.messages = [
        {"role": "user", "content": "Show the generated files."},
        {
            "role": "assistant",
            "content": (
                "Unix MEDIA:/private/workspace/output.png\n"
                "File URI MEDIA:file:///tmp/report.pdf\n"
                "Windows MEDIA:C:\\Users\\alice\\result.png\n"
                "Bare file:///tmp/data.csv\n"
                "Markdown [report](file:///tmp/report.pdf)\n"
                "Image ![chart](file:///tmp/chart.png)\n"
                "Autolink <file:///tmp/log.txt>\n"
                "Loopback MEDIA:http://localhost:8787/api/media?path=/tmp/loopback.png\n"
                "Private MEDIA:http://192.168.1.20/internal.png\n"
                "Authenticated MEDIA:https://hermes.example.test/api/media?path=/tmp/private.png\n"
                "Media subpath MEDIA:https://hermes.example.test/app/api/media/download?id=private\n"
                "Encoded MEDIA:https://hermes.example.test/app/%61pi/media?path=/tmp/private.png\n"
                "Wildcard dot MEDIA:https://127.0.0.1.nip.io/internal.png\n"
                "Wildcard dash MEDIA:https://app.192-168-1-20.sslip.io/internal.png\n"
                "Public MEDIA:https://cdn.example.test/image.png"
            ),
        },
    ]

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][1]["content"]

    assert content.count(OMITTED_ATTACHMENT) == 14
    assert "file://" not in content
    assert "MEDIA:/" not in content
    assert "MEDIA:C:" not in content
    assert "localhost:8787" not in content
    assert "192.168.1.20" not in content
    assert "hermes.example.test/api/media" not in content
    assert "/api/media/download" not in content
    assert "hermes.example.test/app/api/media" not in content
    assert "hermes.example.test/app/%61pi/media" not in content
    assert "127.0.0.1.nip.io" not in content
    assert "app.192-168-1-20.sslip.io" not in content
    assert "/private/workspace" not in content
    assert "MEDIA:https://cdn.example.test/image.png" in content


def test_public_share_snapshot_omits_browser_normalized_private_media_urls():
    import api.shares as shares

    class Session:
        pass

    private_refs = [
        ("abbreviated loopback", "http://127.1/private.png"),
        ("three-part loopback", "http://127.0.1/private.png"),
        ("octal loopback", "http://0177.0.0.1/private.png"),
        ("padded octal loopback", "http://127.00.00.01/private.png"),
        ("hex loopback", "http://0x7f.1/private.png"),
        ("abbreviated private", "http://10.1/private.png"),
        ("ipv6 loopback", "http://[::1]/private.png"),
        ("ipv4-mapped private ipv6", "http://[::ffff:192.168.1.1]/private.png"),
        ("ipv4-mapped loopback ipv6", "http://[::ffff:127.0.0.1]/private.png"),
        ("backslash media path", "https://hermes.example.test\\api\\media?path=/tmp/private.png"),
        (
            "double encoded media path",
            "https://hermes.example.test/app/%2561pi/media?path=/tmp/private.png",
        ),
    ]

    session = Session()
    session.title = "Adversarial media share"
    session.workspace = "/private/workspace"
    session.messages = [
        {
            "role": "assistant",
            "content": "\n".join(f"{label} MEDIA:{ref}" for label, ref in private_refs),
        },
    ]

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]

    assert content.count(OMITTED_ATTACHMENT) == len(private_refs)
    for _label, ref in private_refs:
        assert ref not in content
    assert "127.1" not in content
    assert "0177.0.0.1" not in content
    assert "0x7f.1" not in content
    assert "[::1]" not in content
    assert "::ffff:192.168.1.1" not in content
    assert "::ffff:127.0.0.1" not in content
    assert "\\api\\media" not in content
    assert "%2561pi" not in content


def test_authenticated_media_route_stays_private(monkeypatch):
    import api.auth as auth

    assert "/api/media" not in auth.PUBLIC_PATHS

    class Handler:
        def __init__(self):
            self.status = None
            self.headers = []
            self.wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers.append((name, value))

        def end_headers(self):
            pass

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)

    handler = Handler()
    allowed = auth.check_auth(
        handler,
        SimpleNamespace(path="/api/media", query="path=%2Fprivate%2Foutput.png"),
    )

    assert allowed is False
    assert handler.status == 401
    assert b"Authentication required" in handler.wfile.getvalue()
