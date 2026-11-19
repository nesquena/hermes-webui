"""Samsung SEF trailer: synthetic blocks after a generated 8x8 baseline JPEG.

Samsung cameras append a Samsung Extended Format trailer after the JPEG EOI
(data blocks, an ``SEFH`` directory, then ``<directory length> SEFT``). The
base image is the first frame of the existing Pillow-generated MPF fixture with
its APP2 MPF segment removed, so no private photographs are used.
"""
import base64
from pathlib import Path
import struct
import time

import pytest

from api import helpers


MPO = (Path(__file__).parent / 'fixtures' / 'multipicture.jpg').read_bytes()


def _plain_jpeg():
    """First MPO frame without its APP2 MPF segment: one complete baseline JPEG."""
    frame = MPO[:MPO.index(b'\xff\xd9') + 2]
    app2 = frame.index(b'\xff\xe2')
    length = int.from_bytes(frame[app2 + 2:app2 + 4], 'big')
    return frame[:app2] + frame[app2 + 2 + length:]


JPEG = _plain_jpeg()
BLOCKS = [(0x0A01, b'Image_UTC_Data', b'1700000000000'),
          (0x0AA1, b'MCC_Data', b'440'),
          (0x0C61, b'Camera_Capture_Mode_Info', b'1')]


def sef(blocks=BLOCKS):
    """Data blocks, then the SEFH directory, then ``<len> SEFT``."""
    data, sizes = b'', []
    for kind, name, value in blocks:
        block = struct.pack('<HHI', 0, kind, len(name)) + name + value
        data += block
        sizes.append(len(block))
    directory = b'SEFH' + struct.pack('<II', 107, len(blocks))
    offset = len(data)
    for (kind, _, _), size in zip(blocks, sizes, strict=True):
        directory += struct.pack('<HHII', 0, kind, offset, size)
        offset -= size
    return data + directory + struct.pack('<I', len(directory)) + b'SEFT'


def uri(raw):
    return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode('ascii')


def session(value):
    return {'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': value}}]}]}


def test_fixture_is_a_plain_complete_jpeg():
    assert b'MPF\0' not in JPEG
    assert helpers._is_complete_jpeg(JPEG)


def test_samsung_trailer_keeps_the_photo_native_and_skips_text_redaction(monkeypatch):
    value = uri(JPEG + sef())
    calls = []
    monkeypatch.setattr(helpers, '_redact_text', lambda text, **kw: calls.append(text) or text)
    assert helpers._is_native_raster_data_uri(value)
    assert helpers.redact_session_data(session(value)) == session(value)
    assert value not in calls


def test_samsung_trailer_after_mpf_pictures_is_accepted():
    assert helpers._is_complete_jpeg(MPO)
    assert helpers._is_complete_jpeg(MPO + sef())


def _directory(raw):
    return raw.rindex(b'SEFH')


@pytest.mark.parametrize('attack', ['trailing_secret', 'leading_gap', 'bad_magic', 'dir_length',
                                   'zero_count', 'huge_count', 'entry_padding', 'type_mismatch',
                                   'offset_overflow', 'size_overlap', 'name_length', 'binary_name',
                                   'truncated', 'secret_without_directory'])
def test_samsung_trailer_rejects_unaccounted_or_invalid_bytes(attack):
    trailer = bytearray(sef())
    d = _directory(trailer)
    raw = None
    if attack == 'trailing_secret':
        trailer += b'API_KEY=private-trailing-secret'
    elif attack == 'leading_gap':
        raw = JPEG + b'\0' + bytes(trailer)
    elif attack == 'bad_magic':
        trailer[-4:] = b'SEFX'
    elif attack == 'dir_length':
        struct.pack_into('<I', trailer, len(trailer) - 8, len(trailer))
    elif attack == 'zero_count':
        struct.pack_into('<I', trailer, d + 8, 0)
    elif attack == 'huge_count':
        struct.pack_into('<I', trailer, d + 8, 999999)
    elif attack == 'entry_padding':
        trailer[d + 12] = 1
    elif attack == 'type_mismatch':
        struct.pack_into('<H', trailer, d + 12 + 2, 0x0BAD)
    elif attack == 'offset_overflow':
        struct.pack_into('<I', trailer, d + 12 + 4, d + 1)
    elif attack == 'size_overlap':
        size = int.from_bytes(trailer[d + 12 + 8:d + 12 + 12], 'little')
        struct.pack_into('<I', trailer, d + 12 + 8, size + 1)
    elif attack == 'name_length':
        struct.pack_into('<I', trailer, 4, 1000)
    elif attack == 'binary_name':
        trailer[8] = 0x01
    elif attack == 'truncated':
        del trailer[:1]
    elif attack == 'secret_without_directory':
        raw = JPEG + b'API_KEY=private-trailing-secret' + b'SEFT'
    raw = raw if raw is not None else JPEG + bytes(trailer)
    assert not helpers._is_native_raster_data_uri(uri(raw))


def test_samsung_trailer_is_not_accepted_inside_an_mpf_picture():
    assert not helpers._is_complete_jpeg(JPEG + sef(), _allow_mpf=False)


def test_overlapping_entries_on_one_huge_block_are_rejected_without_scanning_it():
    """64 directory entries that all point at one block with a 1 MB name: the tiling check must reject them before
    the name is read, or a GET re-scans the name once per entry (r1105l: real HTTP GET 3.7 s on master, 74.9 s)."""
    name = b'A' * (1 << 20)
    block = struct.pack('<HHI', 0, 0x0A01, len(name)) + name + b'1'
    directory = b'SEFH' + struct.pack('<II', 107, 64)
    directory += struct.pack('<HHII', 0, 0x0A01, len(block), len(block)) * 64
    raw = JPEG + block + directory + struct.pack('<I', len(directory)) + b'SEFT'
    started = time.perf_counter()
    assert not helpers._is_complete_jpeg(raw)
    assert time.perf_counter() - started < 0.5

