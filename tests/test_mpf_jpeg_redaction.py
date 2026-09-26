"""MPF fixture: Pillow MPO, two generated 8x8 red/blue frames.

Generate with Image.new('RGB', (8, 8), color) for red, blue;
first.save('multipicture.jpg', format='MPO', save_all=True,
           append_images=remaining). No private photographs are used.
"""
import base64
from pathlib import Path
import struct

import pytest

from api import helpers


RAW = (Path(__file__).parent / 'fixtures' / 'multipicture.jpg').read_bytes()
BASE = RAW.index(b'MPF\0') + 4
IFD = BASE + int.from_bytes(RAW[BASE + 4:BASE + 8], 'little')
TAGS = {int.from_bytes(RAW[p:p + 2], 'little'): p
        for p in range(IFD + 2, IFD + 2 + 12 * int.from_bytes(RAW[IFD:IFD + 2], 'little'), 12)}
ENTRIES = BASE + int.from_bytes(RAW[TAGS[0xB002] + 8:TAGS[0xB002] + 12], 'little')


def uri(raw):
    return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode('ascii')


def session(value):
    return {'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': value}}]}]}


@pytest.mark.parametrize('byte_order', ['little', 'big'])
def test_mpf_preserves_native_image_without_text_redaction(monkeypatch, byte_order):
    raw = bytearray(RAW)
    if byte_order == 'big':
        raw[BASE:BASE + 4] = b'MM\x00\x2a'
        struct.pack_into('>I', raw, BASE + 4, IFD - BASE)
        struct.pack_into('>H', raw, IFD, len(TAGS))
        for tag, pos in TAGS.items():
            kind, count, value = struct.unpack_from('<HII', RAW, pos + 2)
            struct.pack_into('>HHI', raw, pos, tag, kind, count)
            if tag != 0xB000:  # Version is four raw ASCII bytes, not an integer.
                struct.pack_into('>I', raw, pos + 8, value)
        for pos in range(ENTRIES, ENTRIES + 32, 16):
            struct.pack_into('>IIIHH', raw, pos, *struct.unpack_from('<IIIHH', RAW, pos))
    value = uri(raw)
    calls = []
    monkeypatch.setattr(helpers, '_redact_text', lambda text, **kw: calls.append(text) or text)
    assert helpers._is_native_raster_data_uri(value)
    assert helpers.redact_session_data(session(value)) == session(value)
    assert value not in calls


@pytest.mark.parametrize('attack', ['trailing_secret', 'gap', 'overlap', 'size',
                                   'count', 'table_offset', 'truncated', 'bad_frame',
                                   'first_offset', 'undeclared_image'])
def test_mpf_rejects_unaccounted_or_invalid_bytes(attack):
    raw = bytearray(RAW)
    if attack == 'trailing_secret':
        raw += b'API_KEY=private-trailing-secret'
    elif attack == 'undeclared_image':
        raw += RAW
    elif attack == 'truncated':
        del raw[-1:]
    elif attack == 'count':
        struct.pack_into('<I', raw, TAGS[0xB001] + 8, 999999)
    elif attack == 'table_offset':
        struct.pack_into('<I', raw, TAGS[0xB002] + 8, len(raw))
    elif attack == 'first_offset':
        struct.pack_into('<I', raw, ENTRIES + 8, 1)
    elif attack == 'bad_frame':
        raw[-2:] = b'xx'
    else:
        pos = ENTRIES + 16 + (4 if attack == 'size' else 8)
        value = int.from_bytes(raw[pos:pos + 4], 'little')
        struct.pack_into('<I', raw, pos, value + (-1 if attack == 'overlap' else 1))
    assert not helpers._is_native_raster_data_uri(uri(raw))


def test_mpf_appended_base64_secret_reaches_real_redactor():
    # Pad inside a declared JPEG COM segment, keeping all MP offsets valid,
    # so the appended decoded credential aligns to a base64 quantum.
    raw = bytearray(RAW)
    padding = b'\xff\xfe\x00\x04xx' if len(raw) % 3 == 0 else (
        b'\xff\xfe\x00\x02' if len(raw) % 3 == 2 else b'\xff\xfe\x00\x03x')
    raw[-2:-2] = padding
    last_size = ENTRIES + 16 + 4
    struct.pack_into('<I', raw, last_size, int.from_bytes(RAW[last_size:last_size + 4], 'little') + len(padding))
    assert len(raw) % 3 == 0
    secret = 'AKIA' + 'Z' * 16
    value = uri(raw + base64.b64decode(secret))
    assert value.endswith(secret)
    assert not helpers._is_native_raster_data_uri(value)
    result = helpers.redact_session_data(session(value))
    assert secret not in result['messages'][0]['content'][0]['image_url']['url']


def test_mpf_outside_authoritative_image_boundary_still_redacts(monkeypatch):
    value = uri(RAW)
    calls = []
    monkeypatch.setattr(helpers, '_redact_text', lambda text, **kw: calls.append(text) or text)
    helpers.redact_session_data({'tool_calls': [{'args': session(value)}]})
    assert value in calls
