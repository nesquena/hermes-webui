from api.routes import _gateway_sse_probe_payload


def test_probe_payload_when_disabled():
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': False}, watcher=None)
    assert status == 404
    assert body == {
        'ok': False,
        'enabled': False,
        'watcher_running': False,
        'error': 'agent sessions not enabled',
        'fallback_poll_ms': 30000,
    }


def test_probe_payload_when_watcher_missing():
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': True}, watcher=None)
    assert status == 503
    assert body == {
        'ok': False,
        'enabled': True,
        'watcher_running': False,
        'error': 'watcher not started',
        'fallback_poll_ms': 30000,
    }


def test_probe_payload_when_watcher_running():
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': True}, watcher=object())
    assert status == 200
    assert body == {
        'ok': True,
        'enabled': True,
        'watcher_running': True,
        'fallback_poll_ms': 30000,
    }
