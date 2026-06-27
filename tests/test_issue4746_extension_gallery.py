"""Behavioral tests for the extension gallery install/uninstall/registry feature."""

import hashlib
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_zip(files: dict) -> bytes:
    """Build an in-memory zip containing the given {name: content} mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _setup_ext_env(monkeypatch, tmp_path):
    """Point extension root and state dir at tmp_path subdirectories."""
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(ext_dir))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    import api.extensions as ext_mod
    monkeypatch.setattr(ext_mod, "_extension_state_dir", lambda: state_dir)
    return ext_dir, state_dir


def test_install_valid(monkeypatch, tmp_path):
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    files = {
        "manifest.json": json.dumps({"version": "1.2.3"}),
        "index.js": "console.log('hello');",
    }
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    mock_resp = MagicMock()
    mock_resp.read.return_value = zip_bytes
    mock_resp.close = MagicMock()
    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    result = ext_mod.install_extension(
        "my-ext",
        "https://hermes-webui.github.io/exts/my-ext.zip",
        sha,
    )
    assert result["installed"] is True
    assert result["id"] == "my-ext"
    assert result["version"] == "1.2.3"
    assert (ext_dir / "my-ext" / "index.js").exists()
    manifest = ext_mod._load_install_manifest()
    assert "my-ext" in manifest["installed"]
    assert "index.js" in manifest["installed"]["my-ext"]["files"]


def test_install_prefixed_zip(monkeypatch, tmp_path):
    """Zip members rooted under <id>/ are stripped so files land at ext_dir/<id>/."""
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    files = {
        "my-ext/manifest.json": json.dumps({"version": "2.0.0"}),
        "my-ext/index.js": "console.log('prefixed');",
        "my-ext/sub/style.css": "body{}",
    }
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    result = ext_mod.install_extension(
        "my-ext",
        "https://hermes-webui.github.io/exts/my-ext.zip",
        sha,
    )
    assert result["installed"] is True
    assert result["version"] == "2.0.0"
    assert (ext_dir / "my-ext" / "manifest.json").exists()
    assert (ext_dir / "my-ext" / "index.js").exists()
    assert (ext_dir / "my-ext" / "sub" / "style.css").exists()
    # Verify NO double-nested directory
    assert not (ext_dir / "my-ext" / "my-ext").exists()
    manifest = ext_mod._load_install_manifest()
    assert "manifest.json" in manifest["installed"]["my-ext"]["files"]


def test_gallery_installed_extension_becomes_runtime_manifest(monkeypatch, tmp_path):
    """Installed gallery extensions are injected without a separate manifest env var."""
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_MANIFEST", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    import api.extensions as ext_mod

    files = {
        "my-ext/manifest.json": json.dumps(
            {
                "version": "1.0.0",
                "extensions": [
                    {
                        "id": "my-ext",
                        "name": "My Extension",
                        "scripts": ["assets/app.js"],
                        "stylesheets": ["assets/app.css"],
                    }
                ],
            }
        ),
        "my-ext/assets/app.js": "console.log('gallery runtime');",
        "my-ext/assets/app.css": "body{}",
    }
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    ext_mod.install_extension(
        "my-ext",
        "https://hermes-webui.github.io/exts/my-ext.zip",
        sha,
    )

    assert ext_mod.get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/my-ext/assets/app.js"],
        "stylesheet_urls": ["/extensions/my-ext/assets/app.css"],
    }
    status = ext_mod.get_extension_status()
    assert status["manifest"]["status"] == "gallery_installed"
    assert status["counts"]["manifest_extensions"] == 1
    assert status["extensions"][0]["id"] == "my-ext"


def test_install_bootstraps_managed_default_root_without_env(monkeypatch, tmp_path):
    """Plug-and-play (#4933): one-click install works with NO env configured.

    With HERMES_WEBUI_EXTENSION_DIR unset and no extension dir on disk, the
    first install must bootstrap the WebUI-managed default
    (STATE_DIR/extensions), land the files there, and make the extension load
    via get_extension_config() — all without any environment setup.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_MANIFEST", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    import api.extensions as ext_mod

    monkeypatch.setattr(ext_mod, "_extension_state_dir", lambda: state_dir)

    default_root = state_dir / "extensions"
    # Pre-install: nothing exists yet, gallery is "configured" but not valid.
    assert not default_root.exists()
    pre = ext_mod.get_extension_status()
    assert pre["enabled"] is False
    assert pre["extension_dir_configured"] is True
    assert pre["extension_dir_valid"] is False
    assert pre["warnings"] == []

    files = {
        "plug-ext/manifest.json": json.dumps(
            {
                "version": "1.0.0",
                "extensions": [
                    {
                        "id": "plug-ext",
                        "name": "Plug And Play",
                        "scripts": ["app.js"],
                    }
                ],
            }
        ),
        "plug-ext/app.js": "console.log('plug and play');",
    }
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()
    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    result = ext_mod.install_extension(
        "plug-ext",
        "https://hermes-webui.github.io/exts/plug-ext.zip",
        sha,
    )
    assert result["installed"] is True

    # The managed default root was created on demand and holds the files.
    assert default_root.is_dir()
    assert (default_root / "plug-ext" / "app.js").exists()

    # And the extension now loads with zero env configuration.
    assert ext_mod.get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/plug-ext/app.js"],
        "stylesheet_urls": [],
    }
    status = ext_mod.get_extension_status()
    assert status["enabled"] is True
    assert status["extension_dir_configured"] is True
    assert status["extension_dir_valid"] is True
    assert status["manifest"]["status"] == "gallery_installed"
    assert status["extensions"][0]["id"] == "plug-ext"


def test_install_bad_hash(monkeypatch, tmp_path):
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    zip_bytes = _make_zip({"index.js": "code"})
    wrong_sha = "a" * 64

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    with pytest.raises(ext_mod.ExtensionInstallError, match="SHA-256"):
        ext_mod.install_extension(
            "bad-ext",
            "https://hermes-webui.github.io/exts/bad-ext.zip",
            wrong_sha,
        )
    assert not (ext_dir / "bad-ext").exists()


def test_install_zipslip(monkeypatch, tmp_path):
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
    zip_bytes = buf.getvalue()
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    with pytest.raises(ext_mod.ExtensionInstallError):
        ext_mod.install_extension(
            "slip-ext",
            "https://hermes-webui.github.io/exts/slip-ext.zip",
            sha,
        )
    assert not (tmp_path / "evil.txt").exists()


def test_uninstall(monkeypatch, tmp_path):
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    files = {"index.js": "code", "style.css": "body{}"}
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    ext_mod.install_extension(
        "rm-ext",
        "https://hermes-webui.github.io/exts/rm-ext.zip",
        sha,
    )
    assert (ext_dir / "rm-ext" / "index.js").exists()

    result = ext_mod.uninstall_extension("rm-ext")
    assert result["uninstalled"] is True
    assert not (ext_dir / "rm-ext" / "index.js").exists()
    assert not (ext_dir / "rm-ext" / "style.css").exists()
    assert not (ext_dir / "rm-ext").exists()
    manifest = ext_mod._load_install_manifest()
    assert "rm-ext" not in manifest["installed"]


def test_uninstall_cleans_nested_dirs(monkeypatch, tmp_path):
    """Uninstall removes empty subdirectories left by nested files."""
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    files = {
        "deep-ext/manifest.json": "{}",
        "deep-ext/sub/a/b.js": "code",
        "deep-ext/sub/c.css": "body{}",
    }
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    ext_mod.install_extension(
        "deep-ext",
        "https://hermes-webui.github.io/exts/deep-ext.zip",
        sha,
    )
    assert (ext_dir / "deep-ext" / "sub" / "a" / "b.js").exists()

    ext_mod.uninstall_extension("deep-ext")
    assert not (ext_dir / "deep-ext").exists()


def test_install_rollback(monkeypatch, tmp_path):
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    files = {"first.js": "a", "second.js": "b"}
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    write_count = [0]
    original_write_bytes = Path.write_bytes

    def patched_write_bytes(self, data):
        write_count[0] += 1
        if write_count[0] >= 2:
            raise OSError("simulated write failure")
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", patched_write_bytes)

    with pytest.raises(ext_mod.ExtensionInstallError, match="Extraction failed"):
        ext_mod.install_extension(
            "roll-ext",
            "https://hermes-webui.github.io/exts/roll-ext.zip",
            sha,
        )
    remaining = list((ext_dir / "roll-ext").glob("**/*")) if (ext_dir / "roll-ext").exists() else []
    assert remaining == []


def test_gallery_registry_list_format(monkeypatch, tmp_path):
    """Registry response as a top-level list (original format)."""
    import api.extensions as ext_mod

    registry_data = [
        {"id": "ext-one", "name": "Extension One", "version": "0.1.0", "description": "First"},
        {"id": "ext-two", "name": "Extension Two", "version": "0.2.0", "description": "Second"},
    ]

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(registry_data).encode("utf-8")

    ext_mod._REGISTRY_CACHE.clear()
    monkeypatch.setattr(ext_mod, "urlopen", lambda *a, **kw: mock_resp)

    result = ext_mod.get_extension_registry()
    assert "entries" in result
    assert len(result["entries"]) == 2
    assert result["entries"][0]["id"] == "ext-one"
    assert result["entries"][1]["id"] == "ext-two"


def test_gallery_registry_extensions_format(monkeypatch, tmp_path):
    """Registry response wrapping extensions under {"extensions": [...]} (live format)."""
    import api.extensions as ext_mod

    registry_data = {
        "version": 1,
        "generated_at": "2026-06-24T18:30:40.265Z",
        "extensions": [
            {"id": "desktop-companion", "name": "Desktop Companion", "version": "0.1.0"},
        ],
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(registry_data).encode("utf-8")

    ext_mod._REGISTRY_CACHE.clear()
    monkeypatch.setattr(ext_mod, "urlopen", lambda *a, **kw: mock_resp)

    result = ext_mod.get_extension_registry()
    assert len(result["entries"]) == 1
    assert result["entries"][0]["id"] == "desktop-companion"


def test_install_rejects_symlinked_ext_dir_outside_root(monkeypatch, tmp_path):
    """Installation rejects a symlinked extension directory pointing outside root."""
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    target_dir = tmp_path / "symlink_target"
    target_dir.mkdir()
    link = ext_dir / "linked-ext"
    try:
        link.symlink_to(target_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    files = {"index.js": "code"}
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    with pytest.raises(ext_mod.ExtensionInstallError):
        ext_mod.install_extension(
            "linked-ext",
            "https://hermes-webui.github.io/exts/linked-ext.zip",
            sha,
        )
    assert not (target_dir / "index.js").exists()


def test_install_rejects_symlinked_ext_dir_inside_root(monkeypatch, tmp_path):
    """Installation rejects a pre-existing symlink even when target is within root."""
    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    import api.extensions as ext_mod

    target_dir = ext_dir / "real-target"
    target_dir.mkdir()
    link = ext_dir / "linked-ext"
    try:
        link.symlink_to(target_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    files = {"index.js": "code"}
    zip_bytes = _make_zip(files)
    sha = hashlib.sha256(zip_bytes).hexdigest()

    monkeypatch.setattr(ext_mod, "_safe_download", lambda *a, **kw: zip_bytes)

    with pytest.raises(ext_mod.ExtensionInstallError, match="symlink"):
        ext_mod.install_extension(
            "linked-ext",
            "https://hermes-webui.github.io/exts/linked-ext.zip",
            sha,
        )


def test_install_rejects_redirect_to_disallowed_host(monkeypatch, tmp_path):
    """Download rejects redirects to disallowed hosts."""
    import api.extensions as ext_mod

    def fake_download(url, max_bytes, timeout=30):
        raise ext_mod.ExtensionInstallError("Download redirected to disallowed host")

    ext_dir, state_dir = _setup_ext_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ext_mod, "_safe_download", fake_download)

    with pytest.raises(ext_mod.ExtensionInstallError, match="disallowed host"):
        ext_mod.install_extension(
            "redir-ext",
            "https://hermes-webui.github.io/exts/redir-ext.zip",
            "a" * 64,
        )
