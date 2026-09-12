"""Legacy stored snapshots must pass the public-reference guard on the way OUT.

``api/shares.py::build_share_snapshot()`` routes NEW message content through
``_sanitize_message()`` and ``_embed_share_media()``, so a share created after
that guard landed carries no live local reference. But ``load_share()`` read a
STORED snapshot's ``messages`` and returned them unchanged, so every share
written by an OLDER build stayed on the vulnerable path forever — the guard can
only protect content it ever saw.

The sinks are still live on the share page. ``static/share.html`` loads
``static/ui.js``, and ``static/share.js::_shareRenderMessages()`` hands each
stored ``msg.content`` straight to ``renderMd()`` and assigns the result with
``innerHTML``:

* ``_markdownHref()`` turns a ``file://`` link target into
  ``api/media?path=…&inline=1``.
* ``_mdImageHtml()`` routes a ``file://`` image target through
  ``_inlineMediaHtmlForRef()``, which emits ``api/media?path=…``.
* ``_isSafeUrl()``/``_tag()`` accept a relative ``api/`` image or link target,
  so a raw ``<img src="api/media?…">`` survives sanitisation.

Each one makes an ANONYMOUS viewer's browser issue an authenticated
same-origin request against our own ``/api/media`` route.

Test discipline in this file:

* the legacy fixture is written DIRECTLY to the share store as JSON, bypassing
  ``build_share_snapshot()`` entirely — exactly how an older build left it.
* the REAL ``load_share()`` reads it back.
* the REAL ``renderMd()`` from ``static/ui.js`` runs in node over both the
  UNGUARDED stored text (the control) and the loaded text. The contrast is the
  test: the control MUST reach a live ``api/media`` sink, and the loaded result
  MUST NOT.
* the load path must take NO filesystem access. Asserted by monkeypatching the
  ``Path`` methods the embed path would call and failing if any of them fires.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

from api import shares
from api.shares import _PLACEHOLDER, guard_public_share_references, load_share

REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SHARE_JS = (REPO / "static" / "share.js").read_text(encoding="utf-8")

NODE = shutil.which("node")

_SECRET = "/etc/shadow.png"

# Legacy message content, one row per live sink named in the module docstring.
# Written the way an older build stored it: unguarded, path intact.
#
# `live_attr` says whether the UNGUARDED text renders a LIVE element attribute
# (a real `src`/`href` the viewer's browser fetches) as opposed to merely
# disclosing the host path as prose. Both are leaks and both must be refused,
# but only the first is the authenticated-request sink, so the control asserts
# the two separately instead of blurring them.
_LEGACY_ROWS = [
    # (label, stored content, renders a live attribute unguarded)
    ("md link file://", f"here is the file [x](file://{_SECRET}) from earlier", True),
    ("md image file://", f"and the shot ![x](file://{_SECRET})", True),
    ("raw img api/media", f'raw tag <img src="api/media?path={_SECRET}">', True),
    ("rooted api/media target", f"rooted target ![x](/api/media?path={_SECRET})", False),
    ("loopback api/media", f"loopback ![x](http://127.0.0.1:8080/api/media?path={_SECRET})", True),
    ("canonical MEDIA token", f"canonical token MEDIA:{_SECRET}", True),
    ("bare file:// prose", f"bare prose file://{_SECRET}", True),
]

_LEGACY_CONTENTS = [content for _label, content, _live in _LEGACY_ROWS]

# Genuine public references in the same legacy snapshot. Re-guarding must not
# destroy these: over-blocking a stored public asset is also a failure.
_LEGACY_PUBLIC_CONTENTS = [
    "public image ![ok](https://cdn.test/a.png?w=800&fmt=webp)",
    "public link [ok](https://cdn.test/page?x=1&y=2)",
    "public proxy ![ok](https://cdn.test/a.png?next=https://images.example.test/b.png)",
    "external token MEDIA:https://cdn.test/a.png",
]


# ── Real share-page renderer driver ──────────────────────────────────────────
# Extracts the production functions by brace depth, the way the existing share
# tests in this repo already do, and drives the REAL renderMd() in node.

def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(src):
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"could not extract {name}()"
    return src[start:pos]


_RENDER_PRELUDE = textwrap.dedent(
    r"""
    const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
    const _PDF_EXTS=/\.pdf$/i;
    const _SVG_EXTS=/\.svg$/i;
    const _HTML_EXTS=/\.html?$/i;
    const _CSV_EXTS=/\.csv$/i;
    const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
    const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
    const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
    function t(k){ return k; }
    function li(){ return ''; }
    function _mediaPlayerHtml(kind,src){ return `<${kind} src="${esc(src)}"></${kind}>`; }
    function _dataImageHtml(){ return ''; }
    function _isSafeDataImageUri(){ return false; }
    function _mediaKindForName(n){
      const s=String(n||'');
      if(_IMAGE_EXTS.test(s)) return 'image';
      if(_AUDIO_EXTS.test(s)) return 'audio';
      if(_VIDEO_EXTS.test(s)) return 'video';
      return 'file';
    }
    // The share page is served from the share origin, which is what makes a
    // loopback rewrite dangerous there.
    global.window={};
    global.document={baseURI:'https://share.example.test/share/abc123'};
    """
)

_RENDER_FUNCS = (
    "_matchBacktickFenceLine",
    "_isBacktickFenceClose",
    "_mediaPathSrc",
    "_mediaTokenRe",
    "_unquoteMediaRef",
    "_mediaTokenMaxLength",
    "_mediaTokenExceedsMaxLength",
    "_isExternalMediaUrl",
    "_localTargetMarkers",
    "_decodeUrlComponentBounded",
    "_externalMediaUrlHidesLocalTarget",
    "_mdImageHtml",
    "_inlineMediaHtmlForRef",
    "renderMd",
)


def _share_page_html(contents: list[str]) -> list[str]:
    """Render each content string exactly as the share page renders it.

    ``share.js`` calls ``renderMd(String(msg.content||''))`` and drops the
    result into ``innerHTML``, so rendering the string with the real
    ``renderMd()`` is what the anonymous viewer's browser does.
    """
    js = _RENDER_PRELUDE
    for name in _RENDER_FUNCS:
        js += "\n" + _extract_function(UI_JS, name)
    js += textwrap.dedent(
        r"""
        const cases=JSON.parse(process.argv[1]);
        process.stdout.write(JSON.stringify(cases.map(c=>renderMd(String(c||'')))));
        """
    )
    proc = subprocess.run(
        ["node", "-e", js, json.dumps(contents)],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(proc.stdout)


def _live_media_sinks(html: str) -> list[str]:
    """Every live local/authenticated reference in rendered *html*."""
    lowered = html.lower()
    hits = []
    for needle in (
        'src="api/media',
        "src='api/media",
        'href="api/media',
        "href='api/media",
        'src="/api/media',
        'href="/api/media',
        "api/media?path=",
        "file://",
        "/etc/shadow",
        "127.0.0.1",
    ):
        if needle in lowered:
            hits.append(needle)
    return hits


def _live_media_attributes(html: str) -> list[str]:
    """Only the LIVE element attributes — a rendered ``src``/``href`` sink.

    Stricter than :func:`_live_media_sinks`, which also counts a path merely
    disclosed as prose. A live attribute is what actually makes the anonymous
    viewer's browser issue the authenticated request.
    """
    lowered = html.lower()
    return [
        needle
        for needle in (
            'src="api/media',
            "src='api/media",
            'href="api/media',
            "href='api/media",
            'src="/api/media',
            'href="/api/media',
            'src="http://127.0.0.1',
        )
        if needle in lowered
    ]


# ── The legacy fixture, written DIRECTLY to the share store ──────────────────

@pytest.fixture()
def legacy_share(tmp_path, monkeypatch):
    """Write a pre-guard snapshot straight to the store and return its token.

    ``build_share_snapshot()`` is deliberately NOT called: the whole point is a
    snapshot whose content never met the guard.
    """
    store = tmp_path / "shares"
    store.mkdir()
    monkeypatch.setattr(shares, "SHARES_DIR", store)
    token = "legacytoken123"
    messages = [
        {"role": "assistant", "content": c, "timestamp": 1.0}
        for c in _LEGACY_CONTENTS + _LEGACY_PUBLIC_CONTENTS
    ]
    payload = {
        "token": token,
        "source_session_id": "sess-legacy",
        "title": "Legacy share",
        "messages": messages,
        "message_count": len(messages),
        "created_at": 1.0,
        "updated_at": 1.0,
        "revoked_at": None,
    }
    (store / f"{token}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return token


def test_legacy_snapshot_is_stored_unguarded(legacy_share, tmp_path):
    """Precondition: the fixture really is raw, pre-guard content on disk."""
    raw = json.loads((shares.SHARES_DIR / f"{legacy_share}.json").read_text())
    stored = [m["content"] for m in raw["messages"]]
    assert stored == _LEGACY_CONTENTS + _LEGACY_PUBLIC_CONTENTS
    assert _PLACEHOLDER not in "".join(stored)
    assert "/etc/shadow" in "".join(stored)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_unguarded_control_reaches_a_live_api_media_sink():
    """The control half of the contrast: without the guard, the sinks FIRE.

    If this ever stops finding a live sink the negative assertion below proves
    nothing, so it is asserted explicitly rather than assumed. Rows marked
    ``live_attr`` must render a real ``src``/``href`` the browser fetches; the
    remaining row must at least disclose the host path.
    """
    rendered = _share_page_html(_LEGACY_CONTENTS)
    failures = []
    for (label, _content, live_attr), html in zip(_LEGACY_ROWS, rendered):
        if live_attr and not _live_media_attributes(html):
            failures.append(f"{label}: no live attribute in {html[:200]!r}")
        if not _live_media_sinks(html):
            failures.append(f"{label}: no local reference at all in {html[:200]!r}")
    assert not failures, "control lost its teeth:\n" + "\n".join(failures)

    joined = " ".join(rendered).lower()
    assert 'src="api/media?path=' in joined, joined[:400]
    assert 'href="api/media?path=' in joined, joined[:400]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_loaded_legacy_snapshot_reaches_no_live_sink(legacy_share):
    """The fix half: load through the REAL load_share(), then render for real."""
    payload = load_share(legacy_share)
    assert payload is not None
    loaded = [m["content"] for m in payload["messages"]]
    attacks = loaded[: len(_LEGACY_ROWS)]

    # The RENDER assertion comes first, deliberately. It is the one that answers
    # the question the reviewer asked — does the anonymous viewer's browser get
    # a live local reference — so it must be the assertion a regression trips,
    # not a text check that happens to fire earlier.
    rendered = _share_page_html(attacks)
    failures = [
        f"{label}: {hits} in {html[:200]!r}"
        for (label, _content, _live), html in zip(_LEGACY_ROWS, rendered)
        if (hits := _live_media_sinks(html))
    ]
    assert not failures, "loaded legacy snapshot still reaches a live sink:\n" + "\n".join(failures)
    assert all(not _live_media_attributes(html) for html in rendered)

    # And the published text itself carries no residue.
    for (label, _content, _live), guarded in zip(_LEGACY_ROWS, attacks):
        assert _PLACEHOLDER in guarded, (label, guarded)
        lowered = guarded.lower()
        for leak in ("file://", "api/media", "media:", "/etc/shadow", "127.0.0.1"):
            assert leak not in lowered, (label, leak, guarded)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_loaded_legacy_snapshot_keeps_public_references_renderable(legacy_share):
    """The guard must not destroy a stored PUBLIC asset."""
    payload = load_share(legacy_share)
    public = [m["content"] for m in payload["messages"]][len(_LEGACY_CONTENTS):]
    assert public == _LEGACY_PUBLIC_CONTENTS, "stored public references were altered"

    rendered = _share_page_html(public)
    assert 'src="https://cdn.test/a.png?w=800&amp;fmt=webp"' in rendered[0], rendered[0]
    assert 'href="https://cdn.test/page?x=1&amp;y=2"' in rendered[1], rendered[1]
    assert 'src="https://cdn.test/a.png?next=https://images.example.test/b.png"' in rendered[2], rendered[2]
    assert 'src="https://cdn.test/a.png"' in rendered[3], rendered[3]
    for html in rendered:
        assert _PLACEHOLDER not in html, html


def test_load_path_touches_only_the_snapshot_file(legacy_share, monkeypatch):
    """The load guard must DECIDE, never resolve or read a REFERENCED file.

    A public read of an anonymous share is not a licence to touch the host
    filesystem, and the concrete path inside a legacy snapshot is untrusted
    input. Every ``Path`` method the embed path would use is RECORDED here
    (delegating to the real one, so pytest keeps working), and the assertion is
    that the only path touched during the load is the snapshot JSON itself.
    """
    import pathlib as _pathlib

    snapshot = shares.SHARES_DIR / f"{legacy_share}.json"
    touched: list[tuple[str, str]] = []
    watched = ("resolve", "is_file", "stat", "read_bytes", "expanduser", "open")
    originals = {name: getattr(_pathlib.Path, name) for name in watched}

    def _recorder(name):
        original = originals[name]

        def _wrapped(self, *a, **kw):
            touched.append((name, str(self)))
            return original(self, *a, **kw)

        return _wrapped

    for name in watched:
        monkeypatch.setattr(_pathlib.Path, name, _recorder(name), raising=True)

    payload = load_share(legacy_share)

    for name in watched:  # stop recording before asserting, or pytest's own
        monkeypatch.setattr(_pathlib.Path, name, originals[name], raising=True)

    assert payload is not None
    stray = [(name, p) for name, p in touched if p != str(snapshot)]
    assert not stray, f"load path touched something other than the snapshot: {stray}"
    # The legitimate accesses did happen, so the recorder was actually wired in.
    assert touched, "recorder never fired — the assertion above would be vacuous"
    assert all(
        _PLACEHOLDER in m["content"]
        for m in payload["messages"][: len(_LEGACY_CONTENTS)]
    )


def test_guard_is_idempotent_on_its_own_output():
    """A snapshot written by the CURRENT build must survive re-guarding.

    ``load_share()`` re-guards on every read, so the decision has to be a
    fixed point or an already-published share would degrade over time.
    """
    for content in _LEGACY_CONTENTS + _LEGACY_PUBLIC_CONTENTS:
        once = guard_public_share_references(content)
        assert guard_public_share_references(once) == once, content


def test_guard_placeholders_a_local_media_token_instead_of_embedding(tmp_path):
    """The load guard has no allowed roots, so a local ref is refused.

    A real image inside a real directory still gets a placeholder from the load
    guard: embedding is the CREATE path's job, and the originating session's
    allowed roots are not knowable at load time.
    """
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    out = guard_public_share_references(f"before MEDIA:{png} after")
    assert out == f"before {_PLACEHOLDER} after", out
    assert "base64," not in out
    # The create path with the same root still embeds — the two differ on
    # purpose, and this pins that they do.
    embedded = shares._embed_share_media(
        f"before MEDIA:{png} after", allowed_roots=(tmp_path,)
    )
    assert 'src="data:image/png;base64,' in embedded, embedded


def test_load_drops_a_message_whose_stored_content_is_not_text(tmp_path, monkeypatch):
    """A structured stored ``content`` is not publishable text — drop it.

    ``/api/session/import`` can put a dict where a string belongs, and an older
    snapshot may carry one. Publishing ``str(dict)`` would leak a raw payload.
    """
    store = tmp_path / "shares"
    store.mkdir()
    monkeypatch.setattr(shares, "SHARES_DIR", store)
    token = "structuredtoken"
    (store / f"{token}.json").write_text(json.dumps({
        "token": token,
        "title": "Mixed",
        "messages": [
            {"role": "user", "content": {"secret": "tool payload"}},
            {"role": "assistant", "content": "plain text survives"},
            "not a dict at all",
        ],
        "created_at": 1.0,
        "updated_at": 1.0,
        "revoked_at": None,
    }), encoding="utf-8")

    payload = load_share(token)
    assert [m["content"] for m in payload["messages"]] == ["plain text survives"]
    assert "tool payload" not in json.dumps(payload)


def test_revoked_legacy_share_is_still_refused(legacy_share):
    """Re-guarding must not accidentally resurrect a revoked snapshot."""
    path = shares.SHARES_DIR / f"{legacy_share}.json"
    stored = json.loads(path.read_text())
    stored["revoked_at"] = 123.0
    path.write_text(json.dumps(stored), encoding="utf-8")
    assert load_share(legacy_share) is None
