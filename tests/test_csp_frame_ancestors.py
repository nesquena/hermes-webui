"""Regression coverage for the configurable CSP frame-ancestors knob.

`HERMES_WEBUI_CSP_FRAME_ANCESTORS` lets an operator allow a trusted app
(e.g. a self-hosted local dashboard) to embed the WebUI in an <iframe>,
opt-in and default-off. With the var unset the policy stays locked down:
frame-ancestors 'none' plus the X-Frame-Options DENY header. It is the
counterpart of `HERMES_WEBUI_CSP_FRAME_EXTRA`, which governs what the
WebUI page itself may embed.
"""

from __future__ import annotations


def test_csp_frame_ancestors_default_is_none(monkeypatch):
    from server import Handler

    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", raising=False)

    policy = Handler.csp_report_only_policy()
    assert "frame-ancestors 'none'; " in policy


def test_csp_frame_ancestors_includes_valid_origins(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_ANCESTORS",
        "http://localhost:3000 https://*.dash.example.com:8443",
    )

    policy = Handler.csp_report_only_policy()
    assert (
        "frame-ancestors "
        "http://localhost:3000 https://*.dash.example.com:8443; "
    ) in policy
    assert "frame-ancestors 'none'" not in policy


def test_csp_frame_ancestors_in_enforced_policy(monkeypatch):
    from api.helpers import _build_csp_enforced_policy

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://127.0.0.1:3000")
    enforced = _build_csp_enforced_policy()
    assert "frame-ancestors http://127.0.0.1:3000; " in enforced


def test_csp_frame_ancestors_rejects_directive_injection(monkeypatch, caplog):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_ANCESTORS",
        "https://ok.example.com; script-src *",
    )

    policy = Handler.csp_report_only_policy()
    assert "https://ok.example.com" not in policy
    assert "script-src *" not in policy
    assert "frame-ancestors 'none'; " in policy  # falls back to safe default
    assert "Ignoring invalid HERMES_WEBUI_CSP_FRAME_ANCESTORS" in caplog.text


def test_csp_frame_ancestors_rejects_paths(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_ANCESTORS", "https://app.example.com/embed"
    )
    policy = Handler.csp_report_only_policy()
    assert "https://app.example.com/embed" not in policy
    assert "frame-ancestors 'none'; " in policy


def test_csp_frame_ancestors_rejects_ws_scheme(monkeypatch):
    """An embedding ancestor is always http(s); ws/wss are not valid."""
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "wss://socket.example.com")
    policy = Handler.csp_report_only_policy()
    assert "wss://socket.example.com" not in policy
    assert "frame-ancestors 'none'; " in policy


def test_csp_frame_ancestors_does_not_affect_frame_src(monkeypatch):
    """The frame-ancestors knob and the frame-src knob are independent."""
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_EXTRA", raising=False)
    policy = Handler.csp_report_only_policy()
    assert "frame-ancestors http://localhost:3000; " in policy
    # ... and NOT leaked into frame-src (which stays same-origin only).
    frame_seg = policy.split("frame-src", 1)[1].split(";", 1)[0]
    assert "localhost:3000" not in frame_seg


def test_x_frame_options_denied_by_default_and_omitted_when_set(monkeypatch):
    from api import helpers

    class _Recorder:
        def __init__(self):
            self.headers = []

        def send_header(self, name, value):
            self.headers.append((name, value))

    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", raising=False)
    rec = _Recorder()
    helpers._security_headers(rec)
    assert ("X-Frame-Options", "DENY") in rec.headers

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
    rec = _Recorder()
    helpers._security_headers(rec)
    assert all(name != "X-Frame-Options" for name, _ in rec.headers)


# ─────────────────────────────────────────────────────────────────────────────
# Review follow-ups on the security gate.
# ─────────────────────────────────────────────────────────────────────────────


class TestFramingPolicyIsDeploymentWide:
    """A profile's own .env must not be able to choose who may frame the WebUI.

    frame-ancestors is a deployment-wide posture, exactly like the isolation
    flag of #4589/#4590: a profile that could set it would decide the embedding
    policy for every user of the deployment, not just for itself.
    """

    def test_profile_dotenv_cannot_set_frame_ancestors(self, tmp_path, monkeypatch):
        import os
        from api.profiles import _reload_dotenv, _PROTECTED_ENV_KEYS
        from api.helpers import _csp_frame_ancestors

        assert "HERMES_WEBUI_CSP_FRAME_ANCESTORS" in _PROTECTED_ENV_KEYS

        monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", raising=False)
        (tmp_path / ".env").write_text(
            "HERMES_WEBUI_CSP_FRAME_ANCESTORS=https://attacker.example.com\n"
            "SOME_OTHER_KEY=ok\n",
            encoding="utf-8",
        )
        try:
            _reload_dotenv(tmp_path)
            assert "HERMES_WEBUI_CSP_FRAME_ANCESTORS" not in os.environ, (
                "a profile .env must not reach the deployment framing policy"
            )
            assert _csp_frame_ancestors() == "'none'"
            # Non-protected keys still load normally.
            assert os.environ.get("SOME_OTHER_KEY") == "ok"
        finally:
            os.environ.pop("SOME_OTHER_KEY", None)
            os.environ.pop("HERMES_WEBUI_CSP_FRAME_ANCESTORS", None)

    def test_profile_dotenv_cannot_widen_operator_value(self, tmp_path, monkeypatch):
        """The operator's value wins; the profile's is dropped, not merged."""
        import os
        from api.profiles import _reload_dotenv
        from api.helpers import _csp_frame_ancestors

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "https://ok.example.com")
        (tmp_path / ".env").write_text(
            "HERMES_WEBUI_CSP_FRAME_ANCESTORS=https://attacker.example.com\n",
            encoding="utf-8",
        )
        _reload_dotenv(tmp_path)
        assert os.environ["HERMES_WEBUI_CSP_FRAME_ANCESTORS"] == "https://ok.example.com"
        assert _csp_frame_ancestors() == "https://ok.example.com"

    def test_runtime_env_path_also_strips_frame_ancestors(self, tmp_path):
        """Second door: the runtime/gateway-parity path must strip it too."""
        from api.profiles import get_profile_runtime_env, _BLOCKED_RUNTIME_ENV_KEYS

        assert "HERMES_WEBUI_CSP_FRAME_ANCESTORS" in _BLOCKED_RUNTIME_ENV_KEYS

        (tmp_path / ".env").write_text(
            "HERMES_WEBUI_CSP_FRAME_ANCESTORS=https://attacker.example.com\n"
            "MY_PROFILE_KEY=value1\n",
            encoding="utf-8",
        )
        env = get_profile_runtime_env(tmp_path)
        assert "HERMES_WEBUI_CSP_FRAME_ANCESTORS" not in env
        assert env.get("MY_PROFILE_KEY") == "value1"


class _Recorder:
    """Minimal stand-in for the request handler, capturing sent headers."""

    def __init__(self):
        self.headers = []

    def send_header(self, name, value):
        self.headers.append((name, value))

    def value_of(self, name):
        return next((v for n, v in self.headers if n == name), None)

    def has(self, name):
        return any(n == name for n, _ in self.headers)


class TestFrameAncestorsResolvedOncePerResponse:
    """X-Frame-Options and CSP must never disagree.

    A conforming browser ignores X-Frame-Options whenever an enforced
    frame-ancestors is present, so the two must come from a single resolution:
    resolving them independently made contradictory headers possible whenever
    the env changed mid-response.
    """

    def test_resolved_exactly_once(self, monkeypatch):
        from api import helpers

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
        calls = []
        real = helpers._csp_frame_ancestors

        def _counting():
            calls.append(1)
            return real()

        monkeypatch.setattr(helpers, "_csp_frame_ancestors", _counting)
        rec = _Recorder()
        helpers._security_headers(rec)
        assert len(calls) == 1, "frame-ancestors must be resolved once per response"

    def test_value_is_cached_on_the_handler(self, monkeypatch):
        from api import helpers

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
        rec = _Recorder()
        helpers._security_headers(rec)
        assert rec._csp_frame_ancestors == "http://localhost:3000"

    def test_report_only_reuses_the_cached_value(self, monkeypatch):
        """The env changing mid-response must not split the two policies.

        Goes through the real end_headers(), because that is where the
        report-only policy is emitted for an actual response.
        """
        from api import helpers
        from server import Handler

        sent = []
        probe = Handler.__new__(Handler)
        probe.request_version = "HTTP/0.9"  # makes the base end_headers a no-op
        probe._headers_buffer = []
        probe.send_header = lambda name, value: sent.append((name, value))

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
        helpers._security_headers(probe)
        enforced = next(v for n, v in sent if n == "Content-Security-Policy")
        assert "frame-ancestors http://localhost:3000; " in enforced

        # Env flips after the enforced policy was already emitted.
        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "https://other.example.com")
        probe.end_headers()
        report_only = next(
            v for n, v in sent if n == "Content-Security-Policy-Report-Only"
        )
        assert "frame-ancestors http://localhost:3000; " in report_only, (
            "the report-only policy must reuse the response's single resolution"
        )
        assert "other.example.com" not in report_only

    def test_xfo_agrees_with_the_emitted_csp(self, monkeypatch):
        """XFO is sent iff the very same resolution came out locked down."""
        from api import helpers

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
        rec = _Recorder()
        helpers._security_headers(rec)
        assert not rec.has("X-Frame-Options")
        assert "frame-ancestors 'none'" not in rec.value_of("Content-Security-Policy")

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "https://bad..example.com")
        rec = _Recorder()
        helpers._security_headers(rec)
        assert rec.value_of("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'; " in rec.value_of("Content-Security-Policy")


MALFORMED_ANCESTORS = [
    "https://.",              # empty label
    "https://_",              # not a host label at all
    "https://~",              # ditto
    "https://example..com",   # empty inner label
    "https://example.com:٣٠٠٠",  # Arabic-Indic digits: no browser parses this as a port
    "https://-example.com",   # a label may not start with a hyphen
    "https://example-.com",   # ... nor end with one
    "https://example.com:1e3",
    "https://example.com:0",
    "https://example.com:70000",
]


class TestMalformedAncestorsFallBackToLockdown:
    """A malformed origin must fall back to 'none', not be waved through.

    An accepted-but-malformed source is the worst outcome: the CSP carries a
    value no browser can match AND X-Frame-Options is omitted, so whether the
    page may be framed becomes browser-dependent.
    """

    @staticmethod
    def _emit(monkeypatch, value):
        from api import helpers

        monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", value)
        rec = _Recorder()
        helpers._security_headers(rec)
        return rec

    def test_each_malformed_origin_is_rejected(self):
        from api.helpers import _valid_csp_frame_ancestor_source

        for value in MALFORMED_ANCESTORS:
            assert not _valid_csp_frame_ancestor_source(value), value

    def test_each_malformed_origin_locks_down_and_keeps_xfo(self, monkeypatch):
        for value in MALFORMED_ANCESTORS:
            rec = self._emit(monkeypatch, value)
            policy = rec.value_of("Content-Security-Policy")
            assert "frame-ancestors 'none'; " in policy, value
            assert value not in policy, value
            assert rec.value_of("X-Frame-Options") == "DENY", (
                f"{value!r} must not silently drop X-Frame-Options"
            )

    def test_one_bad_entry_rejects_the_whole_list(self, monkeypatch):
        rec = self._emit(monkeypatch, "https://ok.example.com https://example..com")
        assert "frame-ancestors 'none'; " in rec.value_of("Content-Security-Policy")
        assert rec.value_of("X-Frame-Options") == "DENY"

    def test_well_formed_origins_still_pass(self):
        from api.helpers import _valid_csp_frame_ancestor_source

        for value in (
            "https://example.com",
            "http://localhost:3000",
            "https://*.dash.example.com",
            "https://example.com:8443",
            "https://example.com:*",
            "https://a.b.example.co.uk",
            "http://127.0.0.1:3000",
        ):
            assert _valid_csp_frame_ancestor_source(value), value


def test_bracketed_ipv6_origin_is_not_supported(monkeypatch):
    """Documents a known limit: only hostnames and IPv4 literals are supported.

    A bracketed IPv6 origin falls back to lockdown instead of being allowed,
    which is the safe direction. `.env.example` states the limit so an operator
    does not silently lose framing they thought they had configured.
    """
    from api import helpers
    from api.helpers import _valid_csp_frame_ancestor_source

    assert not _valid_csp_frame_ancestor_source("http://[::1]:3000")

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://[::1]:3000")
    rec = _Recorder()
    helpers._security_headers(rec)
    assert "frame-ancestors 'none'; " in rec.value_of("Content-Security-Policy")
    assert rec.value_of("X-Frame-Options") == "DENY"
