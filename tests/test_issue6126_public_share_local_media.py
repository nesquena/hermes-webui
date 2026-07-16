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
                "Public MEDIA:https://cdn.example.test/image.png"
            ),
        },
    ]

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][1]["content"]

    assert content.count(OMITTED_ATTACHMENT) == 10
    assert "file://" not in content
    assert "MEDIA:/" not in content
    assert "MEDIA:C:" not in content
    assert "localhost:8787" not in content
    assert "192.168.1.20" not in content
    assert "hermes.example.test/api/media" not in content
    assert "/private/workspace" not in content
    assert "MEDIA:https://cdn.example.test/image.png" in content


def test_authenticated_media_route_stays_private(monkeypatch):
    import api.auth as auth

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
