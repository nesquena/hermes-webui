"""Behavioral contract tests for optional public provider status (#6805)."""

from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

import api.provider_status as status


def _payload(**row):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"version": 1, "generatedAt": now, "providers": [{"slug": "openai", "status": "operational", "checkedAt": now, **row}]}


def test_parser_accepts_schema_and_sanitizes_optional_fields():
    rows = status.parse_status_payload(_payload(description="  service\nready  ", url="HTTPS://status.example:443/inc#ignored"))
    assert rows["openai"]["description"] == "service ready"
    assert "url" not in rows["openai"]


def test_parser_hides_unknown_malformed_and_stale_rows():
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat().replace("+00:00", "Z")
    payload = _payload()
    payload["providers"] = [{"slug": "openai", "status": "unknown", "checkedAt": payload["generatedAt"]}, {"slug": "anthropic", "status": "outage", "checkedAt": old}]
    assert status.parse_status_payload(payload) == {}


def test_parser_rejects_boolean_version_values():
    payload = _payload()
    payload["version"] = True
    assert status.parse_status_payload(payload) == {}


@pytest.mark.parametrize("field", ["generatedAt", "checkedAt"])
def test_parser_hides_future_dated_payloads(field):
    payload = _payload()
    future = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
    if field == "generatedAt":
        payload[field] = future
    else:
        payload["providers"][0][field] = future
    assert status.parse_status_payload(payload) == {}


def test_exact_selection_rejects_adjacent_slugs():
    rows = {"openai": {"slug": "openai", "status": "operational", "checkedAt": "now"}, "openai-codex": {"slug": "openai-codex", "status": "outage", "checkedAt": "now"}}
    assert status.select_provider_status(rows, "openai")["slug"] == "openai"
    assert status.select_provider_status(rows, "openai-codex-preview") is None


def test_url_normalization_rejects_unsafe_parts():
    assert status.normalize_status_url("HTTPS://Status.Example:443/path?q=1") == "https://status.example/path?q=1"
    assert status.normalize_status_url("https://status.example:0/feed") == "https://status.example:0/feed"
    assert status.normalize_status_url("https://user:pass@example.test") is None
    assert status.normalize_status_url("https://example.test/#fragment") is None


def test_disabled_source_is_empty_and_cache_is_url_keyed(monkeypatch):
    status.clear_status_cache()
    monkeypatch.delenv(status.STATUS_URL_ENV, raising=False)
    assert status.get_public_provider_statuses() == {}


def test_invalid_zero_row_payload_gets_failure_ttl(monkeypatch):
    status.clear_status_cache()
    monkeypatch.setattr(status, "_fetch", lambda url: ({}, status.MIN_CACHE_TTL))
    assert status.get_public_provider_statuses("https://one.example/status") == {}
    assert status._cache["https://one.example/status"][0] <= time.monotonic() + status.MIN_CACHE_TTL


def test_wall_clock_row_deadline_revalidates_before_cache_ttl(monkeypatch):
    status.clear_status_cache()
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    rows = {"openai": {"slug": "openai", "status": "operational", "checkedAt": now.isoformat()}}
    calls = []

    def fetch(url):
        calls.append(url)
        return rows, 300, now + timedelta(minutes=10)

    monkeypatch.setattr(status, "_fetch", fetch)
    monkeypatch.setattr(status, "_wall_now", lambda: now)
    status.get_public_provider_statuses("https://status.example/feed")
    monkeypatch.setattr(status, "_wall_now", lambda: now + timedelta(minutes=10, seconds=1))
    status.get_public_provider_statuses("https://status.example/feed")
    assert calls == ["https://status.example/feed", "https://status.example/feed"]
    assert status._cache["https://status.example/feed"][0] > time.monotonic()


def test_readme_environment_rows_remain_a_markdown_table():
    lines = (__import__("pathlib").Path(__file__).resolve().parents[1] / "README.md").read_text().splitlines()
    start = lines.index("| Variable | Default | Description |")
    end = next(index for index in range(start + 1, len(lines)) if lines[index] == "")
    assert all(line.startswith("|") for line in lines[start:end])
    home = next(index for index in range(start, end) if lines[index].startswith("| `HERMES_HOME`"))
    provider = next(index for index in range(start, end) if lines[index].startswith("| `HERMES_WEBUI_PROVIDER_STATUS_URL`"))
    assert home > provider


def test_valid_shape_with_zero_rows_is_cached_as_retryable_failure(monkeypatch):
    status.clear_status_cache()
    payload = _payload()
    payload["providers"] = []

    class Response:
        status = 200
        headers = {"Cache-Control": "max-age=300"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return json.dumps(payload).encode()

    class Opener:
        def open(self, request, timeout): return Response()

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener())
    rows, ttl, deadline = status._fetch("https://empty.example/status")
    assert rows == {}
    assert ttl == status.MIN_CACHE_TTL
    assert deadline is None


def test_same_key_refreshes_coalesce_and_different_urls_run_independently(monkeypatch):
    status.clear_status_cache()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/one"):
            started.set()
            assert release.wait(2)
        return ({"openai": {"slug": "openai", "status": "operational", "checkedAt": "now"}}, 60)

    monkeypatch.setattr(status, "_fetch", fetch)
    results = []
    first = threading.Thread(target=lambda: results.append(status.get_public_provider_statuses("https://one.example/one", refresh=True)))
    second = threading.Thread(target=lambda: results.append(status.get_public_provider_statuses("https://one.example/one", refresh=True)))
    first.start()
    assert started.wait(2)
    second.start()
    independent = status.get_public_provider_statuses("https://two.example/two", refresh=True)
    release.set()
    first.join(2)
    second.join(2)
    assert independent["openai"]["status"] == "operational"
    assert calls.count("https://one.example/one") == 1
    assert calls.count("https://two.example/two") == 1
    assert len(results) == 2


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("unexpected")])
def test_unexpected_fetch_failures_are_fail_soft(monkeypatch, failure):
    status.clear_status_cache()
    monkeypatch.setattr(status, "_fetch", lambda url: (_ for _ in ()).throw(failure))
    assert status.get_public_provider_statuses("https://failure.example/status", refresh=True) == {}


def test_public_status_composes_without_changing_local_authority(monkeypatch):
    import api.providers as providers

    local = {
        "ok": False,
        "provider": "openai",
        "display_name": "OpenAI",
        "supported": True,
        "status": "unavailable",
        "quota": None,
        "message": "local failure",
    }
    public = {"openai": {"slug": "openai", "status": "operational", "checkedAt": "now"}}
    monkeypatch.setattr(providers, "_get_provider_quota_local", lambda provider, refresh=False: dict(local))
    monkeypatch.setattr(providers, "get_public_provider_statuses", lambda **kwargs: public)
    monkeypatch.setattr(providers, "_canonicalise_provider_id", lambda value: "openai")
    monkeypatch.setattr(providers, "_is_known_model_provider", lambda value: True)
    result = providers.get_provider_quota("openai")
    assert result["public_status"]["status"] == "operational"
    assert {key: result[key] for key in local} == local


def test_browser_harness_renders_and_hides_optional_public_status(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 480, "height": 900}, device_scale_factor=1)
        page.set_content('<main id="root"></main>')
        page.add_script_tag(path=str(root / "static" / "i18n.js"))
        page.add_script_tag(content="window.esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));")
        page.add_script_tag(path=str(root / "static" / "panels.js"))
        page.add_style_tag(path=str(root / "static" / "style.css"))
        rendered = page.evaluate("""() => {
            const card = _buildProviderQuotaCard({
                ok: false, provider: 'openai', display_name: 'OpenAI', supported: true,
                status: 'unavailable', quota: null, message: 'Local account unavailable',
                public_status: {
                    slug: 'openai', status: 'degraded', checkedAt: '2026-08-27T12:00:00Z',
                    description: '<img src=x onerror=alert(1)>', url: 'https://status.example/incidents/1'
                }
            });
            document.querySelector('#root').append(card);
            return {
                text: card.innerText,
                detail: card.querySelector('.provider-public-status')?.outerHTML || '',
                link: card.querySelector('.provider-public-status a')?.getAttribute('href') || ''
            };
        }""")
        assert "degraded" in rendered["text"]
        assert "<img" not in rendered["detail"]
        assert rendered["link"] == "https://status.example/incidents/1"
        page.screenshot(path=str(tmp_path / "provider-status.png"), full_page=True)
        hidden = page.evaluate("""() => {
            const card = _buildProviderQuotaCard({ok: false, provider: 'openai', status: 'unavailable', message: 'Local'});
            return card.querySelector('.provider-public-status') === null;
        }""")
        assert hidden is True
        browser.close()


def test_outbound_request_uses_fixed_headers_and_sentinel_bound(monkeypatch):
    seen = {}

    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit):
            seen["limit"] = limit
            return json.dumps(_payload()).encode()

    class Opener:
        def open(self, request, timeout):
            seen["headers"] = dict(request.headers)
            seen["timeout"] = timeout
            return Response()

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener())
    assert status._fetch("https://status.example/feed")[0]["openai"]["status"] == "operational"
    assert seen["limit"] == status.MAX_BODY_BYTES + 1
    assert seen["timeout"] == 3
    assert set(seen["headers"]) == {"Accept", "User-agent"}


def test_fetch_rejects_redirects_and_oversized_payloads(monkeypatch):
    class Redirect:
        status = 302
        headers = {"Location": "https://other.example"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): raise AssertionError("redirect body must not be read")

    class Oversized:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b"x" * limit

    class Opener:
        def __init__(self, response): self.response = response
        def open(self, request, timeout): return self.response

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener(Redirect()))
    assert status._fetch("https://status.example/feed")[0] == {}
    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener(Oversized()))
    assert status._fetch("https://status.example/feed")[0] == {}


def test_fetch_timeout_is_fail_soft(monkeypatch):
    class Opener:
        def open(self, request, timeout): raise TimeoutError("timed out")
    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener())
    assert status._fetch("https://status.example/feed")[0] == {}


def test_slow_drip_body_hits_wall_clock_deadline_and_releases_waiters(monkeypatch):
    payload = json.dumps(_payload()).encode()
    calls = []

    class Socket:
        def settimeout(self, value):
            assert value > 0

    class Raw:
        _sock = Socket()

    class File:
        raw = Raw()

    class Response:
        status = 200
        headers = {}
        fp = File()

        def __init__(self):
            self.remaining = payload

        def __enter__(self): return self
        def __exit__(self, *args): return False

        def read(self, limit):
            time.sleep(0.05)
            if self.remaining:
                value = self.remaining[:1]
                self.remaining = self.remaining[1:]
                return value
            return b""

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return Response()

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener())
    monkeypatch.setattr(status, "_fetch", lambda url: status._fetch_once(url))
    status.clear_status_cache()
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                status.get_public_provider_statuses("https://slow.example/status", refresh=True)
            )
        )
        for _ in range(2)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(status.FETCH_TIMEOUT_SECONDS + 1)
    elapsed = time.monotonic() - started
    assert all(not thread.is_alive() for thread in threads)
    assert elapsed < status.FETCH_TIMEOUT_SECONDS + 1
    assert results == [{}, {}]
    assert calls == ["https://slow.example/status"]


def test_slow_open_without_socket_hits_wall_clock_deadline(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            time.sleep(status.FETCH_TIMEOUT_SECONDS + 0.2)

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener())
    started = time.monotonic()
    assert status._fetch("https://no-socket.example/status")[0] == {}
    assert time.monotonic() - started < status.FETCH_TIMEOUT_SECONDS + 0.15


def test_slow_no_socket_body_hits_wall_clock_deadline(monkeypatch):
    class Response:
        status = 200
        headers = {}

        def __enter__(self): return self
        def __exit__(self, *args): return False

        def read(self, limit):
            time.sleep(status.FETCH_TIMEOUT_SECONDS + 0.2)
            return b"{}"

    class Opener:
        def open(self, request, timeout): return Response()

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda handler: Opener())
    started = time.monotonic()
    assert status._fetch("https://no-socket-body.example/status")[0] == {}
    assert time.monotonic() - started < status.FETCH_TIMEOUT_SECONDS + 0.15


def test_waiter_drops_expired_cache_when_owner_refresh_is_incomplete(monkeypatch):
    status.clear_status_cache()
    stale = {"openai": {"slug": "openai", "status": "outage", "checkedAt": "old"}}
    status._cache["https://stale.example/status"] = (
        time.monotonic() + 60,
        stale,
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    started = threading.Event()

    def fetch(url):
        started.set()
        time.sleep(status.FETCH_TIMEOUT_SECONDS + 0.2)
        return {}, status.MIN_CACHE_TTL, None

    monkeypatch.setattr(status, "_fetch", fetch)
    owner = threading.Thread(
        target=lambda: status.get_public_provider_statuses("https://stale.example/status", refresh=True)
    )
    owner.start()
    assert started.wait(1)
    assert status.get_public_provider_statuses("https://stale.example/status") == {}
    owner.join(status.FETCH_TIMEOUT_SECONDS + 1)
    assert not owner.is_alive()
