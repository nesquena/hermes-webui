import os
import unittest
from unittest.mock import MagicMock, patch
import threading

from api.routes import (
    _SERVER_RESTART_LOCK,
    _detect_server_mode,
    _handle_server_restart,
    handle_post,
)


class TestServerRestartCtl(unittest.TestCase):
    def setUp(self):
        if _SERVER_RESTART_LOCK.locked():
            _SERVER_RESTART_LOCK.release()

    def tearDown(self):
        if _SERVER_RESTART_LOCK.locked():
            _SERVER_RESTART_LOCK.release()

    def test_detect_server_mode_docker(self):
        with patch("os.path.isfile", side_effect=lambda p: p == "/.dockerenv"):
            self.assertEqual(_detect_server_mode(), "Docker")

    def test_detect_server_mode_systemd(self):
        with patch("os.path.isfile", return_value=False):
            with patch.dict(os.environ, {"INVOCATION_ID": "abc-123"}):
                self.assertEqual(_detect_server_mode(), "systemd")

    def test_detect_server_mode_native_app(self):
        with patch("os.path.isfile", return_value=False):
            with patch.dict(os.environ, {"HERMES_WEBUI_NATIVE_APP": "1"}, clear=True):
                self.assertEqual(_detect_server_mode(), "native-app")

    def test_detect_server_mode_foreground_fallback(self):
        with patch("os.path.isfile", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(_detect_server_mode(), "foreground")

    def test_handle_server_restart_refused_when_not_ctl_owned(self):
        handler = MagicMock()
        with patch("api.routes.j") as mock_j:
            with patch("os.path.isfile", return_value=False):
                res = _handle_server_restart(handler)
                self.assertTrue(res)
                mock_j.assert_called_once()
                args, kwargs = mock_j.call_args
                payload = args[1]
                self.assertFalse(payload["ok"])
                self.assertIn("Cannot restart", payload["error"])
                self.assertEqual(kwargs.get("status"), 400)

    def test_handle_server_restart_busy_429(self):
        handler = MagicMock()
        _SERVER_RESTART_LOCK.acquire()
        try:
            with patch("api.routes.j") as mock_j:
                with patch("os.path.isfile", return_value=True):
                    with patch("builtins.open", unittest.mock.mock_open(read_data=str(os.getpid()))):
                        res = _handle_server_restart(handler)
                        self.assertTrue(res)
                        mock_j.assert_called_once()
                        args, kwargs = mock_j.call_args
                        payload = args[1]
                        self.assertFalse(payload["ok"])
                        self.assertIn("already in progress", payload["error"])
                        self.assertEqual(kwargs.get("status"), 429)
        finally:
            _SERVER_RESTART_LOCK.release()

    def test_handle_server_restart_ctl_missing_500(self):
        handler = MagicMock()
        with patch("api.routes.j") as mock_j:
            with patch("os.path.isfile", return_value=True):
                with patch("builtins.open", unittest.mock.mock_open(read_data=str(os.getpid()))):
                    with patch("pathlib.Path.is_file", return_value=False):
                        res = _handle_server_restart(handler)
                        self.assertTrue(res)
                        mock_j.assert_called_once()
                        args, kwargs = mock_j.call_args
                        payload = args[1]
                        self.assertFalse(payload["ok"])
                        self.assertIn("ctl.sh not found", payload["error"])
                        self.assertEqual(kwargs.get("status"), 500)
                        self.assertFalse(_SERVER_RESTART_LOCK.locked())

    def test_handle_server_restart_success(self):
        handler = MagicMock()
        with patch("api.routes.j") as mock_j:
            with patch("os.path.isfile", return_value=True):
                with patch("builtins.open", unittest.mock.mock_open(read_data=str(os.getpid()))):
                    with patch("pathlib.Path.is_file", return_value=True):
                        with patch("threading.Thread") as mock_thread_cls:
                            mock_thread = MagicMock()
                            mock_thread_cls.return_value = mock_thread
                            res = _handle_server_restart(handler)
                            self.assertTrue(res)
                            mock_j.assert_called_once()
                            args, kwargs = mock_j.call_args
                            payload = args[1]
                            self.assertTrue(payload["ok"])
                            self.assertEqual(payload["status"], "restarting")
                            mock_thread.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
