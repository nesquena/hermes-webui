"""Unit and HTTP tests for the wallpaper feature.

Chunk 1 tests run api.wallpaper functions directly using the test-process's
own STATE_DIR isolation (we monkeypatch api.config to point STATE_DIR at
a tmp_path). Later tests in Chunk 2 hit the live test subprocess server
via HTTP.
"""
import hashlib
import importlib
import json
import pathlib

import pytest


# Minimal valid file headers / bodies for each accepted format.
# These are big enough for magic-byte sniffing and small enough to stay
# under any size limits. Real images would be much larger but we don't
# need a decode-able image -- just one whose magic bytes pass.
_JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00' + b'\x00' * 100
_PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
_WEBP_BYTES = b'RIFF\x80\x00\x00\x00WEBP' + b'\x00' * 100  # 4 + 4 (size) + 4 + payload
_SVG_BYTES = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
_HTML_BYTES = b'<!DOCTYPE html><html></html>'


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point STATE_DIR + SETTINGS_FILE to tmp_path so tests don't touch real state.

    api.config reads SETTINGS_FILE at module level for some default-loading
    code, but save_settings() / load_settings() re-read it each call, so
    monkeypatching the module attribute works for all the call sites we use.
    """
    import api.config
    monkeypatch.setattr(api.config, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(api.config, 'SETTINGS_FILE', tmp_path / 'settings.json')
    # Force-reload api.wallpaper so its module-level imports of STATE_DIR
    # pick up the patched value.
    import api.wallpaper
    importlib.reload(api.wallpaper)
    try:
        yield tmp_path
    finally:
        # Restore api.wallpaper's STATE_DIR binding to the real one so a
        # future in-process test that doesn't use this fixture won't write
        # into a deleted tmp_path.
        importlib.reload(api.wallpaper)


def test_save_wallpaper_jpeg_writes_file_and_settings(isolated_state):
    from api.wallpaper import save_wallpaper, get_wallpaper_path
    from api.config import load_settings

    result = save_wallpaper(_JPEG_BYTES)
    assert result['file'].startswith('wallpaper-')
    assert result['file'].endswith('.jpg')

    # File on disk
    path = get_wallpaper_path()
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == _JPEG_BYTES

    # settings.json updated
    settings = load_settings()
    assert settings['wallpaper_file'] == result['file']


def test_save_wallpaper_png_and_webp(isolated_state):
    from api.wallpaper import save_wallpaper

    r1 = save_wallpaper(_PNG_BYTES)
    assert r1['file'].endswith('.png')

    r2 = save_wallpaper(_WEBP_BYTES)
    assert r2['file'].endswith('.webp')


def test_save_wallpaper_rejects_svg_and_html(isolated_state):
    from api.wallpaper import save_wallpaper
    with pytest.raises(ValueError, match='must be JPEG, PNG, or WebP'):
        save_wallpaper(_SVG_BYTES)
    with pytest.raises(ValueError, match='must be JPEG, PNG, or WebP'):
        save_wallpaper(_HTML_BYTES)


def test_save_wallpaper_rejects_oversize(isolated_state):
    from api.wallpaper import save_wallpaper
    from api.config import MAX_WALLPAPER_BYTES
    oversize = _JPEG_BYTES + b'\x00' * (MAX_WALLPAPER_BYTES + 1)
    with pytest.raises(ValueError, match='too large'):
        save_wallpaper(oversize)


def test_save_wallpaper_replaces_old_file(isolated_state):
    """Uploading a second wallpaper deletes the first; only one file remains."""
    from api.wallpaper import save_wallpaper

    r1 = save_wallpaper(_JPEG_BYTES)
    r2 = save_wallpaper(_PNG_BYTES + b'\xde\xad')  # different bytes -> different hash

    files = sorted(p.name for p in isolated_state.glob('wallpaper-*'))
    assert files == [r2['file']]
    assert r2['file'] != r1['file']


def test_delete_wallpaper_clears_file_and_settings(isolated_state):
    from api.wallpaper import save_wallpaper, delete_wallpaper, get_wallpaper_path
    from api.config import load_settings

    save_wallpaper(_JPEG_BYTES)
    delete_wallpaper()

    assert get_wallpaper_path() is None
    assert load_settings()['wallpaper_file'] is None
    assert list(isolated_state.glob('wallpaper-*')) == []


def test_delete_wallpaper_when_unset_is_noop(isolated_state):
    """Calling delete with no wallpaper present is safe."""
    from api.wallpaper import delete_wallpaper, get_wallpaper_path
    delete_wallpaper()  # must not raise
    assert get_wallpaper_path() is None


def test_save_settings_rejects_arbitrary_wallpaper_file_path(isolated_state, monkeypatch):
    """save_settings() must reject wallpaper_file values that don't match the
    'wallpaper-<8 hex>.{jpg,png,webp}' format save_wallpaper() writes.

    Without this guard, a malicious POST /api/settings with
    {"wallpaper_file": "/etc/passwd"} or {"wallpaper_file": "../../foo.jpg"}
    would persist, and GET /api/wallpaper would happily serve any image
    file readable by the webui process from anywhere on disk.
    """
    import api.config as config

    # Pre-set a valid wallpaper_file to verify it's preserved when an
    # invalid value is rejected (rejection should mean "leave as-is",
    # not "wipe the field").
    config.save_settings({"wallpaper_file": "wallpaper-deadbeef.jpg"})
    assert config.load_settings()["wallpaper_file"] == "wallpaper-deadbeef.jpg"

    bad_inputs = [
        "/etc/passwd",                  # absolute system path
        "../../etc/passwd",             # relative traversal
        "../wallpaper-deadbeef.jpg",    # one-up-then-valid-name
        "wallpaper-deadbeef.svg",       # disallowed extension
        "wallpaper-DEADBEEF.jpg",       # wrong case (regex is lowercase-only)
        "wallpaper-12345.jpg",          # wrong digest length (5 chars)
        "wallpaper-deadbeefcafe.jpg",   # too-long digest
        "wallpaper-deadbeef.jpg.gz",    # extension suffix
        "myown.jpg",                    # not a wallpaper-* prefix
        "",                             # empty string
        123,                            # wrong type entirely
        {"file": "x"},                  # dict
    ]

    for bad in bad_inputs:
        config.save_settings({"wallpaper_file": bad})
        assert config.load_settings()["wallpaper_file"] == "wallpaper-deadbeef.jpg", (
            f"Invalid wallpaper_file value {bad!r} silently overwrote settings"
        )

    # Setting None must clear the field — "no wallpaper" is a valid state.
    config.save_settings({"wallpaper_file": None})
    assert config.load_settings()["wallpaper_file"] is None


def test_save_settings_accepts_valid_wallpaper_file(isolated_state):
    """The format save_wallpaper() actually writes must round-trip cleanly."""
    import api.config as config

    valid = "wallpaper-a1b2c3d4.png"
    config.save_settings({"wallpaper_file": valid})
    assert config.load_settings()["wallpaper_file"] == valid

    for ext in ("jpg", "png", "webp"):
        name = f"wallpaper-deadbeef.{ext}"
        config.save_settings({"wallpaper_file": name})
        assert config.load_settings()["wallpaper_file"] == name


# ── HTTP-level tests (against the test subprocess server) ──────────────────

import urllib.request
import urllib.error

from tests.conftest import TEST_BASE, _post


def _post_raw(path, body, mime):
    """POST raw bytes (not JSON / not multipart) with the given Content-Type."""
    req = urllib.request.Request(
        TEST_BASE + path,
        data=body,
        headers={'Content-Type': mime},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def _get_raw(path):
    """GET raw bytes; returns (status, headers, body_bytes)."""
    try:
        with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _cleanup_wallpaper():
    """Remove wallpaper via API after each HTTP test."""
    try:
        _post(TEST_BASE, '/api/wallpaper/delete', {})
    except Exception:
        pass


def test_http_post_wallpaper_jpeg_happy_path():
    status, body = _post_raw('/api/wallpaper', _JPEG_BYTES, 'image/jpeg')
    try:
        assert status == 200
        assert body['ok'] is True
        assert body['file'].startswith('wallpaper-') and body['file'].endswith('.jpg')

        # GET /info should reflect new wallpaper
        info_status, _hdrs, info_body = _get_raw('/api/wallpaper/info')
        assert info_status == 200
        info = json.loads(info_body)
        assert info['has_wallpaper'] is True
        assert info['mime'] == 'image/jpeg'
    finally:
        _cleanup_wallpaper()


def test_http_get_wallpaper_returns_bytes_with_cache_headers():
    _post_raw('/api/wallpaper', _PNG_BYTES, 'image/png')
    try:
        status, headers, body = _get_raw('/api/wallpaper')
        assert status == 200
        assert body == _PNG_BYTES
        assert headers.get('Content-Type') == 'image/png'
        cc = headers.get('Cache-Control', '')
        assert 'immutable' in cc
        assert 'max-age=' in cc
        assert headers.get('ETag')  # any non-empty etag is fine
    finally:
        _cleanup_wallpaper()


def test_http_get_wallpaper_404_when_unset():
    _cleanup_wallpaper()  # ensure clean state
    status, _hdrs, body_bytes = _get_raw('/api/wallpaper')
    assert status == 404
    assert json.loads(body_bytes)['error']  # any error message present


def test_http_post_wallpaper_rejects_oversize():
    from api.config import MAX_WALLPAPER_BYTES
    oversize = _JPEG_BYTES + b'\x00' * (MAX_WALLPAPER_BYTES + 1)
    status, body = _post_raw('/api/wallpaper', oversize, 'image/jpeg')
    assert status == 413
    assert 'too large' in body['error']


def test_http_post_wallpaper_rejects_bad_magic():
    # Fake jpeg Content-Type, but body is HTML
    status, body = _post_raw('/api/wallpaper', _HTML_BYTES, 'image/jpeg')
    assert status == 400
    assert 'JPEG, PNG, or WebP' in body['error']


def test_http_delete_wallpaper_clears_file_and_settings():
    _post_raw('/api/wallpaper', _WEBP_BYTES, 'image/webp')
    # Use POST /api/wallpaper/delete (DELETE method may not be supported)
    resp = _post(TEST_BASE, '/api/wallpaper/delete', {})
    assert resp.get('ok') is True
    info_status, _hdrs, info_body = _get_raw('/api/wallpaper/info')
    info = json.loads(info_body)
    assert info['has_wallpaper'] is False


# ── settings: wallpaper_brightness ─────────────────────────────────────────

def test_settings_brightness_default_and_save():
    """save_settings accepts a valid brightness in range [0.1, 1.5]."""
    resp = _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 0.5})
    # /api/settings returns the merged settings as the full object
    assert resp.get('wallpaper_brightness') == 0.5

    # /info should reflect the new brightness
    info_status, _hdrs, info_body = _get_raw('/api/wallpaper/info')
    assert json.loads(info_body)['brightness'] == 0.5

    # Reset for other tests
    _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 0.6})


def test_settings_brightness_out_of_range_silently_ignored():
    """Out-of-range values are silently kept-as-old (existing webui convention)."""
    _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 0.5})
    # Try out-of-range
    _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 2.0})
    # Should still be 0.5
    info_status, _hdrs, info_body = _get_raw('/api/wallpaper/info')
    assert json.loads(info_body)['brightness'] == 0.5

    # Wrong type (string) also ignored
    _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 'foo'})
    info_status, _hdrs, info_body = _get_raw('/api/wallpaper/info')
    assert json.loads(info_body)['brightness'] == 0.5

    # Reset
    _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 0.6})


def test_concurrent_settings_writes_dont_lose_fields():
    """Two simultaneous /api/settings writes (one for theme, one for brightness)
    must both persist. Without the write lock the second-to-finish would clobber
    the other's just-loaded copy.
    """
    import threading

    # Pin known starting state
    _post(TEST_BASE, '/api/settings', {'theme': 'dark', 'wallpaper_brightness': 0.6})

    errors = []

    def write_theme():
        try:
            _post(TEST_BASE, '/api/settings', {'theme': 'light'})
        except Exception as e:
            errors.append(('theme', e))

    def write_brightness():
        try:
            _post(TEST_BASE, '/api/settings', {'wallpaper_brightness': 0.4})
        except Exception as e:
            errors.append(('brightness', e))

    # Fire both nearly simultaneously
    t1 = threading.Thread(target=write_theme)
    t2 = threading.Thread(target=write_brightness)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []

    # Both fields must have stuck
    info_status, _hdrs, info_body = _get_raw('/api/wallpaper/info')
    assert json.loads(info_body)['brightness'] == 0.4

    # Re-fetch full settings via the existing GET to verify theme too
    settings_status, _hdrs, settings_body = _get_raw('/api/settings')
    settings = json.loads(settings_body)
    assert settings['theme'] == 'light'
    assert settings['wallpaper_brightness'] == 0.4

    # Reset
    _post(TEST_BASE, '/api/settings', {'theme': 'dark', 'wallpaper_brightness': 0.6})
