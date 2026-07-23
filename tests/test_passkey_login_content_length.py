"""Regression test — passkey login must frame its 200 response with Content-Length.

The password-login and logout handlers set a `Content-Length` header on their
success response, but the passkey-login success block wrote the JSON body with
no `Content-Length`. Under HTTP/1.1 keep-alive that response is unframed, so the
browser's `fetch().json()` hangs until it times out. This test pins that the
passkey-login 200 now carries a `Content-Length` matching the body it writes,
mirroring the password-login block.
"""
import io
import json
from types import SimpleNamespace


class FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class RouteFakeHandler:
    def __init__(self):
        self.headers = FakeHeaders({"Host": "localhost:8787", "Content-Length": "0"})
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def test_passkey_login_success_sets_content_length(monkeypatch):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_check_login_rate", lambda ip: True)
    # WebAuthn verification is out of scope here — make it succeed.
    monkeypatch.setattr(passkeys, "finish_login", lambda body, handler: None)
    monkeypatch.setattr(auth, "create_session", lambda: "sess-cookie")
    monkeypatch.setattr(auth, "set_auth_cookie", lambda handler, cookie: None)

    handler = RouteFakeHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/login"))

    assert handler.status == 200
    body = handler.wfile.getvalue()
    assert json.loads(body) == {"ok": True}

    header_map = {k.lower(): v for k, v in handler.sent_headers}
    assert "content-length" in header_map, "passkey-login 200 must be framed"
    assert header_map["content-length"] == str(len(body))


def test_content_length_precedes_end_headers(monkeypatch):
    """`set_auth_cookie` emits Set-Cookie via send_header, so every header —
    including Content-Length — must be sent before end_headers()."""
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_check_login_rate", lambda ip: True)
    monkeypatch.setattr(passkeys, "finish_login", lambda body, handler: None)
    monkeypatch.setattr(auth, "create_session", lambda: "sess-cookie")
    monkeypatch.setattr(auth, "set_auth_cookie",
                        lambda handler, cookie: handler.send_header("Set-Cookie", "s=1"))

    order = []
    handler = RouteFakeHandler()
    orig_send = handler.send_header
    orig_end = handler.end_headers
    handler.send_header = lambda k, v: (order.append(("hdr", k.lower())), orig_send(k, v))[1]
    handler.end_headers = lambda: (order.append(("end", None)), orig_end())[1]

    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/login"))

    end_idx = order.index(("end", None))
    header_names = [name for kind, name in order[:end_idx] if kind == "hdr"]
    assert "content-length" in header_names
    assert "set-cookie" in header_names
