import json
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_probe_uses_official_dashboard_status_fingerprint(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return _FakeResponse({"version": "0.12.0", "release_date": "2026-05-01", "hermes_home": "/tmp/hermes"})

    from api import dashboard_probe

    monkeypatch.setattr(dashboard_probe.urllib.request, "urlopen", fake_urlopen)
    result = dashboard_probe.probe_official_dashboard("127.0.0.1", 9119, timeout=0.25)

    assert result["running"] is True
    assert result["host"] == "127.0.0.1"
    assert result["port"] == 9119
    assert result["url"] == "http://127.0.0.1:9119"
    assert result["version"] == "0.12.0"
    assert calls == [("http://127.0.0.1:9119/api/status", 0.25)]


def test_probe_rejects_non_dashboard_json(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"version": "1.2.3"})

    from api import dashboard_probe

    monkeypatch.setattr(dashboard_probe.urllib.request, "urlopen", fake_urlopen)
    result = dashboard_probe.probe_official_dashboard("localhost", 9119, timeout=0.25)

    assert result == {"running": False}


def test_probe_failure_and_timeout_are_safe_false(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("slow dashboard")

    from api import dashboard_probe

    monkeypatch.setattr(dashboard_probe.urllib.request, "urlopen", fake_urlopen)
    result = dashboard_probe.probe_official_dashboard("127.0.0.1", 9119, timeout=0.01)

    assert result == {"running": False}


def test_dashboard_target_validation_allows_only_loopback_base_urls():
    from api.dashboard_probe import normalize_dashboard_url

    assert normalize_dashboard_url("") is None
    assert normalize_dashboard_url("http://127.0.0.1:9120") == ("127.0.0.1", 9120, "http", "http://127.0.0.1:9120")
    assert normalize_dashboard_url("https://localhost:9443") == ("localhost", 9443, "https", "https://localhost:9443")
    assert normalize_dashboard_url("http://[::1]:9119") == ("::1", 9119, "http", "http://[::1]:9119")

    for bad in (
        "http://example.com:9119",
        "http://169.254.169.254:80",
        "http://127.0.0.1:9119/api/status",
        "http://user:***@127.0.0.1:9119",
        "file:///etc/passwd",
        "http://127.0.0.1:99999",
    ):
        try:
            normalize_dashboard_url(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe dashboard override accepted: {bad}")


def test_status_tries_default_loopback_targets_until_dashboard_found(monkeypatch):
    from api import dashboard_probe

    attempts = []

    def fake_probe(host, port, timeout=0.5, scheme="http"):
        attempts.append((host, port, timeout, scheme))
        if host == "localhost":
            return {"running": True, "host": host, "port": port, "url": "http://localhost:9119", "version": "0.12.0"}
        return {"running": False}

    monkeypatch.setattr(dashboard_probe, "probe_official_dashboard", fake_probe)
    result = dashboard_probe.get_dashboard_status(config_data={})

    assert result["running"] is True
    assert result["host"] == "localhost"
    assert attempts == [("127.0.0.1", 9119, 0.5, "http"), ("localhost", 9119, 0.5, "http")]


def test_status_honors_never_and_strict_override(monkeypatch):
    from api import dashboard_probe

    def fail_probe(*args, **kwargs):
        raise AssertionError("disabled dashboard must not probe")

    monkeypatch.setattr(dashboard_probe, "probe_official_dashboard", fail_probe)
    assert dashboard_probe.get_dashboard_status(config_data={"webui": {"dashboard": {"enabled": "never"}}}) == {
        "running": False,
        "enabled": "never",
    }

    result = dashboard_probe.get_dashboard_status(config_data={"webui": {"dashboard": {"url": "http://example.com:9119"}}})
    assert result["running"] is False
    assert "invalid" in result["error"]




def test_status_skips_auto_probe_when_webui_bind_host_is_non_loopback(monkeypatch):
    from api import dashboard_probe

    def fail_probe(*args, **kwargs):
        raise AssertionError("auto mode must not probe dashboard when WebUI binds non-loopback")

    monkeypatch.setenv("HERMES_WEBUI_HOST", "0.0.0.0")
    monkeypatch.setattr(dashboard_probe, "probe_official_dashboard", fail_probe)

    result = dashboard_probe.get_dashboard_status(config_data={})

    assert result == {"running": False, "enabled": "auto"}


def test_dashboard_status_route_returns_safe_payload(monkeypatch):
    from api import dashboard_probe
    from api.routes import handle_get

    monkeypatch.setattr(
        dashboard_probe,
        "get_dashboard_status",
        lambda: {"running": True, "host": "127.0.0.1", "port": 9119, "url": "http://127.0.0.1:9119", "version": "0.12.0"},
    )

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/dashboard/status")
    handled = handle_get(handler, parsed)

    assert handled is True
    assert handler.status == 200
    assert handler.json_body() == {
        "running": True,
        "host": "127.0.0.1",
        "port": 9119,
        "url": "http://127.0.0.1:9119",
        "version": "0.12.0",
    }


def test_dashboard_config_roundtrip_writes_profile_config_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))

    from api.dashboard_probe import get_dashboard_config, save_dashboard_config

    assert get_dashboard_config() == {"enabled": "auto", "url": ""}
    saved = save_dashboard_config({"enabled": "never", "url": ""})
    assert saved == {"enabled": "never", "url": ""}

    saved = save_dashboard_config({"enabled": "auto", "url": "http://127.0.0.1:19119"})
    assert saved == {"enabled": "auto", "url": "http://127.0.0.1:19119"}
    assert "dashboard:" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    try:
        save_dashboard_config({"enabled": "auto", "url": "http://example.com:9119"})
    except ValueError:
        pass
    else:
        raise AssertionError("external dashboard URL override must be rejected")
