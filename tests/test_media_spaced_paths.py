"""Spaced MEDIA: paths must resolve end-to-end (renderer, allow-list, shares).

A MEDIA path may legitimately contain spaces:

    MEDIA:/home/u/vault/Meeting Notes/2026-07-29 - SDE Focus Group.md

Every MEDIA parser used to capture the path with a ``[^\\s)\\]]+`` class, which
stops at the first space. Three surfaces were affected by the same class of bug:

  1. static/ui.js renderMd()          -> artifact card built from a truncated
                                         path (wrong basename) and the tail
                                         leaked into the bubble as raw prose
  2. api/routes.py allow-list         -> truncated capture never matches the real
                                         on-disk path, so /api/media denies a
                                         legitimate assistant-emitted artifact
  3. api/shares.py inliner            -> same truncation, so a public share
                                         silently fails to embed the file

Widening is bounded on purpose: space tolerance must not swallow trailing prose
or glue an adjacent MEDIA: tag into one invalid path. Those adversarial cases
are asserted here alongside the spaced-path fix so a future widening cannot
regress them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.helpers import media_token_pattern  # noqa: E402

SPACED = "/home/samfp/vault/Meeting Notes/2026-07-29 - SDE Focus Group.md"


# ── The shared Python pattern ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def media_re():
    return re.compile(media_token_pattern())


@pytest.fixture(scope="module")
def share_re():
    return re.compile(media_token_pattern(extra_exclude=">", exclude_urls=True))


def test_spaced_path_captured_whole(media_re):
    """The reported bug: capture must not stop at the first space."""
    assert media_re.findall(f"here you go\nMEDIA:{SPACED}\n") == [SPACED]


def test_spaced_path_captured_whole_in_share(share_re):
    assert share_re.findall(f"MEDIA:{SPACED}") == [SPACED]


@pytest.mark.parametrize(
    "text,expected",
    [
        # Bounded: trailing prose is NOT absorbed into the path.
        ("MEDIA:/tmp/a.png looks good to me", ["/tmp/a.png"]),
        # Bounded: two tags stay two tags (a greedy widening merges them).
        ("MEDIA:/tmp/a.png MEDIA:/tmp/b.png", ["/tmp/a.png", "/tmp/b.png"]),
        # Delimiters still terminate the path.
        ("(MEDIA:/tmp/a b.png)", ["/tmp/a b.png"]),
        # A newline always ends a MEDIA token.
        ("MEDIA:/tmp/My Files/x.md\nnext line", ["/tmp/My Files/x.md"]),
        # Extension-less paths kept working (no-space fallback branch).
        ("MEDIA:/tmp/Caddyfile", ["/tmp/Caddyfile"]),
    ],
)
def test_widening_stays_bounded(media_re, text, expected):
    assert media_re.findall(text) == expected


def test_share_pattern_skips_http_urls(share_re):
    """External images pass through the share inliner untouched."""
    assert share_re.findall("MEDIA:https://example.test/a.png") == []


def test_share_pattern_stops_at_angle_bracket(share_re):
    """'>' terminates the path so an inlined <img ...> tag can't be swallowed."""
    assert share_re.findall("MEDIA:/tmp/a.png>trailing") == ["/tmp/a.png"]


def test_allow_list_pattern_still_matches_urls(media_re):
    """routes.py filters URLs by '://' after matching, so it must capture them."""
    assert media_re.findall("MEDIA:https://example.test/a.png") == [
        "https://example.test/a.png"
    ]


# ── The /api/media allow-list, end-to-end ───────────────────────────────────
# This is the behavioral half: the truncated capture made the allow-list DENY a
# real assistant-emitted artifact whose path contains a space. Asserting the
# regex alone would not catch a future change that resolves the path
# differently, so drive the real predicate.


def test_allow_list_admits_assistant_spaced_path(tmp_path, monkeypatch):
    from api import routes

    target = tmp_path / "Meeting Notes" / "2026-07-29 - SDE Focus Group.md"
    target.parent.mkdir(parents=True)
    target.write_text("# notes", encoding="utf-8")

    class _Session:
        messages = [
            {"role": "assistant", "content": f"Wrote it up.\nMEDIA:{target}\n"}
        ]

    monkeypatch.setattr(routes, "get_session", lambda sid: _Session())
    monkeypatch.setitem(routes.MIME_MAP, ".md", "text/markdown")

    assert routes._session_media_token_allows_path(
        "sess-1", target, {"text/markdown"}
    ) is True


def test_allow_list_still_rejects_user_authored_token(tmp_path, monkeypatch):
    """User content must not be able to mint allow-list entries (threat model)."""
    from api import routes

    target = tmp_path / "My Files" / "secret.md"
    target.parent.mkdir(parents=True)
    target.write_text("nope", encoding="utf-8")

    class _Session:
        messages = [{"role": "user", "content": f"MEDIA:{target}"}]

    monkeypatch.setattr(routes, "get_session", lambda sid: _Session())
    monkeypatch.setitem(routes.MIME_MAP, ".md", "text/markdown")

    assert routes._session_media_token_allows_path(
        "sess-1", target, {"text/markdown"}
    ) is False


def test_allow_list_rejects_unrelated_path(tmp_path, monkeypatch):
    """A spaced token must not widen into admitting a *different* file."""
    from api import routes

    mentioned = tmp_path / "Meeting Notes" / "a.md"
    mentioned.parent.mkdir(parents=True)
    mentioned.write_text("ok", encoding="utf-8")
    other = tmp_path / "Meeting Notes" / "b.md"
    other.write_text("other", encoding="utf-8")

    class _Session:
        messages = [{"role": "assistant", "content": f"MEDIA:{mentioned}"}]

    monkeypatch.setattr(routes, "get_session", lambda sid: _Session())
    monkeypatch.setitem(routes.MIME_MAP, ".md", "text/markdown")

    assert routes._session_media_token_allows_path(
        "sess-1", other, {"text/markdown"}
    ) is False


# ── The browser renderer ────────────────────────────────────────────────────
# ui.js owns the MEDIA shape for the frontend; messages.js reuses it so the
# streamed and settled renderings of one token stay identical. Assert the shared
# helper exists and that no surface kept a private copy of the old class.

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_frontend_media_shape_is_shared():
    assert "function _mediaPathSrc" in UI_JS
    assert "function _mediaTokenRe" in UI_JS
    # messages.js must consume the shared helpers, not re-declare the pattern.
    assert "_mediaTokenRe()" in MESSAGES_JS
    # The anchored single-token matcher was removed: the stream-end flush now
    # partitions the buffered candidate with the shared global matcher, because a
    # candidate can be a token PLUS same-line prose.


def test_no_surface_kept_the_truncating_class():
    """The old first-space-truncating capture must be gone everywhere."""
    old = r"MEDIA:([^\s\)\]]+)"
    for name, src in (
        ("static/ui.js", UI_JS),
        ("static/messages.js", MESSAGES_JS),
        ("api/routes.py", (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")),
        ("api/shares.py", (REPO_ROOT / "api" / "shares.py").read_text(encoding="utf-8")),
    ):
        assert old not in src, f"{name} still uses the truncating MEDIA capture"
