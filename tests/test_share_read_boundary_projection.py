"""Public-share read boundary: legacy snapshots and scheme-relative descendants.

Two blockers from the 3 September re-gate at ``5e90f1fa``.

1. ``api/shares.py::_guarded_public_message()`` checked only that a row was a
   dict with string ``content``, then returned ``{**message, "content": guarded}``.
   That republished every stored field and every stored role, so a pre-fix
   snapshot could serve ``system`` and ``tool`` rows plus siblings such as
   ``provider_details``, ``tool_calls``, ``raw_result``, and workspace paths
   through the anonymous ``/api/share/<token>`` response.
   ``static/share.js::_shareRenderMessages()`` renders every returned row.

   Measured on the pre-fix code with the fixture below, read through the real
   ``load_share()``: 3 forbidden roles published, 4 unapproved sibling fields
   published, and the strings ``sk-SHOULD-NOT-LEAK``, ``/etc/shadow``, and
   ``/home/samfp/private`` all reached the payload. The stored
   ``message_count`` of 99 survived while only 7 rows were returned.

2. ``api/share_refs.py`` enumerated only literal ``http://`` / ``https://``
   descendants. A scheme-relative descendant such as ``?next=//127.0.0.1/x.png``
   carries no scheme token and matches no marker, so it reached the preserve arm
   and published a live private reference. Browser separator and dot-segment
   forms had the same gap.

   Measured on the pre-fix code: 8 of 20 probe rows reached the preserve arm
   wrongly, including the AWS metadata endpoint ``//169.254.169.254``.

Every test drives the REAL production functions. The legacy fixture is written
straight to the share store as raw JSON, so nothing in the write path can
sanitize it first — that is the whole point of a read-time guard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.shares as shares  # noqa: E402
from api.share_refs import public_reference_hides_local_target  # noqa: E402

APPROVED_KEYS = {"role", "content", "timestamp"}
APPROVED_ROLES = {"user", "assistant"}


# ── Blocker 1: the legacy-snapshot read projection ───────────────────────────


def _legacy_snapshot() -> dict:
    """A snapshot shaped as an older build could have written it.

    Deliberately carries invalid roles, secret-bearing sibling fields, a
    structured title, a stale count, and malformed rows.
    """
    return {
        "title": {"text": "Structured title", "html": "<b>nope</b>"},
        "message_count": 99,
        "created_at": 1_700_000_000.0,
        "session_id": "private-session-id",
        "workspace": "/home/samfp/private",
        "messages": [
            {
                "role": "user",
                "content": "hello",
                "provider_details": {"api_key": "sk-SHOULD-NOT-LEAK"},
                "workspace": "/home/samfp/private",
                "raw_result": {"stdout": "internal"},
            },
            {
                "role": "assistant",
                "content": "hi there",
                "tool_calls": [{"name": "terminal", "args": {"cmd": "cat /etc/shadow"}}],
                "provider_details": {"model": "internal-only"},
            },
            {"role": "system", "content": "SYSTEM PROMPT SHOULD NOT LEAK"},
            {"role": "tool", "content": "TOOL RESULT SHOULD NOT LEAK"},
            {"role": "debug", "content": "debug row"},
            {"role": "user"},
            {"role": "user", "content": ""},
            {"role": "user", "content": "   "},
            "not-a-dict",
            None,
            {"role": "user", "content": "ok", "timestamp": "not-a-number"},
            {"role": "user", "content": "inf ts", "timestamp": float("inf")},
        ],
    }


@pytest.fixture()
def loaded_legacy(tmp_path, monkeypatch):
    """Write the raw legacy snapshot to an isolated store and load it for real."""
    store = tmp_path / "shares"
    store.mkdir()
    monkeypatch.setattr(shares, "SHARES_DIR", store)
    token = "legacytoken"
    (store / f"{token}.json").write_text(json.dumps(_legacy_snapshot()), encoding="utf-8")
    out = shares.load_share(token)
    assert out is not None, "load_share returned None; the fixture never loaded"
    return out


def test_legacy_snapshot_publishes_only_approved_roles(loaded_legacy):
    roles = [m.get("role") for m in loaded_legacy["messages"]]
    bad = sorted({r for r in roles if r not in APPROVED_ROLES})
    assert not bad, (
        f"the anonymous share published forbidden roles {bad}. A stored snapshot "
        "is untrusted: the read path must rebuild each row rather than spread it."
    )
    assert roles == ["user", "assistant", "user", "user"], roles


def test_legacy_snapshot_publishes_no_unapproved_fields(loaded_legacy):
    leaked = sorted(
        {k for m in loaded_legacy["messages"] for k in m if k not in APPROVED_KEYS}
    )
    assert not leaked, (
        f"the anonymous share published unapproved sibling fields {leaked}. "
        "`{**message}` republishes whatever an older writer persisted."
    )


@pytest.mark.parametrize(
    "secret",
    [
        "sk-SHOULD-NOT-LEAK",
        "SHOULD NOT LEAK",
        "/etc/shadow",
        "/home/samfp/private",
        "private-session-id",
        "internal-only",
    ],
)
def test_legacy_snapshot_leaks_no_secret_substring(loaded_legacy, secret):
    blob = json.dumps(loaded_legacy)
    assert secret not in blob, (
        f"{secret!r} reached the anonymous /api/share payload"
    )


def test_legacy_snapshot_recomputes_the_message_count(loaded_legacy):
    """A stored count must never survive row drops.

    The fixture stores 99 while only 4 rows are publishable. Trusting the stored
    value tells the viewer that messages are missing rather than filtered.
    """
    assert loaded_legacy["message_count"] == len(loaded_legacy["messages"])
    assert loaded_legacy["message_count"] == 4


def test_legacy_snapshot_accepts_only_a_string_title(loaded_legacy):
    """A structured title must not be stringified into its repr."""
    title = loaded_legacy["title"]
    assert isinstance(title, str), type(title).__name__
    assert title == "Untitled", (
        f"a non-string stored title must fall back to Untitled, got {title!r}. "
        "str() on a dict publishes dict syntax and every nested value."
    )


def test_legacy_snapshot_drops_non_finite_and_non_numeric_timestamps(loaded_legacy):
    for m in loaded_legacy["messages"]:
        if "timestamp" not in m:
            continue
        ts = m["timestamp"]
        assert isinstance(ts, (int, float)) and not isinstance(ts, bool), ts
        assert ts == ts and ts not in (float("inf"), float("-inf")), ts


def test_read_path_and_write_path_share_one_role_set():
    """The two paths must not drift.

    A read that accepted a wider role set than the write would let a legacy
    snapshot serve rows that a fresh snapshot can never contain.
    """
    assert shares._PUBLIC_SHARE_ROLES == frozenset({"user", "assistant"})
    src = Path(shares.__file__).read_text(encoding="utf-8")
    assert src.count("_PUBLIC_SHARE_ROLES") >= 3, (
        "both _sanitize_message (write) and _guarded_public_message (read) must "
        "consult the shared constant, not an inline literal"
    )
    assert 'role not in {"user", "assistant"}' not in src, (
        "an inline role literal came back; it can drift from the shared constant"
    )


def test_load_path_performs_no_filesystem_reads_of_referenced_files(tmp_path, monkeypatch):
    """The read guard classifies; it must never resolve or embed a local file."""
    store = tmp_path / "shares"
    store.mkdir()
    monkeypatch.setattr(shares, "SHARES_DIR", store)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\nSECRETBYTES")
    snapshot = {
        "title": "legacy",
        "messages": [
            {"role": "user", "content": f"![x](file://{secret})"},
            {"role": "user", "content": f"[y](/api/media?path={secret})"},
        ],
    }
    (store / "fs.json").write_text(json.dumps(snapshot), encoding="utf-8")

    opened: list[str] = []
    real_open = Path.open

    def tracking_open(self, *a, **kw):
        opened.append(str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", tracking_open)
    out = shares.load_share("fs")
    assert out is not None
    assert str(secret) not in json.dumps(out), "the referenced path was published"
    assert not [p for p in opened if p == str(secret)], (
        f"the load path read the referenced file: {opened}"
    )


# ── Blocker 2: scheme-relative and browser-normalized descendants ────────────

_MUST_REFUSE = [
    ("scheme-relative loopback in query", "https://cdn.test/a.png?next=//127.0.0.1/x.png"),
    ("scheme-relative loopback in fragment", "https://cdn.test/a.png#//127.0.0.1/x.png"),
    ("scheme-relative private 10.x", "https://cdn.test/a.png?next=//10.0.0.5/x.png"),
    ("scheme-relative private 192.168", "https://cdn.test/a.png?next=//192.168.1.9/x.png"),
    ("scheme-relative metadata endpoint", "https://cdn.test/a.png?next=//169.254.169.254/latest/meta-data"),
    ("scheme-relative localhost name", "https://cdn.test/a.png?next=//localhost:8080/x.png"),
    ("scheme-relative with port", "https://cdn.test/a.png?next=//127.0.0.1:7777/x.png"),
    ("encoded scheme-relative loopback", "https://cdn.test/a.png?next=%2F%2F127.0.0.1%2Fx.png"),
    ("backslash authenticated route", "https://cdn.test/a.png?next=\\api\\media?path=/etc/shadow"),
    ("backslash scheme-relative", "https://cdn.test/a.png?next=\\\\127.0.0.1\\x.png"),
    ("dot-segment authenticated route", "https://cdn.test/a.png?next=/foo/../api/media?path=/x"),
    ("dot-segment with backslash", "https://cdn.test/a.png?next=\\foo\\..\\api\\media?path=/x"),
]

_MUST_PRESERVE = [
    ("plain public URL", "https://cdn.test/a.png"),
    ("public with harmless query", "https://cdn.test/a.png?v=2"),
    ("public wrapping a public absolute", "https://cdn.test/a.png?next=https://images.example.test/b.png"),
    ("public wrapping a public scheme-relative", "https://cdn.test/a.png?next=//images.example.test/b.png"),
    ("encoded percent literal", "https://cdn.test/a.png?pct=100%25"),
    ("query value containing dots", "https://cdn.test/a.png?v=1.2.3"),
]


@pytest.mark.parametrize("label,value", _MUST_REFUSE, ids=[c[0] for c in _MUST_REFUSE])
def test_private_descendant_is_refused(label, value):
    assert public_reference_hides_local_target(value) is True, (
        f"{label}: {value!r} reached the preserve arm. A browser resolves this "
        "against the containing scheme, so it reaches the private host exactly "
        "as an absolute URL would."
    )


@pytest.mark.parametrize("label,value", _MUST_PRESERVE, ids=[c[0] for c in _MUST_PRESERVE])
def test_public_reference_is_preserved(label, value):
    """Over-blocking a genuine public asset is also a failure.

    These are the discriminating controls: a classifier that refused every
    scheme-relative form would pass every refusal test above and still be wrong.
    """
    assert public_reference_hides_local_target(value) is False, (
        f"{label}: {value!r} was refused, but a public host is not a local target"
    )


def test_scheme_relative_candidate_inherits_the_containing_scheme():
    """An ``http`` parent must classify its descendant as ``http``, not ``https``."""
    from api.share_refs import _scheme_relative_candidates

    found = _scheme_relative_candidates("/a.png?next=//127.0.0.1/x.png")
    assert found == ["//127.0.0.1/x.png"], found
    assert public_reference_hides_local_target("http://cdn.test/a.png?next=//127.0.0.1/x.png") is True


def test_browser_normalize_collapses_only_the_path():
    """A ``..`` inside a query value is not a path segment.

    Rewriting it would change the bytes a preserved public URL is compared
    against, which breaks the byte-for-byte preservation contract.
    """
    from api.share_refs import _browser_normalize

    assert _browser_normalize("/foo/../api/media?path=/x") == "/api/media?path=/x"
    assert _browser_normalize("\\api\\media?path=/x") == "/api/media?path=/x"
    # The query is left alone.
    assert _browser_normalize("/a.png?v=1/../2") == "/a.png?v=1/../2"


def test_extra_slashes_still_name_a_host():
    """``///host`` is a live host reference, not an authority-less path.

    Verified against a real URL parser: ``new URL('///127.0.0.1/x.png',
    'https://cdn.test/a.png')`` resolves to host ``127.0.0.1``, because extra
    leading slashes are collapsed. So a triple slash must be enumerated as a
    candidate, exactly like a double slash.

    An earlier version of this test asserted the opposite and was wrong. The
    code was right.
    """
    from api.share_refs import _scheme_relative_candidates

    # No authority at all: nothing to reach.
    assert _scheme_relative_candidates("/a.png?next=//") == []
    # Extra slashes collapse onto a real host, so this IS a candidate.
    assert _scheme_relative_candidates("/a.png?next=///x") == ["//x"]
    assert public_reference_hides_local_target(
        "https://cdn.test/a.png?next=///127.0.0.1/x.png"
    ) is True
