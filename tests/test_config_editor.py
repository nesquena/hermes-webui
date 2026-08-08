"""Tests for the raw config.yaml viewer/editor (api/config_editor.py).

Covers: GET redaction of credential-shaped values (incl. multiline block
scalars) with a path manifest, and the PUT write path's gates in order —
write-disabled (403), invalid YAML (400 + line/column), non-mapping (400),
resubmitted redaction placeholders (400), the security-key denylist (400 +
blocked_paths) including the flat webui_* keys that a prior version of
_is_denylisted_path failed to catch (proven bypasses: OIDC issuer hijack,
prefill-script RCE, gateway-URL SSRF+key exfiltration), a successful write
(atomic, comments preserved via raw text roundtrip, backup file created,
file mode preserved, reload_config invoked), and the etag-based
optimistic-concurrency check (409 on a stale save).
"""

import os
import stat

import pytest

from api import config_editor


def _patch_config_path(monkeypatch, config_path):
    from api import config as api_config

    monkeypatch.setattr(api_config, "_get_config_path", lambda: config_path)


def _patch_reload_counter(monkeypatch):
    from api import config as api_config

    calls = {"n": 0}
    monkeypatch.setattr(api_config, "reload_config", lambda: calls.__setitem__("n", calls["n"] + 1))
    return calls


# ── GET /api/config/raw ─────────────────────────────────────────────────────


def test_get_redacts_api_key_and_lists_path(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# top-level comment\n"
        "providers:\n"
        "  openai:\n"
        "    api_key: sk-abc123SECRETVALUE\n"
        "    base_url: http://localhost:8080\n"
        "agent:\n"
        "  reasoning_effort: high  # inline comment\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.delenv(config_editor._WRITE_GATE_ENV, raising=False)

    result = config_editor.get_config_raw()

    assert "sk-abc123SECRETVALUE" not in result["yaml"]
    assert config_editor._REDACTED_PLACEHOLDER in result["yaml"]
    assert "providers.openai.api_key" in result["redacted"]
    assert result["allowed"] is False
    # Non-sensitive lines and comments are untouched.
    assert "# top-level comment" in result["yaml"]
    assert "base_url: http://localhost:8080" in result["yaml"]
    assert "reasoning_effort: high  # inline comment" in result["yaml"]


def test_get_redacts_nested_mcp_env_token(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mcpServers:\n"
        "  myserver:\n"
        "    env:\n"
        "      MY_ACCESS_TOKEN: abcdef123456\n"
        "      PLAIN_VALUE: keep-me\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)

    result = config_editor.get_config_raw()

    assert "abcdef123456" not in result["yaml"]
    assert "keep-me" in result["yaml"]
    assert "mcpServers.myserver.env.MY_ACCESS_TOKEN" in result["redacted"]


def test_get_redacts_multiline_block_scalar_secret(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  client_secret: |\n"
        "    -----BEGIN PRIVATE KEY-----\n"
        "    verysecretmultilinecontent\n"
        "    -----END PRIVATE KEY-----\n"
        "unrelated: value\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)

    result = config_editor.get_config_raw()

    assert "verysecretmultilinecontent" not in result["yaml"]
    assert "BEGIN PRIVATE KEY" not in result["yaml"]
    assert "agent.client_secret" in result["redacted"]
    assert "unrelated: value" in result["yaml"]
    # The block collapses to a single redacted line, not a dangling `|`.
    assert config_editor._REDACTED_PLACEHOLDER in result["yaml"]


def test_get_allowed_reflects_write_gate_env(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)

    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")
    assert config_editor.get_config_raw()["allowed"] is True

    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "0")
    assert config_editor.get_config_raw()["allowed"] is False


# ── PUT /api/config/raw ──────────────────────────────────────────────────


def test_put_disabled_by_default_returns_403(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.delenv(config_editor._WRITE_GATE_ENV, raising=False)

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("agent:\n  reasoning_effort: low\n")
    assert excinfo.value.status == 403
    assert config_editor._WRITE_GATE_ENV in str(excinfo.value)


def test_put_invalid_yaml_returns_400_with_location(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("agent:\n  bad: [1, 2\n")
    assert excinfo.value.status == 400
    assert excinfo.value.extra.get("line") is not None


def test_put_non_mapping_returns_400(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("- 1\n- 2\n")
    assert excinfo.value.status == 400


def test_put_rejects_redacted_placeholder_in_text(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "providers:\n  openai:\n    api_key: sk-real\n", encoding="utf-8"
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    bad_text = (
        "providers:\n"
        f"  openai:\n    api_key: {config_editor._REDACTED_PLACEHOLDER}\n"
    )
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(bad_text)
    assert excinfo.value.status == 400
    assert "re-fetch" in str(excinfo.value).lower()


def test_put_denylist_blocks_trusted_proxies_change(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "trusted_proxies:\n  - 10.0.0.1\nagent:\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "trusted_proxies:\n  - 10.0.0.1\n  - 10.0.0.2\nagent:\n  reasoning_effort: high\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert "trusted_proxies" in excinfo.value.extra.get("blocked_paths", [])
    # File must be untouched.
    assert "10.0.0.2" not in config_path.read_text(encoding="utf-8")


def test_put_denylist_blocks_webui_auth_change(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "webui:\n  auth:\n    enabled: true\nagent:\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "webui:\n  auth:\n    enabled: false\nagent:\n  reasoning_effort: high\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(p.startswith("webui.auth") for p in excinfo.value.extra.get("blocked_paths", []))


def test_put_denylist_blocks_a_top_level_allowlist(tmp_path, monkeypatch):
    """A top-level `allow*` section is a trust boundary and stays protected."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("allowed_hosts:\n  - localhost\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "allowed_hosts:\n  - localhost\n  - evil.example\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any("allowed_hosts" in p for p in excinfo.value.extra.get("blocked_paths", []))


def test_put_blocks_granting_a_tool_through_allowed_tools(tmp_path, monkeypatch):
    """Adding to an allowlist GRANTS — this is a capability boundary.

    An earlier revision used exactly this edit as the argument that nested
    `allow*` fields are ordinary application config, and asserted the write
    succeeded. It does grant: `write` is a tool the operator had withheld, and
    the raw editor was handing it over.
    """
    config_path = tmp_path / "config.yaml"
    original = "mcpServers:\n  foo:\n    allowed_tools:\n      - read\n"
    config_path.write_text(original, encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "mcpServers:\n  foo:\n    allowed_tools:\n      - read\n      - write\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)

    assert excinfo.value.status == 400
    assert "mcpServers.foo.allowed_tools" in str(excinfo.value.extra.get("blocked_paths", []))
    assert config_path.read_text(encoding="utf-8") == original


def test_put_allows_a_nested_non_boundary_allow_field(tmp_path, monkeypatch):
    """The editor still has to be usable for fields that grant nothing."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  gpt-4:\n    allowed_context_length: 8192\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    result = config_editor.put_config_raw(
        "models:\n  gpt-4:\n    allowed_context_length: 16384\n"
    )

    assert result["ok"] is True
    assert "16384" in config_path.read_text(encoding="utf-8")


def test_put_valid_change_writes_atomically_with_backup_and_reload(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original_text = (
        "# preserved comment\n"
        "agent:\n"
        "  reasoning_effort: high  # inline\n"
        "custom_field: original\n"
    )
    config_path.write_text(original_text, encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")
    reload_calls = _patch_reload_counter(monkeypatch)

    new_text = original_text.replace("custom_field: original", "custom_field: changed")
    result = config_editor.put_config_raw(new_text)

    assert result["ok"] is True
    assert result["etag"], "a successful save must return the new etag"
    written = config_path.read_text(encoding="utf-8")
    assert written == new_text, "raw text roundtrip must preserve untouched comments byte-for-byte"
    assert "# preserved comment" in written
    assert "reasoning_effort: high  # inline" in written

    backup_path = config_path.with_name(config_path.name + config_editor._BACKUP_SUFFIX)
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == original_text

    assert reload_calls["n"] == 1, "reload_config must be invoked exactly once after a successful save"


def test_put_missing_yaml_returns_400(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(None)
    assert excinfo.value.status == 400


# ── Denylist: flat webui_* keys (regression for the proven bypasses) ──────
#
# _is_denylisted_path used to require path[0] == "webui" exactly, matching
# only a hypothetical nested `webui: {auth: ..., security: ...}` shape. The
# real config.yaml uses flat `webui_<name>` top-level keys throughout, so
# that check never fired for them and the raw editor could silently rewrite
# auth, script-execution, and outbound-routing settings the denylist was
# meant to protect. Each test below proves one of the three exploits the
# audit demonstrated end-to-end, then confirms the write never landed.


def test_put_denylist_blocks_flat_webui_oidc_change(tmp_path, monkeypatch):
    """Auth bypass: an attacker-controlled OIDC issuer can mint id_tokens the
    server will accept (api/auth_oidc.py:57-179)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "webui_oidc:\n  issuer: https://trusted.example.com\n  client_id: real-client\n"
        "agent:\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = (
        "webui_oidc:\n  issuer: https://attacker.example.com\n  client_id: real-client\n"
        "agent:\n  reasoning_effort: high\n"
    )
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(p.startswith("webui_oidc") for p in excinfo.value.extra.get("blocked_paths", []))
    assert "attacker.example.com" not in config_path.read_text(encoding="utf-8")


def test_put_denylist_blocks_flat_webui_prefill_messages_script_change(tmp_path, monkeypatch):
    """RCE: webui_prefill_messages_script is shlex.split()'d and run via
    subprocess.run() on every session prefill (api/streaming.py:836-899)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "webui_prefill_messages_script: /opt/hermes/prefill.sh\n"
        "agent:\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "webui_prefill_messages_script: /tmp/evil.sh\nagent:\n  reasoning_effort: high\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(
        p.startswith("webui_prefill_messages_script") for p in excinfo.value.extra.get("blocked_paths", [])
    )
    assert "evil.sh" not in config_path.read_text(encoding="utf-8")


def test_put_denylist_blocks_flat_webui_gateway_base_url_change(tmp_path, monkeypatch):
    """SSRF + credential exfiltration: webui_gateway_base_url picks the
    target host for gateway chat, including the API key (api/gateway_chat.py:150-764)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "webui_gateway_base_url: http://127.0.0.1:8642\nagent:\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "webui_gateway_base_url: http://attacker.example.com\nagent:\n  reasoning_effort: high\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(
        p.startswith("webui_gateway_base_url") for p in excinfo.value.extra.get("blocked_paths", [])
    )
    assert "attacker.example.com" not in config_path.read_text(encoding="utf-8")


# ── Denylist: ADD case (the key was absent, not merely changed) ───────────
#
# _find_denylist_violations walks the union of old and new top-level keys,
# so a brand-new sensitive key should be caught the same way a changed one
# is — but that path was only exercised implicitly, not by a dedicated test.
# These pin it explicitly so a future rewrite of the walk (e.g. one that
# only diffs keys already present in `old`) fails loudly instead of quietly
# reopening the bypass for configs that never had the key to begin with.


def test_put_denylist_blocks_flat_webui_oidc_add(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = (
        "agent:\n  reasoning_effort: high\n"
        "webui_oidc:\n  issuer: https://attacker.example.com\n  client_id: injected\n"
    )
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(p.startswith("webui_oidc") for p in excinfo.value.extra.get("blocked_paths", []))
    assert "attacker.example.com" not in config_path.read_text(encoding="utf-8")


def test_put_denylist_blocks_flat_webui_prefill_messages_script_add(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "agent:\n  reasoning_effort: high\nwebui_prefill_messages_script: /tmp/evil.sh\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(
        p.startswith("webui_prefill_messages_script") for p in excinfo.value.extra.get("blocked_paths", [])
    )
    assert "evil.sh" not in config_path.read_text(encoding="utf-8")


def test_put_denylist_blocks_flat_webui_gateway_base_url_add(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "agent:\n  reasoning_effort: high\nwebui_gateway_base_url: http://attacker.example.com\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(
        p.startswith("webui_gateway_base_url") for p in excinfo.value.extra.get("blocked_paths", [])
    )
    assert "attacker.example.com" not in config_path.read_text(encoding="utf-8")


# ── Denylist: bare (non-webui_-prefixed) sensitive key ─────────────────────


def test_put_denylist_blocks_bare_prefill_messages_script_change(tmp_path, monkeypatch):
    """api/routes.py's _joplin_prefill_script_path() falls back to the bare
    `prefill_messages_script` key alongside the webui_-prefixed one — not
    exploitable via the current subprocess-executing path in
    api/streaming.py, but semantically the same RCE-shaped setting, guarded
    pre-emptively so a future change to the execution path can't silently
    reopen it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "prefill_messages_script: /opt/hermes/prefill.sh\nagent:\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "prefill_messages_script: /tmp/evil.sh\nagent:\n  reasoning_effort: high\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(
        p.startswith("prefill_messages_script") for p in excinfo.value.extra.get("blocked_paths", [])
    )
    assert "evil.sh" not in config_path.read_text(encoding="utf-8")


def test_put_denylist_blocks_bare_prefill_messages_script_add(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "agent:\n  reasoning_effort: high\nprefill_messages_script: /tmp/evil.sh\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any(
        p.startswith("prefill_messages_script") for p in excinfo.value.extra.get("blocked_paths", [])
    )
    assert "evil.sh" not in config_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "flat_key",
    [
        "webui_oidc",
        "webui_auth_something",
        "webui_security_flag",
        "webui_trusted_hosts",
        "webui_passkey_enabled",
        "webui_prefill_messages_script",
        "webui_prefill_messages_script_timeout",
        "webui_gateway_base_url",
        "webui_gateway_use_runs_api",
        "webui_chat_backend",
        "prefill_messages_script",
    ],
)
def test_is_denylisted_path_covers_sensitive_flat_webui_keys(flat_key):
    assert config_editor._is_denylisted_path((flat_key,)) is True
    assert config_editor._is_denylisted_path((flat_key, "nested_field")) is True


def test_is_denylisted_path_does_not_over_block_unrelated_webui_keys():
    # Plain UI-facing settings, not auth/execution/routing — must stay editable.
    assert config_editor._is_denylisted_path(("webui_version",)) is False
    assert config_editor._is_denylisted_path(("webui_external_notes_sources",)) is False


# ── File mode preservation ────────────────────────────────────────────────


def test_write_config_atomic_preserves_file_mode(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    os.chmod(config_path, 0o640)
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")
    _patch_reload_counter(monkeypatch)

    config_editor.put_config_raw("agent:\n  reasoning_effort: low\n")

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o640, f"file mode must be preserved across a save, got {oct(mode)}"


# ── Optimistic concurrency (etag) ───────────────────────────────────────────


def test_get_config_raw_includes_etag(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)

    result = config_editor.get_config_raw()

    assert result["etag"] == config_editor._etag_for(config_path.read_bytes())


def test_put_stale_etag_returns_409_and_does_not_write(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original = "agent:\n  reasoning_effort: high\n"
    config_path.write_text(original, encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    stale_etag = config_editor._etag_for(b"not-the-real-content")
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("agent:\n  reasoning_effort: low\n", etag=stale_etag)
    assert excinfo.value.status == 409
    assert excinfo.value.extra.get("etag") == config_editor._etag_for(original.encode("utf-8"))
    assert config_path.read_text(encoding="utf-8") == original, "a 409 must never write"


def test_put_matching_etag_succeeds(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original = "agent:\n  reasoning_effort: high\n"
    config_path.write_text(original, encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")
    _patch_reload_counter(monkeypatch)

    current_etag = config_editor._etag_for(original.encode("utf-8"))
    new_text = "agent:\n  reasoning_effort: low\n"
    result = config_editor.put_config_raw(new_text, etag=current_etag)

    assert result["ok"] is True
    assert config_path.read_text(encoding="utf-8") == new_text


def test_put_omitted_etag_skips_freshness_check(tmp_path, monkeypatch):
    """etag is optional (backward compatible with callers that don't send
    one) — omitting it must still save normally."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")
    _patch_reload_counter(monkeypatch)

    new_text = "agent:\n  reasoning_effort: low\n"
    result = config_editor.put_config_raw(new_text)

    assert result["ok"] is True
    assert config_path.read_text(encoding="utf-8") == new_text


# ── Adversarial redaction ───────────────────────────────────────────────────
# The previous redactor was a hand-written indentation/`key: value` scanner. It
# had no concept of flow mappings, flow-sequence mappings or aliases, so several
# perfectly valid YAML shapes reached the browser with live credentials while
# the response claimed the text was redacted. Redaction is now decided on a
# complete parse (`yaml.compose`) and applied by slicing the exact source spans,
# which keeps comments/formatting AND cannot be fooled by a grammar gap.

_SECRET = "sk-live-DO-NOT-LEAK-0123456789"


def _redacted(text):
    return config_editor._redact_yaml_text(text)


@pytest.mark.parametrize(
    "label,text",
    [
        ("nested_flow", "provider:\n  options: {api_key: %s}\n"),
        ("flow_sequence", "providers:\n  - {name: demo, access_token: %s}\n"),
        (
            "authorization_header",
            "mcp_servers:\n  demo:\n    headers:\n      Authorization: Bearer %s\n",
        ),
        ("cookie", "http:\n  cookie: session=%s\n"),
        ("private_key", "tls:\n  private_key: %s\n"),
        ("credentials", "store:\n  credentials: %s\n"),
        ("passphrase", "ssh:\n  passphrase: %s\n"),
        ("quoted_key", "provider:\n  \"api-key\": '%s'\n"),
        ("single_quoted_value", "provider:\n  api_key: '%s'\n"),
        ("double_quoted_value", 'provider:\n  api_key: "%s"\n'),
        ("block_scalar", "provider:\n  secret: |\n    %s\n"),
        ("folded_scalar", "provider:\n  secret: >\n    %s\n"),
        ("nested_under_sensitive", "auth:\n  inner:\n    anything: %s\n"),
        ("sequence_under_sensitive", "auth:\n  tokens:\n    - %s\n"),
        ("flow_seq_under_sensitive", "auth: {tokens: [%s]}\n"),
        ("deep_flow", "a: {b: {c: {password: %s}}}\n"),
    ],
)
def test_redaction_covers_every_yaml_shape(label, text):
    document = text % _SECRET
    redacted, paths = _redacted(document)
    assert _SECRET not in redacted, f"{label}: credential leaked -> {redacted!r}"
    assert paths, f"{label}: leak claimed redacted but no path was reported"


def test_redaction_follows_an_alias_to_its_anchor():
    """`defaults: &d SECRET` / `provider: {api_key: *d}` leaked verbatim.

    The anchor sits under a benign key, so a scanner that only looks at the
    line it is on never sees that the value is reachable through a sensitive
    path.
    """
    document = f"defaults: &d {_SECRET}\nprovider:\n  api_key: *d\n"
    redacted, paths = _redacted(document)

    assert _SECRET not in redacted, redacted
    # The anchor must survive so the alias still resolves.
    assert "&d" in redacted
    assert "*d" in redacted
    import yaml
    assert yaml.safe_load(redacted) is not None
    assert paths


def test_redaction_follows_a_merge_key():
    document = (
        f"base: &base\n  api_key: {_SECRET}\n"
        "provider:\n  <<: *base\n  model: gpt\n"
    )
    redacted, _paths = _redacted(document)
    assert _SECRET not in redacted, redacted


def test_redaction_output_is_still_valid_yaml_and_keeps_comments():
    document = (
        "# top comment\n"
        "agent:\n"
        "  reasoning_effort: high  # inline comment\n"
        f"provider:\n  options: {{api_key: {_SECRET}, model: gpt}}\n"
    )
    redacted, _paths = _redacted(document)

    import yaml
    parsed = yaml.safe_load(redacted)
    assert parsed["agent"]["reasoning_effort"] == "high"
    assert parsed["provider"]["options"]["model"] == "gpt"
    assert parsed["provider"]["options"]["api_key"] == config_editor._REDACTED_PLACEHOLDER
    assert "# top comment" in redacted
    assert "# inline comment" in redacted


def test_redaction_leaves_non_credential_values_untouched():
    document = (
        "agent:\n  reasoning_effort: high\n"
        "mcpServers:\n  foo:\n    allowed_tools:\n      - read\n"
    )
    redacted, paths = _redacted(document)
    assert redacted == document
    assert paths == []


def test_get_fails_closed_on_unparsable_yaml(tmp_path, monkeypatch):
    """A document we cannot parse is one we cannot prove is safe to show."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"provider:\n  api_key: {_SECRET}\n  broken: [unclosed\n", encoding="utf-8"
    )
    _patch_config_path(monkeypatch, config_path)

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.get_config_raw()
    assert excinfo.value.status == 409
    assert _SECRET not in str(excinfo.value)


def test_get_surfaces_a_read_failure_instead_of_an_empty_editor(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)

    def boom(self, *args, **kwargs):
        raise OSError("EIO")

    monkeypatch.setattr(type(config_path), "read_bytes", boom)

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.get_config_raw()
    assert excinfo.value.status == 500


def test_put_converts_a_write_failure_into_the_editor_error_contract(
    tmp_path, monkeypatch
):
    """An OSError used to escape the route's `except ConfigEditorError`."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("custom_field: original\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    def boom(_config_path, _text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config_editor, "_write_config_atomic", boom)

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("custom_field: changed\n")

    assert excinfo.value.status == 500
    # No filesystem path leakage in the client-facing message.
    assert str(tmp_path) not in str(excinfo.value)


# ── Redaction: mapping KEYS are data too ────────────────────────────────────


def _redact(text):
    return config_editor._redact_yaml_text(text)


def test_a_credential_used_as_a_mapping_key_is_redacted():
    """Under a sensitive parent, the KEY carries credential material too.

    The walk only ever descended into values, so `api_keys: {<secret>: alice}`
    replaced nothing that mattered: the secret was the key, and it reached the
    browser verbatim inside a response that claimed to be redacted.
    """
    text = "api_keys:\n  sk-live-SECRET-TOKEN: alice\n  sk-live-OTHER-TOKEN: bob\n"
    out, paths = _redact(text)
    assert "sk-live-SECRET-TOKEN" not in out, "credential-bearing key survived redaction"
    assert "sk-live-OTHER-TOKEN" not in out
    assert paths, "redacted keys must appear in the manifest"


def test_a_sensitive_key_name_is_not_itself_redacted():
    """`api_key` is a field name, not a secret — only its value is replaced."""
    text = "provider:\n  api_key: SECRET\n  model: gpt-4\n"
    out, _paths = _redact(text)
    assert "api_key:" in out, "the field name must stay readable"
    assert "SECRET" not in out
    assert "gpt-4" in out


def test_a_complex_key_carries_its_own_sensitivity():
    """A non-scalar key was coerced to an empty segment, losing sensitivity.

    The parent here is deliberately NOT sensitive: the only thing that can mark
    this entry is the complex key itself. An earlier version of this test used
    a `credentials:` parent, which was already sensitive on its own — so it
    passed even with the complex-key handling removed, and pinned nothing.
    """
    text = "settings:\n  ? [api_key, primary]\n  : SECRET-VALUE\n"
    out, _paths = _redact(text)
    assert "SECRET-VALUE" not in out, (
        "a complex key containing a credential name did not mark its value"
    )


def test_a_complex_key_under_a_sensitive_parent_stays_redacted():
    """Inheritance must survive an unclassifiable key, too."""
    text = "credentials:\n  ? [alpha, beta]\n  : SECRET-VALUE\n"
    out, _paths = _redact(text)
    assert "SECRET-VALUE" not in out


def test_a_tag_before_an_anchor_keeps_the_document_parseable():
    """`!tag &name value` lost its anchor, so later aliases stopped resolving."""
    import yaml

    text = (
        "defaults:\n"
        "  api_key: !!str &shared SECRET\n"
        "provider:\n"
        "  api_key: *shared\n"
    )
    out, _paths = _redact(text)
    assert "SECRET" not in out
    # The real proof: the redacted document must still parse, with the alias
    # resolving to the anchor that survived.
    reparsed = yaml.safe_load(out)
    assert reparsed["provider"]["api_key"] == reparsed["defaults"]["api_key"]


@pytest.mark.parametrize("text", [
    "a:\n  api_key: &s SECRET\nb:\n  token: *s\n",
    "a:\n  api_key: !!str &s SECRET\nb:\n  token: *s\n",
    "a: &anchor\n  api_key: SECRET\nb: *anchor\n",
])
def test_every_redacted_result_reparses(text):
    import yaml

    out, _paths = _redact(text)
    assert "SECRET" not in out
    yaml.safe_load(out)  # must not raise


def test_one_span_reached_by_two_sensitive_paths_reports_both():
    """The memo recorded only the first path, under-reporting the manifest."""
    text = (
        "shared: &s SECRET\n"
        "first:\n"
        "  api_key: *s\n"
        "second:\n"
        "  access_token: *s\n"
    )
    out, paths = _redact(text)
    assert "SECRET" not in out
    assert any(p.startswith("first") for p in paths), paths
    assert any(p.startswith("second") for p in paths), paths


# ── GET keeps the error contract at the route ───────────────────────────────


class _RouteHandler:
    """Minimal handler: records what the route actually sent."""

    def __init__(self):
        self.status = None
        self.headers_sent = {}
        self.body = b""
        self.command = "GET"
        self.headers = {}
        self.client_address = ("127.0.0.1", 1234)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass

    @property
    def wfile(self):
        outer = self

        class _W:
            def write(self, data):
                outer.body += data

        return _W()


def _get_config_raw_via_route(monkeypatch):
    """Drive the real GET route, not the helper it calls."""
    import json
    from urllib.parse import urlparse

    import api.routes as routes

    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)

    handler = _RouteHandler()
    routes.handle_get(handler, urlparse("/api/config/raw"))
    payload = json.loads(handler.body.decode("utf-8")) if handler.body else {}
    return handler.status, payload


def test_the_get_route_returns_409_with_line_and_column(tmp_path, monkeypatch):
    """Helper-only tests never covered this: the route dropped the contract.

    `get_config_raw()` raises ConfigEditorError with a usable status, but only
    PUT caught it — GET let it hit the server-level fallback, which turned a
    409 plus line/column into a generic 500 with no detail.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text("valid: yes\n  bad_indent: [unclosed\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)

    status, payload = _get_config_raw_via_route(monkeypatch)
    assert status == 409, f"expected the parse-failure status, got {status}: {payload}"
    assert "line" in payload and "column" in payload, payload
    assert "yaml" not in payload, "unparsable config must not be shown"


def test_the_get_route_surfaces_a_read_failure(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("custom_field: value\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)

    # Patch the I/O itself, not _read_config_bytes — that helper is what turns
    # an OSError into the sanitized ConfigEditorError, so replacing it would
    # test the route against an error the production path never produces.
    def boom(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(type(config_path), "read_bytes", boom)

    status, payload = _get_config_raw_via_route(monkeypatch)
    assert status == 500
    import json

    assert str(tmp_path) not in json.dumps(payload), "no path leakage to the client"


# ── Denylist: nested settings the installed agent actually consumes ──────────


@pytest.mark.parametrize("path", [
    ("browser", "allow_private_urls"),
    ("security", "allow_private_urls"),
    ("channels", "telegram", "allowed_users"),
    ("channels", "matrix", "allowed_rooms"),
    ("channels", "discord", "allow_bots"),
    ("gateway", "allowed_source_cidrs"),
])
def test_nested_security_settings_stay_denylisted(path):
    """These are consumed runtime paths, not invented shapes.

    `browser.allow_private_urls` / `security.allow_private_urls` gate the
    private-IP SSRF check (tools/url_safety.py, hermes_cli/config.py); the
    channel lists decide who may talk to the agent at all. A top-level-only
    `allow*` rule left every one of them editable through the raw editor.
    """
    assert config_editor._is_denylisted_path(path), path


@pytest.mark.parametrize("path", [
    ("models", "gpt-4", "allowed_context_length"),
    ("channels", "discord", "allowed_mentions"),
])
def test_benign_nested_allow_fields_stay_editable(path):
    """Fields that are not a trust boundary must stay editable."""
    assert not config_editor._is_denylisted_path(path), path


def test_allowed_tools_is_a_capability_boundary():
    """It reads like a filter, but an editable allowlist grants as well as it
    restricts: adding an entry hands the agent a tool the operator withheld.

    This was the example used to argue the nested rule could be relaxed, which
    is exactly why it needs its own assertion.
    """
    assert config_editor._is_denylisted_path(("mcpServers", "github", "allowed_tools"))


def test_a_denylisted_key_under_a_numeric_key_is_still_caught():
    """The diff walker compared stringified keys against the real mapping.

    YAML makes `1:` an int, so collecting {str(k)} and then looking those
    strings up missed on both sides: the walker reported no difference and
    never descended, leaving everything below a numeric key editable.
    """
    violations = config_editor._find_denylist_violations(
        {"channels": {1: {"allowed_users": ["alice"]}}},
        {"channels": {1: {"allowed_users": ["alice", "mallory"]}}},
    )
    assert violations == ["channels.1.allowed_users"], violations


def test_a_type_forcing_tag_is_dropped_from_a_redacted_value():
    """`!!binary '•••REDACTED•••'` does not load — the placeholder is not base64.

    The tag only described a value that no longer exists, so keeping it turned
    redaction into a document that fails to parse. Anchors still survive,
    because aliases depend on them.
    """
    import yaml

    out, _paths = _redact("creds:\n  api_key: !!binary |\n    U0VDUkVU\n")
    assert "U0VDUkVU" not in out
    assert "!!binary" not in out
    yaml.safe_load(out)  # must not raise

    anchored, _paths = _redact("a:\n  api_key: !!str &s SECRET\nb:\n  token: *s\n")
    reparsed = yaml.safe_load(anchored)
    assert "&s" in anchored, "the anchor must survive even when its tag does not"
    assert reparsed["b"]["token"] == reparsed["a"]["api_key"]


def test_a_multi_document_config_fails_closed():
    """yaml.compose() refuses a multi-document stream, which must not be
    swallowed: the caller turns YAMLError into a 409 rather than showing a
    document it could not fully analyse."""
    import yaml

    with pytest.raises(yaml.YAMLError):
        _redact("first: ok\n---\nprovider:\n  api_key: SECRET\n")


def test_a_private_url_toggle_cannot_be_flipped_through_put(tmp_path, monkeypatch):
    """End to end through the write path, not just the predicate."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("browser:\n  allow_private_urls: false\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("browser:\n  allow_private_urls: true\n")

    assert excinfo.value.status == 400
    assert "browser.allow_private_urls" in str(excinfo.value.extra.get("blocked_paths", []))
    assert "allow_private_urls: false" in config_path.read_text(encoding="utf-8")


def test_put_rejects_a_multi_document_submission(tmp_path, monkeypatch):
    """A second YAML document must not ride past the denylist.

    Raised as a P1: `safe_load` was said to keep the first document silently and
    discard the rest, so a submission could hide `webui_oidc` behind a `---` and
    still be written verbatim. Measured, it raises instead, and put_config_raw
    turns that into a 400 with the file untouched — but nothing pinned that, so
    this test does.
    """
    config_path = tmp_path / "config.yaml"
    original = "agent:\n  model: gpt-4\n"
    config_path.write_text(original, encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    attack = (
        "agent:\n  model: gpt-4\n"
        "---\n"
        "webui_oidc:\n  issuer: https://attacker.example.com\n  client_id: injected\n"
    )
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(attack)

    assert excinfo.value.status == 400
    assert config_path.read_text(encoding="utf-8") == original
    assert "attacker.example.com" not in config_path.read_text(encoding="utf-8")


# ── Re-gate 2026-07-26: the whole RESPONSE must be clean ────────────────────


def test_a_credential_key_leaks_into_neither_yaml_nor_manifest(tmp_path, monkeypatch):
    """Redacting the YAML while naming the secret in `redacted` is not redaction.

    The manifest exists to tell an operator WHERE something was hidden. Using
    the credential itself as the path segment handed it back one field over —
    and an earlier test missed it by asserting only that `paths` was nonempty.
    """
    import json

    secret = "sk-live-KEYMATERIAL-1234567890"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"api_keys:\n  {secret}: alice\n", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)

    result = config_editor.get_config_raw()

    assert secret not in result["yaml"]
    for entry in result["redacted"]:
        assert secret not in entry, f"credential returned in the manifest: {entry}"
    assert secret not in json.dumps(result), "credential survived somewhere in the response"
    assert result["redacted"], "the redaction still has to be reported"


def test_a_verbatim_uri_tag_keeps_its_anchor(tmp_path, monkeypatch):
    """`!<tag:yaml.org,2002:str>` contains a comma.

    The property regex excluded commas from tag tokens, so the anchor was not
    preserved and a later alias became undefined — GET returned YAML that does
    not load.
    """
    import yaml

    for text in (
        "a:\n  api_key: !<tag:yaml.org,2002:str> &shared SECRET\nb:\n  token: *shared\n",
        "a:\n  api_key: &shared !<tag:yaml.org,2002:str> SECRET\nb:\n  token: *shared\n",
    ):
        out, _paths = config_editor._redact_yaml_text(text)
        assert "SECRET" not in out
        reparsed = yaml.safe_load(out)
        assert reparsed["b"]["token"] == reparsed["a"]["api_key"]


def test_the_editor_follows_the_authoritative_config_path(tmp_path, monkeypatch):
    """HERMES_CONFIG_PATH must win, in production shape.

    The resolver reconstructed `<active_home>/config.yaml` unless a test had
    patched it, so the editor read and wrote one file while reload_config()
    used another — and no test covered the production branch, because the
    branch only existed when NOT under test.
    """
    override = tmp_path / "override.yaml"
    override.write_text("agent:\n  model: from-override\n", encoding="utf-8")
    active_home_cfg = tmp_path / "home" / "config.yaml"
    active_home_cfg.parent.mkdir(parents=True)
    active_home_cfg.write_text("agent:\n  model: from-active-home\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override))
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    assert config_editor._active_profile_config_path() == override
    assert "from-override" in config_editor.get_config_raw()["yaml"]

    config_editor.put_config_raw("agent:\n  model: written-through-editor\n")
    assert "written-through-editor" in override.read_text(encoding="utf-8")
    assert active_home_cfg.read_text(encoding="utf-8") == "agent:\n  model: from-active-home\n"


@pytest.mark.parametrize("path", [
    ("toolsets",),
    ("agent", "toolsets"),
    ("platform_toolsets",),
    ("channels", "telegram", "platform_toolsets"),
    ("browser", "restrict_evaluate"),
])
def test_consumed_capability_settings_are_denylisted(path):
    """These grant a surface or disable a guard, and none starts with "allow".

    A name-prefix rule cannot see them: the dangerous settings are not named
    after their danger.
    """
    assert config_editor._is_denylisted_path(path), path


def test_put_cannot_grant_a_toolset(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original = "agent:\n  toolsets:\n    - read\n"
    config_path.write_text(original, encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw("agent:\n  toolsets:\n    - read\n    - shell\n")
    assert excinfo.value.status == 400
    assert config_path.read_text(encoding="utf-8") == original
