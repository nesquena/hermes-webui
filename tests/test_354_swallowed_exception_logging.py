import io
import logging

import pytest

from api.helpers import read_body


class _CloseFailHandler:
    headers = {"Content-Length": "not-an-int"}
    rfile = io.BytesIO(b"")

    @property
    def close_connection(self):
        return False

    @close_connection.setter
    def close_connection(self, value):
        raise RuntimeError("setter failed")


def test_invalid_content_length_close_failure_is_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="api.helpers")
    with pytest.raises(ValueError):
        read_body(_CloseFailHandler())
    assert "Failed to mark connection closed after invalid Content-Length" in caplog.text
