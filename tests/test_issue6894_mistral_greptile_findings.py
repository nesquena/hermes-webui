"""Regression tests for the four Greptile inline findings on PR #8664.

Round 4 of #8664 (review 2026-11-04 02:35): four inline comments on
``api/config.py`` / ``api/routes.py``, all about the ONE canonical/legacy
record-folding logic. Each one is reproduced here against the REAL
production functions, driven by a REAL config swap and — where the finding
is about resolver interaction — the REAL installed resolver
(``hermes_cli.runtime_provider``), exactly like the maintainer's replay
harness did. No hand-written approximation of the resolver is involved.

The four findings:

  1. P2 "Matching connections get rebuilt" — the trailing comma made
     ``resolved_mode_str`` a 1-tuple, so it never equalled a mode string
     and ``mode_split`` was ALWAYS true. A connection that already matched
     its owner was needlessly rebuilt (extra credential-pool lookup).
  2. P1 "Chat uses the wrong format" — the owner omits ``api_mode``, both
     records share URL and key, and the sibling declares
     ``anthropic_messages``: all three split checks were false, so the
     client kept the sibling's wire format instead of the owner's own
     effective mode.
  3. P1 "Legacy credentials get skipped" — the single-record repair
     recognised only an inline ``api_key``; a record declaring ``key_env``
     or ``key_cmd`` was never repaired, so chat proceeded without the
     record's credential.
  4. P2 "Live models lose their key" — ``_handle_live_models`` read
     ``providers[provider]`` by EXACT spelling; the canonical fold made it
     miss ``providers.mistralai.api_key``, so the authenticated fetch was
     skipped and the user got the two static models.

The resolver-driven tests (everything that requests the ``native`` fixture)
SKIP when the installed Hermes agent is not importable: a hand-rolled
stand-in would be exactly the approximation these findings reject. The
source-shape check and the live-handler tests do NOT use the resolver, so
they run agent-free — the module-wide skip used to disable regressions they
could catch (round 5, review 2026-11-06).

Round 5 adds:

  5. P1 "Relay key reaches another server" — the OpenAI-compat live-models
     fallback resolved the RAW record key but kept fetching the hardcoded
     vendor URL, so a record declaring a relay ``base_url`` had its key sent
     to ``https://api.mistral.ai/v1/models``.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import sys
import textwrap
import types
import urllib.request
from urllib.parse import urlparse

import pytest

import api.config as config


# The installed Hermes agent that ships the real native resolver.
_REAL_AGENT_ROOT = "/home/xiaobao/.hermes/hermes-agent"
if _REAL_AGENT_ROOT not in sys.path:
    sys.path.insert(0, _REAL_AGENT_ROOT)


def _real_hermes_available() -> bool:
    try:
        import hermes_cli.runtime_provider  # noqa: F401
        import agent.credential_pool  # noqa: F401
        return True
    except Exception:
        return False




# ---------------------------------------------------------------------
# hermetic agent-side state
# ---------------------------------------------------------------------


def _scrub_provider_env(monkeypatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
        "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY",
        "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_CLOUD_API_KEY", "OLLAMA_API_KEY", "GLM_API_KEY",
        "KIMI_API_KEY", "MOONSHOT_API_KEY", "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY", "NOUS_API_KEY", "NVIDIA_API_KEY",
        "LM_API_KEY", "LMSTUDIO_API_KEY", "XIAOMI_API_KEY",
        "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _swap_in_test_config(extra_cfg: dict):
    """Swap api.config.cfg for the test, returning a restore callback."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg.update(extra_cfg)
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    def _restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    return _restore


@pytest.fixture
def native(monkeypatch):
    """Point the REAL installed resolver at the swapped WebUI config and a
    hermetic (synthetic-pool-only) credential state. Returns a callable that
    resolves a provider id through the REAL resolver with that state.

    The agent requirement lives HERE, on the fixture, not on the module:
    only the resolver-driven tests need the installed runtime. The
    source-shape check and the live-handler tests below run agent-free, so
    an agent-less run still exercises the regressions they cover (round 5,
    review 2026-11-06: "Independent tests get skipped")."""
    if not _real_hermes_available():
        pytest.skip(
            "installed Hermes agent (real native resolver) not importable"
        )
    import hermes_cli.runtime_provider as rp
    import hermes_cli.config as cfg_mod
    import agent.credential_pool as acp

    _scrub_provider_env(monkeypatch)
    state = {"pools": {}}

    def current_cfg() -> dict:
        providers = config.cfg.get("providers") or {}
        return {
            "model": dict(config.cfg.get("model") or {}),
            "providers": dict(providers),
        }

    monkeypatch.setattr(cfg_mod, "load_config", current_cfg)
    monkeypatch.setattr(cfg_mod, "load_config_readonly", current_cfg)
    monkeypatch.setattr(rp, "load_config", current_cfg)
    monkeypatch.setattr(acp, "_load_config_safe", current_cfg)

    def fake_load_pool(pool_key):
        return state["pools"].get(str(pool_key or "").strip().lower())

    monkeypatch.setattr(rp, "load_pool", fake_load_pool)

    def set_pool(pool_key: str, pool):
        state["pools"][str(pool_key or "").strip().lower()] = pool

    def resolve(provider_id: str) -> dict:
        return rp.resolve_runtime_provider(requested=provider_id)

    resolve.set_pool = set_pool
    return resolve


def _resolve_through_production_chain(requested: str, native_resolver):
    """Mirror api/streaming.py:15602-15645: the resolver may FAIL (the
    canonical spelling has no record), in which case the WebUI keeps
    ``resolved_api_key=None`` and still asks for the connection bundle."""
    try:
        native_rt = native_resolver(requested)
    except Exception:
        native_rt = None
    resolved_api_key = (native_rt or {}).get("api_key")
    resolved_base_url = (native_rt or {}).get("base_url")
    return config.merge_custom_provider_runtime_bundle(
        requested, resolved_api_key, resolved_base_url, native_rt,
        lookup_provider=requested,
    )


# =====================================================================
# ① P2 "Matching connections get rebuilt" — the trailing comma
# =====================================================================


class TestMatchingConnectionIsNotRebuilt:
    """The trailing comma turned ``resolved_mode_str`` into a 1-tuple, so
    ``mode_split`` was always true and a connection that already matched its
    owner was needlessly rebuilt."""

    def test_resolved_mode_str_is_not_a_tuple(self):
        """The production assignment must produce a plain string.

        A source-shape pin: the value is compared against a mode string, so a
        tuple can never satisfy it.
        """
        src = inspect.getsource(config._custom_provider_runtime_bundle_with_provenance)
        tree = ast.parse(textwrap.dedent(src))
        nodes = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "resolved_mode_str"
                for t in n.targets
            )
        ]
        assert nodes, "resolved_mode_str assignment not found in production source"
        assert not isinstance(nodes[0].value, ast.Tuple), (
            "resolved_mode_str is assigned a tuple (trailing comma), so it can "
            "never equal a mode string and mode_split is always true"
        )

    def test_matching_connection_is_not_rebuilt(self, native, monkeypatch):
        """A collision whose resolved connection ALREADY matches the owner on
        every field must not trigger the rebuild.

        The rebuild's observable side effect is an EXTRA credential-pool
        lookup (merge -> _raw_provider_record_runtime_authority ->
        _custom_record_pool_runtime -> the installed runtime's pool resolver).
        """
        providers_cfg = {
            "mistral": {
                "api_key": "owner-key-canary",
                "base_url": "https://relay.example/v1",
                "api_mode": "chat_completions",
            },
            "mistralai": {
                "api_key": "owner-key-canary",
                "base_url": "https://relay.example/v1",
                "api_mode": "chat_completions",
            },
        }
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })

        import hermes_cli.runtime_provider as rp

        calls = []
        real_resolve_pool = getattr(rp, "_try_resolve_from_custom_pool", None)
        assert real_resolve_pool is not None, (
            "the installed runtime must expose the pool resolver"
        )

        try:
            native_rt = native("mistral")

            # The spy is installed AFTER the native resolution, so only the
            # MERGE's own pool lookup is counted.
            def spy_resolve_pool(base_url, provider, api_mode=None, provider_name=None):
                calls.append(base_url)
                return real_resolve_pool(
                    base_url, provider, api_mode, provider_name=provider_name
                )

            monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", spy_resolve_pool)

            bundle = config.merge_custom_provider_runtime_bundle(
                "mistral",
                native_rt.get("api_key"),
                native_rt.get("base_url"),
                native_rt,
            )
            assert not calls, (
                "the connection already matched the owner (same key, same URL, "
                "same declared mode), yet the merge still rebuilt it — "
                f"{len(calls)} extra credential-pool lookup(s)"
            )
            # The values must survive untouched.
            assert bundle["api_mode"] == "chat_completions"
            assert bundle["api_key"] == native_rt.get("api_key")
            assert bundle["base_url"] == native_rt.get("base_url")
        finally:
            restore()


# =====================================================================
# ② P1 "Chat uses the wrong format" — the owner's EFFECTIVE mode
# =====================================================================


class TestOwnerEffectiveModeIsCompared:
    """The owner may declare no ``api_mode``; its effective mode is then its
    OWN default, never the sibling's declared protocol."""

    def test_owner_without_api_mode_does_not_keep_sibling_protocol(self, native):
        providers_cfg = {
            "mistral": {
                "api_key": "shared-key-canary",
                "base_url": "https://relay.example/v1",
            },
            "mistralai": {
                "api_key": "shared-key-canary",
                "base_url": "https://relay.example/v1",
                "api_mode": "anthropic_messages",
            },
        }
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            requested = "mistralai"
            owner_key = config._raw_provider_key_for_provider_id(requested)
            assert owner_key == "mistral", (
                f"the owner must be the FIRST matching raw key, got {owner_key!r}"
            )
            owner_cfg = (config._get_providers_cfg() or {}).get(owner_key) or {}
            assert config._custom_record_api_mode(owner_cfg) is None, (
                "premise: the owner declares no api_mode"
            )

            native_rt = native(requested)
            assert native_rt.get("api_mode") == "anthropic_messages", (
                "premise: the native resolver hands back the SIBLING's protocol"
            )
            owner_rt = native(owner_key)

            bundle = config.merge_custom_provider_runtime_bundle(
                requested,
                native_rt.get("api_key"),
                native_rt.get("base_url"),
                native_rt,
            )
            assert bundle["api_mode"] == owner_rt.get("api_mode"), (
                "the owner declares no api_mode and shares the sibling's URL/key, "
                f"so the bundle kept {bundle.get('api_mode')!r} instead of the "
                f"owner's own effective mode {owner_rt.get('api_mode')!r}. The "
                "client would send the wrong wire format."
            )
            assert bundle["api_mode"] == config._DEFAULT_RECORD_API_MODE
        finally:
            restore()

    def test_genuinely_same_mode_is_not_a_split(self, native):
        """CONTROL for the effective-mode comparison: two records that agree on
        the mode must NOT be reported as a split — the fix must not turn every
        same-mode collision into one."""
        providers_cfg = {
            "mistral": {
                "api_key": "same-key-canary",
                "base_url": "https://relay.example/v1",
                "api_mode": "anthropic_messages",
            },
            "mistralai": {
                "api_key": "same-key-canary",
                "base_url": "https://relay.example/v1",
                "api_mode": "anthropic_messages",
            },
        }
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            native_rt = native("mistral")
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistral",
                native_rt.get("api_key"),
                native_rt.get("base_url"),
                native_rt,
            )
            assert bundle["api_mode"] == "anthropic_messages", (
                "two records that genuinely agree on the protocol must keep it; "
                f"got {bundle.get('api_mode')!r}"
            )
        finally:
            restore()


# =====================================================================
# ③ P1 "Legacy credentials get skipped" — key_env / key_cmd
# =====================================================================


class TestLegacyCredentialSourcesAreRepaired:
    """A legacy record may declare its credential as ``key_env`` or
    ``key_cmd``; the single-record repair used to recognise only an inline
    ``api_key``."""

    def test_key_env_legacy_record_is_repaired(self, native, monkeypatch):
        monkeypatch.setenv("PR8664_LEGACY_KEY_ENV", "legacy-env-key-canary")
        providers_cfg = {
            "mistralai": {
                "key_env": "PR8664_LEGACY_KEY_ENV",
                "base_url": "https://legacy-relay.example/v1",
            },
        }
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            owner_cfg = (config._get_providers_cfg() or {}).get("mistralai") or {}
            assert str(owner_cfg.get("api_key") or "").strip() == "", (
                "premise: the record declares no inline api_key"
            )
            assert config._custom_record_declares_credential(
                owner_cfg, "https://legacy-relay.example/v1", None
            ), (
                "premise: the record DOES declare a credential (key_env)"
            )

            bundle = _resolve_through_production_chain("mistral", native)
            assert bundle.get("api_key") == "legacy-env-key-canary", (
                "the legacy record's key_env credential was skipped; bundle "
                f"api_key is {bundle.get('api_key')!r} instead of the record's "
                "own key_env value"
            )
            assert bundle.get("base_url") == "https://legacy-relay.example/v1"
        finally:
            restore()

    def test_key_cmd_legacy_record_is_repaired(self, native, tmp_path):
        token_file = tmp_path / "token.txt"
        token_file.write_text("legacy-cmd-key-canary", encoding="utf-8")
        providers_cfg = {
            "mistralai": {
                "key_cmd": f"cat {token_file}",
                "base_url": "https://legacy-relay.example/v1",
            },
        }
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            owner_cfg = (config._get_providers_cfg() or {}).get("mistralai") or {}
            assert str(owner_cfg.get("api_key") or "").strip() == "", (
                "premise: the record declares no inline api_key"
            )

            bundle = _resolve_through_production_chain("mistral", native)
            # A key_cmd record's credential is a per-request TOKEN PROVIDER (a
            # callable), not a literal string — the wire clients mint a fresh
            # bearer from it.
            assert bundle.get("api_key") is not None, (
                "the legacy record's key_cmd credential was skipped; bundle "
                f"api_key is {bundle.get('api_key')!r}"
            )
            assert bundle.get("api_key") != config._CUSTOM_NO_AUTH_PLACEHOLDER, (
                "the record declared a key_cmd but the bundle handed out the "
                "keyless placeholder"
            )
            assert callable(bundle.get("api_key")), (
                f"expected the record's key_cmd token provider, got {bundle.get('api_key')!r}"
            )
            assert bundle["api_key"]() == "legacy-cmd-key-canary"
        finally:
            restore()

    def test_keyless_record_is_not_repaired(self, native):
        """CONTROL: a record that declares NO credential source at all is
        genuinely keyless — the wider declaration check must not mistake that
        for "the resolver missed a credential".

        With no declared credential the single-record repair must NOT fire:
        the bundle keeps whatever the resolver produced (here: nothing at
        all, because the canonical spelling has no record) instead of being
        rebuilt as a credentialed record.
        """
        providers_cfg = {
            "mistralai": {
                "base_url": "https://legacy-relay.example/v1",
            },
        }
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            owner_cfg = (config._get_providers_cfg() or {}).get("mistralai") or {}
            assert not config._custom_record_declares_credential(
                owner_cfg, "https://legacy-relay.example/v1", None
            ), (
                "premise: the record declares no credential source at all"
            )
            bundle = _resolve_through_production_chain("mistral", native)
            # No repair fired: the bundle is untouched (the resolver produced
            # no credential for the canonical spelling, and a keyless record
            # has none to supply).
            assert bundle.get("api_key") is None, (
                "a genuinely keyless record must NOT be repaired into a "
                f"credentialed one, got api_key={bundle.get('api_key')!r}"
            )
            assert bundle.get("api_key") != config._CUSTOM_NO_AUTH_PLACEHOLDER, (
                "a genuinely keyless record must not be given a placeholder by "
                "the single-record repair"
            )
        finally:
            restore()


# =====================================================================
# ④ P2 "Live models lose their key" — the raw config key
# =====================================================================


class _FakeHandler:
    """Minimal real BaseHTTPRequestHandler-shaped stub for j(handler, ...)."""

    def __init__(self, path="/api/models/live?provider=mistral"):
        self.path = path
        self.command = "GET"
        self.headers = {"Accept-Encoding": "identity"}
        self.wfile = io.BytesIO()
        self.code = None
        self.headers_sent = []

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.headers_sent.append((k, v))

    def end_headers(self):
        pass

    def flush(self):
        pass

    def write(self, data):
        self.wfile.write(data)


def _run_live_models(monkeypatch, providers_cfg, requested="mistral"):
    """Drive the REAL _handle_live_models handler end to end."""
    import api.routes as routes

    # The handler has a 60s live-models cache; each scenario starts clean.
    routes._clear_live_models_cache()

    restore = _swap_in_test_config({
        "model": {"provider": requested, "default": "some-model"},
        "providers": providers_cfg,
    })
    fetched = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            # A live-ONLY id: "mistral-large-latest" also sits in the static
            # _PROVIDER_MODELS fallback, so asserting it would pass even when
            # the live response was never parsed (round 5, review 2026-11-06:
            # "assert the returned live payload ... so this does not become
            # false-green evidence").
            return json.dumps(
                {"data": [{"id": "mistral-large-live-canary"}]}
            ).encode()

    def fake_urlopen(req, timeout=None):
        fetched["url"] = req.full_url
        fetched["auth"] = req.headers.get("Authorization")
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # provider_model_ids unavailable / returns nothing -> the fallback path.
    fake_mod = types.ModuleType("hermes_cli.models")
    fake_mod.provider_model_ids = lambda p: []
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_mod)

    handler = _FakeHandler(f"/api/models/live?provider={requested}")
    parsed = urlparse(handler.path)
    try:
        routes._handle_live_models(handler, parsed)
        body = handler.wfile.getvalue()
        payload = json.loads(body.decode("utf-8")) if body else {}
        return fetched, payload
    finally:
        restore()


def _payload_ids(payload: dict) -> list:
    return [m.get("id") for m in (payload.get("models") or [])]


class TestLiveModelsReadsTheRawConfigKey:
    """``_handle_live_models`` must resolve the RAW ``providers:`` key before
    reading the credential, so the canonical fold cannot make it miss the
    legacy record's own key."""

    def test_legacy_record_key_is_used_for_the_live_fetch(self, monkeypatch):
        providers_cfg = {
            "mistralai": {
                "api_key": "legacy-live-key-canary",
                "base_url": "https://api.mistral.ai/v1",
            },
        }
        fetched, payload = _run_live_models(monkeypatch, providers_cfg)
        assert fetched.get("auth"), (
            "the authenticated fetch was skipped; the handler returned the "
            f"static fallback models {payload.get('models')!r} instead of the "
            "live list, because providers['mistral'] missed the legacy "
            "providers['mistralai'] key."
        )
        assert fetched["auth"] == "Bearer legacy-live-key-canary"
        assert fetched.get("url") == "https://api.mistral.ai/v1/models"
        assert _payload_ids(payload) == ["mistral-large-live-canary"], (
            "the captured request is not the evidence: the handler must return "
            f"the LIVE payload, not the static fallback {_payload_ids(payload)}"
        )

    def test_canonical_record_key_is_still_used(self, monkeypatch):
        """CONTROL: resolving the raw key must not lose the CANONICAL
        record's own key."""
        providers_cfg = {
            "mistral": {
                "api_key": "canonical-live-key-canary",
                "base_url": "https://api.mistral.ai/v1",
            },
        }
        fetched, payload = _run_live_models(monkeypatch, providers_cfg)
        assert fetched.get("auth"), (
            "the canonical providers.mistral key must still be found by the "
            "live-models fallback"
        )
        assert fetched["auth"] == "Bearer canonical-live-key-canary"
        assert fetched.get("url") == "https://api.mistral.ai/v1/models"
        assert _payload_ids(payload) == ["mistral-large-live-canary"], (
            "the captured request is not the evidence: the handler must return "
            f"the LIVE payload, not the static fallback {_payload_ids(payload)}"
        )

    def test_unrelated_provider_key_is_not_borrowed(self, monkeypatch):
        """CONTROL: a record for a DIFFERENT provider must never supply the
        credential for this one."""
        providers_cfg = {
            "openrouter": {
                "api_key": "other-provider-key-canary",
                "base_url": "https://api.mistral.ai/v1",
            },
        }
        fetched, payload = _run_live_models(monkeypatch, providers_cfg)
        assert not fetched.get("auth"), (
            "a record belonging to another provider must not supply the "
            f"credential; got {fetched.get('auth')!r}"
        )


# =====================================================================
# ⑤ P1 "Relay key reaches another server" — endpoint with its credential
# =====================================================================


class TestVendorFallbackKeepsEndpointWithCredential:
    """#8664 review P1 (nesquena-hermes + Greptile, 2026-11-06): the
    OpenAI-compat live-models fallback resolved the RAW record key but kept
    fetching the hardcoded vendor URL, so a record declaring a relay
    ``base_url`` had its credential sent to
    ``https://api.mistral.ai/v1/models``.

    The destination invariant: the selected record's credential may only
    travel to that record's OWN endpoint. These tests drive the REAL
    ``_handle_live_models`` handler and capture the request BEFORE transport.
    """

    _RELAY_RECORD = {
        "mistralai": {
            "api_key": "legacy-relay-key-canary",
            "base_url": "https://legacy-relay.example/v1",
        },
    }

    def test_relay_record_key_is_not_sent_to_the_vendor_url(self, monkeypatch):
        fetched, payload = _run_live_models(monkeypatch, dict(self._RELAY_RECORD))
        assert not fetched, (
            "the vendor fallback sent the legacy record's credential to "
            f"{fetched.get('url')} (Authorization: {fetched.get('auth')!r}), but "
            "providers.mistralai declares https://legacy-relay.example/v1 — the "
            "credential must travel with its own endpoint, never to the vendor "
            "URL it was not configured for"
        )
        # The user still gets an answer: the static fallback list.
        assert _payload_ids(payload), (
            "skipping the fetch must still return the static fallback list"
        )

    def test_relay_record_key_is_not_sent_to_the_vendor_url_legacy_spelling(
        self, monkeypatch
    ):
        """The same record requested by its LEGACY spelling: the raw-key
        resolution finds it either way, so the invariant must hold for both."""
        fetched, payload = _run_live_models(
            monkeypatch, dict(self._RELAY_RECORD), requested="mistralai"
        )
        assert not fetched, (
            "the vendor fallback sent the legacy record's credential to "
            f"{fetched.get('url')} (Authorization: {fetched.get('auth')!r}) for "
            "the legacy request spelling too"
        )
        assert _payload_ids(payload)

    @pytest.mark.parametrize(
        "record_url",
        ["https://api.mistral.ai/v1", "https://api.mistral.ai/v1/"],
    )
    def test_record_with_the_vendor_endpoint_still_fetches(self, monkeypatch, record_url):
        """CONTROL: when the selected record's OWN endpoint IS the vendor's
        (trailing slash tolerated), the authenticated fetch must still happen
        — the round-4 behaviour this finding must not regress."""
        providers_cfg = {
            "mistralai": {
                "api_key": "legacy-live-key-canary",
                "base_url": record_url,
            },
        }
        fetched, payload = _run_live_models(monkeypatch, providers_cfg)
        assert fetched.get("url") == "https://api.mistral.ai/v1/models"
        assert fetched.get("auth") == "Bearer legacy-live-key-canary"
        assert _payload_ids(payload) == ["mistral-large-live-canary"], (
            "a record whose own endpoint is the vendor's must still get the "
            f"live payload, not the static fallback {_payload_ids(payload)}"
        )

    def test_record_without_endpoint_still_fetches_the_vendor(self, monkeypatch):
        """CONTROL: a record that declares NO endpoint has no differing
        endpoint to protect — the vendor URL is that provider's canonical
        destination for its own key, so the fetch proceeds."""
        providers_cfg = {"mistralai": {"api_key": "legacy-live-key-canary"}}
        fetched, payload = _run_live_models(monkeypatch, providers_cfg)
        assert fetched.get("url") == "https://api.mistral.ai/v1/models"
        assert fetched.get("auth") == "Bearer legacy-live-key-canary"
        assert _payload_ids(payload) == ["mistral-large-live-canary"]

    def test_unrelated_provider_record_is_still_not_borrowed(self, monkeypatch):
        """CONTROL (destination variant): another provider's record — even one
        that points at the vendor URL — must not supply the credential."""
        providers_cfg = {
            "openrouter": {
                "api_key": "other-provider-key-canary",
                "base_url": "https://api.mistral.ai/v1",
            },
        }
        fetched, payload = _run_live_models(monkeypatch, providers_cfg)
        assert not fetched, (
            "a record belonging to another provider must not supply the "
            f"credential; got {fetched.get('auth')!r}"
        )
