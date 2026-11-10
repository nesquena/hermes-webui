"""Behavioral contract for shared installation identity, including real HTTP routes."""
import concurrent.futures
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import subprocess
import threading
from urllib.parse import urljoin
import zlib

import pytest

from api import auth, config, extensions, instance_branding as branding


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "isolated-branding-test")
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", raising=False)
    auth._invalidate_password_hash_cache()
    root = tmp_path / "extensions"
    root.mkdir()
    manifest = root / "extensions.json"
    manifest.write_text(json.dumps({"extensions": [{
        "id": "custom-branding", "scripts": ["branding.js"], "instance_branding_api": 1,
    }]}))
    (root / "branding.js").write_text("/* isolated compatible provider fixture */")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", manifest.name)
    yield tmp_path, manifest
    auth._invalidate_password_hash_cache()


@pytest.fixture
def png():
    Image = pytest.importorskip("PIL.Image")
    from PIL.PngImagePlugin import PngInfo
    metadata = PngInfo()
    metadata.add_text("private", "must disappear")
    output = io.BytesIO()
    Image.new("RGBA", (512, 512), (220, 40, 80, 150)).save(output, "PNG", pnginfo=metadata)
    return output.getvalue()


@pytest.fixture
def http(state):
    from server import Handler

    class MountedHandler(Handler):
        def do_GET(self):
            self.path = self.path.removeprefix("/demo") or "/"
            super().do_GET()

        def do_POST(self):
            self.path = self.path.removeprefix("/demo") or "/"
            super().do_POST()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MountedHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    owner = auth.create_session()

    def request(path, method="GET", data=None, cookie=owner, headers=None):
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        extra = dict(headers or {})
        if cookie:
            extra["Cookie"] = f"{auth.COOKIE_NAME}={cookie}"
        if body is not None:
            extra.setdefault("Content-Type", "image/png" if isinstance(data, bytes) else "application/json")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=extra)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    request.owner = owner
    request.origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def get_state(http, **kwargs):
    status, headers, data = http("/api/instance-branding", **kwargs)
    assert status == 200, data
    assert headers["Cache-Control"] == "no-store"
    return json.loads(data), headers["ETag"]


def save(http, patch, etag=None, **kwargs):
    etag = etag or get_state(http)[1]
    return http("/api/instance-branding", "POST", patch, headers={"If-Match": etag}, **kwargs)


def activate(http, png, name="Team Console"):
    status, _, data = http("/api/instance-branding/image", "POST", png,
                           headers={"If-Match": get_state(http)[1]})
    assert status == 200, data
    status, _, data = save(http, {"enabled": True, "manages_instance_name": True, "instance_name": name})
    assert status == 200, data
    return json.loads(data)


def test_shared_http_identity_initial_html_manifest_assets_notifications(http, png):
    current = activate(http, png, '<Team "Console">')
    effective = current["effective"]
    for path in ("/", "/session/example", "/demo/", "/demo/session/example", "/login"):
        status, _, document = http(path, cookie=None if path == "/login" else http.owner)
        assert status == 200, document
        assert b'&lt;Team &quot;Console&quot;&gt;' in document
        assert b'window.__HERMES_INSTANCE_BRANDING__=' in document
        assert b'static/favicon.svg' not in document
        assert effective["apple_touch_icon"].encode() in document
        assert b'<meta name="apple-mobile-web-app-title" content="&lt;Team &quot;Console&quot;&gt;">' in document
        assert b"password_hash" not in document
    original = json.loads((Path(config.get_static_root()) / "manifest.json").read_bytes())
    for path in ("/manifest.json", "/manifest.webmanifest", "/session/manifest.json", "/session/manifest.webmanifest"):
        for prefix in ("", "/demo"):
            status, headers, data = http(prefix + path, cookie=None)
            assert status == 200
            assert headers["Content-Type"].startswith("application/manifest+json")
            assert headers["Cache-Control"] == "no-store"
            manifest = json.loads(data)
            assert manifest["name"] == manifest["short_name"] == '<Team "Console">'
            for field in ("id", "scope", "start_url"):
                assert manifest[field] == original[field]
            assert manifest["shortcuts"][0]["url"] == original["shortcuts"][0]["url"]
            for icon in manifest["icons"]:
                assert icon["purpose"] == "any"
                url = urljoin("https://example.test" + prefix + path, icon["src"])
                assert url.startswith("https://example.test" + prefix + "/branding/")
    Image = pytest.importorskip("PIL.Image")
    for key, size in (("favicon_32", 32), ("icon_192", 192), ("icon_512", 512), ("apple_touch_icon", 512)):
        status, headers, data = http("/" + effective[key], cookie=None)
        assert status == 200
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "immutable" in headers["Cache-Control"]
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (size, size)
            assert "private" not in image.info
            if key == "apple_touch_icon":
                assert image.mode == "RGB"
        assert http("/session/" + effective[key], cookie=None)[2] == data
        not_modified, conditional_headers, conditional_body = http("/" + effective[key], cookie=None, headers={"If-None-Match": headers["ETag"]})
        assert not_modified == 304 and not conditional_body
        assert int(conditional_headers["Content-Length"]) == len(data)
    ico = http("/" + effective["favicon_ico"], cookie=None)[2]
    with Image.open(io.BytesIO(ico)) as image:
        assert image.ico.sizes() == {(16, 16), (32, 32), (48, 48)}
    # A second independent authenticated access obtains the same server identity.
    other, _ = get_state(http, cookie=auth.create_session())
    assert other["effective"] == effective


def test_lifecycle_disable_downgrade_missing_provider_reset(http, state, png):
    active = activate(http, png)
    root, manifest = state
    persisted = json.loads(config.SETTINGS_FILE.read_text())
    extensions.set_extension_user_enabled("custom-branding", False)
    assert not get_state(http)[0]["effective"]["active"]
    extensions.set_extension_user_enabled("custom-branding", True)
    assert get_state(http)[0]["effective"] == active["effective"]
    for entry in ({"id": "custom-branding", "scripts": ["branding.js"]},
                  {"id": "custom-branding", "enabled": False, "instance_branding_api": 1}, {}):
        manifest.write_text(json.dumps({"extensions": [entry]}))
        current, etag = get_state(http)
        assert not current["effective"]["active"]
        assert current["effective"]["name"] is None
        assert json.loads(http("/manifest.json", cookie=None)[2])["name"] == "Hermes"
        assert http("/", cookie=http.owner)[2].count(b"static/favicon.svg") == 1
        assert json.loads(config.SETTINGS_FILE.read_text()) == persisted
        assert save(http, {"enabled": True}, etag)[0] == 409
    manifest.write_text(json.dumps({"extensions": [{"id": "custom-branding", "scripts": ["branding.js"], "instance_branding_api": 1}]}))
    assert get_state(http)[0]["effective"] == active["effective"]
    assert save(http, {"enabled": False})[0] == 200
    assert json.loads(config.SETTINGS_FILE.read_text())["instance_name"] == "Team Console"
    assert json.loads(http("/api/settings")[2])["instance_name"] == ""
    # No path through generic settings can rewrite the reserved object/name.
    assert http("/api/settings", "POST", {"instance_branding": {"enabled": True}})[0] == 400
    assert http("/api/settings", "POST", {"instance_name": "bypass"})[0] == 409
    assert save(http, {"enabled": True})[0] == 200
    assert save(http, {"action": "reset"})[0] == 200
    assert config.load_settings()["instance_name"] == ""
    assert not get_state(http)[0]["config"]["image_revision"]


def test_unmanaged_native_label_survives_disable_reset(http, png):
    config.save_settings({"instance_name": "Native label"})
    assert save(http, {"enabled": True})[0] == 200
    assert save(http, {"enabled": False})[0] == 200
    assert get_state(http)[0]["effective"]["name"] == "Native label"
    assert save(http, {"action": "reset"})[0] == 200
    assert config.load_settings()["instance_name"] == "Native label"


@pytest.mark.parametrize("auth_type,bound,allowed", [(None,None,True), ("oidc",None,True), ("unknown",None,False), (None,"limited",False)])
def test_management_authority_uses_valid_native_session(http, auth_type, bound, allowed):
    cookie = auth.create_session(auth_type=auth_type, bound_profile=bound)
    status, _, data = http("/api/instance-branding", cookie=cookie)
    if status == 200:
        assert json.loads(data)["can_manage"] is allowed
    else:
        assert not allowed
    assert save(http, {"enabled": True}, cookie=cookie)[0] == (200 if allowed else 403)


def test_auth_off_expired_missing_session_and_csrf(http, monkeypatch):
    assert http("/api/instance-branding", cookie=None)[0] == 401
    stale = auth.create_session()
    auth.invalidate_session(stale)
    assert http("/api/instance-branding", cookie=stale)[0] == 401
    etag = get_state(http)[1]
    for origin, token in (("https://hostile.invalid", None), (http.origin, None), (http.origin, "wrong")):
        headers = {"If-Match": etag, "Origin": origin}
        if token:
            headers[auth.CSRF_HEADER_NAME] = token
        assert http("/api/instance-branding", "POST", {"enabled": True}, headers=headers)[0] == 403
    headers = {"If-Match": etag, "Origin": http.origin, auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(http.owner)}
    assert http("/api/instance-branding", "POST", {"enabled": True}, headers=headers)[0] == 200
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD")
    auth._invalidate_password_hash_cache()
    assert get_state(http)[0]["can_manage"] is False
    assert save(http, {"enabled": True})[0] == 403


def test_etag_limits_types_and_no_normalizer(http, png, monkeypatch):
    _, etag = get_state(http)
    assert http("/api/instance-branding", "POST", {"enabled": True})[0] == 428
    assert save(http, {"enabled": True}, '"stale"')[0] == 412
    assert save(http, {"owner_extension_id": "fake"})[0] == 400
    assert save(http, {"enabled": 1})[0] == 400
    assert save(http, {"instance_name": "hostile\nname"})[0] == 400
    assert http("/api/instance-branding", "POST", b"{}", headers={"If-Match": etag, "Content-Type": "image/png"})[0] == 415
    assert http("/api/instance-branding", "POST", b"x" * 4097, headers={"If-Match": etag})[0] == 413
    # Oversized input is rejected from its headers, before reading the body.
    # Sending 2 MiB after that rejection races the intentional connection close.
    status, headers, _ = http("/api/instance-branding/image", "POST", b"",
                              headers={"If-Match": etag, "Content-Length": str(branding.MAX_UPLOAD + 1)})
    assert status == 413
    assert headers["Connection"].lower() == "close"
    activate(http, png)
    monkeypatch.setattr(branding, "_image_module", lambda: None)
    assert not get_state(http)[0]["can_upload"]
    assert http("/api/instance-branding/image", "POST", png, headers={"If-Match": get_state(http)[1]})[0] == 503
    assert json.loads(http("/manifest.json", cookie=None)[2])["name"] == "Team Console"


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


@pytest.mark.parametrize("change", ["trailing", "truncate", "crc", "apng", "svg", "ico", "huge", "nonsquare", "badpixels"])
def test_hostile_upload_never_replaces_identity(http, png, change):
    before = activate(http, png)
    if change == "trailing":
        bad = png + b"<script>hostile</script>"
    elif change == "truncate":
        bad = png[:-2]
    elif change == "crc":
        bad = png[:30] + bytes([png[30] ^ 255]) + png[31:]
    elif change == "apng":
        bad = png[:33] + chunk(b"acTL", struct.pack(">II", 1, 0)) + png[33:]
    elif change in ("svg", "ico"):
        bad = b"<svg/>" if change == "svg" else b"\0\0\1\0"
    else:
        dimensions = (4096, 4096) if change == "huge" else ((512, 513) if change == "nonsquare" else (512, 512))
        bad = png[:8] + chunk(b"IHDR", struct.pack(">IIBBBBB", *dimensions, 8, 6, 0, 0, 0))
        bad += chunk(b"IDAT", b"not compressed pixels") + chunk(b"IEND", b"")
    status, _, _ = http("/api/instance-branding/image", "POST", bad, headers={"If-Match": get_state(http)[1]})
    assert status == 422
    assert get_state(http)[0] == before


def test_public_assets_allowlist_and_symlinks(http, state, png):
    current = activate(http, png)
    revision = current["config"]["image_revision"]
    assert http(f"/branding/{revision}/master.png", cookie=None)[0] == 302
    assert http(f"/branding/{revision}/master.png")[0] == 404
    assert http(f"/branding/{revision}/../settings.json", cookie=None)[0] == 302
    assert http(f"/branding/{'0'*64}/icon-192.png", cookie=None)[0] == 404
    path = state[0] / "branding" / revision / "icon-192.png"
    path.unlink()
    path.symlink_to(config.SETTINGS_FILE)
    assert http(f"/branding/{revision}/icon-192.png", cookie=None)[0] == 404
    assert not get_state(http)[0]["effective"]["active"]


def test_transaction_serializes_native_preferences_and_branding(http, state, monkeypatch):
    etag = get_state(http)[1]
    started, release = threading.Event(), threading.Event()
    original = config._atomic_write_settings_text

    def delayed(path, text):
        if threading.current_thread().name == "preference-writer":
            started.set()
            assert release.wait(5)
        return original(path, text)

    monkeypatch.setattr(config, "_atomic_write_settings_text", delayed)
    first = threading.Thread(target=lambda: config.save_settings({"bot_name": "Independent"}), name="preference-writer")
    first.start()
    assert started.wait(5)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(save, http, {"instance_name": "Shared", "manages_instance_name": True}, etag)
        release.set()
        assert future.result(10)[0] == 200
    first.join(5)
    assert config.load_settings()["bot_name"] == "Independent"
    assert config.load_settings()["instance_name"] == "Shared"
    assert save(http, {"enabled": True}, etag)[0] == 412
    config.save_settings({"theme": "light"})
    assert config.load_settings()["instance_name"] == "Shared"


def test_write_failure_preserves_config_and_mode(http, monkeypatch):
    config.save_settings({"bot_name": "Private"})
    config.SETTINGS_FILE.chmod(0o600)
    assert save(http, {"instance_name": "First"})[0] == 200
    assert config.SETTINGS_FILE.stat().st_mode & 0o777 == 0o600
    original = config.SETTINGS_FILE.read_bytes()

    def fail(*args):
        raise OSError("simulated full disk")

    monkeypatch.setattr(config, "_atomic_write_settings_text", fail)
    assert save(http, {"instance_name": "Lost"})[0] == 500
    assert config.SETTINGS_FILE.read_bytes() == original


def test_tinted_fallback_composes_before_shared_projection(state, png):
    # The actual #6266 helper is exercised separately by the handoff probe.
    native = json.loads((Path(config.get_static_root()) / "manifest.json").read_bytes())
    tinted = json.loads(json.dumps(native))
    for icon in tinted["icons"]:
        icon["src"] = "static/favicon.svg?tint=orange"
    image, files = branding.normalize_image(png)
    branding._publish_image(image, files)
    config.save_settings({"instance_name": "Tint test"}, _branding={
        "schema_version": 1, "enabled": True, "image_revision": image,
    })
    projected = json.loads(branding.render_manifest(json.dumps(tinted), "/manifest.json"))
    assert all(icon["type"] == "image/png" for icon in projected["icons"])
    config.save_settings({}, _branding={"enabled": False})
    assert json.loads(branding.render_manifest(json.dumps(tinted), "/manifest.json"))["icons"] == tinted["icons"]


def test_trusted_auth_reconciles_changed_authority_before_write(http, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    old = auth.create_session(auth_type="trusted", username="previous-owner")
    # A signed but stale proxy cookie has no authority without the current header.
    assert http("/api/instance-branding", cookie=old)[0] == 401
    status, _, data = http("/api/instance-branding", cookie=None, headers={"Remote-User": "current-owner"})
    assert status == 200
    assert json.loads(data)["can_manage"]
    # Reconciliation rotates a stale identity; stale bound metadata cannot grant management.
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_GROUPS_HEADER", "Remote-Groups")
    monkeypatch.setenv("HERMES_WEBUI_GROUP_PROFILE_MAP", json.dumps({"limited": "limited-profile"}))
    stale_owner = auth.create_session(auth_type="trusted", username="current-owner")
    status, _, data = http("/api/instance-branding", cookie=stale_owner,
                           headers={"Remote-User": "current-owner", "Remote-Groups": "limited"})
    assert status == 403 or not json.loads(data)["can_manage"]


def test_upload_rechecks_etag_after_decode_before_publication(http, png, monkeypatch):
    initial = get_state(http)[1]
    decoded, release = threading.Event(), threading.Event()
    original = branding.normalize_image

    def delayed(data):
        result = original(data)
        decoded.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(branding, "normalize_image", delayed)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(http, "/api/instance-branding/image", "POST", png,
                              headers={"If-Match": initial})
        assert decoded.wait(5)
        assert save(http, {"instance_name": "Concurrent owner"}, initial)[0] == 200
        release.set()
        assert pending.result(10)[0] == 412
    assert not (Path(config.STATE_DIR) / "branding").exists()


def test_last_two_image_references_survive_name_edits(http, png):
    Image = pytest.importorskip("PIL.Image")
    revisions = []
    for color in ("red", "green", "blue"):
        output = io.BytesIO()
        Image.new("RGB", (512, 512), color).save(output, "PNG")
        status, _, data = http("/api/instance-branding/image", "POST", output.getvalue(),
                               headers={"If-Match": get_state(http)[1]})
        assert status == 200
        revisions.append(json.loads(data)["config"]["image_revision"])
    assert save(http, {"instance_name": "Label only"})[0] == 200
    retained = {entry.name for entry in (Path(config.STATE_DIR) / "branding").iterdir()}
    assert retained == set(revisions[-2:])


def test_unchanged_identity_reads_reuse_validated_images(http, png, monkeypatch):
    active = activate(http, png)
    reads = []
    original = branding._read_asset

    def tracked(revision, filename):
        reads.append(filename)
        return original(revision, filename)

    monkeypatch.setattr(branding, "_read_asset", tracked)
    for _ in range(3):
        assert get_state(http)[0]["effective"] == active["effective"]
        assert http("/")[0] == 200
        assert http("/manifest.json")[0] == 200
        assert http("/api/settings")[0] == 200
    assert reads == [], "unchanged images must not be reopened under the settings lock"


@pytest.mark.parametrize("change", ["missing", "corrupt", "symlink", "directory_symlink", "state_root"])
def test_checked_image_cache_detects_filesystem_changes(http, state, png, monkeypatch, change):
    active = activate(http, png)
    root, _ = state
    revision = active["config"]["image_revision"]
    directory = root / "branding" / revision
    path = directory / "icon-192.png"
    original = path.read_bytes()
    if change == "state_root":
        other = root / "other-state"
        other.mkdir()
        monkeypatch.setattr(config, "STATE_DIR", other)
        assert not branding._complete_image(revision)
        return
    if change == "missing":
        path.unlink()
    elif change == "corrupt":
        # Same size, changed CRC: size-only or revision-only caches are unsafe.
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 255]))
    elif change == "symlink":
        path.unlink()
        path.symlink_to(directory / "icon-512.png")
    else:
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    assert not get_state(http)[0]["effective"]["active"]
    assert json.loads(http("/manifest.json")[2])["name"] == "Hermes"
    if change == "directory_symlink":
        directory.unlink()
        moved.rename(directory)
    else:
        path.unlink(missing_ok=True)
        path.write_bytes(original)
    assert get_state(http)[0]["effective"] == active["effective"]


def test_transient_image_read_failure_is_not_cached(http, state, png, monkeypatch):
    active = activate(http, png)
    directory = state[0] / "branding" / active["config"]["image_revision"]
    (directory / "icon-192.png").touch()
    original = branding._read_asset
    failed = False

    def transient(revision, filename):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("transient read failure")
        return original(revision, filename)

    monkeypatch.setattr(branding, "_read_asset", transient)
    assert not get_state(http)[0]["effective"]["active"]
    assert get_state(http)[0]["effective"] == active["effective"]


@pytest.mark.parametrize("width", [1440, 390])
def test_real_cron_editor_survives_branding_change(http, png, width):
    playwright = pytest.importorskip("playwright.sync_api")
    config.save_settings({"onboarding_completed": True, "language": "en", "check_for_updates": False})
    activate(http, png)
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": width, "height": 900},
                                          is_mobile=width == 390, has_touch=width == 390,
                                          service_workers="block")
            context.add_cookies([{"name": auth.COOKIE_NAME, "value": http.owner, "url": http.origin}])
            page = context.new_page()
            # Exercise the real local shell/cron form; CDN integrations are unrelated.
            page.route("**/*", lambda route: route.continue_()
                       if route.request.url.startswith(http.origin + "/") else route.abort())
            page.goto(http.origin + "/", wait_until="load")
            page.wait_for_function("typeof openCronCreate==='function' && typeof showConfirmDialog==='function'")
            page.evaluate("openCronCreate()")
            prompt = page.locator("#cronFormPrompt")
            prompt.fill("Unsaved scheduled-job instructions")
            assert save(http, {"instance_name": "Changed in another tab"})[0] == 200
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.locator("#appDialogOverlay").wait_for(state="visible")
            page.wait_for_function("document.activeElement.id==='appDialogCancel'")
            assert prompt.input_value() == "Unsaved scheduled-job instructions"
            page.locator("#appDialogCancel").click()
            assert prompt.input_value() == "Unsaved scheduled-job instructions"
        finally:
            browser.close()


def test_notification_uses_shared_icon_but_native_badge(state):
    source = (Path(__file__).resolve().parents[1] / "static/messages.js").read_text()
    function = source[source.index("function _notificationOptions("):source.index("function _showPwaNotification(")]
    driver = """
const window={__HERMES_INSTANCE_BRANDING__:{effective:{icon_192:'branding/revision/icon-192.png'}}};
const location={origin:'https://example.test',href:'https://example.test/demo/'};
const S={session:null};
const _appRootPath=()=>'/demo/';
""" + function + """
console.log(JSON.stringify(_notificationOptions('body',{sessionless:true})));
"""
    result = subprocess.run(["node", "-e", driver], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    options = json.loads(result.stdout)
    assert options["icon"] == "branding/revision/icon-192.png"
    assert options["badge"] == "static/favicon-32.png"


def test_unknown_persisted_schema_falls_back_without_rewriting_name(http):
    config.save_settings({"instance_name": "Retained"}, _branding={
        "schema_version": 99, "enabled": True, "manages_instance_name": True,
    })
    current, _ = get_state(http)
    assert not current["effective"]["active"]
    assert current["effective"]["name"] is None
    assert config.load_settings()["instance_name"] == "Retained"


@pytest.mark.parametrize("name", ["\ud800", "\udfff", "Team \ud800 Console", "Team \udfff Console"])
def test_branding_rejects_unencodable_name_without_mutation(http, name):
    assert save(http, {"instance_name": "Previous", "manages_instance_name": True})[0] == 200
    before, etag = get_state(http)
    stored = config.SETTINGS_FILE.read_bytes()
    status, _, _ = save(http, {"instance_name": name}, etag)
    assert status == 400
    assert get_state(http) == (before, etag)
    assert config.SETTINGS_FILE.read_bytes() == stored


@pytest.mark.parametrize("name", ["\ud800", "\udfff", "Team \ud800 Console", "Team \udfff Console"])
def test_native_settings_ignore_unencodable_name_and_save_other_fields(http, name):
    config.save_settings({"instance_name": "Previous", "theme": "dark"})
    before, etag = get_state(http)
    status, _, body = http("/api/settings", "POST", {"instance_name": name, "theme": "light"})
    assert status == 200, body
    assert json.loads(body)["instance_name"] == "Previous"
    assert config.load_settings()["instance_name"] == "Previous"
    assert config.load_settings()["theme"] == "light"
    assert get_state(http) == (before, etag)


@pytest.mark.parametrize("name", ["  Équipe Café  ", "Console 🚀", "漢字 🛰️", "é" * 80])
@pytest.mark.parametrize("path", ["/api/instance-branding", "/api/settings"])
def test_name_paths_preserve_valid_unicode_through_json_and_utf8_storage(http, name, path):
    before, etag = get_state(http)
    # ASCII JSON escapes exercise decoding a valid surrogate pair for each emoji.
    payload = json.dumps({"instance_name": name}, ensure_ascii=True).encode("ascii")
    status, _, body = http(path, "POST", payload, headers={
        "Content-Type": "application/json", "If-Match": etag,
    })
    assert status == 200, body
    assert json.loads(config.SETTINGS_FILE.read_bytes().decode("utf-8", errors="strict"))["instance_name"] == name.strip()
    current, changed_etag = get_state(http)
    assert current["config"]["instance_name"] == name.strip()
    assert current["effective"]["name"] == name.strip()
    assert changed_etag != etag


def test_name_length_policies_remain_distinct(http):
    before, etag = get_state(http)
    assert save(http, {"instance_name": "é" * 81}, etag)[0] == 400
    assert get_state(http) == (before, etag)
    status, _, body = http("/api/settings", "POST", {"instance_name": "é" * 81})
    assert status == 200, body
    assert config.load_settings()["instance_name"] == "é" * 80


@pytest.mark.parametrize("patch", [[1], "invalid", 1, True, None])
def test_non_object_patch_returns_bad_request_without_provider(http, monkeypatch, patch):
    monkeypatch.setattr(branding, "provider_available", lambda: False)
    before, etag = get_state(http)
    status, _, data = http(
        "/api/instance-branding", "POST", json.dumps(patch).encode(),
        headers={"If-Match": etag, "Content-Type": "application/json"},
    )
    assert status == 400, data
    assert get_state(http)[0] == before
