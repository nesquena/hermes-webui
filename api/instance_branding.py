"""Installation identity v1. Settings own configuration; only derivatives are public."""

from functools import lru_cache
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import uuid
import zlib

from api import config

MAX_UPLOAD = 2 * 1024 * 1024
PIPELINE = b"hermes-branding-v1-rgba-lanczos-apple-0d0d1a"
ASSETS = {
    "favicon-32.png": ("favicon_32", "image/png"),
    "icon-192.png": ("icon_192", "image/png"),
    "icon-512.png": ("icon_512", "image/png"),
    "apple-touch-icon.png": ("apple_touch_icon", "image/png"),
    "favicon.ico": ("favicon_ico", "image/x-icon"),
}
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_PATH = re.compile(
    r"/(?:session/)?branding/([0-9a-f]{64})/"
    r"(favicon-32\.png|icon-192\.png|icon-512\.png|apple-touch-icon\.png|favicon\.ico)\Z"
)


class BrandingError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _image_module():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def provider_available():
    from api.extensions import get_extension_config
    inventory = get_extension_config()
    return inventory.get("enabled") is True and any(
        entry.get("id") == "custom-branding"
        and type(entry.get("instance_branding_api")) is int
        and entry["instance_branding_api"] == 1
        for entry in inventory.get("extensions", [])
    )


def _branding(settings):
    raw = settings.get("instance_branding")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "schema_version": raw.get("schema_version", 1),
        "enabled": raw.get("enabled") is True,
        "revision": raw.get("revision", ""),
        "owner_extension_id": "custom-branding",
        "manages_instance_name": raw.get("manages_instance_name") is True,
        "image_revision": raw.get("image_revision"),
        "previous_image_revision": raw.get("previous_image_revision"),
    }


def _read_asset(revision, filename):
    """Fixed names only; descriptor anchored reads reject every symlink component."""
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise FileNotFoundError
    if filename not in {*ASSETS, "master.png"}:
        raise FileNotFoundError
    from api.workspace import open_anchored_fd
    root = Path(config.STATE_DIR).resolve()
    target = root / "branding" / revision / filename
    # Also reject links on platforms without openat, following existing file APIs.
    for part in (root / "branding", target.parent, target):
        if part.is_symlink():
            raise FileNotFoundError
    fd = open_anchored_fd(root, target, want_dir=False)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 20 * 1024 * 1024:
            raise FileNotFoundError
        data = stream.read(20 * 1024 * 1024 + 1)
    if not data or len(data) > 20 * 1024 * 1024:
        raise FileNotFoundError
    if filename.endswith(".png"):
        expected = {"favicon-32.png": 32, "icon-192.png": 192,
                    "icon-512.png": 512, "apple-touch-icon.png": 512}.get(filename)
        try:
            _validate_png(data, minimum=expected or 512, maximum=expected or 2048,
                          max_bytes=20 * 1024 * 1024)
        except BrandingError as exc:
            raise FileNotFoundError from exc
    elif data[:6] != b"\x00\x00\x01\x00\x03\x00":
        raise FileNotFoundError
    return data


def _image_fingerprint(root, revision):
    """Detect replacement, edits, missing files and symlinks without image reads."""
    directory = root / "branding" / revision
    fingerprint = []
    for path in (root / "branding", directory, *(directory / name for name in ASSETS)):
        info = path.lstat()
        is_directory = path in (root / "branding", directory)
        if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
            raise FileNotFoundError
        fingerprint.append((info.st_dev, info.st_ino, info.st_size,
                            info.st_mtime_ns, info.st_ctime_ns))
    return tuple(fingerprint)


@lru_cache(maxsize=16)
def _checked_image(root, revision, fingerprint):
    """Bounded process-local cache; public reads still validate anchored bytes."""
    for filename in ASSETS:
        _read_asset(revision, filename)
    # Never cache transient errors or success if files changed during validation.
    if _image_fingerprint(root, revision) != fingerprint:
        raise FileNotFoundError
    return True


def _complete_image(revision):
    if not revision:
        return True
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        return False
    try:
        root = Path(config.STATE_DIR).resolve()
        return _checked_image(root, revision, _image_fingerprint(root, revision))
    except (OSError, ValueError):
        return False


def _snapshot(settings=None):
    settings = config.load_settings() if settings is None else settings
    branding = _branding(settings)
    provider = provider_available()
    active = (branding["enabled"] and type(branding["schema_version"]) is int
              and branding["schema_version"] == 1 and provider
              and _complete_image(branding["image_revision"]))
    name = settings.get("instance_name") or ""
    if branding["manages_instance_name"] and not active:
        name = ""
    # Include raw name, provider and completeness: tabs observe disable/downgrade too.
    token = hashlib.sha256(json.dumps(
        [branding, settings.get("instance_name", ""), provider, active],
        sort_keys=True, ensure_ascii=True,
    ).encode()).hexdigest()
    effective = {"active": bool(active), "name": name or None, "revision": token}
    image = branding["image_revision"] if active else None
    for filename, (key, _mime) in ASSETS.items():
        effective[key] = f"branding/{image}/{filename}" if image else None
    return branding, effective, provider, f'"{token}"'


def effective_instance_identity(settings=None):
    with config._SETTINGS_WRITE_LOCK:
        return _snapshot(settings)[1]


def can_manage(handler):
    from api import auth
    if not auth.is_auth_enabled():
        return False
    info = auth.ensure_trusted_auth_session(handler)
    cookie = (getattr(handler, "_trusted_auth_session_cookie_value", None)
              or auth.parse_cookie(handler))
    if not cookie or not auth.get_session_info(cookie):
        return False
    if not info or info.get("bound_profile"):
        return False
    # None is the native, signed password/passkey legacy session record.
    return info.get("auth_type") in (None, "password", "passkey", "oidc", "trusted")


def management_state(handler):
    with config._SETTINGS_WRITE_LOCK:
        settings = config.load_settings()
        branding, effective, provider, etag = _snapshot(settings)
        manage = can_manage(handler)
        result = {
            "api_version": 1, "name_supported": True,
            "can_manage": manage, "can_upload": manage and provider and _image_module() is not None,
            "provider_available": provider,
            "config": {key: branding[key] for key in (
                "enabled", "revision", "manages_instance_name", "image_revision")},
            "effective": effective,
        }
        result["config"]["instance_name"] = settings.get("instance_name") or ""
        return result, etag


def _require_match(handler, etag):
    submitted = handler.headers.get("If-Match")
    if not submitted:
        raise BrandingError("If-Match required", 428)
    if submitted != etag:
        raise BrandingError("Identity changed; reload before saving", 412)


def _validate_png(data, *, minimum=512, maximum=2048, max_bytes=MAX_UPLOAD):
    if not 0 < len(data) <= max_bytes or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise BrandingError("Expected a static PNG, at most 2 MiB")
    pos, chunks, width = 8, [], 0
    while pos < len(data):
        if pos + 12 > len(data):
            raise BrandingError("Truncated PNG")
        size, kind = struct.unpack_from(">I4s", data, pos)
        end = pos + 12 + size
        if end > len(data) or size > max_bytes:
            raise BrandingError("Invalid PNG chunk")
        payload = data[pos + 8:pos + 8 + size]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise BrandingError("Invalid PNG CRC")
        if not chunks and kind != b"IHDR":
            raise BrandingError("Missing PNG header")
        if kind in (b"acTL", b"fcTL", b"fdAT"):
            raise BrandingError("Animated PNG is not supported")
        if kind[0] & 32 == 0 and kind not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
            raise BrandingError("Unknown critical PNG chunk")
        if kind == b"PLTE" and (b"PLTE" in chunks or b"IDAT" in chunks):
            raise BrandingError("Invalid PNG palette order")
        if kind == b"IDAT" and b"IDAT" in chunks and chunks[-1] != b"IDAT":
            raise BrandingError("Invalid PNG data order")
        if kind == b"IHDR":
            if chunks or size != 13:
                raise BrandingError("Invalid PNG header")
            width, height = struct.unpack_from(">II", payload)
            if width != height or not minimum <= width <= maximum:
                raise BrandingError("PNG must be square, 512 to 2048 pixels")
        chunks.append(kind)
        pos = end
        if kind == b"IEND":
            if size or pos != len(data) or b"IDAT" not in chunks:
                raise BrandingError("Invalid PNG end or trailing data")
            return width
    raise BrandingError("Missing PNG end")


def normalize_image(data):
    Image = _image_module()
    if Image is None:
        raise BrandingError("Install the optional branding image dependency", 503)
    _validate_png(data)
    # Metadata is not part of the identity. Remove ancillary chunks before
    # Pillow can decompress text/ICC profiles; keep the palette's alpha only.
    pixel_data, pos = bytearray(data[:8]), 8
    while pos < len(data):
        size, kind = struct.unpack_from(">I4s", data, pos)
        end = pos + size + 12
        if kind in (b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"):
            pixel_data.extend(data[pos:end])
        pos = end
    try:
        with Image.open(io.BytesIO(pixel_data), formats=["PNG"]) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise BrandingError("Animated PNG is not supported")
            source.load()
            # frombytes drops metadata (including EXIF/ICC/text) even across resize.
            pixels = Image.frombytes("RGBA", source.size, source.convert("RGBA").tobytes())
        files = {}

        def png(name, image):
            output = io.BytesIO()
            image.save(output, format="PNG")
            files[name] = output.getvalue()

        png("master.png", pixels)
        for size, filename in ((32, "favicon-32.png"), (192, "icon-192.png"), (512, "icon-512.png")):
            png(filename, pixels.resize((size, size), Image.Resampling.LANCZOS))
        apple = Image.new("RGBA", (512, 512), "#0d0d1a")
        apple.alpha_composite(pixels.resize((512, 512), Image.Resampling.LANCZOS))
        png("apple-touch-icon.png", apple.convert("RGB"))
        output = io.BytesIO()
        pixels.save(output, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        files["favicon.ico"] = output.getvalue()
        revision = hashlib.sha256(PIPELINE + files["master.png"]).hexdigest()
        return revision, files
    except BrandingError:
        raise
    except Exception as exc:
        raise BrandingError("PNG could not be decoded completely") from exc


def _publish_image(revision, files):
    from api.workspace import open_anchored_fd
    root = Path(config.STATE_DIR).resolve()
    directory = root / "branding"
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = open_anchored_fd(root, directory, want_dir=True)
    stage_name = ".pending-" + uuid.uuid4().hex
    anchored = os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd
    stage = directory / stage_name
    stage_fd = None
    try:
        if anchored:
            os.mkdir(stage_name, 0o700, dir_fd=fd)
            stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        else:
            if directory.is_symlink():
                raise BrandingError("Invalid branding directory", 409)
            stage.mkdir(mode=0o700)
        for filename, data in files.items():
            if anchored:
                leaf = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=stage_fd)
                stream = os.fdopen(leaf, "wb")
            else:
                stream = (stage / filename).open("xb")
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        if (directory / revision).exists():
            for filename, data in files.items():
                if _read_asset(revision, filename) != data:
                    raise BrandingError("Existing image revision is damaged", 409)
        elif anchored:
            os.rename(stage_name, revision, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            if directory.is_symlink():
                raise BrandingError("Invalid branding directory", 409)
            stage.rename(directory / revision)
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        try:
            if anchored:
                shutil.rmtree(stage_name, dir_fd=fd)
            elif stage.exists():
                shutil.rmtree(stage)
        except FileNotFoundError:
            pass
        os.close(fd)


def _collect_images(current, previous):
    # Keep the current and previous reference; fixed revision directories only.
    directory = Path(config.STATE_DIR) / "branding"
    if directory.is_symlink() or not directory.is_dir():
        return
    from api.workspace import open_anchored_fd
    root = Path(config.STATE_DIR).resolve()
    fd = open_anchored_fd(root, root / "branding", want_dir=True)
    try:
        for entry in directory.iterdir():
            if _REVISION.fullmatch(entry.name) and entry.name not in (current, previous):
                if entry.is_dir() and not entry.is_symlink():
                    if os.open in os.supports_dir_fd:
                        shutil.rmtree(entry.name, dir_fd=fd)
                    else:
                        shutil.rmtree(entry)
    finally:
        os.close(fd)


def mutate(handler, patch=None, image=None):
    """Decode outside the lock; compare and commit under the native settings lock."""
    prepared = normalize_image(image) if image is not None else None
    with config._SETTINGS_WRITE_LOCK:
        if not can_manage(handler):
            raise BrandingError("Owner session is no longer valid", 403)
        settings = config.load_settings()
        branding, _effective, provider, etag = _snapshot(settings)
        _require_match(handler, etag)
        if prepared is None and (not isinstance(patch, dict) or set(patch) - {
            "enabled", "instance_name", "manages_instance_name", "action"
        }):
            raise BrandingError("Expected a branding object with known fields", 400)
        if (type(branding["schema_version"]) is not int or branding["schema_version"] != 1) and patch != {"action": "reset"}:
            raise BrandingError("Unsupported branding schema; reset explicitly", 409)
        if not provider and not (patch and (patch.get("action") == "reset" or patch == {"enabled": False})):
            raise BrandingError("Compatible custom-branding provider is unavailable", 409)
        previous_image = branding["image_revision"]
        changes = {}
        if prepared:
            _publish_image(*prepared)
            if prepared[0] != previous_image:
                branding["previous_image_revision"] = previous_image
            branding["image_revision"] = prepared[0]
        else:
            if "action" in patch:
                if patch != {"action": "reset"}:
                    raise BrandingError("Expected only action: reset", 400)
                if branding["manages_instance_name"]:
                    changes["instance_name"] = ""
                branding = _branding({})
            else:
                for key in ("enabled", "manages_instance_name"):
                    if key in patch:
                        if type(patch[key]) is not bool:
                            raise BrandingError(f"{key} must be boolean", 400)
                        branding[key] = patch[key]
                if "instance_name" in patch:
                    value = patch["instance_name"]
                    if not config._is_valid_instance_name(value) or len(value.strip()) > 80:
                        raise BrandingError("Invalid instance name", 400)
                    changes["instance_name"] = value.strip()
        if branding["enabled"] and not _complete_image(branding["image_revision"]):
            raise BrandingError("Branding image is incomplete", 409)
        branding["schema_version"] = 1
        branding["revision"] = uuid.uuid4().hex
        config.save_settings(changes, _branding=branding)
        # Cleanup failure must not turn a committed write into an apparent failure.
        try:
            _collect_images(branding["image_revision"], branding["previous_image_revision"])
        except OSError:
            pass
        return management_state(handler)


def handle_management(handler, *, upload=False, write=False):
    from api.helpers import arm_connection_close, j
    try:
        if not write:
            state, etag = management_state(handler)
            return j(handler, state, extra_headers={"ETag": etag})
        if not can_manage(handler):
            raise BrandingError("Authenticated unrestricted owner session required", 403)
        # Reject before consuming oversized/unframed input; never interpret chunked data.
        lengths = handler.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0])
                or handler.headers.get("Transfer-Encoding")):
            raise BrandingError("One valid Content-Length required", 400)
        if len(lengths[0]) > 10:
            raise BrandingError("Request too large", 413)
        length = int(lengths[0])
        if length > (MAX_UPLOAD if upload else 4096):
            raise BrandingError("Request too large", 413)
        expected = "image/png" if upload else "application/json"
        if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != expected:
            raise BrandingError(f"Expected {expected}", 415)
        with config._SETTINGS_WRITE_LOCK:
            _require_match(handler, _snapshot()[3])
        if upload and _image_module() is None:
            raise BrandingError("Install the optional branding image dependency", 503)
        data = handler.rfile.read(length)
        if len(data) != length:
            raise BrandingError("Truncated request", 400)
        if upload:
            state, etag = mutate(handler, image=data)
        else:
            try:
                patch = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise BrandingError("Invalid JSON", 400) from exc
            state, etag = mutate(handler, patch=patch)
        return j(handler, state, extra_headers={"ETag": etag})
    except BrandingError as exc:
        arm_connection_close(handler)
        return j(handler, {"error": str(exc)}, status=exc.status)
    except OSError:
        arm_connection_close(handler)
        return j(handler, {"error": "Branding could not be persisted"}, status=500)


def public_asset_path(path):
    return _ASSET_PATH.fullmatch(path)


def serve_asset(handler, path):
    from api.helpers import j
    match = public_asset_path(path)
    try:
        if not match:
            raise FileNotFoundError
        revision, filename = match.groups()
        data = _read_asset(revision, filename)
    except (OSError, ValueError):
        return j(handler, {"error": "not found"}, status=404)
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    handler.send_response(304 if handler.headers.get("If-None-Match") == etag else 200)
    handler.send_header("Content-Type", ASSETS[filename][1])
    handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("ETag", etag)
    # A 304 may declare only the size the corresponding 200 would have sent.
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if handler.headers.get("If-None-Match") != etag:
        handler.wfile.write(data)
    return True


def render_manifest(data, path, identity=None):
    identity = effective_instance_identity() if identity is None else identity
    manifest = json.loads(data)
    if identity["name"]:
        manifest["name"] = manifest["short_name"] = identity["name"]
    if identity["icon_192"]:
        prefix = "../" if path.startswith("/session/") else ""
        icons = [
            {"src": prefix + identity[f"icon_{size}"], "sizes": f"{size}x{size}",
             "type": "image/png", "purpose": "any"}
            for size in (192, 512)
        ]
        manifest["icons"] = icons
        for shortcut in manifest.get("shortcuts", []):
            shortcut["icons"] = [icons[0]]
    return json.dumps(manifest, ensure_ascii=False).encode("utf-8")


def render_html(document, identity=None):
    identity = effective_instance_identity() if identity is None else identity
    if identity["name"]:
        name = html.escape(identity["name"], quote=True)
        document = re.sub(r"<title>.*?</title>", lambda _m: f"<title>{name}</title>", document, count=1, flags=re.S)
    if identity["name"] or identity["active"]:
        apple_name = html.escape(identity["name"] or "Hermes", quote=True)
        apple_meta = f'<meta name="apple-mobile-web-app-title" content="{apple_name}">'
        if '<meta name="apple-mobile-web-app-title"' in document:
            document = re.sub(r'<meta name="apple-mobile-web-app-title"[^>]*>',
                              lambda _m: apple_meta, document)
        else:
            document = document.replace("</head>", apple_meta + "\n</head>", 1)
    if identity["favicon_32"]:
        document = re.sub(r'<link\b[^>]*\brel="(?:icon|shortcut icon|apple-touch-icon)"[^>]*>\s*', "", document)
        links = [
            f'<link rel="icon" type="image/png" sizes="32x32" href="{identity["favicon_32"]}" data-instance-branding="1">',
            f'<link rel="shortcut icon" href="{identity["favicon_ico"]}" data-instance-branding="1">',
            f'<link rel="apple-touch-icon" sizes="512x512" href="{identity["apple_touch_icon"]}" data-instance-branding="1">',
        ]
        # After the existing base href bootstrap, so session and subpath resolution survive.
        document = document.replace("</head>", "\n".join(links) + "\n</head>", 1)
    payload = json.dumps({"api_version": 1, "effective": identity}, ensure_ascii=True).replace("<", "\\u003c")
    return document.replace("</head>", f"<script>window.__HERMES_INSTANCE_BRANDING__={payload};</script>\n</head>", 1)
