"""Public shares mask the Agent's registered exact secret values.

The Agent keeps runtime registries of exact secret values that have no fixed
shape: vault values (``browser_vault`` fills) and values applied from external
secret sources. Its ``redact_sensitive_text`` scrubs them before any pattern
pass. A public share reached them only through WebUI's redaction memo, and a
stored snapshot was served as written:

* a string memoized before a value was registered kept its memoized result, so
  a share built (or refreshed) afterwards published the value;
* a snapshot written before a value was registered served it on every
  anonymous read;
* a pattern pass that ran first could rewrite part of a registered value, so
  the exact value no longer matched and its remainder was published.

Shares now run the exact values first, on the prose a share publishes (title
and message text), when a snapshot is built and when a stored one is served.
Roles, part types and other schema fields are never scrubbed, so a short
registered value such as "user" or "text" cannot break a share. In prose such
a value is masked like any other registered value, as the Agent does. In-app
API responses are unchanged by this.

All values are synthetic.
"""
import io
import json
import sys
import types
import urllib.parse
from collections import OrderedDict
from unittest import mock

import pytest

from api import helpers

_VAULT_VALUE = "DUMMYVAULTzq81Kx7pLm"
_SOURCE_VALUE = "DUMMYSRCval9Kq2Wm4Zt"
_VAULT_MASK = "«redacted-vault-secret»"
_SOURCE_MASK = "«redacted:secret-source»"
SID = "s_share_registered_values"


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for ``handle_post``/``handle_get``."""

    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        self.client_address = ("127.0.0.1", 12345)

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


def _clear_memos():
    helpers._redact_fn_lru.cache_clear()
    helpers._might_contain_sensitive_text_lru.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_memos():
    _clear_memos()
    yield
    _clear_memos()


@pytest.fixture(params=[True, False], ids=["redaction-on", "redaction-off"])
def redaction_setting(request, monkeypatch):
    """``api_redact_enabled`` as the given value; a public share must not depend on it."""
    import api.config as config

    real = config.load_settings
    monkeypatch.setattr(
        config, "load_settings", lambda *a, **k: {**real(*a, **k), "api_redact_enabled": request.param}
    )
    return request.param


@pytest.fixture
def vault():
    """The Agent's vault-value registry for the current profile, emptied before and after."""
    redact = pytest.importorskip("agent.redact", reason="hermes-agent not installed")
    needed = ("register_vault_redaction_value", "clear_vault_redaction_values", "redact_registered_vault_values")
    if not all(hasattr(redact, name) for name in needed):
        pytest.skip("installed agent.redact has no vault value registry")
    redact.clear_vault_redaction_values()
    yield redact
    redact.clear_vault_redaction_values()


@pytest.fixture
def secret_sources():
    """``publish(values)`` for the current profile's secret-source registry, emptied before and after."""
    sources = pytest.importorskip("agent.redact_secret_sources", reason="Agent has no secret-source registry")
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    sources.publish_secret_source_values(home, {})
    yield lambda values: sources.publish_secret_source_values(home, values)
    sources.publish_secret_source_values(home, {})


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Sessions and share snapshots inside tmp_path."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares

    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    yield tmp_path


def _save_session(title, messages):
    from api import models

    session = models.Session(session_id=SID, title=title)
    session.messages = messages
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    return session


def _post(path, body):
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(body).encode())
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _get(path):
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _publish():
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200, reply
    return reply["share"]["token"]


def _public_read(token):
    """What an anonymous visitor of the link gets."""
    status, reply = _get(f"/api/share/{token}")
    assert status == 200, reply
    return reply["share"]


def _snapshot(title, messages):
    from api import shares

    return shares.build_share_snapshot(types.SimpleNamespace(session_id=SID, title=title, messages=messages))


def _texts(share):
    return [share["title"]] + [m["content"] for m in share["messages"]]


# ── Building a share ────────────────────────────────────────────────────────


def test_new_share_masks_values_registered_after_the_memo(vault, secret_sources, redaction_setting):
    """A share refreshed after a registration masks the values, whatever ``api_redact_enabled`` says."""
    text = f"the field now holds {_VAULT_VALUE} and the job printed {_SOURCE_VALUE}"
    assert helpers._might_contain_sensitive_text(text) is False, "probe must carry no WebUI marker"
    title = f"Run {_VAULT_VALUE}"
    messages = [{"role": "user", "content": text}, {"role": "assistant", "content": [{"type": "text", "text": text}]}]

    # Before the registration nothing masks the values, and the results are memoized.
    assert _texts(_snapshot(title, messages)) == [title, text, text]
    assert helpers._redact_fn_lru.cache_info().currsize > 0

    vault.register_vault_redaction_value(_VAULT_VALUE)
    secret_sources({"DB_PASSWORD": _SOURCE_VALUE})

    masked = text.replace(_VAULT_VALUE, _VAULT_MASK).replace(_SOURCE_VALUE, _SOURCE_MASK)
    snapshot = _snapshot(title, messages)
    assert _texts(snapshot) == [f"Run {_VAULT_MASK}", masked, masked]
    assert [m["role"] for m in snapshot["messages"]] == ["user", "assistant"]


def test_new_share_scrubs_exact_values_before_patterns(vault, redaction_setting):
    """A pattern that already masked part of a registered value must not hide the rest of it."""
    outer = "DUMMYprefix ghp_" + "A" * 40 + " DUMMYsuffix"
    marked = "see https://example.invalid/ then " + outer
    messages = [{"role": "user", "content": marked}, {"role": "assistant", "content": "ok"}]

    # In-app view and a first share before the registration: the token inside is
    # masked by a pattern and that partly masked text is memoized.
    partly = helpers._redact_text(marked, _enabled=True)
    assert "ghp_" + "A" * 40 not in partly and "DUMMYprefix" in partly
    assert _texts(_snapshot(marked, messages))[:2] == [partly, partly]

    vault.register_vault_redaction_value(outer)
    expected = "see https://example.invalid/ then " + _VAULT_MASK
    assert vault.redact_sensitive_text(marked, force=True) == expected, "the Agent's own order"

    snapshot = _snapshot(marked, messages)
    assert _texts(snapshot)[:2] == [expected, expected]
    assert all("DUMMYprefix" not in t and "DUMMYsuffix" not in t for t in _texts(snapshot))


def test_new_share_masks_a_value_split_across_text_parts(vault):
    """The text parts of one message are joined before publishing; the joined text is scrubbed too."""
    head, tail = _VAULT_VALUE[:10], _VAULT_VALUE[10:]
    messages = [{"role": "user", "content": [{"type": "text", "text": head}, {"type": "text", "text": tail}]}]
    assert _texts(_snapshot("t", messages))[1] == _VAULT_VALUE  # memoized before the registration

    vault.register_vault_redaction_value(_VAULT_VALUE)
    assert _texts(_snapshot("t", messages))[1] == _VAULT_MASK


def test_exact_values_run_in_the_agents_order(vault, secret_sources):
    """Vault values first, then secret-source values, as ``redact_sensitive_text`` does."""
    from api import shares

    outer = "DUMMYVAULTouter" + _SOURCE_VALUE + "tail"
    vault.register_vault_redaction_value(outer)
    secret_sources({"DB_PASSWORD": _SOURCE_VALUE})
    text = f"the field now holds {outer} ok"
    expected = vault.redact_sensitive_text(text, force=True)
    assert expected == f"the field now holds {_VAULT_MASK} ok"
    assert _texts(_snapshot("t", [{"role": "user", "content": text}]))[1] == expected
    assert shares._force_redact_credentials(text) == expected


# ── Serving a stored share ──────────────────────────────────────────────────


def test_stored_share_read_masks_values_registered_after_publishing(state, vault, secret_sources, redaction_setting):
    from api import shares

    text = f"the field now holds {_VAULT_VALUE} and the job printed {_SOURCE_VALUE}"
    _save_session(f"Run {_VAULT_VALUE}", [
        {"role": "user", "content": text, "timestamp": 100},
        {"role": "assistant", "content": "done", "timestamp": 101},
    ])
    token = _publish()
    assert _texts(_public_read(token)) == [f"Run {_VAULT_VALUE}", text, "done"]
    stored = (shares.SHARES_DIR / f"{token}.json").read_bytes()

    vault.register_vault_redaction_value(_VAULT_VALUE)
    secret_sources({"DB_PASSWORD": _SOURCE_VALUE})

    masked = text.replace(_VAULT_VALUE, _VAULT_MASK).replace(_SOURCE_VALUE, _SOURCE_MASK)
    share = _public_read(token)
    assert _texts(share) == [f"Run {_VAULT_MASK}", masked, "done"]
    assert [(m["role"], m["timestamp"]) for m in share["messages"]] == [("user", 100), ("assistant", 101)]
    assert share["message_count"] == 2
    assert shares.load_share(token)["messages"] == share["messages"]
    # Served masked; the snapshot on disk is not rewritten by a read.
    assert (shares.SHARES_DIR / f"{token}.json").read_bytes() == stored


def test_stored_share_read_keeps_embedded_image_data(state, vault):
    """Image bytes a snapshot embedded are served as written; the text around them is scrubbed."""
    from api import shares

    b64 = "iVBORw0KGgo" + "user" + "AAAANSUhEUgAAAAEAAAAB" + _VAULT_VALUE + "CAYAAAAfFcSJ"
    image = f'<img src="data:image/png;base64,{b64}" class="msg-media-img" alt="user.png" loading="lazy">'
    content = f"the user saw {_VAULT_VALUE}\n\n{image}\n\nlater user text"
    _save_session("Images", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}])
    meta = shares.write_share_snapshot(
        {"title": "Images", "messages": [{"role": "assistant", "content": content}], "message_count": 1},
        source_session_id=SID,
    )

    vault.register_vault_redaction_value("user")
    vault.register_vault_redaction_value(_VAULT_VALUE)

    served = _public_read(meta["share_token"])["messages"][0]["content"]
    assert f"data:image/png;base64,{b64}\"" in served
    assert served == (
        f"the {_VAULT_MASK} saw {_VAULT_MASK}\n\n"
        f'<img src="data:image/png;base64,{b64}" class="msg-media-img" alt="{_VAULT_MASK}.png" loading="lazy">'
        f"\n\nlater {_VAULT_MASK} text"
    )


# ── Short registered values never rewrite the schema ────────────────────────

_SCHEMA_WORDS = ("user", "assistant", "text", "type", "role", "content")


def test_short_registered_values_leave_roles_and_types_intact(state, vault, redaction_setting):
    """Registering ordinary words that equal schema values keeps the share valid.

    The words are masked where they occur in prose, exactly as the Agent masks
    a registered value anywhere in a tool result: a registered value has no
    minimum length. Roles, part types and timestamps stay as they are.
    """
    for word in _SCHEMA_WORDS:
        vault.register_vault_redaction_value(word)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello there"}], "timestamp": 100},
        {"role": "assistant", "content": "the user said some text", "timestamp": 101},
    ]

    snapshot = _snapshot("Hello", messages)
    assert snapshot["messages"] == [
        {"role": "user", "content": "hello there", "timestamp": 100},
        {"role": "assistant", "content": f"the {_VAULT_MASK} said some {_VAULT_MASK}", "timestamp": 101},
    ]
    assert snapshot["title"] == "Hello" and snapshot["message_count"] == 2

    _save_session("Hello", messages)
    token = _publish()
    share = _public_read(token)
    assert share["messages"] == snapshot["messages"]
    assert share["title"] == "Hello" and share["message_count"] == 2


@pytest.fixture
def plugin_pattern(vault):
    """A plugin redaction pattern: the generic walker then sends every string through the Agent."""
    if not all(hasattr(vault, n) for n in ("register_redaction_patterns", "_reset_plugin_redaction_patterns")):
        pytest.skip("installed agent.redact has no plugin redaction patterns")
    vault._reset_plugin_redaction_patterns()
    _clear_memos()
    vault.register_redaction_patterns([r"dummyplugin_[a-z0-9]{24}"], source="plugin:share-test")
    yield
    vault._reset_plugin_redaction_patterns()
    _clear_memos()


def test_share_keeps_roles_and_types_the_walker_masked(state, vault, plugin_pattern, redaction_setting):
    """The share takes roles, part types and timestamps from the source message.

    With a plugin pattern registered, redact_session_data sends roles and part
    types through the Agent, which masks a registered "user" or "text". The
    in-app response keeps that behaviour; a share must still build and read.
    """
    for word in ("user", "text"):
        vault.register_vault_redaction_value(word)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 100},
        {"role": "assistant", "content": "the user said hi", "timestamp": 101},
    ]
    if redaction_setting:
        walked = helpers.redact_session_data({"messages": messages})["messages"]
        assert walked[0]["role"] == _VAULT_MASK, "precondition: the walker masks the role here"

    expected = [
        {"role": "user", "content": "hello", "timestamp": 100},
        {"role": "assistant", "content": f"the {_VAULT_MASK} said hi", "timestamp": 101},
    ]
    assert _snapshot("Hello", messages)["messages"] == expected
    _save_session("Hello", messages)
    share = _public_read(_publish())
    assert share["messages"] == expected and share["message_count"] == 2


def test_in_app_session_response_does_not_use_the_share_scrub(state, vault, monkeypatch):
    """The in-app session JSON keeps master's behaviour: the share-only scrub never runs there."""
    from api import shares

    calls = []
    monkeypatch.setattr(shares, "_EXACT_VALUE_SCRUBBERS", (lambda t: calls.append(t) or t,), raising=False)
    for word in _SCHEMA_WORDS:
        vault.register_vault_redaction_value(word)
    _save_session("Hello", [
        {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 100},
        {"role": "assistant", "content": "world", "timestamp": 101},
    ])

    status, reply = _get(f"/api/session?session_id={SID}&messages=1&resolve_model=0")
    assert status == 200, reply
    messages = reply["session"]["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", [{"type": "text", "text": "hello"}]),
        ("assistant", "world"),
    ]
    assert calls == []


# ── Agents without the scrubbers ────────────────────────────────────────────


def test_agent_without_the_scrubbers_binds_none():
    from api import shares

    no_scrubbers = {"agent.redact": types.ModuleType("agent.redact"), "agent.redact_secret_sources": None}
    with mock.patch.dict(sys.modules, no_scrubbers):
        assert shares._bind_exact_value_scrubbers() == ()


def test_agent_with_only_the_vault_scrubber_binds_it_alone(monkeypatch):
    from api import shares

    redact = types.ModuleType("agent.redact")
    redact.redact_registered_vault_values = lambda text: text.replace("DUMMYONLY", "[v]")
    with mock.patch.dict(sys.modules, {"agent.redact": redact, "agent.redact_secret_sources": None}):
        scrubbers = shares._bind_exact_value_scrubbers()
    assert scrubbers == (redact.redact_registered_vault_values,)
    monkeypatch.setattr(shares, "_EXACT_VALUE_SCRUBBERS", scrubbers)
    assert _texts(_snapshot("DUMMYONLY", [{"role": "user", "content": "plain DUMMYONLY words"}])) == [
        "[v]", "plain [v] words",
    ]


def test_share_builds_and_reads_without_the_scrubbers(state, monkeypatch, redaction_setting):
    """An Agent that has neither scrubber: the share works and reads as before."""
    from api import shares

    monkeypatch.setattr(shares, "_EXACT_VALUE_SCRUBBERS", (), raising=False)
    text = f"the field now holds {_VAULT_VALUE} ok"
    marked = "see https://example.invalid/ then ghp_" + "A" * 40
    _save_session("Plain title", [
        {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": 100},
        {"role": "assistant", "content": marked, "timestamp": 101},
    ])
    token = _publish()
    share = _public_read(token)
    assert share["title"] == "Plain title"
    assert share["messages"] == [
        {"role": "user", "content": text, "timestamp": 100},
        {"role": "assistant", "content": helpers._redact_fn_uncached(marked), "timestamp": 101},
    ]
    assert "ghp_" + "A" * 40 not in json.dumps(share)
