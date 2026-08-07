import hashlib
import json
import struct
import threading
import zlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


_VALID_WALLPAPER_FILE = f"wallpaper-{'a' * 64}.png"


def _configure_settings_file(config, monkeypatch, tmp_path: Path) -> Path:
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    return settings_file


def test_wallpaper_settings_defaults(monkeypatch, tmp_path: Path) -> None:
    import api.config as config

    _configure_settings_file(config, monkeypatch, tmp_path)

    settings = config.load_settings()

    assert settings["wallpaper_file"] == ""
    assert settings["wallpaper_opacity"] == 0.8
    assert settings["wallpaper_scope"] == "chat"


def test_wallpaper_internal_settings_keys_are_not_publicly_allowed() -> None:
    import api.config as config

    assert config.WALLPAPER_SETTINGS_KEYS == {
        "wallpaper_file",
        "wallpaper_opacity",
        "wallpaper_scope",
    }
    assert config.WALLPAPER_SETTINGS_KEYS.isdisjoint(config._SETTINGS_ALLOWED_KEYS)


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"wallpaper_opacity": True}, "wallpaper_opacity"),
        ({"wallpaper_opacity": "0.8"}, "wallpaper_opacity"),
        ({"wallpaper_opacity": None}, "wallpaper_opacity"),
        ({"wallpaper_opacity": float("nan")}, "wallpaper_opacity"),
        ({"wallpaper_opacity": float("inf")}, "wallpaper_opacity"),
        ({"wallpaper_opacity": float("-inf")}, "wallpaper_opacity"),
        ({"wallpaper_opacity": -0.01}, "wallpaper_opacity"),
        ({"wallpaper_opacity": 1.01}, "wallpaper_opacity"),
        ({"wallpaper_scope": "Chat"}, "wallpaper_scope"),
        ({"wallpaper_scope": "both"}, "wallpaper_scope"),
        ({"wallpaper_scope": "app "}, "wallpaper_scope"),
        ({"wallpaper_file": "wallpaper.png"}, "wallpaper_file"),
        ({"wallpaper_file": f"wallpaper-{'A' * 64}.png"}, "wallpaper_file"),
        ({"wallpaper_file": f"wallpaper-{'a' * 63}.png"}, "wallpaper_file"),
        ({"wallpaper_file": f"wallpaper-{'a' * 64}.gif"}, "wallpaper_file"),
        ({"wallpaper_file": f"nested/wallpaper-{'a' * 64}.png"}, "wallpaper_file"),
        ({"theme": "dark"}, "theme"),
    ],
)
def test_wallpaper_internal_settings_validator_rejects_invalid_values(
    update: dict, match: str
) -> None:
    import api.config as config

    with pytest.raises((TypeError, ValueError), match=match):
        config._validate_wallpaper_internal_update(update)


@pytest.mark.parametrize(
    "update",
    [
        {"wallpaper_opacity": 0},
        {"wallpaper_opacity": 1},
        {"wallpaper_opacity": 0.25},
        {"wallpaper_scope": "chat"},
        {"wallpaper_scope": "app"},
        {"wallpaper_file": ""},
        {"wallpaper_file": _VALID_WALLPAPER_FILE},
        {
            "wallpaper_file": f"wallpaper-{'b' * 64}.jpg",
            "wallpaper_opacity": 0.5,
            "wallpaper_scope": "app",
        },
        {"wallpaper_file": f"wallpaper-{'c' * 64}.webp"},
    ],
)
def test_wallpaper_internal_settings_validator_accepts_strict_values(
    update: dict,
) -> None:
    import api.config as config

    config._validate_wallpaper_internal_update(update)


def test_wallpaper_internal_settings_locked_writer_requires_owned_lock(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)

    with pytest.raises(AssertionError, match="_SETTINGS_WRITE_LOCK"):
        config._save_wallpaper_settings_locked({"wallpaper_scope": "app"})

    assert not settings_file.exists()


def test_wallpaper_internal_settings_locked_writer_persists_valid_update(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)
    update = {
        "wallpaper_file": _VALID_WALLPAPER_FILE,
        "wallpaper_opacity": 0.35,
        "wallpaper_scope": "app",
    }

    with config._SETTINGS_WRITE_LOCK:
        result = config._save_wallpaper_settings_locked(update)

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert {key: persisted[key] for key in config.WALLPAPER_SETTINGS_KEYS} == update
    assert {key: result[key] for key in config.WALLPAPER_SETTINGS_KEYS} == update


@pytest.mark.parametrize(
    "update",
    [
        {"wallpaper_opacity": True},
        {"wallpaper_opacity": float("nan")},
        {"wallpaper_scope": "CHAT"},
        {"wallpaper_file": "../../secret.png"},
        {"theme": "light"},
    ],
)
def test_wallpaper_internal_settings_locked_writer_rejects_invalid_update(
    monkeypatch, tmp_path: Path, update: dict
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises((TypeError, ValueError)):
            config._save_wallpaper_settings_locked(update)

    assert not settings_file.exists()


def test_wallpaper_internal_settings_public_save_cannot_create_wallpaper_state(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)

    result = config.save_settings(
        {
            "theme": "light",
            "wallpaper_file": _VALID_WALLPAPER_FILE,
            "wallpaper_opacity": 0.2,
            "wallpaper_scope": "app",
        }
    )

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["theme"] == "light"
    assert config.WALLPAPER_SETTINGS_KEYS.isdisjoint(persisted)
    assert result["wallpaper_file"] == ""
    assert result["wallpaper_opacity"] == 0.8
    assert result["wallpaper_scope"] == "chat"


def test_wallpaper_internal_settings_public_save_cannot_overwrite_internal_state(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)
    original = {
        "wallpaper_file": _VALID_WALLPAPER_FILE,
        "wallpaper_opacity": 0.45,
        "wallpaper_scope": "app",
    }
    with config._SETTINGS_WRITE_LOCK:
        config._save_wallpaper_settings_locked(original)

    result = config.save_settings(
        {
            "wallpaper_file": "",
            "wallpaper_opacity": 1.0,
            "wallpaper_scope": "chat",
            "show_token_usage": True,
        }
    )

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert {key: persisted[key] for key in config.WALLPAPER_SETTINGS_KEYS} == original
    assert {key: result[key] for key in config.WALLPAPER_SETTINGS_KEYS} == original
    assert persisted["show_token_usage"] is True


@pytest.mark.parametrize(
    ("key", "corrupt_value", "expected"),
    [
        ("wallpaper_opacity", True, 0.8),
        ("wallpaper_opacity", "0.4", 0.8),
        ("wallpaper_opacity", float("nan"), 0.8),
        ("wallpaper_opacity", float("inf"), 0.8),
        ("wallpaper_opacity", -0.1, 0.8),
        ("wallpaper_opacity", 1.1, 0.8),
        ("wallpaper_scope", "Chat", "chat"),
        ("wallpaper_scope", "desktop", "chat"),
        ("wallpaper_file", "../outside.png", ""),
        ("wallpaper_file", f"wallpaper-{'A' * 64}.png", ""),
    ],
)
def test_wallpaper_metadata_corrupt_persisted_values_normalize_to_defaults(
    monkeypatch, tmp_path: Path, key: str, corrupt_value, expected
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)
    settings_file.write_text(json.dumps({key: corrupt_value}), encoding="utf-8")

    settings = config.load_settings()

    assert settings[key] == expected


def test_wallpaper_metadata_corrupt_filename_normalization_does_not_touch_path(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)
    referenced = tmp_path / "do-not-touch.png"
    original_bytes = b"not-an-image-but-still-untouched"
    referenced.write_bytes(original_bytes)
    settings_file.write_text(
        json.dumps({"wallpaper_file": str(referenced)}), encoding="utf-8"
    )
    real_open = Path.open
    real_unlink = Path.unlink

    def _guarded_open(path, *args, **kwargs):
        if path == referenced:
            raise AssertionError("normalization opened the referenced wallpaper path")
        return real_open(path, *args, **kwargs)

    def _guarded_unlink(path, *args, **kwargs):
        if path == referenced:
            raise AssertionError("normalization deleted the referenced wallpaper path")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _guarded_open)
    monkeypatch.setattr(Path, "unlink", _guarded_unlink)

    settings = config.load_settings()

    assert settings["wallpaper_file"] == ""
    monkeypatch.setattr(Path, "open", real_open)
    assert referenced.read_bytes() == original_bytes


def test_wallpaper_metadata_valid_persisted_filename_is_not_checked_on_load(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config

    settings_file = _configure_settings_file(config, monkeypatch, tmp_path)
    settings_file.write_text(
        json.dumps({"wallpaper_file": _VALID_WALLPAPER_FILE}), encoding="utf-8"
    )

    settings = config.load_settings()

    assert settings["wallpaper_file"] == _VALID_WALLPAPER_FILE


# Wallpaper image fixtures are generated here so no unreviewable binary fixture is
# checked into the repository.
def _jpeg_segment(marker: int, payload: bytes = b"") -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _jpeg_sof(
    marker: int = 0xC0,
    *,
    width: int = 1,
    height: int = 1,
    components=((1, 0x11, 0),),
    precision: int = 8,
) -> bytes:
    payload = bytes([precision]) + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    payload += bytes([len(components)])
    payload += b"".join(bytes(component) for component in components)
    return _jpeg_segment(marker, payload)


def _jpeg_sos(
    components=((1, 0x00),), *, ss: int = 0, se: int = 63, ah: int = 0, al: int = 0
) -> bytes:
    payload = bytes([len(components)])
    payload += b"".join(bytes(component) for component in components)
    payload += bytes([ss, se, (ah << 4) | al])
    return _jpeg_segment(0xDA, payload)


def _jpeg_tables() -> bytes:
    dqt = _jpeg_segment(0xDB, b"\x00" + bytes(range(1, 65)))
    dc_table = b"\x00\x01" + (b"\x00" * 15) + b"\x00"
    ac_table = b"\x10\x01" + (b"\x00" * 15) + b"\x00"
    return dqt + _jpeg_segment(0xC4, dc_table + ac_table)


def _baseline_jpeg(
    *,
    width: int = 1,
    height: int = 1,
    components=((1, 0x11, 0),),
    entropy: bytes = b"\x2a",
    before_sof: bytes = b"",
    between_sof_and_sos: bytes = b"",
    after_scan: bytes = b"",
    sof_marker: int = 0xC0,
    sos: bytes | None = None,
    eoi: bytes = b"\xff\xd9",
) -> bytes:
    return (
        b"\xff\xd8"
        + before_sof
        + _jpeg_tables()
        + _jpeg_sof(
            sof_marker, width=width, height=height, components=components
        )
        + between_sof_and_sos
        + (sos if sos is not None else _jpeg_sos(tuple((c[0], 0) for c in components)))
        + entropy
        + after_scan
        + eoi
    )


def _progressive_jpeg(*, entropy: bytes = b"\x15") -> bytes:
    components = ((1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0))
    scans = _jpeg_sos(((1, 0), (2, 0), (3, 0)), ss=0, se=0) + entropy
    for component_id in (1, 2, 3):
        scans += _jpeg_sos(((component_id, 0),), ss=1, se=63) + entropy
    return (
        b"\xff\xd8"
        + _jpeg_tables()
        + _jpeg_sof(0xC2, components=components)
        + scans
        + b"\xff\xd9"
    )


def _resize_jpeg_entropy(image: bytes, target_size: int) -> bytes:
    eoi = image[-2:]
    return image[:-2] + (b"\x2a" * (target_size - len(image))) + eoi


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _png_chunk(
    name: bytes,
    payload: bytes = b"",
    *,
    length: int | None = None,
    crc: int | None = None,
) -> bytes:
    checksum = zlib.crc32(name + payload) if crc is None else crc
    return struct.pack(">I", len(payload) if length is None else length) + name + payload + struct.pack(">I", checksum)


def _png_ihdr(
    *,
    width: int = 1,
    height: int = 1,
    bit_depth: int = 8,
    color_type: int = 6,
    compression: int = 0,
    filter_method: int = 0,
    interlace: int = 0,
) -> bytes:
    return _png_chunk(
        b"IHDR",
        struct.pack(
            ">IIBBBBB",
            width,
            height,
            bit_depth,
            color_type,
            compression,
            filter_method,
            interlace,
        ),
    )


def _png_raw_rows(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    filters: tuple[int, ...] | None = None,
) -> bytes:
    row_bytes = (width * _PNG_CHANNELS[color_type] * bit_depth + 7) // 8
    row_filters = filters or ((0,) * height)
    assert len(row_filters) == height
    return b"".join(bytes([row_filter]) + bytes(row_bytes) for row_filter in row_filters)


def _png_from_chunks(*chunks: bytes, trailing: bytes = b"") -> bytes:
    return _PNG_SIGNATURE + b"".join(chunks) + trailing


def _png_image(
    *,
    width: int = 1,
    height: int = 1,
    bit_depth: int = 8,
    color_type: int = 6,
    filters: tuple[int, ...] | None = None,
    before_plte: tuple[bytes, ...] = (),
    plte: bytes | None = None,
    before_idat: tuple[bytes, ...] = (),
    compressed: bytes | None = None,
    idat_parts: int = 1,
    after_idat: tuple[bytes, ...] = (),
) -> bytes:
    raw = _png_raw_rows(width, height, bit_depth, color_type, filters)
    encoded = zlib.compress(raw) if compressed is None else compressed
    if plte is None and color_type == 3:
        plte = b"\x00\x00\x00"
    split_at = len(encoded) // idat_parts if idat_parts > 1 else len(encoded)
    idats = []
    for index in range(idat_parts):
        start = index * split_at
        end = len(encoded) if index == idat_parts - 1 else (index + 1) * split_at
        idats.append(_png_chunk(b"IDAT", encoded[start:end]))
    chunks = [
        _png_ihdr(
            width=width,
            height=height,
            bit_depth=bit_depth,
            color_type=color_type,
        ),
        *before_plte,
    ]
    if plte is not None:
        chunks.append(_png_chunk(b"PLTE", plte))
    chunks.extend((*before_idat, *idats, *after_idat, _png_chunk(b"IEND")))
    return _png_from_chunks(*chunks)


@pytest.mark.parametrize(
    ("color_type", "bit_depth"),
    [
        (0, 1),
        (0, 2),
        (0, 4),
        (0, 8),
        (0, 16),
        (2, 8),
        (2, 16),
        (3, 1),
        (3, 2),
        (3, 4),
        (3, 8),
        (4, 8),
        (4, 16),
        (6, 8),
        (6, 16),
    ],
)
def test_png_accepts_every_legal_color_type_bit_depth_pair(
    color_type: int, bit_depth: int
) -> None:
    from api.wallpaper import validate_wallpaper

    image = _png_image(width=3, bit_depth=bit_depth, color_type=color_type)

    result = validate_wallpaper(image)
    assert (result.mime_type, result.extension, result.width, result.height) == (
        "image/png",
        "png",
        3,
        1,
    )
    assert result.digest == hashlib.sha256(image).hexdigest()


def test_png_accepts_sub_byte_rows_all_filter_types_and_consecutive_idat() -> None:
    from api.wallpaper import validate_wallpaper

    image = _png_image(
        width=9,
        height=5,
        bit_depth=1,
        color_type=0,
        filters=(0, 1, 2, 3, 4),
        idat_parts=3,
    )

    assert validate_wallpaper(image).height == 5


@pytest.mark.parametrize(
    "image",
    [
        _png_image(before_plte=(_png_chunk(b"sRGB", b"\x03"),)),
        _png_image(before_plte=(_png_chunk(b"gAMA", struct.pack(">I", 45_455)),)),
        _png_image(before_idat=(_png_chunk(b"pHYs", struct.pack(">IIB", 1, 2, 1)),)),
        _png_image(color_type=2, plte=b"\x00\x00\x00"),
        _png_image(
            bit_depth=4,
            color_type=0,
            before_idat=(_png_chunk(b"tRNS", struct.pack(">H", 15)),),
        ),
        _png_image(
            bit_depth=8,
            color_type=2,
            before_idat=(_png_chunk(b"tRNS", struct.pack(">HHH", 255, 1, 2)),),
        ),
        _png_image(
            bit_depth=2,
            color_type=3,
            plte=b"\x00\x00\x00\xff\xff\xff",
            before_idat=(_png_chunk(b"tRNS", b"\x00\xff"),),
        ),
    ],
)
def test_png_accepts_exact_allowed_ancillary_chunks(image: bytes) -> None:
    from api.wallpaper import validate_wallpaper

    assert validate_wallpaper(image).mime_type == "image/png"


@pytest.mark.parametrize(
    "name",
    [b"cHRM", b"iCCP", b"sBIT", b"bKGD", b"hIST", b"tEXt", b"zTXt", b"iTXt", b"tIME", b"vpAg", b"aaAa"],
)
def test_png_accepts_ancillary_chunks_without_inspecting_metadata(name: bytes) -> None:
    from api.wallpaper import validate_wallpaper

    result = validate_wallpaper(_png_image(before_idat=(_png_chunk(name),)))

    assert (result.mime_type, result.width, result.height) == ("image/png", 1, 1)


def test_png_accepts_interlaced_header_without_decoding_pixels() -> None:
    from api.wallpaper import validate_wallpaper

    image = _png_from_chunks(_png_ihdr(width=7, height=5, interlace=1))

    result = validate_wallpaper(image)

    assert (result.mime_type, result.width, result.height) == ("image/png", 7, 5)


def test_png_header_probe_rejects_invalid_ihdr_coding_fields() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _png_from_chunks(_png_ihdr(compression=1))

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image)


def _png_with_raw_stream(raw: bytes, *, compressed: bytes | None = None) -> bytes:
    stream = zlib.compress(raw) if compressed is None else compressed
    return _png_from_chunks(
        _png_ihdr(),
        _png_chunk(b"IDAT", stream),
        _png_chunk(b"IEND"),
    )


def _png_non_eof_stream(raw: bytes) -> bytes:
    compressor = zlib.compressobj()
    return compressor.compress(raw) + compressor.flush(zlib.Z_SYNC_FLUSH)


def _webp_chunk(name: bytes, payload: bytes, *, pad: bytes | None = None) -> bytes:
    padding = (b"\x00" if len(payload) % 2 else b"") if pad is None else pad
    return name + struct.pack("<I", len(payload)) + payload + padding


def _webp_riff(*chunks: bytes, form: bytes = b"WEBP", trailing: bytes = b"") -> bytes:
    body = form + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body + trailing


def _webp_vp8_payload(
    *,
    width: int = 2,
    height: int = 1,
    key_frame: bool = True,
    version: int = 0,
    show_frame: bool = True,
    partition_length: int = 1,
    start_code: bytes = b"\x9d\x01\x2a",
    horizontal_scale: int = 0,
    vertical_scale: int = 0,
    partition: bytes | None = None,
) -> bytes:
    frame_tag = (
        (not key_frame)
        | (version << 1)
        | (show_frame << 4)
        | (partition_length << 5)
    )
    partition_bytes = bytes(partition_length) if partition is None else partition
    return (
        frame_tag.to_bytes(3, "little")
        + start_code
        + ((horizontal_scale << 14) | width).to_bytes(2, "little")
        + ((vertical_scale << 14) | height).to_bytes(2, "little")
        + partition_bytes
    )


def _webp_bits(*fields: tuple[int, int]) -> bytes:
    bits: list[int] = []
    for value, count in fields:
        bits.extend((value >> offset) & 1 for offset in range(count))
    return bytes(
        sum(bit << offset for offset, bit in enumerate(bits[start : start + 8]))
        for start in range(0, len(bits), 8)
    )


def _webp_vp8l_payload(
    *,
    width: int = 2,
    height: int = 1,
    alpha: bool = False,
    signature: int = 0x2F,
    version: int = 0,
    red: int = 255,
    green: int = 0,
    blue: int = 0,
    alpha_value: int = 255,
    distance: int = 0,
    entropy: bytes | None = None,
) -> bytes:
    packed = (
        (width - 1)
        | ((height - 1) << 14)
        | (alpha << 28)
        | (version << 29)
    )

    # The accepted generated lossless subset has no transforms/cache/meta-groups
    # and five one-symbol simple Huffman trees (green, red, blue, alpha, distance).
    fields: list[tuple[int, int]] = [(0, 1), (0, 1), (0, 1)]
    for symbol in (green, red, blue, alpha_value, distance):
        fields.extend(((1, 1), (0, 1), (1, 1), (symbol, 8)))
    encoded_entropy = _webp_bits(*fields) if entropy is None else entropy
    return bytes([signature]) + packed.to_bytes(4, "little") + encoded_entropy


def _webp_vp8l_entropy_with_first_tree(
    *first_tree_fields: tuple[int, int],
) -> bytes:
    fields: list[tuple[int, int]] = [
        (0, 1),
        (0, 1),
        (0, 1),
        *first_tree_fields,
    ]
    for symbol in (255, 0, 255, 0):
        fields.extend(((1, 1), (0, 1), (1, 1), (symbol, 8)))
    return _webp_bits(*fields)


def _webp_vp8x_payload(
    *, flags: int = 0, width: int = 2, height: int = 1, reserved: bytes = b"\x00\x00\x00"
) -> bytes:
    return (
        bytes([flags])
        + reserved
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def test_webp_accepts_generated_simple_vp8_and_red_vp8l() -> None:
    from api.wallpaper import validate_wallpaper

    vp8 = _webp_riff(_webp_chunk(b"VP8 ", _webp_vp8_payload()))
    red_vp8l = _webp_riff(_webp_chunk(b"VP8L", _webp_vp8l_payload()))

    for image in (vp8, red_vp8l):
        result = validate_wallpaper(image)
        assert (result.mime_type, result.extension, result.width, result.height) == (
            "image/webp",
            "webp",
            2,
            1,
        )
        assert result.digest == hashlib.sha256(image).hexdigest()


def test_webp_accepts_extended_metadata_without_decoding_pixels() -> None:
    from api.wallpaper import validate_wallpaper

    image = _webp_riff(
        _webp_chunk(b"VP8X", _webp_vp8x_payload(flags=0x3C)),
        _webp_chunk(b"ICCP", b"profile"),
        _webp_chunk(b"ALPH", b"\x1c\xff\x80"),
        _webp_chunk(b"VP8 ", _webp_vp8_payload()),
        _webp_chunk(b"EXIF", b"exif"),
        _webp_chunk(b"XMP ", b"xmp"),
    )

    assert validate_wallpaper(image).width == 2


def test_webp_header_probe_rejects_vp8x_without_image_chunk() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _webp_riff(_webp_chunk(b"VP8X", _webp_vp8x_payload()))

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image)


def test_webp_header_probe_uses_image_dimensions_not_vp8x_canvas() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _webp_riff(
        _webp_chunk(b"VP8X", _webp_vp8x_payload(width=1, height=1)),
        _webp_chunk(b"VP8L", _webp_vp8l_payload(width=10_000, height=5_001)),
    )

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image)


def test_webp_header_probe_rejects_multiple_images_and_malformed_tail() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _webp_riff(
        _webp_chunk(b"VP8L", _webp_vp8l_payload()),
        _webp_chunk(b"VP8L", _webp_vp8l_payload()),
    )
    malformed_tail = _webp_riff(
        _webp_chunk(b"VP8L", _webp_vp8l_payload()),
        b"JUNK",
    )

    for candidate in (image, malformed_tail):
        with pytest.raises(WallpaperValidationError):
            validate_wallpaper(candidate)


def test_webp_header_probe_rejects_invalid_riff_and_chunk_boundaries() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    valid = _webp_riff(_webp_chunk(b"VP8L", _webp_vp8l_payload()))
    bad_riff = valid[:4] + b"\x00\x00\x00\x00" + valid[8:]
    bad_chunk = valid[:16] + b"\xff\xff\xff\xff" + valid[20:]

    for image in (bad_riff, bad_chunk):
        with pytest.raises(WallpaperValidationError):
            validate_wallpaper(image)


@pytest.mark.parametrize(
    ("width", "height", "accepted"),
    [
        (16_384, 1, True),
        (10_000, 5_000, True),
        (10_000, 5_001, False),
    ],
)
def test_webp_simple_vp8l_enforces_shared_dimension_limits(
    width: int, height: int, accepted: bool
) -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _webp_riff(
        _webp_chunk(b"VP8L", _webp_vp8l_payload(width=width, height=height))
    )
    if accepted:
        assert (validate_wallpaper(image).width, validate_wallpaper(image).height) == (
            width,
            height,
        )
    else:
        with pytest.raises(WallpaperValidationError):
            validate_wallpaper(image)


def test_wallpaper_public_boundary_returns_frozen_signature_metadata() -> None:
    from api.wallpaper import ValidatedWallpaper, validate_wallpaper

    image = _baseline_jpeg(width=7, height=5)
    result = validate_wallpaper(
        image, mime_type="text/html", filename="definitely-not-an-image.svg"
    )

    assert result == ValidatedWallpaper(
        mime_type="image/jpeg",
        extension="jpg",
        width=7,
        height=5,
        digest=hashlib.sha256(image).hexdigest(),
        size=len(image),
    )
    with pytest.raises(FrozenInstanceError):
        result.width = 8


@pytest.mark.parametrize(
    "image",
    [
        b"",
        b"<html><body>not an image</body></html>",
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        bytes(range(1, 129)),
        b"\x89PNG\r\n\x1a\n" + b"not-yet-supported",
        b"RIFF\x04\x00\x00\x00WEBP",
        b"GIF89a",
        b"BMnot-supported",
    ],
)
def test_wallpaper_public_boundary_rejects_empty_nonimages_and_unsupported_signatures(
    image: bytes,
) -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image, mime_type="image/jpeg", filename="wallpaper.jpg")


@pytest.mark.parametrize(
    ("width", "height", "accepted"),
    [
        (1, 1, True),
        (16_384, 1, True),
        (16_385, 1, False),
        (10_000, 5_000, True),
        (10_000, 5_001, False),
    ],
)
def test_wallpaper_public_boundary_enforces_exact_dimension_limits(
    width: int, height: int, accepted: bool
) -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _baseline_jpeg(width=width, height=height)
    if accepted:
        assert validate_wallpaper(image).width == width
    else:
        with pytest.raises(WallpaperValidationError):
            validate_wallpaper(image)


def test_wallpaper_public_boundary_accepts_exact_encoded_limit() -> None:
    from api.wallpaper import validate_wallpaper

    image = _resize_jpeg_entropy(_baseline_jpeg(), 10 * 1024 * 1024)

    assert len(image) == 10 * 1024 * 1024
    assert validate_wallpaper(image).size == len(image)


def test_wallpaper_public_boundary_rejects_one_byte_over_encoded_limit() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = _resize_jpeg_entropy(_baseline_jpeg(), 10 * 1024 * 1024 + 1)

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image)


def test_wallpaper_public_boundary_maps_released_memoryview_to_validation_error() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = memoryview(_baseline_jpeg())
    image.release()

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image)


def test_wallpaper_public_boundary_snapshots_mutable_input(monkeypatch) -> None:
    import api.wallpaper as wallpaper

    image = bytearray(_baseline_jpeg())
    original = bytes(image)
    real_probe = wallpaper._probe_jpeg_dimensions

    def _mutate_after_validation(data):
        dimensions = real_probe(data)
        image[-3] ^= 0x01
        return dimensions

    monkeypatch.setattr(wallpaper, "_probe_jpeg_dimensions", _mutate_after_validation)

    assert wallpaper.validate_wallpaper(image).digest == hashlib.sha256(original).hexdigest()


def test_wallpaper_public_boundary_hashes_only_after_structural_validation(
    monkeypatch,
) -> None:
    import api.wallpaper as wallpaper

    def _unexpected_hash(*args, **kwargs):
        raise AssertionError("invalid input was hashed")

    monkeypatch.setattr(wallpaper.hashlib, "sha256", _unexpected_hash)

    with pytest.raises(wallpaper.WallpaperValidationError):
        wallpaper.validate_wallpaper(b"\xff\xd8\xff\xe0\x00")


@pytest.mark.parametrize(
    "image",
    [
        _baseline_jpeg(),
        _baseline_jpeg(entropy=b"\x2a\xff\x00\x31"),
        _baseline_jpeg(entropy=b"\x2a\xff\xff\xff\xd9", eoi=b""),
        _progressive_jpeg(),
    ],
)
def test_jpeg_accepts_baseline_progressive_stuffing_and_fill(image: bytes) -> None:
    from api.wallpaper import validate_wallpaper

    assert validate_wallpaper(image).mime_type == "image/jpeg"


def test_jpeg_header_probe_requires_complete_frame_component_header() -> None:
    from api.wallpaper import WallpaperValidationError, validate_wallpaper

    image = b"\xff\xd8\xff\xc0\x00\x07\x08\x00\x01\x00\x01"

    with pytest.raises(WallpaperValidationError):
        validate_wallpaper(image)


def test_jpeg_accepts_progressive_ac_eobrun_huffman_symbol_before_sof() -> None:
    from api.wallpaper import validate_wallpaper

    progressive_ac_dht = _jpeg_segment(
        0xC4,
        b"\x10" + b"\x01" + (b"\x00" * 15) + b"\x10",
    )
    image = _progressive_jpeg().replace(
        _jpeg_sof(0xC2, components=((1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0))),
        progressive_ac_dht
        + _jpeg_sof(
            0xC2, components=((1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0))
        ),
        1,
    )

    assert validate_wallpaper(image).mime_type == "image/jpeg"


def test_jpeg_accepts_restarts_and_dri_changes_between_scans() -> None:
    from api.wallpaper import validate_wallpaper

    components = ((1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0))
    image = (
        b"\xff\xd8"
        + _jpeg_tables()
        + _jpeg_sof(0xC0, components=components)
        + _jpeg_segment(0xDD, b"\x00\x02")
        + _jpeg_sos(((1, 0),))
        + b"\x22\xff\xd0\x23\xff\xd1"
        + _jpeg_segment(0xDD, b"\x00\x01")
        + _jpeg_sos(((2, 0),))
        + b"\x24\xff\xd0"
        + _jpeg_segment(0xDD, b"\x00\x00")
        + _jpeg_sos(((3, 0),))
        + b"\x25\xff\xd9"
    )

    assert validate_wallpaper(image).width == 1


def _progressive_with_scans(scans: list[tuple[tuple, int, int, int, int]]) -> bytes:
    components = ((1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0))
    encoded = b"\xff\xd8" + _jpeg_tables() + _jpeg_sof(0xC2, components=components)
    for scan_components, ss, se, ah, al in scans:
        encoded += _jpeg_sos(scan_components, ss=ss, se=se, ah=ah, al=al) + b"\x21"
    return encoded + b"\xff\xd9"


def test_jpeg_accepts_progressive_initialization_and_refinement() -> None:
    from api.wallpaper import validate_wallpaper

    scans = [
        (((1, 0), (2, 0), (3, 0)), 0, 0, 0, 1),
        (((1, 0), (2, 0), (3, 0)), 0, 0, 1, 0),
    ]
    for component in (1, 2, 3):
        scans.extend(
            [
                (((component, 0),), 1, 63, 0, 1),
                (((component, 0),), 1, 63, 1, 0),
            ]
        )

    assert validate_wallpaper(_progressive_with_scans(scans)).height == 1


# Descriptor-root and filesystem-security tests use only pytest-owned temporary
# directories. They never point a wallpaper primitive at a host file.
def _open_wallpaper_descriptor_root(monkeypatch, tmp_path: Path):
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    with config._SETTINGS_WRITE_LOCK:
        root = wallpaper._WallpaperDir.open_locked()
    return root, state_dir


def test_wallpaper_descriptor_root_is_lazy_and_requires_shared_lock(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "created-after-import"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    state_dir.mkdir()

    monkeypatch.setattr(wallpaper, "_settings_lock_is_owned", lambda: False)
    with pytest.raises(wallpaper.WallpaperUnavailableError, match="_SETTINGS_WRITE_LOCK"):
        wallpaper._WallpaperDir.open_locked()
    assert not (state_dir / "wallpaper").exists()

    monkeypatch.undo()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    with config._SETTINGS_WRITE_LOCK:
        root = wallpaper._WallpaperDir.open_locked()
    try:
        assert (state_dir / "wallpaper").is_dir()
    finally:
        root.close()


@pytest.mark.parametrize(
    ("missing_operation", "support_set"),
    [
        ("open", "supports_dir_fd"),
        ("mkdir", "supports_dir_fd"),
        ("stat", "supports_dir_fd"),
        ("unlink", "supports_dir_fd"),
        ("stat", "supports_follow_symlinks"),
    ],
)
def test_wallpaper_descriptor_capability_missing_operation_fails_closed(
    monkeypatch, tmp_path: Path, missing_operation: str, support_set: str
) -> None:
    import os

    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    supported = set(getattr(wallpaper.os, support_set))
    supported.discard(getattr(wallpaper.os, missing_operation))
    monkeypatch.setattr(wallpaper.os, support_set, supported)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="capability"):
            wallpaper._WallpaperDir.open_locked()
    assert os.listdir(state_dir) == []


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_wallpaper_descriptor_capability_missing_open_flag_fails_closed(
    monkeypatch, tmp_path: Path, missing_flag: str
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delattr(wallpaper.os, missing_flag)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="capability"):
            wallpaper._WallpaperDir.open_locked()
    assert list(state_dir.iterdir()) == []


def test_wallpaper_descriptor_capability_requires_fd_enumeration(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    supported = set(wallpaper.os.supports_fd)
    supported.discard(wallpaper.os.listdir)
    supported.discard(wallpaper.os.scandir)
    monkeypatch.setattr(wallpaper.os, "supports_fd", supported)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="enumeration"):
            wallpaper._WallpaperDir.open_locked()
    assert list(state_dir.iterdir()) == []


def test_wallpaper_descriptor_capability_requires_noreplace_adapter(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(wallpaper, "_RENAME_NOREPLACE_ADAPTER", None)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="no-replace"):
            wallpaper._WallpaperDir.open_locked()
    assert list(state_dir.iterdir()) == []


@pytest.mark.parametrize("symlink_level", ["state", "wallpaper"])
def test_wallpaper_descriptor_root_rejects_symlink(
    monkeypatch, tmp_path: Path, symlink_level: str
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    real = tmp_path / "attacker-tree"
    real.mkdir()
    state_dir = tmp_path / "state"
    if symlink_level == "state":
        state_dir.symlink_to(real, target_is_directory=True)
    else:
        state_dir.mkdir()
        (state_dir / "wallpaper").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._WallpaperDir.open_locked()
    assert list(real.iterdir()) == []


def test_wallpaper_descriptor_root_rejects_non_directory(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = state_dir / "wallpaper"
    marker.write_bytes(b"do-not-touch")
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._WallpaperDir.open_locked()
    assert marker.read_bytes() == b"do-not-touch"


def test_wallpaper_descriptor_root_path_swap_fails_identity_recheck(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    original = state_dir / "wallpaper"
    moved = state_dir / "wallpaper-original"
    original.rename(moved)
    original.mkdir()
    (moved / "original-marker").write_bytes(b"original")
    (original / "replacement-marker").write_bytes(b"replacement")

    try:
        with config._SETTINGS_WRITE_LOCK:
            with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
                root.reverify_locked()
        assert (moved / "original-marker").read_bytes() == b"original"
        assert (original / "replacement-marker").read_bytes() == b"replacement"
    finally:
        root.close()


def test_wallpaper_descriptor_reverify_requires_shared_lock(
    monkeypatch, tmp_path: Path
) -> None:
    import api.wallpaper as wallpaper

    root, _ = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    monkeypatch.setattr(wallpaper, "_settings_lock_is_owned", lambda: False)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="_SETTINGS_WRITE_LOCK"):
            root.reverify_locked()
    finally:
        root.close()


def test_wallpaper_descriptor_root_retains_state_fd_and_closes_both(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    root, _ = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    wallpaper_fd = root.fd
    state_fd = root.state_fd
    assert wallpaper_fd >= 0
    assert state_fd >= 0

    root.close()
    root.close()

    with pytest.raises(OSError):
        os.fstat(wallpaper_fd)
    with pytest.raises(OSError):
        os.fstat(state_fd)


def test_wallpaper_descriptor_root_reverify_brackets_parent_identity_and_stats_child_by_fd(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    original_state = tmp_path / "state-original"
    real_lstat = wallpaper.os.lstat
    real_stat = wallpaper.os.stat
    child_stat_seen = False

    def _guarded_lstat(path):
        if path == state_dir / "wallpaper":
            raise AssertionError("child identity must be sampled through state_fd")
        return real_lstat(path)

    def _swap_parent_at_child_stat(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal child_stat_seen
        if path == "wallpaper" and dir_fd == root.state_fd and not child_stat_seen:
            child_stat_seen = True
            state_dir.rename(original_state)
            state_dir.mkdir()
            (state_dir / "wallpaper").mkdir()
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(wallpaper.os, "lstat", _guarded_lstat)
    monkeypatch.setattr(wallpaper.os, "stat", _swap_parent_at_child_stat)
    try:
        with config._SETTINGS_WRITE_LOCK:
            with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
                root.reverify_locked()
        assert child_stat_seen is True
        assert (original_state / "wallpaper").is_dir()
        assert (state_dir / "wallpaper").is_dir()
    finally:
        root.close()


def test_wallpaper_descriptor_enumeration_uses_owned_fd(
    monkeypatch, tmp_path: Path
) -> None:
    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    (state_dir / "wallpaper" / "operator-note").write_bytes(b"untouched")
    try:
        assert root.listdir() == ["operator-note"]
    finally:
        root.close()


@pytest.mark.parametrize(
    "name",
    [
        "../wallpaper-" + "a" * 64 + ".png",
        "wallpaper-" + "A" * 64 + ".png",
        "wallpaper-" + "a" * 63 + ".png",
        "wallpaper-" + "a" * 64 + ".gif",
        ".wallpaper-stage-" + "a" * 31 + ".tmp",
        ".wallpaper-stage-" + "A" * 32 + ".tmp",
        ".wallpaper-stage-" + "a" * 32 + ".tmp/child",
        ".",
        "operator-note",
    ],
)
def test_wallpaper_descriptor_corrupt_filename_never_reaches_mutation_adapter(
    monkeypatch, name: str
) -> None:
    import api.wallpaper as wallpaper

    calls = []
    monkeypatch.setattr(
        wallpaper,
        "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER",
        lambda *args: calls.append(args) or True,
    )

    assert wallpaper._unlink_if_identity(name, (1, 2), 12345) is False
    assert calls == []


def test_wallpaper_descriptor_corrupt_names_never_reach_open_or_install(
    monkeypatch
) -> None:
    import api.wallpaper as wallpaper

    open_calls = []
    rename_calls = []
    monkeypatch.setattr(
        wallpaper.os,
        "open",
        lambda *args, **kwargs: open_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        wallpaper,
        "_RENAME_NOREPLACE_ADAPTER",
        lambda *args: rename_calls.append(args) or True,
    )

    for name in (None, "", "../outside", "operator-note"):
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._read_verified_file(name, 12345)
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._rename_noreplace(name, _VALID_WALLPAPER_FILE, 12345)
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._rename_noreplace(
                ".wallpaper-stage-" + "a" * 32 + ".tmp", name, 12345
            )

    assert open_calls == []
    assert rename_calls == []


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo", "directory"])
def test_wallpaper_descriptor_content_symlink_and_nonregular_entries_are_not_read(
    monkeypatch, tmp_path: Path, entry_kind: str
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    content_name = "wallpaper-" + "a" * 64 + ".png"
    content_path = state_dir / "wallpaper" / content_name
    outside = tmp_path / "outside"
    outside.write_bytes(b"attacker-controlled")
    if entry_kind == "symlink":
        content_path.symlink_to(outside)
    elif entry_kind == "fifo":
        os.mkfifo(content_path)
    else:
        content_path.mkdir()

    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._read_verified_file(content_name, root.fd)
        assert outside.read_bytes() == b"attacker-controlled"
    finally:
        root.close()


def test_wallpaper_descriptor_content_regular_to_fifo_path_swap_does_not_block(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    content_name = "wallpaper-" + "a" * 64 + ".png"
    content_path = state_dir / "wallpaper" / content_name
    saved_path = state_dir / "wallpaper" / "saved-regular"
    content_path.write_bytes(b"trusted-original")
    real_open = wallpaper.os.open
    flags_seen = None

    def _swap_to_fifo_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal flags_seen
        if path == content_name:
            flags_seen = flags
            content_path.rename(saved_path)
            os.mkfifo(content_path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(wallpaper.os, "open", _swap_to_fifo_before_open)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError):
            wallpaper._read_verified_file(content_name, root.fd)
        assert flags_seen is not None
        assert flags_seen & os.O_NONBLOCK
        assert saved_path.read_bytes() == b"trusted-original"
        assert content_path.is_fifo()
    finally:
        root.close()


def test_wallpaper_descriptor_content_path_swap_fails_identity_check(
    monkeypatch, tmp_path: Path
) -> None:
    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    content_name = "wallpaper-" + "a" * 64 + ".png"
    content_path = state_dir / "wallpaper" / content_name
    saved_path = state_dir / "wallpaper" / "saved-original"
    content_path.write_bytes(b"trusted-original")
    real_open = wallpaper.os.open
    swapped = False

    def _swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == content_name and not swapped:
            swapped = True
            content_path.rename(saved_path)
            content_path.write_bytes(b"attacker-replacement")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(wallpaper.os, "open", _swap_before_open)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
            wallpaper._read_verified_file(content_name, root.fd)
        assert saved_path.read_bytes() == b"trusted-original"
        assert content_path.read_bytes() == b"attacker-replacement"
    finally:
        root.close()


def test_wallpaper_descriptor_stage_name_mode_and_identity_are_verified(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    image = _png_image()
    try:
        assert wallpaper._STAGE_NAME.fullmatch(stage.name)
        os.write(stage.fd, image)
        assert wallpaper._validate_stage(stage, root.fd).digest == hashlib.sha256(image).hexdigest()
        os.fchmod(stage.fd, 0o640)
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="mode"):
            wallpaper._validate_stage(stage, root.fd)
        assert (state_dir / "wallpaper" / stage.name).read_bytes() == image
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_stage_validates_actual_staged_bytes(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, _ = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    invalid_staged_bytes = b"not-the-valid-caller-bytes"
    os.write(stage.fd, invalid_staged_bytes)
    try:
        with pytest.raises(wallpaper.WallpaperValidationError):
            wallpaper._validate_stage(stage, root.fd)
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_stage_rejects_ownership_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    image = _png_image()
    os.write(stage.fd, image)
    real_fstat = wallpaper.os.fstat

    def _foreign_owner_fstat(fd):
        metadata = real_fstat(fd)
        if fd == stage.fd:
            values = list(metadata)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(wallpaper.os, "fstat", _foreign_owner_fstat)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="ownership"):
            wallpaper._validate_stage(stage, root.fd)
        assert (state_dir / "wallpaper" / stage.name).read_bytes() == image
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_stage_path_replacement_is_not_validated(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    image = _png_image()
    os.write(stage.fd, image)
    stage_path = state_dir / "wallpaper" / stage.name
    moved_path = state_dir / "wallpaper" / "retained-stage"
    stage_path.rename(moved_path)
    stage_path.write_bytes(image)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
            wallpaper._validate_stage(stage, root.fd)
        assert moved_path.read_bytes() == image
        assert stage_path.read_bytes() == image
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_stage_rejects_one_byte_over_limit(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, _ = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    os.write(stage.fd, b"x" * (wallpaper.MAX_ENCODED_BYTES + 1))
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="size"):
            wallpaper._validate_stage(stage, root.fd)
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_noreplace_race_never_overwrites_destination(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    source = wallpaper._create_stage(root.fd)
    destination_name = "wallpaper-" + "b" * 64 + ".png"
    destination_path = state_dir / "wallpaper" / destination_name
    os.write(source.fd, b"source")

    def _insert_destination() -> None:
        destination_path.write_bytes(b"attacker-destination")

    monkeypatch.setattr(wallpaper, "_RENAME_NOREPLACE_RACE_HOOK", _insert_destination)
    try:
        assert wallpaper._rename_noreplace(source.name, destination_name, root.fd) is False
        assert destination_path.read_bytes() == b"attacker-destination"
        assert (state_dir / "wallpaper" / source.name).read_bytes() == b"source"
    finally:
        source.close()
        root.close()


def test_wallpaper_descriptor_install_verifies_destination_identity(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    destination_name = "wallpaper-" + "c" * 64 + ".png"
    destination_path = state_dir / "wallpaper" / destination_name
    os.write(stage.fd, b"source")

    def _lying_adapter(src, dst, dir_fd):
        destination_path.write_bytes(b"attacker-destination")
        return True

    monkeypatch.setattr(wallpaper, "_RENAME_NOREPLACE_ADAPTER", _lying_adapter)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
            wallpaper._install_stage_noreplace(stage, destination_name, root.fd)
        assert destination_path.read_bytes() == b"attacker-destination"
        assert (state_dir / "wallpaper" / stage.name).read_bytes() == b"source"
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_install_source_path_swap_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    os.write(stage.fd, b"retained-source")
    stage_path = state_dir / "wallpaper" / stage.name
    retained_path = state_dir / "wallpaper" / "retained-source"
    destination_name = "wallpaper-" + "d" * 64 + ".png"
    destination_path = state_dir / "wallpaper" / destination_name

    def _swap_source() -> None:
        stage_path.rename(retained_path)
        stage_path.write_bytes(b"attacker-source")

    monkeypatch.setattr(wallpaper, "_RENAME_NOREPLACE_RACE_HOOK", _swap_source)
    try:
        with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
            wallpaper._install_stage_noreplace(stage, destination_name, root.fd)
        assert retained_path.read_bytes() == b"retained-source"
        assert destination_path.read_bytes() == b"attacker-source"
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_unlink_without_atomic_adapter_leaves_owned_temp(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    os.write(stage.fd, b"pytest-owned")
    identity = wallpaper._file_identity(os.fstat(stage.fd))
    monkeypatch.setattr(wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", None)
    try:
        assert wallpaper._unlink_if_identity(stage.name, identity, root.fd) is False
        assert (state_dir / "wallpaper" / stage.name).read_bytes() == b"pytest-owned"
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_unlink_identity_adapter_can_remove_owned_temp_node(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    os.write(stage.fd, b"pytest-owned")
    identity = wallpaper._file_identity(os.fstat(stage.fd))

    class _InjectedAtomicAdapter:
        def __call__(self, name, expected_identity, dir_fd):
            # This injected test double models a platform primitive whose final
            # operation atomically compares identity and unlinks this temp node.
            assert name == stage.name
            assert expected_identity == identity
            os.unlink(name, dir_fd=dir_fd)
            return True

    atomic_adapter = _InjectedAtomicAdapter()

    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", atomic_adapter
    )
    try:
        assert wallpaper._unlink_if_identity(stage.name, identity, root.fd) is True
        assert not (state_dir / "wallpaper" / stage.name).exists()
    finally:
        stage.close()
        root.close()


def test_wallpaper_descriptor_unlink_path_swap_leaves_replacement_untouched(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    root, state_dir = _open_wallpaper_descriptor_root(monkeypatch, tmp_path)
    stage = wallpaper._create_stage(root.fd)
    os.write(stage.fd, b"original")
    identity = wallpaper._file_identity(os.fstat(stage.fd))
    stage_path = state_dir / "wallpaper" / stage.name
    moved_path = state_dir / "wallpaper" / "moved-original"

    def _racing_atomic_adapter(name, expected_identity, dir_fd):
        # Model the race immediately before an atomic compare-and-unlink
        # primitive's final conditional boundary.
        stage_path.rename(moved_path)
        stage_path.write_bytes(b"attacker-replacement")
        replacement_identity = wallpaper._file_identity(
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        )
        assert replacement_identity != expected_identity
        return False

    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _racing_atomic_adapter
    )
    try:
        assert wallpaper._unlink_if_identity(stage.name, identity, root.fd) is False
        assert moved_path.read_bytes() == b"original"
        assert stage_path.read_bytes() == b"attacker-replacement"
    finally:
        stage.close()
        root.close()


# Authoritative reader/cache tests use only wallpaper files and settings rooted in
# pytest-owned temporary directories.
def _configure_active_wallpaper(
    monkeypatch, tmp_path: Path, image: bytes
) -> tuple[Path, str]:
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "reader-state"
    state_dir.mkdir()
    settings_file = tmp_path / "reader-settings.json"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    validated = wallpaper.validate_wallpaper(image)
    name = f"wallpaper-{validated.digest}.{validated.extension}"
    wallpaper_dir = state_dir / "wallpaper"
    wallpaper_dir.mkdir()
    (wallpaper_dir / name).write_bytes(image)
    with config._SETTINGS_WRITE_LOCK:
        config._save_wallpaper_settings_locked(
            {
                "wallpaper_file": name,
                "wallpaper_opacity": 0.35,
                "wallpaper_scope": "app",
            }
        )
    return wallpaper_dir, name


def test_wallpaper_info_and_snapshot_return_frozen_authoritative_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    image = _png_image(width=3, height=2)
    _configure_active_wallpaper(monkeypatch, tmp_path, image)
    digest = hashlib.sha256(image).hexdigest()

    info = wallpaper.get_wallpaper_info()
    snapshot = wallpaper.open_wallpaper_snapshot()

    assert info == wallpaper.WallpaperInfo(
        has_wallpaper=True,
        opacity=0.35,
        scope="app",
        mime_type="image/png",
        image_version=digest,
    )
    with pytest.raises(FrozenInstanceError):
        info.scope = "chat"
    assert snapshot is not None
    assert snapshot.size == len(image)
    assert snapshot.mime_type == "image/png"
    assert snapshot.etag == f'"{digest}"'
    assert snapshot.fd == snapshot.file.fileno()
    fd = snapshot.fd
    with snapshot as retained:
        assert retained.file.read() == image
    snapshot.close()
    assert snapshot.fd == -1
    with pytest.raises(OSError):
        os.fstat(fd)


def test_wallpaper_snapshot_retains_open_descriptor_after_active_path_replacement(
    monkeypatch, tmp_path: Path
) -> None:
    import api.wallpaper as wallpaper

    image = _png_image(width=2)
    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, image)
    snapshot = wallpaper.open_wallpaper_snapshot()
    assert snapshot is not None

    active = wallpaper_dir / name
    retained = wallpaper_dir / "retained-original"
    active.rename(retained)
    active.write_bytes(_png_image(width=4))
    try:
        assert snapshot.file.read() == image
    finally:
        snapshot.close()


def test_wallpaper_validation_cache_uses_exact_immutable_descriptor_identity(
    monkeypatch, tmp_path: Path
) -> None:
    import api.wallpaper as wallpaper

    image = _png_image()
    _configure_active_wallpaper(monkeypatch, tmp_path, image)
    wallpaper._VALIDATION_CACHE = None
    real_validate = wallpaper.validate_wallpaper
    validations = []

    def _counted_validate(data, *args, **kwargs):
        validations.append(bytes(data))
        return real_validate(data, *args, **kwargs)

    monkeypatch.setattr(wallpaper, "validate_wallpaper", _counted_validate)

    assert wallpaper.get_wallpaper_info().has_wallpaper is True
    assert wallpaper.get_wallpaper_info().has_wallpaper is True
    assert validations == [image]
    key, cached = wallpaper._VALIDATION_CACHE
    assert key == wallpaper._validation_cache_key(
        type(
            "Metadata",
            (),
            {
                "st_dev": key[0],
                "st_ino": key[1],
                "st_size": key[2],
                "st_mtime_ns": key[3],
            },
        )()
    )
    assert isinstance(key, tuple)
    with pytest.raises(TypeError):
        key[0] = key[0]
    with pytest.raises(FrozenInstanceError):
        cached.digest = "0" * 64


@pytest.mark.parametrize("field_index", range(4))
def test_wallpaper_validation_cache_revalidates_when_any_identity_field_changes(
    monkeypatch, tmp_path: Path, field_index: int
) -> None:
    import api.wallpaper as wallpaper

    image = _png_image()
    _configure_active_wallpaper(monkeypatch, tmp_path, image)
    wallpaper._VALIDATION_CACHE = None
    assert wallpaper.get_wallpaper_info().has_wallpaper is True
    key, cached = wallpaper._VALIDATION_CACHE
    changed_key = tuple(
        value + 1 if index == field_index else value
        for index, value in enumerate(key)
    )
    wallpaper._VALIDATION_CACHE = (changed_key, cached)
    real_validate = wallpaper.validate_wallpaper
    validations = 0

    def _counted_validate(data, *args, **kwargs):
        nonlocal validations
        validations += 1
        return real_validate(data, *args, **kwargs)

    monkeypatch.setattr(wallpaper, "validate_wallpaper", _counted_validate)

    assert wallpaper.get_wallpaper_info().has_wallpaper is True
    assert validations == 1


def test_wallpaper_validation_cache_revalidates_when_mtime_identity_changes(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    import api.wallpaper as wallpaper

    image = _png_image()
    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, image)
    wallpaper._VALIDATION_CACHE = None
    real_validate = wallpaper.validate_wallpaper
    validations = 0

    def _counted_validate(data, *args, **kwargs):
        nonlocal validations
        validations += 1
        return real_validate(data, *args, **kwargs)

    monkeypatch.setattr(wallpaper, "validate_wallpaper", _counted_validate)
    assert wallpaper.get_wallpaper_info().has_wallpaper is True
    metadata = (wallpaper_dir / name).stat()
    os.utime(
        wallpaper_dir / name,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
    )
    assert wallpaper.get_wallpaper_info().has_wallpaper is True
    assert validations == 2


@pytest.mark.parametrize("entry_kind", ["missing", "corrupt", "symlink", "directory"])
def test_wallpaper_reader_repairs_invalid_active_entry_to_absence(
    monkeypatch, tmp_path: Path, entry_kind: str
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    image = _png_image()
    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, image)
    active = wallpaper_dir / name
    retained = wallpaper_dir / "retained-valid"
    active.rename(retained)
    if entry_kind == "corrupt":
        active.write_bytes(b"not-an-image")
    elif entry_kind == "symlink":
        active.symlink_to(retained)
    elif entry_kind == "directory":
        active.mkdir()

    info = wallpaper.get_wallpaper_info()

    assert info == wallpaper.WallpaperInfo(
        has_wallpaper=False,
        opacity=0.8,
        scope="chat",
        mime_type=None,
        image_version=None,
    )
    assert wallpaper.open_wallpaper_snapshot() is None
    assert config.load_settings()["wallpaper_file"] == ""
    assert retained.read_bytes() == image


def test_wallpaper_reader_repairs_valid_tampered_bytes_under_digest_name(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    original = _png_image(width=1)
    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, original)
    tampered = _png_image(width=2)
    (wallpaper_dir / name).write_bytes(tampered)
    wallpaper._VALIDATION_CACHE = None

    assert wallpaper.open_wallpaper_snapshot() is None
    assert wallpaper.get_wallpaper_info().has_wallpaper is False
    assert config.load_settings()["wallpaper_file"] == ""
    assert (wallpaper_dir / name).read_bytes() == tampered


def test_wallpaper_reader_repair_returns_absence_when_persistence_not_committed(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, _png_image())
    (wallpaper_dir / name).write_bytes(b"corrupt")

    def _failed_repair(update):
        raise config.SettingsPersistenceError(
            config.SettingsPersistenceError.NOT_COMMITTED
        )

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _failed_repair)
    wallpaper._VALIDATION_CACHE = None

    assert wallpaper.get_wallpaper_info().has_wallpaper is False
    assert wallpaper.open_wallpaper_snapshot() is None
    assert config.load_settings()["wallpaper_file"] == name


def test_wallpaper_reader_repair_fails_closed_after_indeterminate_commit(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, _png_image())
    (wallpaper_dir / name).write_bytes(b"corrupt")
    real_save = config._save_wallpaper_settings_locked
    rereads = []

    def _indeterminate_repair(update):
        real_save(update)
        raise config.SettingsPersistenceError(
            config.SettingsPersistenceError.COMMITTED_OR_INDETERMINATE
        )

    real_load = config.load_settings

    def _observed_load():
        rereads.append(None)
        return real_load()

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _indeterminate_repair)
    monkeypatch.setattr(config, "load_settings", _observed_load)
    wallpaper._VALIDATION_CACHE = None

    assert wallpaper.get_wallpaper_info().has_wallpaper is False
    assert len(rereads) >= 2
    assert real_load()["wallpaper_file"] == ""


def test_wallpaper_snapshot_race_repairs_active_path_changed_during_validation(
    monkeypatch, tmp_path: Path
) -> None:
    import api.config as config
    import api.wallpaper as wallpaper

    image = _png_image(width=1)
    wallpaper_dir, name = _configure_active_wallpaper(monkeypatch, tmp_path, image)
    active = wallpaper_dir / name
    retained = wallpaper_dir / "retained-raced"
    real_validate = wallpaper.validate_wallpaper
    raced = False

    def _race_path(data, *args, **kwargs):
        nonlocal raced
        result = real_validate(data, *args, **kwargs)
        if not raced:
            raced = True
            active.rename(retained)
            active.write_bytes(image)
        return result

    wallpaper._VALIDATION_CACHE = None
    monkeypatch.setattr(wallpaper, "validate_wallpaper", _race_path)

    assert wallpaper.open_wallpaper_snapshot() is None
    assert config.load_settings()["wallpaper_file"] == ""
    assert retained.read_bytes() == image
    assert active.read_bytes() == image


# Mutation transaction tests use only pytest-owned settings and wallpaper roots.
def _configure_wallpaper_mutation_storage(monkeypatch, tmp_path: Path):
    import api.config as config
    import api.wallpaper as wallpaper

    state_dir = tmp_path / "mutation-state"
    state_dir.mkdir()
    settings_file = tmp_path / "mutation-settings.json"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    wallpaper._VALIDATION_CACHE = None
    return config, wallpaper, state_dir, settings_file


def _pytest_atomic_unlink_adapter():
    import os

    def _remove(name, expected_identity, dir_fd):
        metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            return False
        os.unlink(name, dir_fd=dir_fd)
        return True

    return _remove


def test_wallpaper_replace_stages_before_lock_and_closes_descriptors(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=2)
    lock_owned_during_stage = []
    staged_fds = []
    real_create = wallpaper._create_stage

    def _observed_create(dir_fd):
        lock_owned_during_stage.append(config._settings_lock_is_owned())
        stage = real_create(dir_fd)
        staged_fds.append(stage.fd)
        return stage

    monkeypatch.setattr(wallpaper, "_create_stage", _observed_create)

    info = wallpaper.replace_wallpaper(image, opacity=0.4, scope="app")

    assert lock_owned_during_stage == [False]
    assert info.image_version == hashlib.sha256(image).hexdigest()
    assert config.load_settings()["wallpaper_opacity"] == 0.4
    assert not any(
        path.name.startswith(".wallpaper-stage-")
        for path in (state_dir / "wallpaper").iterdir()
    )
    for fd in staged_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_wallpaper_replace_root_swap_between_stage_and_lock_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image()
    original_root = state_dir / "wallpaper-original"
    replacement_root = state_dir / "wallpaper"
    real_write = wallpaper._write_stage

    def _swap_after_stage(stage, root, data):
        result = real_write(stage, root, data)
        replacement_root.rename(original_root)
        replacement_root.mkdir()
        (replacement_root / "attacker-marker").write_bytes(b"untouched")
        return result

    monkeypatch.setattr(wallpaper, "_write_stage", _swap_after_stage)

    with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
        wallpaper.replace_wallpaper(image)

    assert not settings_file.exists()
    assert (replacement_root / "attacker-marker").read_bytes() == b"untouched"
    assert any(path.name.startswith(".wallpaper-stage-") for path in original_root.iterdir())


def test_wallpaper_replace_reuses_active_same_content_without_install_or_unlink(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(monkeypatch, tmp_path)
    image = _png_image(width=3)
    first = wallpaper.replace_wallpaper(image, opacity=0.2, scope="chat")
    install_calls = []
    unlink_calls = []
    monkeypatch.setattr(
        wallpaper,
        "_install_stage_noreplace",
        lambda *args: install_calls.append(args) or True,
    )
    monkeypatch.setattr(
        wallpaper,
        "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER",
        lambda *args: unlink_calls.append(args) or False,
    )

    second = wallpaper.replace_wallpaper(image, opacity=0.7, scope="app")

    assert second.image_version == first.image_version
    assert install_calls == []
    assert unlink_calls == []
    assert config.load_settings()["wallpaper_opacity"] == 0.7


def test_wallpaper_replace_reuses_valid_same_content_orphan(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=4)
    validated = wallpaper.validate_wallpaper(image)
    name = f"wallpaper-{validated.digest}.{validated.extension}"
    wallpaper_dir = state_dir / "wallpaper"
    wallpaper_dir.mkdir()
    orphan = wallpaper_dir / name
    orphan.write_bytes(image)
    identity = (orphan.stat().st_dev, orphan.stat().st_ino)
    install_calls = []
    monkeypatch.setattr(
        wallpaper,
        "_install_stage_noreplace",
        lambda *args: install_calls.append(args) or True,
    )

    info = wallpaper.replace_wallpaper(image)

    assert info.image_version == validated.digest
    assert install_calls == []
    assert (orphan.stat().st_dev, orphan.stat().st_ino) == identity
    assert config.load_settings()["wallpaper_file"] == name


@pytest.mark.parametrize("entry_kind", ["corrupt", "symlink", "directory"])
def test_wallpaper_replace_collision_never_overwrites_or_removes_destination(
    monkeypatch, tmp_path: Path, entry_kind: str
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=5)
    validated = wallpaper.validate_wallpaper(image)
    destination = state_dir / "wallpaper" / f"wallpaper-{validated.digest}.png"
    destination.parent.mkdir()
    outside = tmp_path / "outside-collision"
    outside.write_bytes(b"outside")
    if entry_kind == "corrupt":
        destination.write_bytes(b"corrupt")
    elif entry_kind == "symlink":
        destination.symlink_to(outside)
    else:
        destination.mkdir()

    with pytest.raises(wallpaper.WallpaperCollisionError):
        wallpaper.replace_wallpaper(image)

    assert not settings_file.exists()
    assert outside.read_bytes() == b"outside"
    if entry_kind == "corrupt":
        assert destination.read_bytes() == b"corrupt"
    elif entry_kind == "symlink":
        assert destination.is_symlink()
    else:
        assert destination.is_dir()


def test_wallpaper_replace_collision_at_noreplace_boundary_never_clobbers(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=6)
    validated = wallpaper.validate_wallpaper(image)
    destination = state_dir / "wallpaper" / f"wallpaper-{validated.digest}.png"

    def _insert_destination():
        destination.write_bytes(b"boundary-entry")

    monkeypatch.setattr(wallpaper, "_RENAME_NOREPLACE_RACE_HOOK", _insert_destination)

    with pytest.raises(wallpaper.WallpaperCollisionError):
        wallpaper.replace_wallpaper(image)

    assert destination.read_bytes() == b"boundary-entry"
    assert not settings_file.exists()


def test_wallpaper_replace_reused_orphan_swap_before_settings_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=24)
    validated = wallpaper.validate_wallpaper(image)
    name = f"wallpaper-{validated.digest}.png"
    wallpaper_dir = state_dir / "wallpaper"
    wallpaper_dir.mkdir()
    destination = wallpaper_dir / name
    retained_orphan = wallpaper_dir / "retained-orphan"
    destination.write_bytes(image)
    unlink_calls = []
    real_decide = wallpaper._decide_destination_locked

    def _decide_then_swap(*args):
        decision = real_decide(*args)
        destination.rename(retained_orphan)
        destination.write_bytes(b"attacker-replacement")
        return decision

    monkeypatch.setattr(wallpaper, "_decide_destination_locked", _decide_then_swap)
    monkeypatch.setattr(
        wallpaper,
        "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER",
        lambda *args: unlink_calls.append(args) or False,
    )

    with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
        wallpaper.replace_wallpaper(image)

    assert not settings_file.exists()
    assert retained_orphan.read_bytes() == image
    assert destination.read_bytes() == b"attacker-replacement"
    assert unlink_calls == []


def test_wallpaper_replace_fresh_install_swap_before_settings_rolls_back_only_installed_identity(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=25)
    validated = wallpaper.validate_wallpaper(image)
    name = f"wallpaper-{validated.digest}.png"
    wallpaper_dir = state_dir / "wallpaper"
    destination = wallpaper_dir / name
    retained_install = wallpaper_dir / "retained-install"
    removal_calls = []
    real_identity = wallpaper._installed_identity

    def _record_then_swap(*args):
        identity = real_identity(*args)
        destination.rename(retained_install)
        destination.write_bytes(b"attacker-replacement")
        return identity

    def _conditional_remove(entry, expected_identity, dir_fd):
        removal_calls.append((entry, expected_identity))
        current = wallpaper._file_identity(
            wallpaper.os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        )
        assert current != expected_identity
        return False

    monkeypatch.setattr(wallpaper, "_installed_identity", _record_then_swap)
    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _conditional_remove
    )

    with pytest.raises(wallpaper.WallpaperUnavailableError, match="identity"):
        wallpaper.replace_wallpaper(image)

    installed_identity = wallpaper._file_identity(retained_install.stat())
    assert not settings_file.exists()
    assert retained_install.read_bytes() == image
    assert destination.read_bytes() == b"attacker-replacement"
    assert removal_calls == [(name, installed_identity)]


def test_wallpaper_replace_stage_write_failure_closes_fds_and_keeps_settings(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    stage_fds = []
    root_fds = []
    real_open_root = wallpaper._WallpaperDir.open_for_staging
    real_create = wallpaper._create_stage

    def _record_root():
        root = real_open_root()
        root_fds.extend((root.fd, root.state_fd))
        return root

    def _record_stage(dir_fd):
        stage = real_create(dir_fd)
        stage_fds.append(stage.fd)
        return stage

    monkeypatch.setattr(wallpaper._WallpaperDir, "open_for_staging", _record_root)
    monkeypatch.setattr(wallpaper, "_create_stage", _record_stage)
    monkeypatch.setattr(
        wallpaper,
        "_write_all",
        lambda *args: (_ for _ in ()).throw(
            wallpaper.WallpaperUnavailableError("stage write failed")
        ),
    )

    with pytest.raises(wallpaper.WallpaperUnavailableError, match="stage write failed"):
        wallpaper.replace_wallpaper(_png_image())

    assert not settings_file.exists()
    assert any(path.name.startswith(".wallpaper-stage-") for path in (state_dir / "wallpaper").iterdir())
    for fd in stage_fds + root_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_wallpaper_replace_durability_order(monkeypatch, tmp_path: Path) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(monkeypatch, tmp_path)
    image = _png_image(width=7)
    events = []
    real_write = wallpaper._write_stage
    real_reverify = wallpaper._WallpaperDir.reverify_locked
    real_collision = wallpaper._decide_destination_locked
    real_install = wallpaper._install_stage_noreplace
    real_record = wallpaper._installed_identity
    real_sync = wallpaper._fsync_wallpaper_directory
    real_save = config._save_wallpaper_settings_locked

    def _write(*args):
        events.extend(("stage_write", "stage_flush", "stage_fsync"))
        return real_write(*args)

    def _reverify(root):
        events.append("root_recheck")
        return real_reverify(root)

    def _collision(*args):
        events.append("authoritative_reread")
        events.append("collision_decision")
        return real_collision(*args)

    def _install(*args):
        events.append("install")
        return real_install(*args)

    def _record(*args):
        events.append("record_identity")
        return real_record(*args)

    sync_count = 0

    def _sync(*args):
        nonlocal sync_count
        sync_count += 1
        events.append("directory_fsync" if sync_count == 1 else "final_directory_fsync")
        return real_sync(*args)

    def _save(*args):
        events.append("settings_commit")
        return real_save(*args)

    monkeypatch.setattr(wallpaper, "_write_stage", _write)
    monkeypatch.setattr(wallpaper._WallpaperDir, "reverify_locked", _reverify)
    monkeypatch.setattr(wallpaper, "_decide_destination_locked", _collision)
    monkeypatch.setattr(wallpaper, "_install_stage_noreplace", _install)
    monkeypatch.setattr(wallpaper, "_installed_identity", _record)
    monkeypatch.setattr(wallpaper, "_fsync_wallpaper_directory", _sync)
    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _save)

    wallpaper.replace_wallpaper(image)

    assert events == [
        "stage_write",
        "stage_flush",
        "stage_fsync",
        "root_recheck",
        "authoritative_reread",
        "collision_decision",
        "install",
        "record_identity",
        "directory_fsync",
        "settings_commit",
        "final_directory_fsync",
    ]


def test_wallpaper_replace_not_committed_rolls_back_only_installed_identity(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    old_image = _png_image(width=8)
    old_info = wallpaper.replace_wallpaper(old_image)
    old_name = config.load_settings()["wallpaper_file"]
    new_image = _png_image(width=9)
    new_validated = wallpaper.validate_wallpaper(new_image)
    new_name = f"wallpaper-{new_validated.digest}.png"
    removed = []
    atomic_remove = _pytest_atomic_unlink_adapter()

    def _observed_remove(name, identity, dir_fd):
        removed.append((name, identity))
        return atomic_remove(name, identity, dir_fd)

    def _fail_settings(update):
        raise config.SettingsPersistenceError(config.SettingsPersistenceError.NOT_COMMITTED)

    monkeypatch.setattr(wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _observed_remove)
    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _fail_settings)

    with pytest.raises(wallpaper.WallpaperPersistenceError) as raised:
        wallpaper.replace_wallpaper(new_image)

    assert raised.value.commit_state == wallpaper.WallpaperPersistenceError.NOT_COMMITTED
    assert config.load_settings()["wallpaper_file"] == old_name
    assert wallpaper.get_wallpaper_info().image_version == old_info.image_version
    assert (state_dir / "wallpaper" / old_name).read_bytes() == old_image
    assert not (state_dir / "wallpaper" / new_name).exists()
    assert len(removed) == 1
    assert removed[0][0] == new_name


def test_wallpaper_replace_indeterminate_commit_preserves_new_content_and_rereads(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    wallpaper.replace_wallpaper(_png_image(width=10))
    image = _png_image(width=11)
    validated = wallpaper.validate_wallpaper(image)
    name = f"wallpaper-{validated.digest}.png"
    real_save = config._save_wallpaper_settings_locked
    real_load = config.load_settings
    rereads = []

    def _commit_then_fail(update):
        real_save(update)
        raise config.SettingsPersistenceError(
            config.SettingsPersistenceError.COMMITTED_OR_INDETERMINATE
        )

    def _observed_load():
        rereads.append(None)
        return real_load()

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _commit_then_fail)
    monkeypatch.setattr(config, "load_settings", _observed_load)

    with pytest.raises(wallpaper.WallpaperPersistenceError) as raised:
        wallpaper.replace_wallpaper(image)

    assert raised.value.commit_state == raised.value.COMMITTED_OR_INDETERMINATE
    assert rereads
    assert real_load()["wallpaper_file"] == name
    assert (state_dir / "wallpaper" / name).read_bytes() == image


def test_wallpaper_replace_install_verification_failure_rolls_back_owned_identity(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=23)
    name = f"wallpaper-{hashlib.sha256(image).hexdigest()}.png"
    atomic_remove = _pytest_atomic_unlink_adapter()
    real_install = wallpaper._install_stage_noreplace

    def _install_then_fail(*args):
        assert real_install(*args) is True
        raise wallpaper.WallpaperUnavailableError("install verification failed")

    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", atomic_remove
    )
    monkeypatch.setattr(wallpaper, "_install_stage_noreplace", _install_then_fail)

    with pytest.raises(
        wallpaper.WallpaperUnavailableError, match="install verification failed"
    ):
        wallpaper.replace_wallpaper(image)

    assert not settings_file.exists()
    assert not (state_dir / "wallpaper" / name).exists()


def test_wallpaper_replace_failure_boundary_after_install_rolls_back_owned_identity(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=21)
    name = f"wallpaper-{hashlib.sha256(image).hexdigest()}.png"
    removed = []
    atomic_remove = _pytest_atomic_unlink_adapter()

    def _observed_remove(entry, identity, dir_fd):
        removed.append((entry, identity))
        return atomic_remove(entry, identity, dir_fd)

    monkeypatch.setattr(wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _observed_remove)
    monkeypatch.setattr(
        wallpaper,
        "_installed_identity",
        lambda *args: (_ for _ in ()).throw(
            wallpaper.WallpaperUnavailableError("record failed")
        ),
    )

    with pytest.raises(wallpaper.WallpaperUnavailableError, match="record failed"):
        wallpaper.replace_wallpaper(image)

    assert not settings_file.exists()
    assert not (state_dir / "wallpaper" / name).exists()
    assert len(removed) == 1
    assert removed[0][0] == name


def test_wallpaper_replace_directory_sync_failure_rolls_back_before_settings(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, settings_file = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=12)
    name = f"wallpaper-{hashlib.sha256(image).hexdigest()}.png"
    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _pytest_atomic_unlink_adapter()
    )
    monkeypatch.setattr(
        wallpaper,
        "_fsync_wallpaper_directory",
        lambda root: (_ for _ in ()).throw(OSError("sync failed")),
    )

    with pytest.raises(wallpaper.WallpaperPersistenceError) as raised:
        wallpaper.replace_wallpaper(image)

    assert raised.value.commit_state == raised.value.NOT_COMMITTED
    assert not settings_file.exists()
    assert not (state_dir / "wallpaper" / name).exists()


def test_wallpaper_replace_rollback_identity_swap_leaves_replacement_untouched(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=22)
    name = f"wallpaper-{hashlib.sha256(image).hexdigest()}.png"
    destination = state_dir / "wallpaper" / name
    moved = state_dir / "wallpaper" / "retained-installed"

    def _fail_settings(update):
        raise config.SettingsPersistenceError(config.SettingsPersistenceError.NOT_COMMITTED)

    def _swap_at_remove(entry, expected_identity, dir_fd):
        destination.rename(moved)
        destination.write_bytes(b"attacker-replacement")
        return False

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _fail_settings)
    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _swap_at_remove
    )

    with pytest.raises(wallpaper.WallpaperPersistenceError):
        wallpaper.replace_wallpaper(image)

    assert moved.read_bytes() == image
    assert destination.read_bytes() == b"attacker-replacement"


def test_wallpaper_replace_cleanup_failure_after_commit_keeps_new_state(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    old_image = _png_image(width=13)
    wallpaper.replace_wallpaper(old_image)
    old_name = config.load_settings()["wallpaper_file"]
    image = _png_image(width=14)
    validated = wallpaper.validate_wallpaper(image)
    new_name = f"wallpaper-{validated.digest}.png"
    monkeypatch.setattr(
        wallpaper,
        "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER",
        lambda *args: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    real_sync = wallpaper._fsync_wallpaper_directory
    sync_count = 0

    def _fail_final_sync(root):
        nonlocal sync_count
        sync_count += 1
        if sync_count > 1:
            raise OSError("final sync failed")
        return real_sync(root)

    monkeypatch.setattr(wallpaper, "_fsync_wallpaper_directory", _fail_final_sync)

    info = wallpaper.replace_wallpaper(image)

    assert info.image_version == validated.digest
    assert config.load_settings()["wallpaper_file"] == new_name
    assert (state_dir / "wallpaper" / new_name).read_bytes() == image
    assert (state_dir / "wallpaper" / old_name).read_bytes() == old_image


def test_wallpaper_metadata_mutation_validates_active_and_keeps_version(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(monkeypatch, tmp_path)
    image = _png_image(width=15)
    original = wallpaper.replace_wallpaper(image, opacity=0.3, scope="chat")

    updated = wallpaper.update_wallpaper_metadata(opacity=0.65, scope="app")

    assert updated.image_version == original.image_version
    assert updated.mime_type == original.mime_type
    assert updated.opacity == 0.65
    assert updated.scope == "app"
    settings = config.load_settings()
    assert settings["wallpaper_opacity"] == 0.65
    assert settings["wallpaper_scope"] == "app"


def test_wallpaper_metadata_mutation_requires_valid_active(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )

    with pytest.raises(wallpaper.WallpaperNotFoundError):
        wallpaper.update_wallpaper_metadata(opacity=0.5)

    image = _png_image(width=16)
    wallpaper.replace_wallpaper(image)
    name = config.load_settings()["wallpaper_file"]
    (state_dir / "wallpaper" / name).write_bytes(b"corrupt")
    wallpaper._VALIDATION_CACHE = None

    with pytest.raises(wallpaper.WallpaperUnavailableError):
        wallpaper.update_wallpaper_metadata(scope="app")


def test_wallpaper_clear_persists_all_defaults_before_conditional_removal(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=17)
    wallpaper.replace_wallpaper(image, opacity=0.25, scope="app")
    old_name = config.load_settings()["wallpaper_file"]
    observed_settings = []
    atomic_remove = _pytest_atomic_unlink_adapter()

    def _remove_after_defaults(name, identity, dir_fd):
        observed_settings.append(config.load_settings())
        return atomic_remove(name, identity, dir_fd)

    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _remove_after_defaults
    )

    result = wallpaper.clear_wallpaper()

    assert result == wallpaper.WallpaperInfo(False, 0.8, "chat", None, None)
    assert len(observed_settings) == 1
    assert {
        key: observed_settings[0][key] for key in config.WALLPAPER_SETTINGS_KEYS
    } == {
        "wallpaper_file": "",
        "wallpaper_opacity": 0.8,
        "wallpaper_scope": "chat",
    }
    settings = config.load_settings()
    assert {key: settings[key] for key in config.WALLPAPER_SETTINGS_KEYS} == {
        "wallpaper_file": "",
        "wallpaper_opacity": 0.8,
        "wallpaper_scope": "chat",
    }
    assert not (state_dir / "wallpaper" / old_name).exists()


@pytest.mark.parametrize("configured", [False, True])
def test_wallpaper_clear_persists_defaults_when_storage_is_unavailable(
    monkeypatch, tmp_path: Path, configured: bool
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    prior_name = ""
    if configured:
        image = _png_image(width=37)
        wallpaper.replace_wallpaper(image, opacity=0.25, scope="app")
        prior_name = config.load_settings()["wallpaper_file"]
    else:
        with config._SETTINGS_WRITE_LOCK:
            config._save_wallpaper_settings_locked(
                {"wallpaper_opacity": 0.25, "wallpaper_scope": "app"}
            )

    def _unavailable_root():
        raise wallpaper.WallpaperUnavailableError("storage unavailable")

    monkeypatch.setattr(wallpaper._WallpaperDir, "open_for_cleanup", _unavailable_root)

    assert wallpaper.clear_wallpaper() == wallpaper._absent_wallpaper_info()
    saved = config.load_settings()
    assert {key: saved[key] for key in config.WALLPAPER_SETTINGS_KEYS} == {
        "wallpaper_file": "",
        "wallpaper_opacity": 0.8,
        "wallpaper_scope": "chat",
    }
    if prior_name:
        assert (state_dir / "wallpaper" / prior_name).exists()


def test_wallpaper_clear_without_production_remove_capability_leaves_file(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=18)
    wallpaper.replace_wallpaper(image)
    old_name = config.load_settings()["wallpaper_file"]
    monkeypatch.setattr(wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", None)

    wallpaper.clear_wallpaper()

    assert config.load_settings()["wallpaper_file"] == ""
    assert (state_dir / "wallpaper" / old_name).read_bytes() == image


def test_wallpaper_clear_failure_before_commit_preserves_prior_state(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=19)
    wallpaper.replace_wallpaper(image, opacity=0.6, scope="app")
    old_settings = config.load_settings()
    old_name = old_settings["wallpaper_file"]

    def _fail_settings(update):
        raise config.SettingsPersistenceError(config.SettingsPersistenceError.NOT_COMMITTED)

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _fail_settings)

    with pytest.raises(wallpaper.WallpaperPersistenceError) as raised:
        wallpaper.clear_wallpaper()

    assert raised.value.commit_state == raised.value.NOT_COMMITTED
    assert config.load_settings()["wallpaper_file"] == old_name
    assert (state_dir / "wallpaper" / old_name).read_bytes() == image


def test_wallpaper_same_content_persistence_failure_never_unlinks_active(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=20)
    wallpaper.replace_wallpaper(image)
    name = config.load_settings()["wallpaper_file"]
    unlink_calls = []
    monkeypatch.setattr(
        wallpaper,
        "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER",
        lambda *args: unlink_calls.append(args) or True,
    )

    def _fail_settings(update):
        raise config.SettingsPersistenceError(config.SettingsPersistenceError.NOT_COMMITTED)

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _fail_settings)

    with pytest.raises(wallpaper.WallpaperPersistenceError):
        wallpaper.replace_wallpaper(image, opacity=0.9)

    assert unlink_calls == []
    assert (state_dir / "wallpaper" / name).read_bytes() == image


def test_wallpaper_clear_when_empty_still_persists_all_defaults(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    with config._SETTINGS_WRITE_LOCK:
        config._save_wallpaper_settings_locked(
            {"wallpaper_opacity": 0.2, "wallpaper_scope": "app"}
        )

    assert wallpaper.clear_wallpaper() == wallpaper._absent_wallpaper_info()
    saved = config.load_settings()
    assert {key: saved[key] for key in config.WALLPAPER_SETTINGS_KEYS} == {
        "wallpaper_file": "",
        "wallpaper_opacity": 0.8,
        "wallpaper_scope": "chat",
    }


def test_wallpaper_clear_indeterminate_failure_preserves_referenced_file(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    image = _png_image(width=26)
    wallpaper.replace_wallpaper(image)
    name = config.load_settings()["wallpaper_file"]
    real_save = config._save_wallpaper_settings_locked
    removals = []

    def _commit_then_fail(update):
        real_save(update)
        raise config.SettingsPersistenceError(
            config.SettingsPersistenceError.COMMITTED_OR_INDETERMINATE
        )

    monkeypatch.setattr(config, "_save_wallpaper_settings_locked", _commit_then_fail)
    monkeypatch.setattr(
        wallpaper,
        "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER",
        lambda *args: removals.append(args) or True,
    )

    with pytest.raises(wallpaper.WallpaperPersistenceError) as caught:
        wallpaper.clear_wallpaper()

    assert caught.value.commit_state == caught.value.COMMITTED_OR_INDETERMINATE
    assert removals == []
    assert (state_dir / "wallpaper" / name).read_bytes() == image


class _BarrierObservedRLock:
    def __init__(self, lock, participants: set[str]):
        self._lock = lock
        self._participants = participants
        self._barrier = threading.Barrier(len(participants))
        self.attempts = []
        self._attempts_lock = threading.Lock()

    def acquire(self, *args, **kwargs):
        name = threading.current_thread().name
        if name in self._participants and not self._lock._is_owned():
            with self._attempts_lock:
                self.attempts.append(name)
            self._barrier.wait(timeout=5)
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        return self._lock.release()

    def _is_owned(self):
        return self._lock._is_owned()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


@pytest.mark.parametrize("mutation", ["replace", "clear"])
def test_acquired_wallpaper_snapshot_remains_complete_during_mutation(
    monkeypatch, tmp_path: Path, mutation: str
) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    first = _png_image(width=27)
    wallpaper.replace_wallpaper(first)
    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _pytest_atomic_unlink_adapter()
    )
    observed_lock = _BarrierObservedRLock(
        config._SETTINGS_WRITE_LOCK, {"wallpaper-mutation"}
    )
    monkeypatch.setattr(config, "_SETTINGS_WRITE_LOCK", observed_lock)
    acquired = threading.Event()
    mutation_finished = threading.Event()
    snapshot_bytes = []
    errors = []

    def _reader():
        snapshot = None
        try:
            snapshot = wallpaper.open_wallpaper_snapshot()
            assert snapshot is not None
            acquired.set()
            assert mutation_finished.wait(timeout=5)
            snapshot_bytes.append(snapshot.file.read())
        except BaseException as exc:
            errors.append(exc)
        finally:
            if snapshot is not None:
                snapshot.close()

    def _mutate():
        try:
            if mutation == "replace":
                wallpaper.replace_wallpaper(_png_image(width=28))
            else:
                wallpaper.clear_wallpaper()
        except BaseException as exc:
            errors.append(exc)
        finally:
            mutation_finished.set()

    reader = threading.Thread(target=_reader, name="snapshot-reader")
    worker = threading.Thread(target=_mutate, name="wallpaper-mutation")
    reader.start()
    assert acquired.wait(timeout=5), "snapshot reader did not acquire active bytes"
    worker.start()
    try:
        worker.join(timeout=5)
        assert not worker.is_alive(), "wallpaper mutation did not finish"
        reader.join(timeout=5)
        assert not reader.is_alive(), "snapshot reader did not finish"
    finally:
        mutation_finished.set()
        observed_lock._barrier.abort()
        reader.join(timeout=5)
        worker.join(timeout=5)

    assert not reader.is_alive(), "snapshot reader remained blocked after cleanup"
    assert not worker.is_alive(), "wallpaper mutation remained blocked after cleanup"
    assert errors == []
    assert observed_lock.attempts == ["wallpaper-mutation"]
    assert snapshot_bytes == [first]


@pytest.mark.parametrize("reader_first", [True, False])
def test_authoritative_reader_racing_replacement_never_observes_absence(
    monkeypatch, tmp_path: Path, reader_first: bool
) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    first = _png_image(width=29)
    second = _png_image(width=30)
    first_version = wallpaper.replace_wallpaper(first).image_version
    expected_second = wallpaper.validate_wallpaper(second).digest
    participants = {"authoritative-reader", "wallpaper-replacement"}
    observed_lock = _BarrierObservedRLock(config._SETTINGS_WRITE_LOCK, participants)
    monkeypatch.setattr(config, "_SETTINGS_WRITE_LOCK", observed_lock)
    results = []
    errors = []

    def _reader():
        try:
            snapshot = wallpaper.open_wallpaper_snapshot()
            assert snapshot is not None
            try:
                results.append((snapshot.etag.strip('"'), snapshot.file.read()))
            finally:
                snapshot.close()
        except BaseException as exc:
            errors.append(exc)

    def _replace():
        try:
            wallpaper.replace_wallpaper(second)
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=_reader, name="authoritative-reader")
    replacement = threading.Thread(target=_replace, name="wallpaper-replacement")
    threads = (reader, replacement) if reader_first else (replacement, reader)
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), f"{thread.name} did not finish"
    finally:
        observed_lock._barrier.abort()
        reader.join(timeout=5)
        replacement.join(timeout=5)

    assert not reader.is_alive(), "authoritative reader remained blocked after cleanup"
    assert not replacement.is_alive(), "replacement remained blocked after cleanup"
    assert errors == []
    assert set(observed_lock.attempts) == participants
    assert results in [[(first_version, first)], [(expected_second, second)]]
    assert wallpaper.get_wallpaper_info().image_version == expected_second


@pytest.mark.parametrize(
    ("mutation", "unrelated_update", "expected_wallpaper"),
    [
        (
            "upload",
            {"show_token_usage": True},
            ("replacement", 0.4, "app"),
        ),
        (
            "metadata_update",
            {"show_quota_chip": True},
            ("original", 0.6, "app"),
        ),
        (
            "clear",
            {"show_tps": True},
            ("", 0.8, "chat"),
        ),
    ],
)
def test_wallpaper_generic_settings_serializes_with_all_mutations(
    monkeypatch,
    tmp_path: Path,
    mutation: str,
    unrelated_update: dict,
    expected_wallpaper: tuple[str, float, str],
) -> None:
    config, wallpaper, _, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    original_image = _png_image(width=31)
    original = wallpaper.replace_wallpaper(original_image, opacity=0.2, scope="chat")
    original_name = config.load_settings()["wallpaper_file"]
    replacement_image = _png_image(width=32)
    replacement = wallpaper.validate_wallpaper(replacement_image)
    replacement_name = f"wallpaper-{replacement.digest}.png"
    expected_name, expected_opacity, expected_scope = expected_wallpaper
    expected_name = {
        "original": original_name,
        "replacement": replacement_name,
        "": "",
    }[expected_name]
    initial_state = (original_name, 0.2, "chat")
    final_state = (expected_name, expected_opacity, expected_scope)
    observed_payloads = []
    payload_lock = threading.Lock()
    real_write = config._atomic_write_settings_text

    def _observe_write(path, text):
        payload = json.loads(text)
        with payload_lock:
            observed_payloads.append(
                tuple(payload[key] for key in (
                    "wallpaper_file",
                    "wallpaper_opacity",
                    "wallpaper_scope",
                ))
            )
        return real_write(path, text)

    participants = {"generic-settings", f"wallpaper-{mutation}"}
    observed_lock = _BarrierObservedRLock(config._SETTINGS_WRITE_LOCK, participants)
    monkeypatch.setattr(config, "_SETTINGS_WRITE_LOCK", observed_lock)
    monkeypatch.setattr(config, "_atomic_write_settings_text", _observe_write)
    errors = []

    def _generic():
        try:
            config.save_settings(dict(unrelated_update))
        except BaseException as exc:
            errors.append(exc)

    def _wallpaper():
        try:
            if mutation == "upload":
                wallpaper.replace_wallpaper(
                    replacement_image, opacity=expected_opacity, scope=expected_scope
                )
            elif mutation == "metadata_update":
                wallpaper.update_wallpaper_metadata(
                    opacity=expected_opacity, scope=expected_scope
                )
            else:
                wallpaper.clear_wallpaper()
        except BaseException as exc:
            errors.append(exc)

    generic = threading.Thread(target=_generic, name="generic-settings")
    wallpaper_worker = threading.Thread(
        target=_wallpaper, name=f"wallpaper-{mutation}"
    )
    generic.start()
    wallpaper_worker.start()
    try:
        generic.join(timeout=5)
        wallpaper_worker.join(timeout=5)
        assert not generic.is_alive(), "generic settings worker did not finish"
        assert not wallpaper_worker.is_alive(), "wallpaper worker did not finish"
    finally:
        observed_lock._barrier.abort()
        generic.join(timeout=5)
        wallpaper_worker.join(timeout=5)

    assert not generic.is_alive(), "generic settings worker remained blocked after cleanup"
    assert not wallpaper_worker.is_alive(), "wallpaper worker remained blocked after cleanup"
    assert errors == []
    assert set(observed_lock.attempts) == participants
    saved = config.load_settings()
    unrelated_key, unrelated_value = next(iter(unrelated_update.items()))
    assert saved[unrelated_key] == unrelated_value
    assert tuple(
        saved[key]
        for key in ("wallpaper_file", "wallpaper_opacity", "wallpaper_scope")
    ) == final_state
    assert observed_payloads
    assert set(observed_payloads) <= {initial_state, final_state}
    if expected_name:
        info = wallpaper.get_wallpaper_info()
        assert info == wallpaper.WallpaperInfo(
            True,
            expected_opacity,
            expected_scope,
            "image/png",
            replacement.digest if mutation == "upload" else original.image_version,
        )
    else:
        assert wallpaper.get_wallpaper_info() == wallpaper._absent_wallpaper_info()


def test_wallpaper_cleanup_removes_only_valid_owned_orphans_and_syncs_each(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    active_image = _png_image(width=32)
    wallpaper.replace_wallpaper(active_image)
    active_name = config.load_settings()["wallpaper_file"]
    wallpaper_dir = state_dir / "wallpaper"
    orphan_image = _png_image(width=33)
    orphan = wallpaper.validate_wallpaper(orphan_image)
    orphan_name = f"wallpaper-{orphan.digest}.png"
    (wallpaper_dir / orphan_name).write_bytes(orphan_image)
    stage_name = ".wallpaper-stage-" + "a" * 32 + ".tmp"
    (wallpaper_dir / stage_name).write_bytes(_png_image(width=34))
    (wallpaper_dir / stage_name).chmod(0o600)
    corrupt_stage = wallpaper_dir / (".wallpaper-stage-" + "b" * 32 + ".tmp")
    corrupt_stage.write_bytes(b"incomplete")
    unknown = wallpaper_dir / "operator-note"
    unknown.write_bytes(b"untouched")
    removed = []
    syncs = []
    atomic_remove = _pytest_atomic_unlink_adapter()

    def _remove(name, identity, dir_fd):
        removed.append(name)
        return atomic_remove(name, identity, dir_fd)

    monkeypatch.setattr(wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _remove)
    monkeypatch.setattr(
        wallpaper,
        "_fsync_wallpaper_directory",
        lambda root: syncs.append(root.fd),
    )

    wallpaper.cleanup_wallpaper_orphans()

    assert set(removed) == {orphan_name, stage_name}
    assert len(syncs) == 2
    assert (wallpaper_dir / active_name).read_bytes() == active_image
    assert corrupt_stage.read_bytes() == b"incomplete"
    assert unknown.read_bytes() == b"untouched"


def test_wallpaper_cleanup_unavailable_adapter_or_enumeration_leaves_entries(
    monkeypatch, tmp_path: Path
) -> None:
    config, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    wallpaper_dir = state_dir / "wallpaper"
    wallpaper_dir.mkdir()
    image = _png_image(width=35)
    validated = wallpaper.validate_wallpaper(image)
    orphan = wallpaper_dir / f"wallpaper-{validated.digest}.png"
    orphan.write_bytes(image)
    monkeypatch.setattr(wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", None)

    wallpaper.cleanup_wallpaper_orphans()
    assert orphan.read_bytes() == image

    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _pytest_atomic_unlink_adapter()
    )
    supported = set(wallpaper.os.supports_fd)
    supported.discard(wallpaper.os.listdir)
    supported.discard(wallpaper.os.scandir)
    monkeypatch.setattr(wallpaper.os, "supports_fd", supported)
    wallpaper.cleanup_wallpaper_orphans()
    assert orphan.read_bytes() == image


def test_wallpaper_cleanup_does_not_require_upload_noreplace_capability(
    monkeypatch, tmp_path: Path
) -> None:
    _, wallpaper, state_dir, _ = _configure_wallpaper_mutation_storage(
        monkeypatch, tmp_path
    )
    wallpaper_dir = state_dir / "wallpaper"
    wallpaper_dir.mkdir()
    image = _png_image(width=36)
    validated = wallpaper.validate_wallpaper(image)
    orphan = wallpaper_dir / f"wallpaper-{validated.digest}.png"
    orphan.write_bytes(image)
    monkeypatch.setattr(wallpaper, "_RENAME_NOREPLACE_ADAPTER", None)
    monkeypatch.setattr(
        wallpaper, "_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER", _pytest_atomic_unlink_adapter()
    )

    wallpaper.cleanup_wallpaper_orphans()

    assert not orphan.exists()
