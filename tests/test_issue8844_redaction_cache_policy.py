"""#8844: the small-string redaction memo must follow the agent's runtime policy.

``agent.redact.register_redaction_patterns()`` (reached from plugins through
``PluginContext.register_redaction_patterns``) extends the redactor at runtime
by rebinding the ``agent.redact`` globals ``_PREFIX_RE`` and
``_PREFIX_SUBSTRINGS``. ``_redact_fn_lru`` memoized strings of up to
``_REDACT_CACHE_MAX_TEXT_LEN`` characters by text alone, so a short string
redacted BEFORE a registration kept being served with the newly-registered
secret intact afterwards. Large strings already fail closed (see
tests/test_redact_large_string_cache.py).

Contract verified here:
  1. real agent: prime a short string, register through the real plugin API,
     and the next ``_redact_text`` / ``redact_session_data`` result is redacted;
  2. the prefilter verdict memo is policy-independent: after a registration it
     still agrees with a fresh scan;
  3. a stub ``agent.redact`` whose policy globals are swapped by a fake
     ``register`` proves the same without hermes-agent installed (CI);
  4. old agents (no registry, legacy signature) and no agent keep today's
     memo behaviour, without exceptions;
  5. a request computing under the old policy while a registration lands
     cannot repopulate the memo with its stale result;
  6. the warm hot path does no regex compilation, registry walk or redactor
     call; only a bounded number of policy-namespace reads.

The stub tests never reload ``api.helpers`` (#8845): they build the redactor
with ``_build_redact_fn()`` while the stub is in ``sys.modules`` and swap it
in with ``mock.patch.object``.
"""
import functools
import re
import sys
import threading
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402

# A marker the WebUI prefilter already trips on ("://"), so these strings
# reach the redactor before and after registration.
_URL = "see https://example.invalid/docs "


@pytest.fixture(autouse=True)
def _isolate_production_memo():
    """Start and finish with an empty production small-string memo."""
    helpers._redact_fn_lru.cache_clear()
    yield
    helpers._redact_fn_lru.cache_clear()


# ── 1/2. Real agent registry ────────────────────────────────────────────────

_REAL_TOKEN = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0"
_REAL_PATTERN = r"nvapi-[A-Za-z0-9]{60}"


@pytest.fixture
def real_registry():
    redact = pytest.importorskip("agent.redact", reason="hermes-agent not installed")
    if not hasattr(redact, "register_redaction_patterns"):
        pytest.skip("installed agent.redact has no runtime pattern registry")
    if helpers._redact_fn_uncached.__name__ != "_combined_redact":
        pytest.skip("api.helpers was built without the agent redactor")
    bound = _bound_agent_redactor()
    if getattr(bound, "__globals__", None) is not vars(redact):
        # A stubbed-agent test reloaded api.helpers under a stub (#8845); the
        # production objects no longer redact through this registry.
        pytest.skip("api.helpers is bound to a different agent.redact than the installed one")
    reset = getattr(redact, "_reset_plugin_redaction_patterns", None)
    if reset is None:
        pytest.skip("installed agent.redact has no registry reset seam")
    reset()
    yield redact
    reset()


def _bound_agent_redactor():
    """The ``redact_sensitive_text`` captured by ``_combined_redact``, if any."""
    for cell in getattr(helpers._redact_fn_uncached, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and getattr(value, "__name__", "") == "redact_sensitive_text":
            return value
    return None


def _register_through_plugin_api(redact, patterns):
    """Register as a plugin does; fall back to the agent API it wraps."""
    try:
        from hermes_cli.plugins import PluginContext
    except ImportError:
        return redact.register_redaction_patterns(patterns, source="plugin:test-8844")
    ctx = PluginContext.__new__(PluginContext)
    ctx.manifest = types.SimpleNamespace(name="test-8844")
    return PluginContext.register_redaction_patterns(ctx, patterns)


def _session_content(text):
    out = helpers.redact_session_data({"messages": [{"role": "assistant", "content": text}]})
    return out["messages"][0]["content"]


def test_real_agent_registration_invalidates_primed_short_string(real_registry):
    text = _URL + _REAL_TOKEN + " tail"
    assert len(text) <= helpers._REDACT_CACHE_MAX_TEXT_LEN
    assert helpers._might_contain_sensitive_text(text)

    # Prime the memo the way dashboard polls / SSE / shares do, and prove the
    # second read really is a memo hit (otherwise nothing stale could exist).
    primed = helpers._redact_text(text, _enabled=True)
    hits = helpers._redact_fn_lru.cache_info().hits
    assert _session_content(text) == primed
    assert helpers._redact_fn_lru.cache_info().hits > hits
    assert _REAL_TOKEN in primed, "the installed agent already knows the probe token shape"

    assert _register_through_plugin_api(real_registry, [_REAL_PATTERN]) == 1

    oracle = helpers._redact_fn_uncached(text)
    assert _REAL_TOKEN not in oracle, "the registered pattern is not effective"
    assert helpers._redact_text(text, _enabled=True) == oracle
    assert _session_content(text) == oracle
    # The memo still works under the new policy.
    hits = helpers._redact_fn_lru.cache_info().hits
    assert helpers._redact_text(text, _enabled=True) == oracle
    assert helpers._redact_fn_lru.cache_info().hits > hits


def test_real_agent_registry_reset_is_also_followed(real_registry):
    """Registry teardown rebinds the same globals; the memo follows it too."""
    text = _URL + _REAL_TOKEN
    assert _register_through_plugin_api(real_registry, [_REAL_PATTERN]) == 1
    assert _REAL_TOKEN not in helpers._redact_text(text, _enabled=True)
    real_registry._reset_plugin_redaction_patterns()
    assert helpers._redact_text(text, _enabled=True) == helpers._redact_fn_uncached(text)


def test_prefilter_verdict_memo_is_policy_independent(real_registry):
    """The prefilter consults only WebUI's fixed markers, so its memo cannot go
    stale on registration. Guard it: after a registration the memoized verdict
    must equal a fresh scan, for both a marker-bearing and a token-only text."""
    texts = (_URL + _REAL_TOKEN, "plain " + _REAL_TOKEN)
    before = [helpers._might_contain_sensitive_text(t) for t in texts]
    hits = helpers._might_contain_sensitive_text_lru.cache_info().hits
    assert [helpers._might_contain_sensitive_text(t) for t in texts] == before
    assert helpers._might_contain_sensitive_text_lru.cache_info().hits - hits == len(texts)
    assert before == [True, False], "the probe texts no longer exercise both verdicts"
    assert _register_through_plugin_api(real_registry, [_REAL_PATTERN]) == 1
    after = [helpers._might_contain_sensitive_text(t) for t in texts]
    assert after == [helpers._might_contain_sensitive_text_uncached(t) for t in texts]
    assert after == before


# ── 3. Stub agent: CI coverage without hermes-agent ────────────────────────

# Mirrors the real registry shape: redact_sensitive_text reads module globals
# at call time; register rebinds _PREFIX_RE first and _PREFIX_SUBSTRINGS second.
_STUB_SOURCE = r'''
import re

_BUILTIN = r"zzbuiltin_[A-Za-z0-9]{12,}"
_PLUGIN_PREFIX_PATTERNS = []


def _compile(patterns):
    return re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(patterns) + r")(?![A-Za-z0-9_-])")


_PREFIX_RE = _compile([_BUILTIN])
_PREFIX_SUBSTRINGS = ("zzbuiltin_",)
CALLS = []
GATE = {}


def redact_sensitive_text(text, force=False):
    CALLS.append(text)
    prefix_re, substrings = _PREFIX_RE, _PREFIX_SUBSTRINGS  # snapshot, like one call does
    gate = GATE.pop(text, None)
    if gate is not None:
        gate["barrier"].wait()  # tell the test: the OLD policy is snapshotted
        gate["release"].wait(5)
    if any(p in text for p in substrings):
        text = prefix_re.sub("[stub-redacted]", text)
    return text


def register_redaction_patterns(patterns, source="plugin"):
    global _PREFIX_RE, _PREFIX_SUBSTRINGS
    _PLUGIN_PREFIX_PATTERNS.extend(patterns)
    combined = [_BUILTIN] + _PLUGIN_PREFIX_PATTERNS
    _PREFIX_RE = _compile(combined)
    _PREFIX_SUBSTRINGS = tuple(p.split("[", 1)[0] for p in combined)
    return len(patterns)
'''

_STUB_TOKEN = "acme_live_" + "Q7r8S9t0U1v2W3x4"
_STUB_PATTERN = r"acme_live_[A-Za-z0-9]{16}"


def _stub_agent_module(source=_STUB_SOURCE):
    module = types.ModuleType("agent.redact")
    exec(compile(source, "<stub agent.redact>", "exec"), module.__dict__)
    return module


def _build_with_stub(module):
    stub_modules = {"agent": types.ModuleType("agent"), "agent.redact": module}
    with mock.patch.dict(sys.modules, stub_modules):
        return helpers._build_redact_fn()


@pytest.fixture
def stub_agent():
    """Yield the stub module with api.helpers redacting through it (no reload)."""
    module = _stub_agent_module()
    redact_fn = _build_with_stub(module)
    assert redact_fn.__name__ == "_combined_redact"
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ):
        yield module


def test_stub_registration_invalidates_primed_short_string(stub_agent):
    text = _URL + _STUB_TOKEN + " tail"
    assert helpers._might_contain_sensitive_text(text)

    assert helpers._redact_text(text, _enabled=True) == text
    assert helpers._redact_text(text, _enabled=True) == text
    assert stub_agent.CALLS.count(text) == 1, "the second call must be a memo hit"

    assert stub_agent.register_redaction_patterns([_STUB_PATTERN]) == 1

    redacted = helpers._redact_text(text, _enabled=True)
    assert _STUB_TOKEN not in redacted
    assert "[stub-redacted]" in redacted
    assert _STUB_TOKEN not in _session_content(text)
    # One recompute under the new policy, then memo hits again.
    assert stub_agent.CALLS.count(text) == 2


def test_stub_partial_swap_window_is_not_pinned(stub_agent):
    """Registration rebinds _PREFIX_RE before _PREFIX_SUBSTRINGS. A call landing
    between the two sees a half-built policy; it must not be served once the
    second rebinding completes."""
    text = _URL + _STUB_TOKEN
    assert helpers._redact_text(text, _enabled=True) == text
    combined = [r"zzbuiltin_[A-Za-z0-9]{12,}", _STUB_PATTERN]
    stub_agent._PREFIX_RE = stub_agent._compile(combined)  # first half only
    assert helpers._redact_text(text, _enabled=True) == text  # agent pre-screen still old
    stub_agent._PREFIX_SUBSTRINGS = ("zzbuiltin_", "acme_live_")  # second half
    assert _STUB_TOKEN not in helpers._redact_text(text, _enabled=True)


def test_stub_patch_restore_leaves_production_memo_consistent():
    """A rotation inside a patched test must not leave a stale memo behind."""
    production_lru = helpers._redact_fn_lru
    module = _stub_agent_module()
    redact_fn = _build_with_stub(module)
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ):
        text = _URL + _STUB_TOKEN
        helpers._redact_text(text, _enabled=True)
        module.register_redaction_patterns([_STUB_PATTERN])
        assert _STUB_TOKEN not in helpers._redact_text(text, _enabled=True)
    assert helpers._redact_fn_lru is production_lru
    sample = _URL + "gh" + "p_" + "0123456789abcdefghijklmnopqrstuvwxyzAB"
    assert helpers._redact_text(sample, _enabled=True) == helpers._redact_fn_uncached(sample)
    assert helpers._redact_text(_URL + _STUB_TOKEN, _enabled=True) == helpers._redact_fn_uncached(
        _URL + _STUB_TOKEN
    )


# ── 4. Old agents and no agent ─────────────────────────────────────────────

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


@pytest.mark.parametrize(
    "shape",
    ["pre_registry_agent", "legacy_signature_agent", "agent_without_redactor", "agent_import_fails"],
)
def test_old_agent_and_no_agent_keep_todays_memo(shape):
    if shape == "agent_import_fails":
        stub = None  # ``import agent.redact`` raises ImportError
    elif shape == "agent_without_redactor":
        stub = types.ModuleType("agent.redact")
    else:
        source = _PRE_REGISTRY_SOURCE if shape == "pre_registry_agent" else _LEGACY_SIGNATURE_SOURCE
        stub = _stub_agent_module(source)
    with mock.patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": stub}):
        redact_fn = helpers._build_redact_fn()
    expected_name = "_combined_redact" if shape in {"pre_registry_agent", "legacy_signature_agent"} else "_fallback_redact"
    assert redact_fn.__name__ == expected_name

    lru = functools.lru_cache(maxsize=64)(redact_fn)
    texts = [_URL + "zzbuiltin_" + "A" * 16, _URL + "gh" + "p_" + "0123456789abcdefghijklmnop", _URL + "plain"]
    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", lru
    ):
        first = [helpers._redact_text(t, _enabled=True) for t in texts]
        second = [helpers._redact_text(t, _enabled=True) for t in texts]
        assert first == second == [redact_fn(t) for t in texts]
        info = helpers._redact_fn_lru.cache_info()
        assert helpers._redact_fn_lru is lru, "a constant policy must never rotate the memo"
        assert info.misses == len(texts) and info.hits == len(texts)
        if stub is not None and hasattr(stub, "CALLS"):
            assert len(stub.CALLS) == len(texts) * 2  # memo calls + the oracle calls above


# ── 5. Concurrency ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("trigger", ["same_text", "other_text"])
def test_in_flight_old_policy_result_is_not_served_after_registration(stub_agent, trigger):
    """Thread A computes TEXT under the old policy and is held inside the agent
    redactor (after it snapshotted the policy) while a registration lands.

    ``same_text``: A finishes and publishes its result first; the first
    post-registration call must not be served that stale result.
    ``other_text``: a post-registration call on a DIFFERENT string observes
    the new policy first, then A publishes; a later call on TEXT must still
    not be served A's stale result (an in-place cache_clear() fails here)."""
    text = _URL + _STUB_TOKEN + " in-flight"
    other = _URL + _STUB_TOKEN + " other"
    barrier = threading.Barrier(2, timeout=5)
    release = threading.Event()
    stub_agent.GATE[text] = {"barrier": barrier, "release": release}
    results = {}

    def compute_under_old_policy():
        results["a"] = helpers._redact_text(text, _enabled=True)

    worker = threading.Thread(target=compute_under_old_policy, daemon=True)
    worker.start()
    try:
        barrier.wait()  # A is inside the agent redactor holding the OLD policy
        assert stub_agent.register_redaction_patterns([_STUB_PATTERN]) == 1
        if trigger == "other_text":
            assert _STUB_TOKEN not in helpers._redact_text(other, _enabled=True)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results["a"] == text, "precondition: A really computed under the old policy"

    after = helpers._redact_text(text, _enabled=True)
    assert _STUB_TOKEN not in after, "a pre-registration result was served after registration"
    assert _STUB_TOKEN not in helpers._redact_text(text, _enabled=True)


# ── 6. Hot-path cost ───────────────────────────────────────────────────────


class _CountingNamespace(dict):
    """Policy namespace view that counts reads and forwards to the real module."""

    def __init__(self, target):
        super().__init__()
        self.target = target
        self.reads = 0

    def get(self, key, default=None):
        self.reads += 1
        return self.target.get(key, default)

    def __getitem__(self, key):
        self.reads += 1
        return self.target[key]


class _CountingList(list):
    def __init__(self, *args):
        super().__init__(*args)
        self.walks = 0

    def __iter__(self):
        self.walks += 1
        return super().__iter__()

    def __len__(self):
        self.walks += 1
        return super().__len__()


def test_warm_hot_path_does_no_compile_registry_walk_or_redactor_call():
    module = _stub_agent_module()
    module._PLUGIN_PREFIX_PATTERNS = _CountingList(module._PLUGIN_PREFIX_PATTERNS)
    redact_fn = _build_with_stub(module)
    counting_ns = _CountingNamespace(module.__dict__)
    redact_fn._redaction_policy_ns = counting_ns
    module.register_redaction_patterns([_STUB_PATTERN])
    text = _URL + _STUB_TOKEN
    calls = 2000
    real_compile = re._compile
    compiles = []

    def counting_compile(*args, **kwargs):
        compiles.append(args[:1])
        return real_compile(*args, **kwargs)

    with mock.patch.object(helpers, "_redact_fn_uncached", redact_fn), mock.patch.object(
        helpers, "_redact_fn_lru", functools.lru_cache(maxsize=64)(redact_fn)
    ):
        warm = helpers._redact_fn_cached(text)
        assert _STUB_TOKEN not in warm
        redactor_calls = len(module.CALLS)
        module._PLUGIN_PREFIX_PATTERNS.walks = 0
        counting_ns.reads = 0
        hits = helpers._redact_fn_lru.cache_info().hits
        with mock.patch.object(re, "_compile", counting_compile):
            for _ in range(calls):
                assert helpers._redact_fn_cached(text) == warm
        assert compiles == []
        assert module._PLUGIN_PREFIX_PATTERNS.walks == 0
        assert len(module.CALLS) == redactor_calls
        assert helpers._redact_fn_lru.cache_info().hits - hits == calls
        assert counting_ns.reads <= 2 * calls, "more than two policy reads per call"
