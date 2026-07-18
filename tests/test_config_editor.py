"""Tests for the raw config.yaml viewer/editor (api/config_editor.py).

Covers: GET redaction of credential-shaped values (incl. multiline block
scalars) with a path manifest, and the PUT write path's gates in order —
write-disabled (403), invalid YAML (400 + line/column), non-mapping (400),
resubmitted redaction placeholders (400), the security-key denylist (400 +
blocked_paths), and a successful write (atomic, comments preserved via raw
text roundtrip, backup file created, reload_config invoked).
"""

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


def test_put_denylist_blocks_allow_prefixed_key_anywhere(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mcpServers:\n  foo:\n    allowed_tools:\n      - read\n",
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    monkeypatch.setenv(config_editor._WRITE_GATE_ENV, "1")

    new_text = "mcpServers:\n  foo:\n    allowed_tools:\n      - read\n      - write\n"
    with pytest.raises(config_editor.ConfigEditorError) as excinfo:
        config_editor.put_config_raw(new_text)
    assert excinfo.value.status == 400
    assert any("allowed_tools" in p for p in excinfo.value.extra.get("blocked_paths", []))


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

    assert result == {"ok": True}
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
