"""Regression tests for issue #6980 — Cursor-style ${env:VAR} config expansion.

Before the fix, ``api.config._expand_env_vars`` resolved the ENTIRE captured
group (including the ``env:`` prefix) as an env-var name, so a
``${env:BIFROST_API_KEY}`` reference stayed as a literal placeholder and a
custom-provider ``/v1/models`` probe built ``Authorization: Bearer
${env:BIFROST_API_KEY}``.

These tests assert observable behavior: prefix stripping, legacy bare-ref
regression, fail-closed missing-var handling with thread-safe dedup, non-env
SecretRef verbatim preservation, recursion, the (a)/(b)/(c) cross-profile leak
gate driven through the real per-request TLS bind, and no plaintext write-back
on a settings save.
"""

from __future__ import annotations

import logging
import threading

import api.config as config
import api.profiles as profiles


def test_expand_env_vars_resolves_env_prefix(monkeypatch):
    monkeypatch.setenv("BIFROST_API_KEY", "sk-cursor-style-secret")
    config._thread_ctx.env = {}
    assert config._expand_env_vars("${env:BIFROST_API_KEY}") == "sk-cursor-style-secret"


def test_expand_env_vars_resolves_legacy_bare_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-bare-secret")
    config._thread_ctx.env = {}
    assert config._expand_env_vars("${ANTHROPIC_TOKEN}") == "sk-bare-secret"


def test_expand_env_vars_uppercase_env_falls_through_as_bare_name(monkeypatch):
    """Uppercase ${ENV:VAR} is not the Cursor-style form; it resolves (or fails)
    as a legacy bare name rather than being prefix-stripped."""
    monkeypatch.delenv("ENV:BIFROST_API_KEY", raising=False)
    config._thread_ctx.env = {}
    assert config._expand_env_vars("${ENV:BIFROST_API_KEY}") == "${ENV:BIFROST_API_KEY}"


def test_expand_env_vars_missing_var_keeps_placeholder_and_warns_once(monkeypatch, caplog):
    config._env_ref_warned.clear()
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    config._thread_ctx.env = {}

    for _ in range(5):
        assert config._expand_env_vars("${env:MISSING_PROVIDER_KEY}") == "${env:MISSING_PROVIDER_KEY}"

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and "MISSING_PROVIDER_KEY" in r.getMessage()
        and "not set" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "~/.hermes/.env" in warnings[0]


def test_expand_env_vars_missing_var_dedup_is_thread_safe(monkeypatch, caplog):
    config._env_ref_warned.clear()
    monkeypatch.delenv("MISSING_CONCURRENT_KEY", raising=False)
    config._thread_ctx.env = {}

    barrier = threading.Barrier(8)

    def probe():
        barrier.wait()
        config._expand_env_vars("${env:MISSING_CONCURRENT_KEY}")

    threads = [threading.Thread(target=probe) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and "MISSING_CONCURRENT_KEY" in r.getMessage()
        and "not set" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_expand_env_vars_non_env_sources_stay_verbatim(monkeypatch):
    monkeypatch.setenv("VAULT", "unused")
    monkeypatch.setenv("BITWARDEN", "unused")
    monkeypatch.setenv("FILE", "unused")
    config._thread_ctx.env = {}
    for ref in ("${file:./secret.txt}", "${vault:prod/api}", "${bitwarden:item}"):
        assert config._expand_env_vars(ref) == ref


def test_expand_env_vars_recurses_into_dicts_and_lists(monkeypatch):
    monkeypatch.setenv("DEEP_KEY", "deep-value")
    monkeypatch.setenv("LIST_KEY", "list-value")
    config._thread_ctx.env = {}
    payload = {
        "providers": [
            {"api_key": "${env:DEEP_KEY}"},
            {"api_key": "literal"},
            {"nested": {"k": "${LIST_KEY}"}},
        ],
        "model": {"api_key": "${env:DEEP_KEY}"},
    }
    assert config._expand_env_vars(payload) == {
        "providers": [
            {"api_key": "deep-value"},
            {"api_key": "literal"},
            {"nested": {"k": "list-value"}},
        ],
        "model": {"api_key": "deep-value"},
    }


def test_env_prefix_named_profile_does_not_leak_process_key(monkeypatch, tmp_path):
    """Production-shaped cross-profile gate: process env holds profile A's key,
    a request bound to named profile B must not see it anywhere — not in B's
    cached config, not via resolve_custom_provider_connection, and not via the
    scoped resolver that feeds the live-models Authorization header."""
    base = tmp_path / ".hermes"
    b_home = base / "profiles" / "b"
    b_home.mkdir(parents=True)
    (b_home / "config.yaml").write_text(
        "custom_providers:\n"
        "  - name: Team\n"
        "    base_url: http://gpu.local:8000/v1\n"
        "    api_key: ${env:BIFROST_API_KEY}\n",
        encoding="utf-8",
    )
    # Drop the conftest HERMES_CONFIG_PATH override so _get_config_path() reaches
    # the real get_active_hermes_home() -> named-profile path below.
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("BIFROST_API_KEY", "sk-profile-A-secret")
    # Root detection is patched to a pure function to avoid the subprocess
    # list_profiles_api() call; the per-request TLS bind below is REAL.
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    config._thread_ctx.env = {}
    config._thread_ctx.block_process_env_fallback = False

    profiles.set_request_profile("b")
    try:
        config.reload_config()
        cached = config.get_config()
        providers = cached.get("custom_providers", [])
        assert providers and providers[0]["api_key"] == "${env:BIFROST_API_KEY}"

        key, base_url = config.resolve_custom_provider_connection("custom:team")
        assert key in (None, ""), key
        assert base_url == "http://gpu.local:8000/v1"

        # The chokepoint feeding `Authorization: Bearer <key>` in the live-models
        # probe fails closed under the named profile (no process-env fallthrough).
        assert config._resolve_config_ref_value("${env:BIFROST_API_KEY}") == ""
    finally:
        profiles.clear_request_profile()

    # Unscoped/root read still resolves the env: prefix from the process env,
    # preserving legacy single-profile behavior.
    profiles.clear_request_profile()
    config._thread_ctx.env = {}
    assert config._resolve_config_ref_value("${env:BIFROST_API_KEY}") == "sk-profile-A-secret"


def test_settings_save_preserves_env_placeholder_not_resolved_secret(monkeypatch, tmp_path):
    """A read-modify-write settings save must round-trip the ${env:VAR} template,
    never the resolved secret."""
    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    cfg_path = base / "config.yaml"
    cfg_path.write_text(
        "display:\n"
        "  show_reasoning: true\n"
        "custom_providers:\n"
        "  - name: Team\n"
        "    base_url: http://gpu.local:8000/v1\n"
        "    api_key: ${env:BIFROST_API_KEY}\n",
        encoding="utf-8",
    )
    # Point the (conftest-isolated) HERMES_CONFIG_PATH override at this test's
    # config so the settings save round-trips the file we assert on.
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("BIFROST_API_KEY", "sk-super-secret")
    config._thread_ctx.env = {}

    config.reload_config()
    config.set_reasoning_display(False)

    on_disk = cfg_path.read_text(encoding="utf-8")
    assert "${env:BIFROST_API_KEY}" in on_disk
    assert "sk-super-secret" not in on_disk
    assert "show_reasoning: false" in on_disk