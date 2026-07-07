"""Shared test helpers for static source assertions."""


def source_between(src: str, start_marker: str, end_marker: str) -> str:
    start = src.find(start_marker)
    assert start >= 0, f"{start_marker} not found"
    end = src.find(end_marker, start)
    assert end > start, f"{end_marker} not found after {start_marker}"
    return src[start:end]
