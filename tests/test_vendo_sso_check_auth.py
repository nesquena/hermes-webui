"""SSO short-circuit in api.auth.check_auth.

When VENDO_AUTH=1 and the trusted X-Vendo-* headers are present, check_auth
returns True without consulting the password machinery. When the env is on
but headers are missing, check_auth returns 403 (the proxy already handles
unauthenticated traffic; reaching the origin without headers means the
proxy was bypassed).
"""
import io
import os
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from api import auth


HEADERS_OK = {
    "X-Vendo-User-Id": "u_abc",
    "X-Vendo-User-Email": "yousef@vendo.run",
    "X-Vendo-User-Name": "Yousef Habib",
    "X-Vendo-Tenant-Id": "t_xyz",
    "X-Vendo-Role": "owner",
}


def _handler(headers: dict):
    h = MagicMock()
    h.headers = headers
    h.wfile = io.BytesIO()
    return h


class TestSsoShortCircuit:
    def test_allows_request_with_valid_vendo_headers(self):
        with patch.dict(os.environ, {"VENDO_AUTH": "1"}, clear=False):
            h = _handler(HEADERS_OK)
            assert auth.check_auth(h, urlparse("/")) is True
            # Did NOT call send_response (no 302/401/403)
            h.send_response.assert_not_called()

    def test_blocks_request_when_sso_on_but_headers_missing(self):
        with patch.dict(os.environ, {"VENDO_AUTH": "1"}, clear=False):
            h = _handler({})  # no Vendo headers
            assert auth.check_auth(h, urlparse("/")) is False
            h.send_response.assert_called_with(403)

    def test_falls_through_to_password_flow_when_sso_off(self):
        # VENDO_AUTH unset, no password set: existing flow returns True.
        env = {k: v for k, v in os.environ.items()
               if k not in ("VENDO_AUTH", "HERMES_WEBUI_PASSWORD")}
        with patch.dict(os.environ, env, clear=True):
            h = _handler({})
            assert auth.check_auth(h, urlparse("/")) is True
