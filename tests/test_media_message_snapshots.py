"""Message-level media snapshots: freeze file bytes at settle time.

PR #6922 made /api/media revalidate on every use (no-cache + ETag), so an
in-place overwrite of a file (same filename) also rewrites every historical
chat preview that referenced it — the old/new comparison is lost. This suite
covers the fix: at settle time the WebUI snapshots each local-file MEDIA:
reference into a content-addressed store and stamps the message with
``_media_snapshots``; the frontend appends ``&snap=<digest>`` to historical
preview URLs and /api/media serves the frozen bytes instead of the live file.

Key property under test: a snapshot survives the original file being
overwritten AND deleted, while a request WITHOUT a snap keeps serving the live
(possibly new) bytes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _FakeHandler:
    def __init__(self, headers=None):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = dict(headers or {})

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, key):
        return next((v for k, v in self.sent_headers if k == key), "") or ""


@pytest.fixture
def routes():
    from api import routes

    return routes


@pytest.fixture(autouse=True)
def media_allowed_root(tmp_path, monkeypatch):
    # tmp_path is under /tmp only on Linux; register it and make Path.home()
    # resolve to the same fixture root on Windows so deny tests reach #3234.
    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


@pytest.fixture
def snap_dir(tmp_path, monkeypatch):
    """Isolate the snapshot store per test and point it at tmp_path."""
    store = tmp_path / "media_snapshots"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    return store


def _media_get(routes, monkeypatch, target, headers=None, query_extra=""):
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = _FakeHandler(headers)
    parsed = SimpleNamespace(path="/api/media", query=f"path={target}{query_extra}")
    routes._handle_media(handler, parsed)
    return handler


# ── capture_snapshot ───────────────────────────────────────────────────────


def test_capture_snapshot_stores_content_addressed_bytes(snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot, snapshot_path_for_digest

    source = tmp_path / "report.html"
    source.write_bytes(b"<html>v1</html>")

    digest = capture_snapshot(source)
    assert digest and len(digest) == 64

    stored = snapshot_path_for_digest(digest)
    assert stored is not None
    assert stored.read_bytes() == b"<html>v1</html>"


def test_capture_snapshot_dedupes_identical_content(snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")

    d1 = capture_snapshot(a)
    d2 = capture_snapshot(b)
    assert d1 == d2
    # One .snap blob only (the dedup contract); the source-binding sidecar
    # (.src.json) is a separate small file and does not count as a blob.
    assert len(list(snap_dir.glob("*.snap"))) == 1


def test_capture_snapshot_skips_missing_and_over_cap(snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot

    assert capture_snapshot(tmp_path / "missing.png") is None

    big = tmp_path / "big.mp4"
    big.write_bytes(b"x" * 1024)
    assert capture_snapshot(big, max_file_bytes=512) is None


def test_capture_snapshot_skips_directories(snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot

    assert capture_snapshot(tmp_path) is None


# ── resolve_media_ref / media_capture_allowed ──────────────────────────────


def test_resolve_media_ref_handles_file_url_and_expands_home(tmp_path, monkeypatch):
    from api.media_snapshots import resolve_media_ref

    target = tmp_path / "x.html"
    target.write_text("hi")

    assert resolve_media_ref(str(target)) == target.resolve()
    assert resolve_media_ref("file://" + str(target)) == target.resolve()
    assert resolve_media_ref("https://example.com/a.png") is None
    assert resolve_media_ref("data:image/png;base64,AAAA") is None
    assert resolve_media_ref("") is None


def test_media_capture_allowed_denies_hermes_state(tmp_path, monkeypatch):
    from api.media_snapshots import media_capture_allowed

    # Files under an allowed root (tmp) are fine...
    allowed = tmp_path / "ok.html"
    allowed.write_text("x")
    assert media_capture_allowed(allowed) is True

    # ...but a deny-listed filename under a Hermes root is never snapshotted.
    # Point HOME at the fake tree so <fake-home>/.hermes counts as a root.
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    secret = hermes_home / "settings.json"
    secret.write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert media_capture_allowed(secret) is False


# ── annotate_media_snapshots ───────────────────────────────────────────────


def test_annotate_stamps_assistant_messages_with_snapshots(snap_dir, tmp_path):
    from api.media_snapshots import annotate_media_snapshots

    target = tmp_path / "report.html"
    target.write_text("<html>v1</html>")

    messages = [
        {"role": "user", "content": "please build it"},
        {"role": "assistant", "content": f"done: MEDIA:{target}"},
        {"role": "assistant", "content": "no media here"},
    ]
    captured = annotate_media_snapshots(messages)
    assert captured == 1

    stamped = messages[1]["_media_snapshots"]
    assert str(target) in stamped
    assert len(stamped[str(target)]) == 64


def test_annotate_is_idempotent_across_settles(snap_dir, tmp_path):
    from api.media_snapshots import annotate_media_snapshots

    target = tmp_path / "report.html"
    target.write_text("<html>v1</html>")
    messages = [{"role": "assistant", "content": f"MEDIA:{target}"}]

    assert annotate_media_snapshots(messages) == 1
    assert annotate_media_snapshots(messages) == 0  # fast-path skip
    assert len(list(snap_dir.glob("*.snap"))) == 1


def test_annotate_skips_remote_and_data_refs(snap_dir, tmp_path):
    from api.media_snapshots import annotate_media_snapshots

    messages = [
        {"role": "assistant", "content": "MEDIA:https://example.com/a.png"},
        {"role": "assistant", "content": "MEDIA:data:image/png;base64,AAAA"},
    ]
    assert annotate_media_snapshots(messages) == 0
    assert "_media_snapshots" not in messages[0]
    assert "_media_snapshots" not in messages[1]


# ── /api/media?snap= serving ───────────────────────────────────────────────


def test_handle_media_serves_snapshot_after_inplace_overwrite(routes, monkeypatch, snap_dir, tmp_path):
    """THE regression test: same filename overwritten must not rewrite old
    previews when the message pins a snapshot digest."""
    from api.media_snapshots import capture_snapshot

    target = tmp_path / "report.html"
    target.write_text("<html>v1</html>")
    digest = capture_snapshot(target)

    # Overwrite the file in place (the scenario that used to break history).
    time.sleep(0.01)
    target.write_text("<html>v2 - completely different</html>")

    # Without snap: live file (new bytes).
    live = _media_get(routes, monkeypatch, target)
    assert live.status == 200
    assert bytes(live.body) == b"<html>v2 - completely different</html>"

    # With snap: frozen v1 bytes, immutable caching.
    pinned = _media_get(routes, monkeypatch, target, query_extra=f"&snap={digest}")
    assert pinned.status == 200
    assert bytes(pinned.body) == b"<html>v1</html>"
    assert pinned.header("Cache-Control") == "private, max-age=31536000, immutable"


def test_handle_media_snapshot_survives_file_deletion(routes, monkeypatch, snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot

    target = tmp_path / "pic.png"
    target.write_bytes(b"png-bytes-v1")
    digest = capture_snapshot(target)

    target.unlink()

    pinned = _media_get(routes, monkeypatch, target, query_extra=f"&snap={digest}")
    assert pinned.status == 200
    assert bytes(pinned.body) == b"png-bytes-v1"


def test_handle_media_invalid_snap_falls_back_to_live(routes, monkeypatch, snap_dir, tmp_path):
    target = tmp_path / "pic.png"
    target.write_bytes(b"live-bytes")
    replayed = _media_get(routes, monkeypatch, target, query_extra="&snap=not-a-digest")
    assert replayed.status == 200
    assert bytes(replayed.body) == b"live-bytes"


def test_handle_media_missing_snap_falls_back_to_live(routes, monkeypatch, snap_dir, tmp_path):
    target = tmp_path / "pic.png"
    target.write_bytes(b"live-bytes")
    missing = _media_get(
        routes, monkeypatch, target, query_extra="&snap=" + "0" * 64
    )
    assert missing.status == 200
    assert bytes(missing.body) == b"live-bytes"


def test_handle_media_snap_does_not_bypass_deny(routes, monkeypatch, snap_dir, tmp_path):
    """snap= must never widen the path allow-list: a denied path stays denied
    even when a valid snapshot digest is supplied."""
    from api.media_snapshots import capture_snapshot

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    secret = hermes_home / "settings.json"
    secret.write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    digest = capture_snapshot(secret)  # capture itself may be blocked; if not...

    denied = _media_get(routes, monkeypatch, secret, query_extra=f"&snap={digest or '0'*64}")
    # settings.json under a hermes root is denied by the #3234 deny list.
    assert denied.status == 403


def test_handle_media_denies_direct_store_path(routes, monkeypatch, tmp_path):
    """The snapshot STORE directory itself is not a servable media path.

    The store lives under STATE_DIR (a Hermes root) in production; the #3234
    deny list must reject a bare path= fetch of a snapshot blob there, so the
    store is only reachable through the validated snap= parameter.
    """
    from api.media_snapshots import capture_snapshot

    # Simulate the production layout: the store lives under a Hermes root, with
    # HOME pointing at tmp_path so that directory counts as a Hermes root. The
    # #3234 deny list denies <hermes_root>/media_snapshots.
    hermes_home = tmp_path / ".hermes"
    store = hermes_home / "media_snapshots"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))

    target = tmp_path / "a.png"
    target.write_bytes(b"bytes")
    digest = capture_snapshot(target)
    store_file = store / f"{digest}.snap"
    assert store_file.exists()

    denied = _media_get(routes, monkeypatch, store_file)
    assert denied.status == 403


def test_handle_media_snapshot_range_request(routes, monkeypatch, snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot

    target = tmp_path / "clip.mp4"
    target.write_bytes(b"0123456789")
    digest = capture_snapshot(target)

    handler = _media_get(
        routes,
        monkeypatch,
        target,
        headers={"Range": "bytes=2-4"},
        query_extra=f"&snap={digest}",
    )
    assert handler.status == 206
    assert bytes(handler.body) == b"234"
    assert handler.header("Content-Range") == "bytes 2-4/10"


def test_handle_media_snapshot_download_name_uses_original(routes, monkeypatch, snap_dir, tmp_path):
    from api.media_snapshots import capture_snapshot

    target = tmp_path / "report.html"
    target.write_bytes(b"<html>v1</html>")
    digest = capture_snapshot(target)
    target.unlink()

    handler = _media_get(routes, monkeypatch, target, query_extra=f"&snap={digest}")
    disposition = handler.header("Content-Disposition")
    assert "report.html" in disposition
    assert f"{digest}.snap" not in disposition


# ── Round 2: capture/serve deny parity + source-path binding (#6979) ────────


def test_media_capture_allowed_denies_default_webui_state_layout(tmp_path, monkeypatch):
    """MUST-FIX 1 repro: default-layout <HERMES_HOME>/webui/sessions/victim.json
    (== STATE_DIR/sessions) must NEVER be captured.

    Round 1 capture omitted STATE_DIR from its deny roots; the serve path
    denies it. Capture now shares the serve predicate, so this returns False.
    """
    from api.media_snapshots import media_capture_allowed

    hermes_home = tmp_path / ".hermes"
    state_dir = hermes_home / "webui"
    (state_dir / "sessions").mkdir(parents=True)
    victim = state_dir / "sessions" / "victim.json"
    victim.write_text("{}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("api.config.STATE_DIR", str(state_dir))

    assert media_capture_allowed(victim) is False


def test_annotate_never_captures_denied_state_file(snap_dir, tmp_path, monkeypatch):
    """MUST-FIX 1 end-to-end: annotating a message whose MEDIA: ref points at a
    denied state file must capture NOTHING (Round 1 captured it and the digest
    then acted as a bearer capability)."""
    from api.media_snapshots import annotate_media_snapshots

    hermes_home = tmp_path / ".hermes"
    state_dir = hermes_home / "webui"
    (state_dir / "sessions").mkdir(parents=True)
    victim = state_dir / "sessions" / "victim.json"
    victim.write_text('{"secret": 1}')

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("api.config.STATE_DIR", str(state_dir))

    messages = [{"role": "assistant", "content": f"here it is: MEDIA:{victim}"}]
    captured = annotate_media_snapshots(messages)
    assert captured == 0
    assert "_media_snapshots" not in messages[0]
    assert len(list(snap_dir.glob("*.snap"))) == 0


def test_handle_media_snap_requires_source_path_binding(routes, monkeypatch, snap_dir, tmp_path):
    """MUST-FIX 1 serve-side repro: a digest captured from path A must not be
    servable through path B, even when B is an allowed path.

    Round 1 served stored bytes for ANY allowed path carrying the digest
    (/tmp/whatever.png?snap=<digest> -> 200 with A's bytes). With the binding:
    B falls back to its live file (404 when absent; live bytes when present).
    """
    from api.media_snapshots import capture_snapshot

    source = tmp_path / "a.png"
    source.write_bytes(b"frozen-bytes-from-a")
    digest = capture_snapshot(source)

    # Bound path: snapshot bytes served.
    bound = _media_get(routes, monkeypatch, source, query_extra=f"&snap={digest}")
    assert bound.status == 200
    assert bytes(bound.body) == b"frozen-bytes-from-a"

    # Unbound + absent path: NOT the snapshot; live fallback -> 404.
    other_missing = tmp_path / "b.png"
    unbound = _media_get(routes, monkeypatch, other_missing, query_extra=f"&snap={digest}")
    assert unbound.status == 404

    # Unbound + present path: live bytes, never the frozen snapshot.
    other_live = tmp_path / "b.png"
    other_live.write_bytes(b"live-bytes-of-b")
    unbound_live = _media_get(routes, monkeypatch, other_live, query_extra=f"&snap={digest}")
    assert unbound_live.status == 200
    assert bytes(unbound_live.body) == b"live-bytes-of-b"


def test_handle_media_snap_dedup_binds_every_source_path(routes, monkeypatch, snap_dir, tmp_path):
    """Dedup must not break source binding: identical bytes captured from two
    different paths share one digest, and BOTH paths may serve it (each was a
    legitimate capture source); a third path may not."""
    from api.media_snapshots import capture_snapshot

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")
    d1 = capture_snapshot(a)
    d2 = capture_snapshot(b)
    assert d1 == d2

    sa = _media_get(routes, monkeypatch, a, query_extra=f"&snap={d1}")
    sb = _media_get(routes, monkeypatch, b, query_extra=f"&snap={d2}")
    assert sa.status == 200 and bytes(sa.body) == b"same-bytes"
    assert sb.status == 200 and bytes(sb.body) == b"same-bytes"

    c = tmp_path / "c.png"
    c.write_bytes(b"live-different-bytes")
    sc = _media_get(routes, monkeypatch, c, query_extra=f"&snap={d1}")
    assert sc.status == 200
    # Live file served (distinguishable from the frozen bytes), NOT the snapshot.
    assert bytes(sc.body) == b"live-different-bytes"


def test_handle_media_denies_custom_named_store_via_bare_path(routes, monkeypatch, tmp_path):
    """MUST-FIX 2 repro: a custom-named snapshot store (HERMES_WEBUI_MEDIA_SNAPSHOT_DIR
    pointing anywhere, e.g. /tmp/custom-store-name) must not be readable through
    a bare path= request. Round 1 only deny-listed the literal name
    'media_snapshots', so the blobs leaked with no snap= needed."""
    from api.media_snapshots import capture_snapshot

    store = tmp_path / "custom-store-name"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))

    target = tmp_path / "a.png"
    target.write_bytes(b"stored-bytes")
    digest = capture_snapshot(target)
    store_file = store / f"{digest}.snap"
    assert store_file.exists()

    denied = _media_get(routes, monkeypatch, store_file)
    assert denied.status == 403


def test_is_valid_digest_rejects_trailing_newline():
    """SHOULD-FIX: `$` matched before a terminal newline; fullmatch must not."""
    from api.media_snapshots import is_valid_digest

    good = "a" * 64
    assert is_valid_digest(good) is True
    assert is_valid_digest(good + "\n") is False
    assert is_valid_digest("a" * 63) is False
    assert is_valid_digest("A" * 64) is False  # lowercase hex only
    assert is_valid_digest("") is False
    assert is_valid_digest(None) is False  # type: ignore[arg-type]


def test_annotate_evicted_snapshot_is_final(snap_dir, tmp_path):
    """SHOULD-FIX: a recorded digest is FINAL — after its blob is evicted, a
    re-settle must not re-capture the CURRENT live bytes and rebind the
    historical message (Round 1 did, silently following overwrites again)."""
    from api.media_snapshots import annotate_media_snapshots

    target = tmp_path / "report.html"
    target.write_text("<html>v1</html>")
    messages: list = [{"role": "assistant", "content": f"MEDIA:{target}"}]

    assert annotate_media_snapshots(messages) == 1
    snaps0: dict = messages[0].get("_media_snapshots") or {}
    digest = snaps0[str(target)]

    # Simulate quota eviction: blob gone.
    blob = snap_dir / f"{digest}.snap"
    assert blob.exists()
    blob.unlink()

    # Overwrite the live file, then re-settle the SAME message.
    target.write_text("<html>v2 - overwritten</html>")
    assert annotate_media_snapshots(messages) == 0
    snaps1: dict = messages[0].get("_media_snapshots") or {}
    assert snaps1[str(target)] == digest
    assert not blob.exists()  # no re-capture


def test_quota_eviction_drops_source_binding_sidecar(snap_dir, tmp_path, monkeypatch):
    """Evicting a snapshot blob must also drop its source-binding sidecar."""
    from api.media_snapshots import _binding_path_for_digest, capture_snapshot

    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_CAP_BYTES", "64")
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(b"x" * 64)
    new.write_bytes(b"y" * 64)
    d_old = capture_snapshot(old)
    d_new = capture_snapshot(new)
    assert d_old and d_new

    if not _binding_path_for_digest(d_old).exists():
        # Oldest was evicted (its binding sidecar must be gone too).
        assert not (snap_dir / f"{d_old}.snap").exists()
    else:
        assert not _binding_path_for_digest(d_new).exists()
        assert not (snap_dir / f"{d_new}.snap").exists()


# ── display-metadata persistence (state.db ↔ sidecar merge) ────────────────


def test_media_snapshots_registered_in_display_metadata_keys():
    from api.models import _SESSION_MESSAGE_DISPLAY_METADATA_KEYS

    assert "_media_snapshots" in _SESSION_MESSAGE_DISPLAY_METADATA_KEYS


def test_merge_session_display_metadata_preserves_snapshots():
    from api.models import _merge_session_display_metadata

    target = {"role": "assistant", "content": "x"}
    source = {"role": "assistant", "content": "x", "_media_snapshots": {"/a": "a" * 64}}
    _merge_session_display_metadata(target, source)
    assert target["_media_snapshots"] == {"/a": "a" * 64}


# ── frontend stamping helper (behavioral, node-executed) ───────────────────


def _extract_stamp_helper():
    """Extract esc() and _stampMediaSnapshots() verbatim from static/ui.js."""
    src = open(ROOT / "static" / "ui.js", encoding="utf-8").read()

    def extract_function(name):
        start = src.find(f"function {name}(")
        if start < 0:
            raise AssertionError(f"{name} not found in ui.js")
        i = src.find("{", start)
        depth = 1
        i += 1
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        return src[start:i]

    esc_line = next(line for line in src.split("\n") if line.startswith("const esc="))
    return esc_line, extract_function("_stampMediaSnapshots")


def _run_stamp(html, snaps):
    """Run the real _stampMediaSnapshots under node with mocked collaborators."""
    import json
    import subprocess
    import tempfile

    esc_def, fn_def = _extract_stamp_helper()
    js_code = esc_def + "\n" + fn_def + "\n"
    js_code += "var html=process.argv[2];\n"
    js_code += "var snaps=JSON.parse(process.argv[3]);\n"
    js_code += "process.stdout.write(_stampMediaSnapshots(html,snaps));\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(js_code)
        tfname = tf.name
    try:
        result = subprocess.run(
            ["node", tfname, html, json.dumps(snaps)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return result.stdout
    finally:
        os.unlink(tfname)


def test_frontend_stamp_appends_snap_to_media_urls():
    html = '<img class="msg-media-img" src="api/media?path=%2Fhome%2Fx%2Freport.html">'
    snaps = {"/home/x/report.html": "a" * 64}
    out = _run_stamp(html, snaps)
    assert f"&snap={'a' * 64}" in out
    assert "api/media?path=%2Fhome%2Fx%2Freport.html&snap=" in out


def test_frontend_stamp_ignores_unrelated_paths():
    html = '<img src="api/media?path=%2Fother%2Ffile.png">'
    snaps = {"/home/x/report.html": "a" * 64}
    out = _run_stamp(html, snaps)
    assert "&snap=" not in out


def test_frontend_stamp_ignores_invalid_digests():
    html = '<img src="api/media?path=%2Fhome%2Fx%2Freport.html">'
    snaps = {"/home/x/report.html": "not-a-digest"}
    out = _run_stamp(html, snaps)
    assert "&snap=" not in out


def test_frontend_stamp_tags_lazy_preview_placeholders():
    html = '<div class="html-preview-load" data-path="/home/x/report.html">…</div>'
    snaps = {"/home/x/report.html": "b" * 64}
    out = _run_stamp(html, snaps)
    assert f'data-snap="{"b" * 64}"' in out


def test_frontend_stamp_prefix_paths_do_not_corrupt():
    """MUST-FIX 3 repro: /tmp/report.png is a PREFIX of /tmp/report.png.backup.
    Round 1's substring split/join rewrote the longer URL as the shorter path +
    the FIRST digest + '.backup' and dropped its own digest — the .backup
    preview then pointed at the wrong bytes. Each path= value must be rewritten
    atomically with its own digest."""
    html = ('<img src="api/media?path=%2Ftmp%2Freport.png">'
            '<img src="api/media?path=%2Ftmp%2Freport.png.backup">')
    snaps = {"/tmp/report.png": "a" * 64, "/tmp/report.png.backup": "b" * 64}
    out = _run_stamp(html, snaps)
    assert f"path=%2Ftmp%2Freport.png&snap={'a' * 64}" in out
    assert f"path=%2Ftmp%2Freport.png.backup&snap={'b' * 64}" in out
    # The shorter URL must not have swallowed the longer one.
    assert f"path=%2Ftmp%2Freport.png&snap={'a' * 64}.backup" not in out


def test_frontend_stamp_prefix_data_paths_do_not_corrupt():
    """Same prefix class for lazy-preview placeholders (data-path attributes)."""
    html = ('<div class="diff-inline-load" data-path="/tmp/a.png">x</div>'
            '<div class="diff-inline-load" data-path="/tmp/a.png.backup">y</div>')
    snaps = {"/tmp/a.png": "c" * 64, "/tmp/a.png.backup": "d" * 64}
    out = _run_stamp(html, snaps)
    assert f'data-path="/tmp/a.png" data-snap="{"c" * 64}"' in out
    assert f'data-path="/tmp/a.png.backup" data-snap="{"d" * 64}"' in out


def test_frontend_stamp_handles_html_escaped_data_paths():
    """data-path values are HTML-escaped (esc()): a path containing & must still
    resolve exactly after unescaping, and must not get a bogus stamp."""
    html = '<div class="html-preview-load" data-path="/tmp/a&amp;b.html">…</div>'
    snaps = {"/tmp/a&b.html": "e" * 64}
    out = _run_stamp(html, snaps)
    assert f'data-snap="{"e" * 64}"' in out
    # Non-matching escaped path is untouched.
    html2 = '<div class="html-preview-load" data-path="/tmp/other&amp;x.html">…</div>'
    out2 = _run_stamp(html2, snaps)
    assert "data-snap=" not in out2


def test_frontend_stamp_handles_file_url_forms():
    # The backend indexes under BOTH the resolved path and the raw token; the
    # helper must stamp either key form.
    html = '<img src="api/media?path=%2Fhome%2Fx%2Freport.html">'
    snaps = {"file:///home/x/report.html": "c" * 64}
    out = _run_stamp(html, snaps)
    assert "&snap=" not in out  # resolved form is the URL param; raw key alone
    # must not wrongly stamp, but the resolved key MUST:
    snaps2 = {"/home/x/report.html": "c" * 64}
    out2 = _run_stamp(html, snaps2)
    assert f"&snap={'c' * 64}" in out2


def test_frontend_stamp_source_invariants():
    """Non-vacuous source checks: the helper must be called at BOTH settled
    render sites with the message's _media_snapshots, and lazy loaders must
    consume data-snap."""
    src = open(ROOT / "static" / "ui.js", encoding="utf-8").read()
    # Main transcript path and transparent ordered segments must stamp.
    assert "_stampMediaSnapshots(bodyHtml, m._media_snapshots)" in src
    # Transparent segments: original _getCachedRender line is preserved (source
    # window contract), stamping applied on the next line via *_Stamped.
    assert "_getCachedRender(partDisplayText,false);" in src
    assert "_stampMediaSnapshots(partBodyHtml,m._media_snapshots)" in src
    # Worklog scene prose path is intentionally NOT stamped (folded view; the
    # scene render chain is exercised by harness-extracted tests that would
    # break on new helper references — snapshot support covers the main
    # transcript + transparent segments, which are the comparison surface).
    # Lazy loaders must read the stamped digest.
    assert "_mediaSnapQuery(el)" in src
    assert "el.dataset.snap" in src
