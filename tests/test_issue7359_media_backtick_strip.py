"""Regression tests for issue #7359 — MEDIA: token regex must not capture trailing backtick.

Two-pass scan (#7680 re-gate, 9/22):

- `` `MEDIA:path` `` (backtick-wrapped, inline-code form): the wrapping
  backticks are stripped by a pre-pass before the bare class scans the
  text. This makes the closing backtick disappear as a wrapper instead
  of being captured as part of the path.
- ``MEDIA:/path...`` (bare, no surrounding backticks): the capture class
  no longer lists the backtick in the exclusion set, so a filename
  that legally contains a backtick (``report`final.png``) is captured
  in full instead of being truncated at the first backtick.

The pre-fix (round-1) PR added the backtick to the bare exclusion set,
which traded one bug for another: it dropped the closing wrapper on
the inline-code form but truncated any bare path that happened to
contain a backtick in the filename. The two-pass scan keeps the
original permissive bare class and adds an explicit pre-pass for the
wrapped form, so both cases round-trip correctly.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Source-shape: each site's expected bare-token capture literal. The
# bare class no longer lists the backtick in the exclusion set; a
# backtick inside the filename (legal on POSIX and Windows) is
# captured in full. The wrapped-form pre-pass below strips the
# inline-code backticks before the bare scan.
JS_SITES: dict[str, list[str]] = {
    "static/ui.js": [
        r"/MEDIA:([^\s\)\]]+)/g",       # linkifier
        r"/MEDIA:[^\s]+/g",             # TTS flattener (no paren/bracket exclusion)
    ],
    "static/messages.js": [
        r"/^MEDIA:([^\s\)\]]+)$/",      # streaming chunk finalize
        r"/MEDIA:([^\s\)\]]+)/g",       # streaming combined walk
        r"/MEDIA:[^\s\)\]]*$/",         # streaming partial tail
    ],
}
PY_SITES: dict[str, str] = {
    "api/routes.py": r"MEDIA:([^\s\)\]]+)",
    "api/media_snapshots.py": r"MEDIA:([^\s\)\]]+)",
}

# #7680 re-gate (9/22): every site that scans bare MEDIA: tokens also
# runs the wrapped-form pre-pass (`` `MEDIA:path` `` → ``MEDIA:path``)
# so the closing backtick of an inline-code form is consumed before the
# bare scan.
JS_BACKTICK_PREPASS: dict[str, str] = {
    "static/ui.js": r"/`MEDIA:([^`\s]+)`/g",
    "static/messages.js": r"/`MEDIA:([^`\s]+)`/g",
}
PY_BACKTICK_PREPASS: dict[str, str] = {
    "api/routes.py": r"`MEDIA:([^`\s]+)`",
    "api/media_snapshots.py": r"`MEDIA:([^`\s]+)`",
}


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-shape: every site must carry the bare-token class + the pre-pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relpath", "needle"),
    [
        (relpath, needle)
        for relpath, needles in JS_SITES.items()
        for needle in needles
    ],
)
def test_js_site_carries_bare_token_class(relpath: str, needle: str) -> None:
    """Every JS capture site carries the bare-token class literal."""
    src = _read(relpath)
    assert needle in src, (
        f"{relpath} is missing the bare-token capture literal: {needle!r}"
    )


@pytest.mark.parametrize(
    ("relpath", "needle"),
    [
        (relpath, JS_BACKTICK_PREPASS[relpath])
        for relpath in JS_BACKTICK_PREPASS
    ],
)
def test_js_site_carries_wrapped_prepass(relpath: str, needle: str) -> None:
    """#7680 re-gate: every JS site that scans bare MEDIA: tokens also
    runs the wrapped-form pre-pass so the inline-code closing backtick
    is eaten before the bare scan."""
    src = _read(relpath)
    assert needle in src, (
        f"{relpath} is missing the wrapped-form pre-pass literal: {needle!r}"
    )


@pytest.mark.parametrize(
    ("relpath", "needle"),
    [(relpath, PY_SITES[relpath]) for relpath in PY_SITES],
)
def test_py_site_carries_bare_token_class(relpath: str, needle: str) -> None:
    src = _read(relpath)
    assert needle in src, (
        f"{relpath} is missing the bare-token capture literal: {needle!r}"
    )


@pytest.mark.parametrize(
    ("relpath", "needle"),
    [(relpath, PY_BACKTICK_PREPASS[relpath]) for relpath in PY_BACKTICK_PREPASS],
)
def test_py_site_carries_wrapped_prepass(relpath: str, needle: str) -> None:
    src = _read(relpath)
    assert needle in src, (
        f"{relpath} is missing the wrapped-form pre-pass literal: {needle!r}"
    )


def test_all_seven_sites_covered() -> None:
    """Pin: 5 JS sites + 2 Python sites = 7 capture sites, all guarded."""
    js_total = sum(len(v) for v in JS_SITES.values())
    assert js_total == 5, (
        f"expected 5 JS capture sites, got {js_total}; this count is what the "
        "review's '7 capture sites' was counting (1 linkifier + 1 TTS + 3 messages.js)"
    )
    assert len(PY_SITES) == 2


@pytest.fixture(scope="module")
def _media_token_re() -> re.Pattern[str]:
    """Compile the live ``_MEDIA_TOKEN_RE`` from api/routes.py (plus the
    pre-pass) so behaviour tests exercise the real source."""
    routes_text = _read("api/routes.py")
    m = re.search(
        r'_MEDIA_TOKEN_RE = re\.compile\(r"([^"]+)"\)', routes_text, re.M
    )
    assert m, "_MEDIA_TOKEN_RE compile() not found in api/routes.py"
    bare = re.compile(m.group(1))
    pre = re.compile(r"`MEDIA:([^`\s]+)`")
    return _TwoPass(bare, pre)


@pytest.fixture(scope="module")
def _media_snap_re() -> re.Pattern[str]:
    """Compile the live ``media_re`` from api/media_snapshots.py (plus the
    pre-pass) so behaviour tests exercise the real source."""
    snap_text = _read("api/media_snapshots.py")
    m = re.search(
        r'media_re = _re\.compile\(r"([^"]+)"\)', snap_text, re.M
    )
    assert m, "media_re compile() not found in api/media_snapshots.py"
    bare = re.compile(m.group(1))
    pre = re.compile(r"`MEDIA:([^`\s]+)`")
    return _TwoPass(bare, pre)


class _TwoPass:
    """Compose the wrapped-form pre-pass with the bare-token class so the
    behaviour tests below exercise the live source exactly as it runs."""

    def __init__(self, bare: re.Pattern[str], prepass: re.Pattern[str]):
        self._bare = bare
        self._pre = prepass

    def findall(self, text: str) -> list[str]:
        text = self._pre.sub(lambda m: f"MEDIA:{m.group(1)}", text)
        return self._bare.findall(text)


def test_backend_allowlist_strips_backtick(_media_token_re) -> None:
    """``_MEDIA_TOKEN_RE`` (with the pre-pass) must not include the closing
    backtick in the captured path."""
    captured = _media_token_re.findall("get `MEDIA:/home/kim/skills.zip` now")
    assert captured == ["/home/kim/skills.zip"], captured
    assert all(not c.endswith("`") for c in captured)


def test_backend_allowlist_bare_token_still_captures(_media_token_re) -> None:
    """Backward-compat: bare ``MEDIA:/path`` (no backtick) still matches."""
    captured = _media_token_re.findall(
        "MEDIA:/home/kim/a.zip and MEDIA:/home/kim/b.tar"
    )
    assert captured == ["/home/kim/a.zip", "/home/kim/b.tar"], captured


def test_backend_allowlist_bare_path_with_backtick_in_filename(
    _media_token_re,
) -> None:
    """#7680 finding 1: a bare ``MEDIA:/tmp/report`final.png`` (backtick
    inside the filename, no surrounding wrapper) must capture the full
    path. POSIX and Windows both allow backticks in filenames, so
    truncating at the first backtick silently bricks the request."""
    captured = _media_token_re.findall("see MEDIA:/tmp/report`final.png now")
    assert captured == ["/tmp/report`final.png"], captured
    assert not any(c.endswith("`") and c != "/tmp/report`final.png" for c in captured), captured


def test_backend_allowlist_rejects_trailing_paren(_media_token_re) -> None:
    """Pre-existing behaviour: ``)`` is still excluded."""
    captured = _media_token_re.findall("see [file](MEDIA:/home/kim/a.zip) here")
    assert all(not c.endswith(")") for c in captured), captured


def test_snapshot_capture_strips_backtick(_media_snap_re) -> None:
    """``media_re`` (with the pre-pass) must not include the closing
    backtick in the captured path."""
    captured = _media_snap_re.findall(
        "assistant: `MEDIA:/home/kim/shot.png` and MEDIA:/home/kim/a.zip"
    )
    assert captured == ["/home/kim/shot.png", "/home/kim/a.zip"], captured
    assert all(not c.endswith("`") for c in captured)


def test_snapshot_capture_bare_path_with_backtick_in_filename(
    _media_snap_re,
) -> None:
    """#7680 finding 1: same bare-with-backtick case for the snapshot
    pipeline. Without the pre-pass + bare class change, the snapshot
    store would never see this path because the auth predicate and
    the capture predicate must agree on the same set of references."""
    captured = _media_snap_re.findall("MEDIA:/home/kim/shot`final.png tail")
    assert captured == ["/home/kim/shot`final.png"], captured


def test_snapshot_capture_bare_token_still_captures(_media_snap_re) -> None:
    """Backward-compat: bare ``MEDIA:/path`` still matches."""
    captured = _media_snap_re.findall(
        "MEDIA:/home/kim/a.zip and MEDIA:/home/kim/b.zip"
    )
    assert captured == ["/home/kim/a.zip", "/home/kim/b.zip"], captured


# ---------------------------------------------------------------------------
# Revert-sensitivity: the pre-fix class really would have truncated
# the bare-with-backtick case, and the round-1 fix would have left the
# wrapped form broken in a different way.
# ---------------------------------------------------------------------------


def test_round1_buggy_class_truncated_bare_path_with_backtick() -> None:
    """The buggy (round-1) class greedily truncated at the first backtick
    in the path, so ``MEDIA:/tmp/report`final.png`` was captured as
    ``/tmp/report``. This is the regression we are guarding against."""
    buggy = re.compile(r"MEDIA:([^\s\)\]\`]+)")
    assert buggy.findall("MEDIA:/tmp/report`final.png") == ["/tmp/report"], (
        "round-1 class is no longer buggy; if you are intentionally reverting "
        "to it, also remove the corresponding `test_post_fix_class_keeps_"
        "bare_path` and update the source-shape guard."
    )


def test_round1_buggy_class_dropped_wrapped_closing_backtick() -> None:
    """The round-1 class was actually correct for the wrapped form: it
    dropped the closing backtick that the round-0 class greedily
    included. Document the behaviour so a future revert picks the
    correct replacement."""
    round1 = re.compile(r"MEDIA:([^\s\)\]\`]+)")
    assert round1.findall("`MEDIA:/home/kim/a.zip`") == ["/home/kim/a.zip"]


def test_round1_two_pass_compose_keeps_bare_path_intact() -> None:
    """The full #7680 fix is the two-pass compose: pre-pass strips the
    wrapped form, then the bare class (no backtick in the exclusion
    set) captures the full path. Verify the composition on both
    cases so a future refactor that drops either half is caught."""
    pre = re.compile(r"`MEDIA:([^`\s]+)`")
    bare = re.compile(r"MEDIA:([^\s\)\]]+)")

    def findall_composed(text: str) -> list[str]:
        text = pre.sub(lambda m: f"MEDIA:{m.group(1)}", text)
        return bare.findall(text)

    # bare-with-backtick: pre-pass doesn't touch it, bare captures full path
    assert findall_composed("MEDIA:/tmp/report`final.png") == [
        "/tmp/report`final.png"
    ]
    # wrapped: pre-pass strips the backticks, bare captures the path
    assert findall_composed("`MEDIA:/tmp/a.zip`") == ["/tmp/a.zip"]
    # mixed
    assert findall_composed(
        "see `MEDIA:/home/kim/shot.png` and MEDIA:/tmp/r`f.png tail"
    ) == ["/home/kim/shot.png", "/tmp/r`f.png"]
    # plain bare: unchanged
    assert findall_composed("MEDIA:/home/kim/a.zip") == ["/home/kim/a.zip"]


# ---------------------------------------------------------------------------
# node --check (so a JS syntax error in the modified source fails the suite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relpath", sorted(JS_SITES.keys()))
def test_js_file_parses(relpath: str) -> None:
    """``node --check`` on every modified JS file."""
    proc = subprocess.run(
        ["node", "--check", str(REPO_ROOT / relpath)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"{relpath} failed `node --check`:\n{proc.stderr}"
    )
