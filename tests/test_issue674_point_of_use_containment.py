"""Point-of-use containment for the Aider history reader (#674).

The scanner refuses symlinked entries and skips oversized files, but the
parser opened the path LATER, by name:

    text = path.read_text(encoding='utf-8', errors='replace')

That leaves two races AGENTS.md requires to be closed ("check containment at
the point of use and account for races"):

1. The file (or a parent directory) is replaced with a symlink between the
   scan and the open -> the read follows it outside the scan root.
2. The file grows after the size check -> the read is unbounded.

Both are pinned here against the real parser, and the swap is performed
BETWEEN the scan and the parse -- the exact window the review named.
"""

from __future__ import annotations


def _write_history(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_refuses_a_symlinked_history_file(tmp_path):
    """A history file that is a symlink must not be read through."""
    import api.models as models

    outside = _write_history(tmp_path / "outside.md", "\n#### leaked from outside\n")
    link = tmp_path / "repo" / ".aider.chat.history.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    messages, _started_at, _title = models._parse_aider_chat_history(link)

    assert messages == [], (
        "the parser must refuse a symlinked history file instead of reading "
        f"through it; got {messages!r}"
    )


def test_parse_refuses_a_history_that_grew_past_the_byte_cap(tmp_path):
    """A file that grew after the scan's size check must not be read whole."""
    import api.models as models

    path = _write_history(
        tmp_path / "repo" / ".aider.chat.history.md",
        "\n#### turn one\n" + ("x" * 5000),
    )

    messages, _started_at, _title = models._parse_aider_chat_history(
        path, max_bytes=64
    )

    assert messages == [], (
        "the parser must refuse a file that is larger than the byte cap "
        f"(read without limit); got {messages!r}"
    )


def test_scan_then_parse_swap_of_a_parent_directory_does_not_leak(tmp_path):
    """A parent directory swapped for a symlink after the scan must not leak.

    The swap happens between the scan (which checks every entry) and the
    parse (which reads the file), so it is exactly the window the review
    described: the scanner saw a real directory, the reader saw a symlink.
    """
    import api.models as models

    root = tmp_path / "root"
    sub = root / "sub"
    _write_history(sub / ".aider.chat.history.md", "\n#### real turn\n")

    outside = tmp_path / "outside"
    _write_history(outside / ".aider.chat.history.md", "\n#### leaked from outside\n")

    real_cached_parse = models._parse_aider_chat_history_cached

    def swapping_parse(path, **kwargs):
        # The race: the checked directory becomes a symlink to an outside one.
        (sub / ".aider.chat.history.md").unlink()
        sub.rmdir()
        sub.symlink_to(outside, target_is_directory=True)
        return real_cached_parse(path, **kwargs)

    models._parse_aider_chat_history_cached = swapping_parse
    try:
        sessions = models.get_aider_sessions(scan_dir=root)
    finally:
        models._parse_aider_chat_history_cached = real_cached_parse

    titles = [s.get("title") for s in sessions]
    assert "leaked from outside" not in titles, (
        "a directory swapped for a symlink after the scan must not be read; "
        f"got {titles!r}"
    )
