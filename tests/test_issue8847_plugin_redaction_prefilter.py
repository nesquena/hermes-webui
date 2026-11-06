"""#8847: plugin-registered redaction patterns must reach the Agent redactor.

``_redact_text`` skips the redactor when ``_might_contain_sensitive_text``
finds none of WebUI's fixed markers. ``agent.redact.register_redaction_patterns()``
(reached from plugins through ``PluginContext.register_redaction_patterns``)
adds token shapes the Agent then masks, but WebUI's marker list does not know
them, so text such as ``"plain words nvapi-<60 chars> end"`` reached session
payloads, SSE and shares in clear while the Agent alone masked it.

Fix (fail closed): while the Agent has any plugin-registered pattern, the
prefilter shortcut is skipped and every string goes to the redactor. Whether
patterns exist is read when #8863's policy fingerprint (``_PREFIX_RE`` /
``_PREFIX_SUBSTRINGS`` identity) changes, so it is invalidated by the same
mechanism as the small-string memo; there is no second scheme.

Contract verified here:
  1. real agent: a registered shape in marker-free text is masked through
     ``_redact_text`` and ``redact_session_data``;
  2. real agent: a string redacted (as clean) BEFORE the registration is
     masked afterwards, and the fast path returns after a registry reset;
  3. without plugin patterns the shortcut still skips the redactor for plain
     text (no agent call, no registry walk, bounded policy reads), and the
     same harness fails closed once a pattern is registered;
  4. Agents that predate the registry, without the policy globals, with the
     legacy signature, and no Agent at all keep today's fast path;
  5. an Agent where plugins can register (``register_redaction_patterns``)
     but the registry name is absent (renamed) fails closed: every string goes
     to the redactor. A registry that is present but empty keeps the fast
     path.
"""
import functools
import socket
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402

_TOKEN = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0"
_PATTERN = r"nvapi-[A-Za-z0-9]{60}"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("network disabled in test_issue8847")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def _isolate_memos():
    helpers._redact_fn_lru.cache_clear()
    helpers._might_contain_sensitive_text_lru.cache_clear()
    yield
    helpers._redact_fn_lru.cache_clear()
    helpers._might_contain_sensitive_text_lru.cache_clear()


def _session_content(text):
    out = helpers.redact_session_data({"messages": [{"role": "assistant", "content": text}]})
    return out["messages"][0]["content"]


def _redact_fn_cached_spy():
    """``(calls, spy)``: the spy records every string ``_redact_text`` hands
    to the redactor, then delegates to the real ``_redact_fn_cached``."""
    calls = []
    real = helpers._redact_fn_cached

    def spy(text):
        calls.append(text)
        return real(text)

    return calls, spy


def _spy_redact_fn_cached(monkeypatch):
    calls, spy = _redact_fn_cached_spy()
    monkeypatch.setattr(helpers, "_redact_fn_cached", spy)
    return calls


# ── 1/2. Real agent registry ────────────────────────────────────────────────


def _bound_agent_redactor():
    for cell in getattr(helpers._redact_fn_uncached, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and getattr(value, "__name__", "") == "redact_sensitive_text":
            return value
    return None


@pytest.fixture
def real_registry():
    redact = pytest.importorskip("agent.redact", reason="hermes-agent not installed")
    if not hasattr(redact, "register_redaction_patterns"):
        pytest.skip("installed agent.redact has no runtime pattern registry")
    if helpers._redact_fn_uncached.__name__ != "_combined_redact":
        pytest.skip("api.helpers was built without the agent redactor")
    if getattr(_bound_agent_redactor(), "__globals__", None) is not vars(redact):
        pytest.skip("api.helpers is bound to a different agent.redact than the installed one")
    reset = getattr(redact, "_reset_plugin_redaction_patterns", None)
    if reset is None:
        pytest.skip("installed agent.redact has no registry reset seam")
    reset()
    yield redact
    reset()


def _register_through_plugin_api(redact, patterns):
    try:
        from hermes_cli.plugins import PluginContext
    except ImportError:
        return redact.register_redaction_patterns(patterns, source="plugin:test-8847")
    ctx = PluginContext.__new__(PluginContext)
    ctx.manifest = types.SimpleNamespace(name="test-8847")
    return PluginContext.register_redaction_patterns(ctx, patterns)


def test_real_agent_plugin_pattern_in_marker_free_text_is_redacted(real_registry):
    text = "plain words " + _TOKEN + " end"
    assert helpers._might_contain_sensitive_text(text) is False, "probe must carry no WebUI marker"

    assert _register_through_plugin_api(real_registry, [_PATTERN]) == 1
    assert _TOKEN not in real_registry.redact_sensitive_text(text, force=True), "agent alone must mask"
    oracle = helpers._redact_fn_uncached(text)
    assert _TOKEN not in oracle

    assert helpers._redact_text(text, _enabled=True) == oracle
    assert _session_content(text) == oracle


def test_real_agent_string_seen_before_registration_is_redacted_after(real_registry, monkeypatch):
    calls = _spy_redact_fn_cached(monkeypatch)
    text = "earlier words " + _TOKEN + " end"

    # Before any plugin pattern: the shortcut serves it, the verdict is memoized.
    assert helpers._redact_text(text, _enabled=True) is text
    assert _session_content(text) is text
    assert calls == []
    hits = helpers._might_contain_sensitive_text_lru.cache_info().hits
    assert helpers._might_contain_sensitive_text(text) is False
    assert helpers._might_contain_sensitive_text_lru.cache_info().hits > hits

    assert _register_through_plugin_api(real_registry, [_PATTERN]) == 1

    redacted = helpers._redact_text(text, _enabled=True)
    assert _TOKEN not in redacted
    assert redacted == helpers._redact_fn_uncached(text)
    assert _TOKEN not in _session_content(text)
    # Fail closed: every string of the payload, the marker-free role too, now
    # goes to the redactor.
    assert calls == [text, "assistant", text]

    # A registry reset rebinds the fingerprint again: back to the fast path.
    real_registry._reset_plugin_redaction_patterns()
    other = "later words " + _TOKEN + " end"
    assert helpers._redact_text(other, _enabled=True) is other
    assert _session_content(other) is other
    assert calls == [text, "assistant", text]


# ── 3. No-plugin fast path (stub agent: runs without hermes-agent) ──────────

# Mirrors the real registry: redact_sensitive_text reads the module globals at
# call time; register mutates the registry, then rebinds _PREFIX_RE and
# _PREFIX_SUBSTRINGS (#8863's fingerprint).
_STUB_SOURCE = r'''
import re

_BUILTIN = r"zzbuiltin_[A-Za-z0-9]{12,}"
_PLUGIN_PREFIX_PATTERNS = REGISTRY_FACTORY()
CALLS = []


def _compile(patterns):
    return re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(patterns) + r")(?![A-Za-z0-9_-])")


_PREFIX_RE = _compile([_BUILTIN])
_PREFIX_SUBSTRINGS = ("zzbuiltin_",)


def redact_sensitive_text(text, force=False):
    CALLS.append(text)
    if any(p in text for p in _PREFIX_SUBSTRINGS):
        text = _PREFIX_RE.sub("[stub-redacted]", text)
    return text


def _rebuild(plugin_patterns):
    global _PREFIX_RE, _PREFIX_SUBSTRINGS
    combined = [_BUILTIN] + list(plugin_patterns)
    _PREFIX_RE = _compile(combined)
    _PREFIX_SUBSTRINGS = tuple(p.split("[", 1)[0] for p in combined)


def register_redaction_patterns(patterns, source="plugin"):
    _PLUGIN_PREFIX_PATTERNS.extend(patterns)
    _rebuild(_PLUGIN_PREFIX_PATTERNS)
    return len(patterns)


def _reset_plugin_redaction_patterns():
    _PLUGIN_PREFIX_PATTERNS.clear()
    _rebuild([])
'''

_STUB_TOKEN = "acme_live_" + "Q7r8S9t0U1v2W3x4"
_STUB_PATTERN = r"acme_live_[A-Za-z0-9]{16}"


class _CountingList(list):
    """Registry that counts every walk (iteration, len, truth test)."""

    walks = 0

    def __iter__(self):
        self.walks += 1
        return super().__iter__()

    def __len__(self):
        self.walks += 1
        return super().__len__()


class _CountingNamespace(dict):
    def __init__(self, target):
        super().__init__()
        self.target = target
        self.reads = 0

    def get(self, key, default=None):
        self.reads += 1
        return self.target.get(key, default)


def _stub_agent_module(source=_STUB_SOURCE, registry_factory=list):
    module = types.ModuleType("agent.redact")
    module.REGISTRY_FACTORY = registry_factory
    exec(compile(source, "<stub agent.redact>", "exec"), module.__dict__)
    return module


def _build_with(module):
    stub_modules = {"agent": types.ModuleType("agent"), "agent.redact": module}
    with mock.patch.dict(sys.modules, stub_modules):
        return helpers._build_redact_fn()


def _plain_texts(n, tag):
    return [f"{tag} plain words {i} " + _STUB_TOKEN + " end" for i in range(n)]


def test_no_plugin_fast_path_skips_agent_and_registration_fails_closed():
    module = _stub_agent_module(registry_factory=_CountingList)
    redact_fn = _build_with(module)
    counting_ns = _CountingNamespace(module.__dict__)
    redact_fn._redaction_policy_ns = counting_ns
    lru = functools.lru_cache(maxsize=64)(redact_fn)
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", lru
    ):
        assert helpers._redact_text("warm-up", _enabled=True) == "warm-up"  # first sync
        plain = _plain_texts(200, "before")
        assert not any(helpers._might_contain_sensitive_text(t) for t in plain)
        module._PLUGIN_PREFIX_PATTERNS.walks = 0
        counting_ns.reads = 0

        # No plugin pattern: unchanged fast path. The agent is never called, the
        # registry is never walked, the policy check costs <= 2 namespace reads.
        assert all(helpers._redact_text(t, _enabled=True) is t for t in plain)
        assert module.CALLS == []
        assert lru.cache_info().misses == 0
        assert module._PLUGIN_PREFIX_PATTERNS.walks == 0
        assert counting_ns.reads <= 2 * len(plain)

        # A registration rebinds the fingerprint: the very next plain string goes
        # to the agent, including one already seen as clean.
        assert module.register_redaction_patterns([_STUB_PATTERN]) == 1
        for text in (plain[0], _plain_texts(1, "after")[0]):
            redacted = helpers._redact_text(text, _enabled=True)
            assert _STUB_TOKEN not in redacted and "[stub-redacted]" in redacted
            assert _STUB_TOKEN not in _session_content(text)
        # Every string reaches the agent once (the session's role string too),
        # then the policy-scoped memo serves it.
        assert module.CALLS == [plain[0], "assistant", _plain_texts(1, "after")[0]]

        # Reset rebinds it again: back to the shortcut, no further agent calls.
        module._reset_plugin_redaction_patterns()
        calls = len(module.CALLS)
        again = _plain_texts(50, "reset")
        assert all(helpers._redact_text(t, _enabled=True) is t for t in again)
        assert len(module.CALLS) == calls


def test_dict_registry_shape_also_fails_closed():
    """The current Agent keys the registry by source (a dict); the first shape
    was a list. Both are truthy exactly when a pattern is registered."""
    source = _STUB_SOURCE.replace(
        "    _PLUGIN_PREFIX_PATTERNS.extend(patterns)\n    _rebuild(_PLUGIN_PREFIX_PATTERNS)",
        "    _PLUGIN_PREFIX_PATTERNS.setdefault(source, []).extend(patterns)\n"
        "    _rebuild([p for ps in _PLUGIN_PREFIX_PATTERNS.values() for p in ps])",
    )
    assert source != _STUB_SOURCE
    module = _stub_agent_module(source, registry_factory=dict)
    redact_fn = _build_with(module)
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ):
        text = "plain words " + _STUB_TOKEN + " end"
        assert helpers._redact_text(text, _enabled=True) is text
        assert module.CALLS == []
        module.register_redaction_patterns([_STUB_PATTERN], source="plugin:x")
        assert _STUB_TOKEN not in helpers._redact_text(text, _enabled=True)
        assert module.CALLS == [text]


class _RegisterAfterRegistryRead(dict):
    """Policy namespace that lands a registration right after the registry is
    read: the window between the reads of one policy snapshot."""

    def __init__(self, module):
        super().__init__()
        self.module = module
        self.armed = False

    def get(self, key, default=None):
        value = self.module.__dict__.get(key, default)
        if key == "_PLUGIN_PREFIX_PATTERNS" and self.armed:
            self.armed = False
            value = type(value)(value)  # what the read saw, before the registration
            self.module.register_redaction_patterns([_STUB_PATTERN])
        return value


def test_registration_racing_the_policy_snapshot_is_not_pinned():
    """The registry is read after the fingerprint, so a registration landing
    between the reads leaves an old fingerprint in the state and the next call
    resyncs. Read the other way round, the state would pair the NEW
    fingerprint with an empty registry and keep the shortcut until the next
    registration."""
    module = _stub_agent_module()
    redact_fn = _build_with(module)
    namespace = _RegisterAfterRegistryRead(module)
    redact_fn._redaction_policy_ns = namespace
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ):
        text = _plain_texts(1, "race")[0]
        assert helpers._redact_text(text, _enabled=True) is text  # synced, no plugins
        module._reset_plugin_redaction_patterns()  # moves the fingerprint: next call resyncs
        namespace.armed = True
        helpers._redact_text(text, _enabled=True)  # this call races the registration
        assert not namespace.armed, "the race was not exercised"
        assert module._PLUGIN_PREFIX_PATTERNS == [_STUB_PATTERN]
        assert _STUB_TOKEN not in helpers._redact_text(text, _enabled=True)


# ── 4. Old agents and no agent ─────────────────────────────────────────────

# Rebinds the #8863 fingerprint like a registry would and keeps
# register_redaction_patterns, but the registry has another name: what WebUI
# sees if the Agent renames its private registry. Fails closed (section 5).
_NO_REGISTRY_ATTR_SOURCE = _STUB_SOURCE.replace("_PLUGIN_PREFIX_PATTERNS", "_OTHER_NAME")

_PRE_REGISTRY_SOURCE = r'''
import re
_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_-])(zzbuiltin_[A-Za-z0-9]{12,})(?![A-Za-z0-9_-])")
CALLS = []


def redact_sensitive_text(text, force=False):
    CALLS.append(text)
    return _PREFIX_RE.sub("[old-redacted]", text)
'''

_LEGACY_SIGNATURE_SOURCE = r'''
CALLS = []


def redact_sensitive_text(text):
    CALLS.append(text)
    return text.replace("zzbuiltin_", "[legacy]")
'''


def _old_agent_shapes():
    yield "pre_registry_agent", _stub_agent_module(_PRE_REGISTRY_SOURCE)
    yield "legacy_signature_agent", _stub_agent_module(_LEGACY_SIGNATURE_SOURCE)
    yield "agent_without_redactor", types.ModuleType("agent.redact")
    yield "agent_import_fails", None


def test_agents_without_plugin_registry_keep_todays_fast_path():
    assert "_PLUGIN_PREFIX_PATTERNS" not in _NO_REGISTRY_ATTR_SOURCE
    for shape, module in _old_agent_shapes():
        redact_fn = _build_with(module)
        expected = "_fallback_redact" if shape in {"agent_without_redactor", "agent_import_fails"} else (
            "_combined_redact"
        )
        assert redact_fn.__name__ == expected, shape
        calls, spy = _redact_fn_cached_spy()
        with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
            helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
        ), mock.patch.object(helpers, "_redact_fn_cached", spy):
            plain = _plain_texts(3, shape)
            assert all(helpers._redact_text(t, _enabled=True) is t for t in plain), shape
            if hasattr(module, "register_redaction_patterns"):
                # The fingerprint moves, but nothing says plugin patterns exist.
                module.register_redaction_patterns([_STUB_PATTERN])
                assert all(helpers._redact_text(t, _enabled=True) is t for t in plain), shape
            assert calls == [], shape
            assert getattr(module, "CALLS", []) == [], shape
            marked = "see https://example.invalid/ " + _STUB_TOKEN
            assert helpers._redact_text(marked, _enabled=True) == redact_fn(marked), shape

    # Control: the same Agent once it does expose a populated registry. The
    # attribute is the only switch, and the harness above would see it.
    module = _stub_agent_module()
    redact_fn = _build_with(module)
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ):
        text = _plain_texts(1, "control")[0]
        assert helpers._redact_text(text, _enabled=True) is text
        module.register_redaction_patterns([_STUB_PATTERN])
        assert _STUB_TOKEN not in helpers._redact_text(text, _enabled=True)


# ── 5. Registry name absent vs present-but-empty ───────────────────────────


def _register(patterns, source="plugin"):
    return len(patterns)


def _policy_state_from(namespace):
    def redact_fn(text):
        return text

    if namespace is not None:
        redact_fn._redaction_policy_ns = namespace
    return helpers._redaction_policy_state_for(functools.lru_cache(maxsize=4)(redact_fn))


@pytest.mark.parametrize(
    "names, expected",
    [
        ({"_PLUGIN_PREFIX_PATTERNS": [_STUB_PATTERN], "register_redaction_patterns": _register}, True),
        ({"_PLUGIN_PREFIX_PATTERNS": {"plugin:x": [_STUB_PATTERN]}}, True),
        ({"_PLUGIN_PREFIX_PATTERNS": [], "register_redaction_patterns": _register}, False),
        ({"_PLUGIN_PREFIX_PATTERNS": {}, "register_redaction_patterns": _register}, False),
        ({"register_redaction_patterns": _register}, True),  # registry unreadable: fail closed
        ({"_PREFIX_RE": None}, False),  # predates the registry: cannot register
        (None, False),  # no policy namespace at all (_NO_REDACTION_POLICY)
    ],
    ids=["populated", "populated-no-register", "empty-list", "empty-dict", "renamed", "pre-registry", "none"],
)
@pytest.mark.parametrize("view", [dict, types.MappingProxyType])
def test_plugin_bit_tells_absent_registry_from_empty(names, expected, view):
    namespace = None if names is None else view(dict(names))
    assert _policy_state_from(namespace)[4] is expected


@pytest.mark.parametrize("view", [None, types.MappingProxyType], ids=["module_globals", "mappingproxy"])
def test_registry_renamed_but_registration_possible_fails_closed(view):
    """Plugins can register, but WebUI can't read the registry: it can't know
    whether patterns exist, so marker-free text goes to the Agent anyway."""
    module = _stub_agent_module(_NO_REGISTRY_ATTR_SOURCE)
    assert "_PLUGIN_PREFIX_PATTERNS" not in vars(module) and callable(module.register_redaction_patterns)
    redact_fn = _build_with(module)
    assert redact_fn._redaction_policy_ns is vars(module)
    if view is not None:
        redact_fn._redaction_policy_ns = view(vars(module))
    calls, spy = _redact_fn_cached_spy()
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ), mock.patch.object(helpers, "_redact_fn_cached", spy):
        text = _plain_texts(1, "renamed")[0]
        assert helpers._might_contain_sensitive_text(text) is False

        # Before any registration: plain text with no WebUI marker is sent.
        assert helpers._redact_text(text, _enabled=True) == text
        assert calls == [text]
        assert module.CALLS == [text]

        # After one, the plugin shape is masked, in text served before too.
        assert module.register_redaction_patterns([_STUB_PATTERN]) == 1
        for probe in (text, _plain_texts(1, "renamed-after")[0]):
            redacted = helpers._redact_text(probe, _enabled=True)
            assert _STUB_TOKEN not in redacted and "[stub-redacted]" in redacted
            assert _STUB_TOKEN not in _session_content(probe)

        # A reset doesn't make the registry readable: still fail closed.
        module._reset_plugin_redaction_patterns()
        other = _plain_texts(1, "renamed-reset")[0]
        seen = len(module.CALLS)
        assert helpers._redact_text(other, _enabled=True) == other
        assert module.CALLS[seen:] == [other]


@pytest.mark.parametrize("registry_factory", [list, dict])
@pytest.mark.parametrize("view", [None, types.MappingProxyType], ids=["module_globals", "mappingproxy"])
def test_present_but_empty_registry_keeps_plain_text_fast_path(registry_factory, view):
    module = _stub_agent_module(registry_factory=registry_factory)
    registry = module._PLUGIN_PREFIX_PATTERNS
    assert type(registry) is registry_factory and not registry
    assert callable(module.register_redaction_patterns)
    redact_fn = _build_with(module)
    if view is not None:
        redact_fn._redaction_policy_ns = view(vars(module))
    calls, spy = _redact_fn_cached_spy()
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ), mock.patch.object(helpers, "_redact_fn_cached", spy):
        plain = _plain_texts(20, "empty")
        assert not any(helpers._might_contain_sensitive_text(t) for t in plain)
        assert all(helpers._redact_text(t, _enabled=True) is t for t in plain)
        assert _session_content(plain[0]) is plain[0]
        assert calls == []
        assert module.CALLS == []
        assert helpers._current_redaction_policy()[4] is False
