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
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4"
    "0000000049454e44ae426082"
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


# ── One grammar, two languages ───────────────────────────────────────────────


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
