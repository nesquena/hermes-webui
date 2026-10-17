"""Focused tests for the server-side Hindsight proxy validation and envelopes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import hindsight


def _handler():
    return SimpleNamespace(client_address=("127.0.0.1", 1234))


def test_validate_api_url_rejects_local_and_non_https(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_HINDSIGHT_ALLOW_INSECURE", raising=False)
    for value in (
        "file:///etc/passwd",
        "http://127.0.0.1:8787",
        "https://169.254.169.254/latest",
        "https://10.0.0.1",
    ):
        with pytest.raises(ValueError):
            hindsight._validate_api_url(value)


def test_validate_api_url_rejects_private_dns_resolution(monkeypatch):
    monkeypatch.setattr(hindsight.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(ValueError):
        hindsight._validate_api_url("https://memory.example")


def test_bank_id_is_strict_and_path_is_quoted(monkeypatch):
    assert hindsight._validate_bank_id("shared-agent-memory") == "shared-agent-memory"
    for value in ("../admin", "bank?x=1", "", "a" * 65):
        with pytest.raises(ValueError):
            hindsight._validate_bank_id(value)
    cfg = {"bank_id": "shared-agent-memory"}
    assert hindsight._bank_path(cfg, "memories/list?limit=1") == "/v1/default/banks/shared-agent-memory/memories/list?limit=1"


def test_hindsight_config_uses_profile_files_not_process_env(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(
        json.dumps({"api_url": "https://memory.example", "bank_id": "profile-bank"}),
        encoding="utf-8",
    )
    (home / ".env").write_text("HINDSIGHT_API_KEY=profile-key\n", encoding="utf-8")
    monkeypatch.setattr(hindsight, "_active_home", lambda: home)
    monkeypatch.setattr(hindsight.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))])
    monkeypatch.setattr(hindsight, "_parse_config", lambda _home: {"api_url": "https://memory.example", "bank_id": "profile-bank"})
    monkeypatch.setattr(hindsight, "_profile_secret", lambda name, _home: "profile-key")
    monkeypatch.setattr(hindsight, "get_config_snapshot", lambda: {"memory": {"provider": "hindsight", "memory_enabled": True}} , raising=False)
    cfg = hindsight._hindsight_config()
    assert cfg["_api_key"] == "profile-key"
    assert cfg["bank_id"] == "profile-bank"
    public = json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")})
    assert "profile-key" not in public


def test_non_json_upstream_is_normalized(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b"not json"

    monkeypatch.setattr(hindsight, "_open_pinned", lambda *args, **kwargs: Response())
    status, body = hindsight._proxy_sync("GET", "/health", "key", "https://memory.example", timeout=1)
    assert status == 502
    assert body == {"detail": "Hindsight returned a non-JSON response"}


def test_proxy_uses_pinned_no_redirect_transport(monkeypatch):
    """A validated origin must not be re-resolved or redirect the bearer."""
    called = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"ok": true}'

    def open_pinned(request, *, timeout):
        called["url"] = request.full_url
        called["authorization"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(hindsight, "_open_pinned", open_pinned)
    monkeypatch.setattr(hindsight.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("unsafe urlopen used"))
    status, body = hindsight._proxy_sync("GET", "/health", "key", "https://memory.example", timeout=1)
    assert (status, body) == (200, {"ok": True})
    assert called == {"url": "https://memory.example/health", "authorization": "Bearer key"}


def test_pinned_transport_covers_explicitly_allowed_http_origins(monkeypatch):
    captured = []

    class Opener:
        def open(self, _request, timeout):
            return timeout

    monkeypatch.setattr(hindsight.urllib.request, "build_opener", lambda *handlers: (captured.extend(handlers), Opener())[1])
    assert hindsight._open_pinned(hindsight.urllib.request.Request("http://memory.example/health"), timeout=3) == 3
    assert any(type(handler).__name__ == "_PinnedHTTPHandler" for handler in captured)
    assert any(type(handler).__name__ == "_PinnedHTTPSHandler" for handler in captured)


def test_recall_redacts_successful_upstream_fields(monkeypatch):
    monkeypatch.setattr(hindsight, "_hindsight_config", lambda: {"enabled": True, "recall_budget": "mid", "_api_key": "key", "api_url": "https://memory.example", "bank_id": "bank"})
    monkeypatch.setattr(hindsight, "_proxy", lambda *_args, **_kwargs: (200, {"results": [{"text": "token=super-secret", "context": "key=super-secret"}], "trace": "token=super-secret", "entities": ["token=super-secret"]}))
    monkeypatch.setattr("api.helpers._redact_text", lambda value, **_kwargs: str(value).replace("super-secret", "[REDACTED]"))
    monkeypatch.setattr("api.helpers.j", lambda _handler, payload, **_kwargs: payload)
    payload = hindsight.handle_recall(_handler(), {"query": "test"})
    assert payload["results"][0]["text"] == "token=[REDACTED]"
    assert payload["results"][0]["context"] == "key=[REDACTED]"
    assert payload["trace"] == "token=[REDACTED]"
    assert payload["entities"] == ["token=[REDACTED]"]


def test_recall_input_limits_before_proxy(monkeypatch):
    monkeypatch.setattr(hindsight, "_hindsight_config", lambda: {"enabled": True, "recall_budget": "mid", "_api_key": "key", "api_url": "https://memory.example", "bank_id": "bank"})
    called = False

    def proxy(*args, **kwargs):
        nonlocal called
        called = True
        return 200, {"results": []}

    monkeypatch.setattr(hindsight, "_proxy", proxy)
    # The response helper is intentionally mocked: this test is about the
    # observable validation boundary, not HTTP serialization.
    monkeypatch.setattr("api.helpers.bad", lambda handler, msg, status=400: {"status": status, "error": msg})
    monkeypatch.setattr("api.helpers.j", lambda *args, **kwargs: {"ok": True})
    result = hindsight.handle_recall(_handler(), {"query": "x" * (hindsight.MAX_QUERY_CHARS + 1)})
    assert result["status"] == 400
    assert called is False


@pytest.mark.parametrize(
    "upstream_total, expected",
    [
        (7, 7),
        ({"$gt": 0}, None),          # non-scalar shape
        ("12 DROP TABLE", None),     # string that would render as a count
        (True, None),                # bool is an int subclass, renders as "True"
        (-1, None),                  # nonsensical count
    ],
)
def test_status_does_not_forward_unvalidated_upstream_total(monkeypatch, upstream_total, expected):
    """handle_status must not pass an upstream field straight to the browser.

    Every other handler routes upstream values through _redact_success_value /
    _coerce_upstream_text before they reach the client; handle_status forwarded
    data['total'] raw, so a malformed or compromised upstream controlled the
    shape of a field the Memory panel renders as a count.
    """
    monkeypatch.setattr(
        hindsight,
        "_hindsight_config",
        lambda: {
            "enabled": True, "provider": "hindsight", "api_url": "https://memory.example",
            "bank_id": "bank", "mode": "local_external", "memory_mode": "hybrid",
            "recall_budget": "mid", "api_key_present": True, "_api_key": "key",
        },
    )
    monkeypatch.setattr(hindsight, "_proxy", lambda *_a, **_k: (200, {"total": upstream_total}))
    monkeypatch.setattr("api.helpers.j", lambda _handler, payload, **_kwargs: payload)
    hindsight._STATUS_CACHE.clear()
    payload = hindsight.handle_status(_handler())
    if expected is None:
        assert "total_memories" not in payload, payload
    else:
        assert payload["total_memories"] == expected


def test_status_cache_key_covers_the_enabled_branch_selector(monkeypatch, tmp_path):
    """Toggling memory_enabled must change the status response immediately.

    handle_status caches `result` under a key that did not include `enabled`,
    yet `enabled` selects which branch produces that result: the constant
    disabled hint, or a live upstream probe. Flipping memory.memory_enabled
    leaves api_url/bank_id/provider/key identical, so the pre-fix key was
    byte-identical across the toggle and the panel served the stale branch's
    answer for up to STATUS_TTL (30s). The user-visible direction is the
    enable case: you switch memory on and the panel still reports unreachable
    with a hint telling you to configure what you just configured.

    This drives the real handler, and toggles only the one config flag.
    """
    home = tmp_path / "profile"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(
        json.dumps({"api_url": "https://memory.example", "bank_id": "bank"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(hindsight, "_active_home", lambda: home)
    monkeypatch.setattr(hindsight, "_load_env_file", lambda _p: {})
    monkeypatch.setattr(hindsight, "_profile_secret", lambda _n, _h: "key")
    monkeypatch.setattr(hindsight, "_validate_api_url", lambda u: str(u).rstrip("/"))
    monkeypatch.setattr(hindsight, "_validate_bank_id", lambda b: str(b))
    monkeypatch.setattr(hindsight, "_proxy", lambda *_a, **_k: (200, {"total": 3}))
    monkeypatch.setattr("api.helpers.j", lambda _handler, payload, **_kwargs: payload)

    memory_enabled = {"value": False}
    monkeypatch.setattr(
        "api.config.get_config_snapshot",
        lambda: {"memory": {"provider": "hindsight", "memory_enabled": memory_enabled["value"]}},
    )

    hindsight._STATUS_CACHE.clear()

    disabled = hindsight.handle_status(_handler())
    assert disabled["enabled"] is False
    assert disabled["reachable"] is False
    assert "total_memories" not in disabled, disabled

    # Only memory_enabled changes. No TTL expiry, no cache clear.
    memory_enabled["value"] = True
    enabled = hindsight.handle_status(_handler())
    assert enabled["enabled"] is True
    assert enabled["reachable"] is True, enabled
    assert enabled.get("total_memories") == 3, enabled
    assert "hint" not in enabled, enabled


@pytest.mark.parametrize("bad_tags", ["oops", {"a": 1}, 7, True, ["ok", 5, None]])
def test_list_and_recall_normalize_upstream_tags(monkeypatch, bad_tags):
    """Upstream `tags` must reach the browser as a list of strings.

    The recent-memories renderer guards only on truthiness and .length, so a
    truthy non-list (a bare string is the easy case) clears the guard,
    survives .slice(), then throws on .join() and takes the whole Memory
    panel down. Coerce at the trust boundary rather than relying on which
    renderer happens to use Array.isArray.
    """
    cfg = {"enabled": True, "recall_budget": "mid", "_api_key": "key",
           "api_url": "https://memory.example", "bank_id": "bank"}
    monkeypatch.setattr(hindsight, "_hindsight_config", lambda: cfg)
    monkeypatch.setattr("api.helpers._redact_text", lambda value, **_kwargs: value)
    monkeypatch.setattr("api.helpers.j", lambda _handler, payload, **_kwargs: payload)

    monkeypatch.setattr(hindsight, "_proxy", lambda *_a, **_k: (
        200, {"memories": [{"id": "1", "text": "t", "tags": bad_tags}], "total": 1}))
    listed = hindsight.handle_list_memories(_handler(), SimpleNamespace(query=""))
    tags = listed["memories"][0]["tags"]
    assert isinstance(tags, list), f"list tags not coerced: {tags!r}"
    assert all(isinstance(x, str) for x in tags), f"non-string tag survived: {tags!r}"

    monkeypatch.setattr(hindsight, "_proxy", lambda *_a, **_k: (
        200, {"results": [{"text": "t", "tags": bad_tags}]}))
    recalled = hindsight.handle_recall(_handler(), {"query": "q"})
    rtags = recalled["results"][0]["tags"]
    assert isinstance(rtags, list), f"recall tags not coerced: {rtags!r}"
    assert all(isinstance(x, str) for x in rtags), f"non-string tag survived: {rtags!r}"
