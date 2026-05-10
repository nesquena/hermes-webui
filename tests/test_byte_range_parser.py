"""Edge-case unit tests for _parse_range_header (PR #1290).

The byte-range parser is security-relevant — malformed Range headers from
clients can cause off-by-one bugs, integer overflows, or info disclosure
if not handled correctly per RFC 7233. The PR adds higher-level tests
for media inline streaming, but the range parser itself has no direct
unit tests. This file pins the parser's contract.
"""

import pytest

from api.routes import _parse_range_header


# Each tuple: (header, file_size, expected_result)
# expected_result is None for invalid/unsatisfiable, or (start, end) inclusive.
RANGE_CASES = [
    # ── Valid ranges ─────────────────────────────────────────────────────
    ("bytes=0-99", 1000, (0, 99), "explicit start-end"),
    ("bytes=0-", 1000, (0, 999), "open-ended start"),
    ("bytes=100-", 1000, (100, 999), "open-ended from middle"),
    ("bytes=-500", 1000, (500, 999), "suffix range — last 500 bytes"),
    ("bytes=-99999", 1000, (0, 999), "suffix > file_size clamps to start=0"),
    ("bytes=0-99999", 1000, (0, 999), "end > file_size clamps to file_size-1"),
    ("bytes=999-1500", 1000, (999, 999), "end past file clamps to last byte"),
    ("bytes=100-100", 1000, (100, 100), "single-byte range"),
    ("bytes=999-999", 1000, (999, 999), "last byte"),
    ("bytes= 0-99", 1000, (0, 99), "whitespace inside trimmed"),
    # ── Invalid → None (caller sends 416 Range Not Satisfiable) ─────────
    ("", 1000, None, "empty header"),
    ("bytes 0-100", 1000, None, "wrong format — space instead of ="),
    ("bytes=", 1000, None, "no spec after ="),
    ("bytes=-", 1000, None, "bare dash, no numbers"),
    ("bytes=-0", 1000, None, "zero-length suffix"),
    ("bytes=0-99,200-299", 1000, None, "multipart not supported"),
    ("bytes=500-100", 1000, None, "reversed range"),
    ("bytes=999999-", 1000, None, "start past file"),
    ("bytes=abc-def", 1000, None, "non-numeric"),
    ("bytes=-abc", 1000, None, "non-numeric suffix"),
    (" bytes=0-99", 1000, None, "leading space — must startswith bytes="),
    # ── Empty/zero file ─────────────────────────────────────────────────
    ("bytes=0-99", 0, None, "empty file always None"),
    # ── Negative numbers (should not yield negative offsets) ────────────
    ("bytes=-1", 1000, (999, 999), "suffix=1 — last byte"),
]


@pytest.mark.parametrize(
    "header,file_size,expected,description",
    RANGE_CASES,
    ids=[c[3] for c in RANGE_CASES],
)
def test_parse_range_header(header, file_size, expected, description):
    actual = _parse_range_header(header, file_size)
    assert actual == expected, (
        f"_parse_range_header({header!r}, {file_size}) = {actual!r}, "
        f"expected {expected!r} — {description}"
    )


# ── Invariants beyond the case table ────────────────────────────────────


def test_returned_offsets_are_non_negative_for_valid_inputs():
    for header, file_size, expected, _ in RANGE_CASES:
        if expected is None:
            continue
        start, end = _parse_range_header(header, file_size)
        assert start >= 0, f"start must be non-negative: {start} from {header!r}"
        assert end >= start, f"end must be >= start: ({start},{end}) from {header!r}"
        assert end < file_size, (
            f"end must be < file_size for valid range: {end} >= {file_size} "
            f"from {header!r}"
        )


def test_content_length_is_positive_for_valid_ranges():
    """The (start, end) returned must always describe at least one byte —
    otherwise _serve_file_bytes would compute Content-Length=0 incorrectly."""
    for header, file_size, expected, _ in RANGE_CASES:
        if expected is None:
            continue
        start, end = _parse_range_header(header, file_size)
        content_length = end - start + 1
        assert content_length >= 1, (
            f"Valid range must have positive content-length, got {content_length} "
            f"from {header!r} on size {file_size}"
        )
