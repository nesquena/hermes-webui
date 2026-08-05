"""Cross-language MEDIA grammar tests (PR #6607 re-review).

The MEDIA token grammar is implemented twice — ``media_token_pattern()`` in
api/helpers.py and ``_mediaPathSrc()`` in static/ui.js. A divergence between
them is not cosmetic: the frontend renders and requests one path while the
backend allow-list and the public-share inliner resolve a different string, so
an image the UI just displayed is denied by /api/media and replaced with a
placeholder in a share.

These tests pin the reviewer-reported cases and assert the two implementations
agree after unquoting, which is the value each side hands to ``Path()``.
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

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def media_re():
    return re.compile(media_token_pattern())


# ── Reviewer-reported blockers (#6607 re-review) ─────────────────────────────
# Three deterministic failures at head bfc3c2d3, plus the cross-language
# agreement that the two grammars are one grammar.


@pytest.mark.parametrize(
    "text,expected",
    [
        # 1. Dotted DIRECTORY before a space. The old lazy any-extension run
        #    settled on `/tmp/v1.2` because the following space satisfied the
        #    boundary lookahead, so the path never reached `chart.png`.
        ("MEDIA:/tmp/v1.2 Reports/chart.png", ["/tmp/v1.2 Reports/chart.png"]),
        ("MEDIA:/tmp/v2.5 Data/final.report.png",
         ["/tmp/v2.5 Data/final.report.png"]),
        # 2. Explicit quoted form: the unambiguous spelling for a path holding
        #    spaces AND closing delimiters. Python had no quoted alternative at
        #    all, so this captured `"/tmp/My`.
        ('MEDIA:"/tmp/My Files/report (final).png"',
         ['"/tmp/My Files/report (final).png"']),
        ("MEDIA:'/tmp/My Files/single.png'",
         ["'/tmp/My Files/single.png'"]),
        ('MEDIA:"/tmp/dir]/od[d).png"', ['"/tmp/dir]/od[d).png"']),
        # Unicode and percent-sensitive characters survive.
        ("MEDIA:/tmp/café 文字/图.png", ["/tmp/café 文字/图.png"]),
        ('MEDIA:"/tmp/pct %20 dir/x.png"', ['"/tmp/pct %20 dir/x.png"']),
        # Adjacent tags stay separate; trailing prose is not absorbed.
        ("see MEDIA:/tmp/one.png and MEDIA:/tmp/two.png",
         ["/tmp/one.png", "/tmp/two.png"]),
        ("MEDIA:/tmp/a.png looks good to me", ["/tmp/a.png"]),
        # A dotted stem still resolves whole (no known-extension allow-list).
        ("MEDIA:/tmp/archive.png.bak", ["/tmp/archive.png.bak"]),
        # Non-media extensions must keep working — the grammar is not restricted
        # to a renderable-format list.
        ("MEDIA:/tmp/My Sheets/book.xlsx", ["/tmp/My Sheets/book.xlsx"]),
        ("MEDIA:/tmp/data.json", ["/tmp/data.json"]),
    ],
)
def test_reviewer_reported_grammar_cases(media_re, text, expected):
    assert media_re.findall(text) == expected


def test_unquote_media_ref_strips_one_matching_pair():
    from api.helpers import unquote_media_ref

    assert unquote_media_ref('"/tmp/a b.png"') == "/tmp/a b.png"
    assert unquote_media_ref("'/tmp/a b.png'") == "/tmp/a b.png"
    # Not a matching pair — leave it alone rather than corrupting the path.
    assert unquote_media_ref('"/tmp/a.png') == '"/tmp/a.png'
    assert unquote_media_ref("/tmp/it's.png") == "/tmp/it's.png"
    assert unquote_media_ref("") == ""


def test_quoted_ref_is_admitted_by_the_allow_list(tmp_path, monkeypatch):
    """The cross-boundary consequence of the quoted mismatch.

    static/ui.js defines AND unquotes a quoted alternative, so the frontend
    requests /api/media?path=/tmp/My Files/x.png. Python had no quoted
    alternative and no unquote step, so the allow-list entry was built from
    `"/tmp/My` — the path the renderer asks for was never in the allow-list and
    /api/media denied a file the UI had just displayed.
    """
    from api import routes

    target = tmp_path / "My Files" / "report (final).png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n")

    class _Session:
        messages = [
            {"role": "assistant", "content": f'Here you go MEDIA:"{target}" ok'}
        ]

    monkeypatch.setattr(routes, "get_session", lambda sid: _Session())
    assert routes._session_media_token_allows_path(
        "sess-1", target, {"image/png"}
    ) is True


def test_quoted_ref_from_user_content_is_still_rejected(tmp_path, monkeypatch):
    """Adding the quoted form must not weaken the threat model: user-authored
    tokens still cannot mint allow-list entries."""
    from api import routes

    target = tmp_path / "My Files" / "secret.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n")

    class _Session:
        messages = [{"role": "user", "content": f'MEDIA:"{target}"'}]

    monkeypatch.setattr(routes, "get_session", lambda sid: _Session())
    assert routes._session_media_token_allows_path(
        "sess-1", target, {"image/png"}
    ) is False


# A 1x1 PNG that passes the share inliner's magic-byte and MIME checks.
# Written as a commented, chunk-by-chunk bytes literal rather than a single
# bytes.fromhex(...) blob so each PNG chunk is readable in place and the fixture
# needs no decoding step at import time.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"                      # signature
    b"\x00\x00\x00\rIHDR"                     # IHDR chunk header
    b"\x00\x00\x00\x01\x00\x00\x00\x01"       # 1x1
    b"\x08\x06\x00\x00\x00"                   # 8-bit RGBA
    b"\x1f\x15\xc4\x89"                       # IHDR CRC
    b"\x00\x00\x00\nIDAT"                     # IDAT chunk header
    b"x\x9cc\x00\x01\x00\x00\x05\x00\x01"     # zlib-compressed single pixel
    b"\r\n-\xb4"                              # IDAT CRC
    b"\x00\x00\x00\x00IEND\xaeB`\x82"         # IEND
)


@pytest.mark.parametrize("quoted", [True, False])
def test_share_inliner_embeds_spaced_path(tmp_path, quoted):
    """The share inliner passed the raw capture into Path(), so a spaced ref was
    replaced with a placeholder in a PUBLIC share even though the file was
    inside an allowed root."""
    from api import shares

    target = tmp_path / "My Files" / ("report final.png" if not quoted
                                      else "report (final).png")
    target.parent.mkdir(parents=True)
    target.write_bytes(_TINY_PNG)

    ref = f'"{target}"' if quoted else str(target)
    out = shares._embed_share_media(f"see MEDIA:{ref} ok",
                                    allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in out


def test_share_inliner_still_rejects_path_outside_allowed_roots(tmp_path):
    """Quoting must not become a traversal escape hatch."""
    from api import shares

    outside = tmp_path / "outside" / "x.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(_TINY_PNG)
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    out = shares._embed_share_media(f'see MEDIA:"{outside}" ok',
                                    allowed_roots=(allowed,))
    assert "data:image/png;base64," not in out


# ── External URLs carrying a nested MEDIA: keyword ───────────────────────────
#
# The share matcher used to spell its URL exemption as `exclude_urls=True`, a
# negative lookahead at the match start. An external URL was therefore rejected
# by NOT MATCHING, and `re.sub` resumed scanning one character later — INSIDE the
# token it had just refused. If that URL's own path or query contained the
# literal `MEDIA:`, the nested occurrence matched as a fresh local token, so the
# tail of a legitimate external URL was rewritten (and local paths were probed
# from share text). The fix matches the full canonical token and classifies it in
# `_replace_ref()`.


@pytest.mark.parametrize(
    "text,label",
    [
        (
            "MEDIA:https://cdn.test/img/MEDIA:/etc/passwd.png",
            "nested MEDIA: in the URL path",
        ),
        (
            "MEDIA:https://cdn.test/i.png?src=MEDIA:/etc/shadow.png",
            "nested MEDIA: in the URL query",
        ),
        (
            "MEDIA:HTTPS://cdn.test/a/MEDIA:/etc/passwd.png",
            "uppercase scheme (schemes are case-insensitive)",
        ),
        (
            'MEDIA:"https://cdn.test/q/MEDIA:/etc/passwd.png"',
            "quoted external URL",
        ),
    ],
)
def test_external_url_with_nested_media_keyword_is_preserved_exactly(text, label):
    """An exempt external token must survive byte-for-byte."""
    from api import shares

    out = shares._embed_share_media(text, allowed_roots=())
    assert out == text, f"{label}: external token was rewritten"
    assert shares._PLACEHOLDER not in out, (
        f"{label}: a local-path placeholder was spliced into an external URL"
    )


def test_local_path_inside_allowed_root_still_embeds(tmp_path):
    """Positive control: dropping the pattern-level guard must not stop embedding."""
    from api import shares

    target = tmp_path / "shot.png"
    target.write_bytes(_TINY_PNG)

    out = shares._embed_share_media(f"see MEDIA:{target} ok", allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in out


@pytest.mark.parametrize(
    "ref,label",
    [
        ("file:///etc/passwd.png", "file:// is always rejected"),
        ("/etc/passwd.png", "absolute path outside every allowed root"),
    ],
)
def test_local_negative_controls_still_placeholdered(ref, label):
    """Negative controls: local refs must still never leak into a share."""
    from api import shares

    out = shares._embed_share_media(f"MEDIA:{ref}", allowed_roots=())
    assert shares._PLACEHOLDER in out, f"{label}: expected a placeholder"
    assert ref not in out, f"{label}: the local path leaked into the share"


# ── One grammar, two languages ───────────────────────────────────────────────


def _js_media_capture_and_remainder(cases: list[str]) -> list[list[str | None]]:
    """Return ``[capture, exact remainder]`` per case from the REAL JS grammar."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    def extract(src: str, name: str) -> str:
        start = src.index(f"function {name}(")
        depth = 0
        started = False
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
                started = True
            elif src[i] == "}":
                depth -= 1
                if started and depth == 0:
                    return src[start:i + 1]
        raise AssertionError(f"unbalanced braces extracting {name}")

    script = "\n".join([
        extract(UI_JS, "_mediaPathSrc"),
        extract(UI_JS, "_mediaTokenRe"),
        "const cases = JSON.parse(process.argv[1]);",
        "const out = cases.map((s) => {",
        "  const re = _mediaTokenRe();",
        "  const m = re.exec(s);",
        "  return m ? [m[1], s.slice(m.index + m[0].length)] : [null, null];",
        "});",
        "console.log(JSON.stringify(out));",
    ])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(cases)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


# Terminal punctuation ownership. `(text, expected capture, exact remainder)` —
# fixed expectations, NOT a mirrored comparison, so the table pins the intended
# token/remainder split rather than merely proving the two sides agree.
#
# Sentence punctuation is only prose when a REAL delimiter follows it. At
# absolute end-of-input the token keeps the character on purpose: a streaming
# chunk can stop mid-token, and splitting there would capture `/tmp/a` out of
# `MEDIA:/tmp/a.png` on the final chunk and desync the streamed and settled
# renderings. Each sentence case below is therefore written with following text.
_PUNCTUATION_OWNERSHIP = [
    ("see MEDIA:/tmp/a.png. Next", "/tmp/a.png", ". Next"),
    ("see MEDIA:/tmp/a.png! Next", "/tmp/a.png", "! Next"),
    ("see MEDIA:/tmp/a.png? Next", "/tmp/a.png", "? Next"),
    # A newline is a delimiter too, so a period at end-of-line is still prose.
    ("see MEDIA:/tmp/a.png.\nNext", "/tmp/a.png", ".\nNext"),
    # A dot genuinely inside the name is still part of the ref.
    ("MEDIA:/tmp/a.tar.gz ok", "/tmp/a.tar.gz", " ok"),
    ("MEDIA:/tmp/v1.2/chart.png. Next", "/tmp/v1.2/chart.png", ". Next"),
    # Real query punctuation stays INSIDE an external URL.
    ("MEDIA:https://x.test/i.png?w=1&h=2", "https://x.test/i.png?w=1&h=2", ""),
    ("MEDIA:https://x.test/a.png?q=1. Next", "https://x.test/a.png?q=1", ". Next"),
    ("MEDIA:HTTPS://x.test/a.png", "HTTPS://x.test/a.png", ""),
    ("MEDIA:https://fal.media/generated", "https://fal.media/generated", ""),
    # End-of-input keeps the character: the stream may simply stop here.
    ("MEDIA:/tmp/a.png", "/tmp/a.png", ""),
    # Pre-existing contract that already worked; must not regress.
    ("MEDIA:/tmp/a.png, next", "/tmp/a.png", ", next"),
    ("MEDIA:/tmp/noext. Next", "/tmp/noext", ". Next"),
    ('MEDIA:"/tmp/a b.png".', '"/tmp/a b.png"', "."),
    ("MEDIA:/tmp/v1.2 Reports/chart.png done", "/tmp/v1.2 Reports/chart.png", " done"),
    ("MEDIA:/a.png MEDIA:/b.png", "/a.png", " MEDIA:/b.png"),
    ("MEDIA:C:/tmp/live.png ", "C:/tmp/live.png", " "),
    # Windows-style drive path must survive intact (`:` closes a token, so a lazy
    # form would truncate this to `C`).
    ("MEDIA:C:/tmp/live.png. Next", "C:/tmp/live.png", ". Next"),
]


@pytest.mark.parametrize("text,expected_capture,expected_remainder", _PUNCTUATION_OWNERSHIP)
def test_python_terminal_punctuation_ownership(text, expected_capture, expected_remainder):
    """Python: sentence punctuation is prose; URL query punctuation is the ref."""
    import re as _re

    from api.helpers import media_token_pattern

    m = _re.compile(media_token_pattern()).search(text)
    assert m is not None, f"no match for {text!r}"
    assert m.group(1) == expected_capture
    assert text[m.end():] == expected_remainder


def test_js_terminal_punctuation_ownership_matches_python():
    """JS must make the SAME token/remainder split as Python, case by case."""
    cases = [text for text, _, _ in _PUNCTUATION_OWNERSHIP]
    results = _js_media_capture_and_remainder(cases)
    for (text, expected_capture, expected_remainder), (capture, remainder) in zip(
        _PUNCTUATION_OWNERSHIP, results, strict=True
    ):
        assert capture == expected_capture, f"JS capture diverged for {text!r}"
        assert remainder == expected_remainder, f"JS remainder diverged for {text!r}"


# Ambiguous unquoted spaced refs. If the first word already ends in a filename
# extension, it owns the token and ordinary prose after it must remain prose.
# Dotted-directory support is still unambiguous when the continuation contains
# a path separator (`/tmp/v1.2 Reports/chart.png`). A genuinely ambiguous spaced
# filename can use the already-supported quoted form.
_SPACED_AMBIGUITY_OWNERSHIP = [
    (
        "MEDIA:/tmp/a.png see README.md",
        "/tmp/a.png",
        " see README.md",
    ),
    (
        "MEDIA:/tmp/a.png see README.md next",
        "/tmp/a.png",
        " see README.md next",
    ),
    (
        "MEDIA:/tmp/v1.2 Reports/chart.png",
        "/tmp/v1.2 Reports/chart.png",
        "",
    ),
    (
        "MEDIA:/tmp/My Files/final report.png done",
        "/tmp/My Files/final report.png",
        " done",
    ),
]


@pytest.mark.parametrize("text,expected_capture,expected_remainder", _SPACED_AMBIGUITY_OWNERSHIP)
def test_python_spaced_path_does_not_absorb_dotted_prose(
    text, expected_capture, expected_remainder
):
    """A complete first filename wins over later dot-bearing prose."""
    import re as _re

    from api.helpers import media_token_pattern

    m = _re.compile(media_token_pattern()).search(text)
    assert m is not None, f"no match for {text!r}"
    assert m.group(1) == expected_capture
    assert text[m.end():] == expected_remainder


def test_js_spaced_path_does_not_absorb_dotted_prose():
    """JavaScript applies the same fixed capture/remainder contract."""
    cases = [text for text, _, _ in _SPACED_AMBIGUITY_OWNERSHIP]
    results = _js_media_capture_and_remainder(cases)
    for (text, expected_capture, expected_remainder), (capture, remainder) in zip(
        _SPACED_AMBIGUITY_OWNERSHIP, results, strict=True
    ):
        assert capture == expected_capture, f"JS capture diverged for {text!r}"
        assert remainder == expected_remainder, f"JS remainder diverged for {text!r}"


def _js_media_captures(cases: list[str]) -> list[str | None]:
    """Run the JS grammar over *cases* under node, returning unquoted captures."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    def extract(src: str, name: str) -> str:
        start = src.index(f"function {name}(")
        depth = 0
        started = False
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
                started = True
            elif src[i] == "}":
                depth -= 1
                if started and depth == 0:
                    return src[start:i + 1]
        raise AssertionError(f"unbalanced braces extracting {name}")

    script = "\n".join([
        extract(UI_JS, "_mediaPathSrc"),
        extract(UI_JS, "_unquoteMediaRef"),
        "const cases = JSON.parse(process.argv[1]);",
        "const out = cases.map((s) => {",
        "  const re = new RegExp(String.raw`MEDIA:(${_mediaPathSrc()})`);",
        "  const m = re.exec(s);",
        "  return m ? _unquoteMediaRef(m[1]) : null;",
        "});",
        "console.log(JSON.stringify(out));",
    ])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(cases)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


def test_python_and_js_grammars_agree(media_re):
    """The two implementations must be ONE grammar.

    Compared AFTER unquoting, because that is the value each side actually hands
    to Path() / the /api/media request — the layer where a divergence became a
    denied image and a placeholdered share.
    """
    from api.helpers import unquote_media_ref

    cases = [
        "MEDIA:/tmp/plain.png",
        "MEDIA:/tmp/archive.png.bak",
        "MEDIA:/tmp/v1.2 Reports/chart.png",
        'MEDIA:"/tmp/My Files/report (final).png"',
        "MEDIA:'/tmp/My Files/single.png'",
        'MEDIA:"/tmp/dir]/od[d).png"',
        "MEDIA:/tmp/no-ext-file",
        "MEDIA:/tmp/a.png trailing prose",
        "MEDIA:/tmp/café 文字/图.png",
        'MEDIA:"/tmp/pct %20 dir/x.png"',
        "see MEDIA:/tmp/one.png and MEDIA:/tmp/two.png",
        "MEDIA:/tmp/v2.5 Data/final.report.png",
        f"MEDIA:{SPACED}",
        "MEDIA:/tmp/My Files/x.md\nnext line",
        "MEDIA:/tmp/Caddyfile",
        "(MEDIA:/tmp/a b.png)",
        "MEDIA:/tmp/data.json",
        "MEDIA:/tmp/My Sheets/book.xlsx",
    ]

    js = _js_media_captures(cases)
    for text, js_capture in zip(cases, js, strict=True):
        m = media_re.search(text)
        py_capture = unquote_media_ref(m.group(1)) if m else None
        assert py_capture == js_capture, (
            f"grammar divergence for {text!r}: "
            f"python={py_capture!r} js={js_capture!r}"
        )
