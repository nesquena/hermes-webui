"""#8590: public-share embeds must not follow a symlink planted inside a root.

The attachment root is trusted configuration: it is resolved once (at server
startup) and later walked no-follow, so a symlink planted afterwards -- e.g.
inside a session workspace that contains the attachment root -- cannot move
it outside.  A root that cannot be resolved (symlink loop, EINVAL from a
concurrent swap) is dropped instead of failing the share with a 500.

Legitimately symlinked configurations (a symlinked HERMES_HOME under a HOME
workspace, a symlinked workspace, a symlinked attachment override, macOS-style
``/tmp -> /private/tmp``) must keep embedding.

Out of scope (separate issue): a WORKSPACE whose own path runs through
attacker-writable space; ``Session`` re-resolves it by name on every load.
"""
from __future__ import annotations

import base64
import errno
import mimetypes
import os
import pathlib
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import shares, upload

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
mimetypes.init()

PNG = b"\x89PNG\r\n\x1a\n"
INSIDE = PNG + b"INSIDE-ROOT" + b"\0" * 16
OUTSIDE = PNG + b"OUTSIDE-SECRET" + b"\0" * 16
_DATA_RE = re.compile(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)")


def _payloads(out: str) -> list[bytes]:
    return [base64.b64decode(m) for m in _DATA_RE.findall(out)]


def _leaks(out: str) -> bool:
    return any(b"OUTSIDE-SECRET" in p for p in _payloads(out))


def _embed(ref: str, roots) -> str:
    return shares._embed_share_media(ref, allowed_roots=tuple(roots))


def _put(d: Path, data: bytes) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "img.png").write_bytes(data)


def _swap_to(path: Path, target: Path) -> None:
    os.rename(path, path.with_name(path.name + ".saved"))
    os.symlink(target, path)


def _share(ws, text: str) -> str:
    session = SimpleNamespace(
        workspace=str(ws), messages=[{"role": "assistant", "content": text}], title="t"
    )
    return shares.build_share_snapshot(session)["messages"][0]["content"]


def _layout(base: Path, monkeypatch, kind: str):
    """(workspace, attachment root, swap) with the attachment root nested in
    the workspace, configured like production: via the override env var or
    the default ``STATE_DIR / "attachments"``."""
    ws = base / "home"
    out = base / "outside"
    att = ws / ".hermes" / "webui" / "attachments"
    _put(att, INSIDE)
    _put(out / "attachments", OUTSIDE)
    if kind.startswith("default"):
        monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
        monkeypatch.setattr(upload, "STATE_DIR", att.parent)
    else:
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    if kind.endswith("ancestor"):
        return ws, att, lambda: _swap_to(ws / ".hermes" / "webui", out)
    return ws, att, lambda: _swap_to(att, out / "attachments")


LAYOUTS = ["override-ancestor", "override-root", "default-ancestor", "default-root"]


def _ref(kind: str, base: Path, att: Path) -> str:
    return {
        "relative": "MEDIA:img.png",
        "absolute-configured": f"MEDIA:{att}/img.png",
        "absolute-target": f"MEDIA:{base}/outside/attachments/img.png",
    }[kind]


REFS = ["relative", "absolute-configured", "absolute-target"]


# --- Static redirect through the real build_share_snapshot ------------------


@pytest.mark.parametrize("ref_kind", REFS)
@pytest.mark.parametrize("layout", LAYOUTS)
def test_share_attachment_root_redirected_inside_workspace_never_embeds_outside(
    tmp_path, monkeypatch, layout, ref_kind
):
    ws, att, swap = _layout(tmp_path, monkeypatch, layout)
    # The server shared from this configuration before the symlink appeared
    # (non-vacuous: the attachment embeds).
    assert _payloads(_share(ws, "MEDIA:img.png")) == [INSIDE]
    swap()
    out = _share(ws, _ref(ref_kind, tmp_path, att))
    assert not _leaks(out), out[:120]
    assert out == shares._PLACEHOLDER


# --- Swap before every filesystem step of a share, incl. root resolution ----


class _Hook:
    NAMES = ("lstat", "readlink", "stat", "open", "fstat", "read")

    def __init__(self, fire_at=None, action=None):
        self.fire_at, self.action, self.fired, self.events = fire_at, action, False, []
        self.owner = threading.get_ident()

    def install(self, mp):
        for name in self.NAMES:
            real = getattr(os, name)

            def wrapper(*a, _real=real, _name=name, **k):
                if threading.get_ident() == self.owner:
                    i = len(self.events)
                    self.events.append(_name)
                    if i == self.fire_at and not self.fired:
                        self.fired = True
                        self.action()
                return _real(*a, **k)

            mp.setattr(os, name, wrapper)
            if real in os.supports_dir_fd:
                mp.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {wrapper})
            if real in os.supports_follow_symlinks:
                mp.setattr(os, "supports_follow_symlinks", set(os.supports_follow_symlinks) | {wrapper})


@pytest.mark.parametrize("ref_kind", REFS)
def test_share_swap_at_every_step_including_resolution(tmp_path, monkeypatch, ref_kind):
    def run(base, fire_at=None):
        with monkeypatch.context() as cfg:
            ws, att, swap = _layout(base, cfg, "override-ancestor")
            assert _payloads(_share(ws, "MEDIA:img.png")) == [INSIDE]  # earlier share
            hook = _Hook(fire_at, swap)
            with monkeypatch.context() as mp:
                hook.install(mp)
                try:
                    out = _share(ws, _ref(ref_kind, base, att))
                except Exception as exc:  # noqa: BLE001 - an escape is itself a failure
                    out = f"EXC {type(exc).__name__}"
        return hook, out

    dry, out = run(tmp_path / "dry")
    if ref_kind != "absolute-target":
        assert _payloads(out) == [INSIDE], out[:120]
    bad = []
    for n in range(len(dry.events)):
        hook, out = run(tmp_path / f"run{n}", n)
        assert hook.fired
        if out.startswith("EXC") or _leaks(out):
            bad.append((n, dry.events[n], out[:24]))
    assert len(dry.events) > 5
    assert not bad, f"leaks/exceptions when swapped before step: {bad}"


# --- Unresolvable roots fail closed, never escape ---------------------------


def test_symlink_loop_root_is_dropped_and_other_root_still_serves(tmp_path):
    ws = tmp_path / "ws"
    _put(ws, INSIDE)
    loop = ws / "loop"
    os.symlink(loop, loop)
    assert _payloads(_embed("MEDIA:img.png", [ws, loop])) == [INSIDE]
    assert _embed("MEDIA:img.png", [loop]) == shares._PLACEHOLDER


def test_resolve_einval_on_one_root_is_dropped(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _put(ws, INSIDE)
    inner = ws / "inbox" / "att"
    inner.mkdir(parents=True)
    real = pathlib.Path.resolve

    def resolve(self, strict=False):
        if str(self) == str(inner):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real(self, strict=strict)

    monkeypatch.setattr(pathlib.Path, "resolve", resolve)
    assert _payloads(_embed("MEDIA:img.png", [ws, inner])) == [INSIDE]


def test_share_with_symlink_loop_workspace_is_placeholder_not_error(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    loop = tmp_path / "loop"
    os.symlink(loop, loop)
    assert _share(loop, "MEDIA:img.png") == shares._PLACEHOLDER


def test_share_with_einval_workspace_still_serves_attachments(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    att = tmp_path / "att"
    _put(att, INSIDE)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    assert _payloads(_share(ws, "MEDIA:img.png")) == [INSIDE]
    real = pathlib.Path.resolve

    def resolve(self, strict=False):
        if str(self) == str(ws):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real(self, strict=strict)

    monkeypatch.setattr(pathlib.Path, "resolve", resolve)
    assert _payloads(_share(ws, "MEDIA:img.png")) == [INSIDE]


def test_share_with_symlink_loop_attachment_root_drops_it(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _put(ws, INSIDE)
    loop = tmp_path / "att-loop"
    os.symlink(loop, loop)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(loop))
    assert _payloads(_share(ws, "MEDIA:img.png")) == [INSIDE]
    assert _share(ws, f"MEDIA:{loop}/img.png") == shares._PLACEHOLDER


# --- Legitimate configurations keep embedding (green before and after) -------


LEGIT = PNG + b"LEGIT-ATTACHMENT" + b"\0" * 16


def _symlinked_hermes_home(tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "data" / "hermes"
    att = data / "webui" / "attachments"
    _put(att, LEGIT)
    home.mkdir()
    os.symlink(data, home / ".hermes")  # ~/.hermes -> /data/hermes
    return home, att


def test_symlinked_hermes_home_under_home_workspace_direct_all_spellings(tmp_path):
    # The auditor's probe: workspace = HOME, attachments under the symlinked
    # ~/.hermes, every ref spelling embeds.
    home, att = _symlinked_hermes_home(tmp_path)
    roots = [home, home / ".hermes" / "webui" / "attachments"]
    for ref in ("MEDIA:img.png", f"MEDIA:{home}/.hermes/webui/attachments/img.png", f"MEDIA:{att}/img.png"):
        assert _payloads(_embed(ref, roots)) == [LEGIT], ref


@pytest.mark.parametrize("config", ["override-through-link", "default-state-dir"])
def test_symlinked_hermes_home_under_home_workspace_share(tmp_path, monkeypatch, config):
    home, att = _symlinked_hermes_home(tmp_path)
    if config == "default-state-dir":
        # config.py resolves STATE_DIR at import, so it is the target spelling.
        monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
        monkeypatch.setattr(upload, "STATE_DIR", att.parent)
    else:
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(home / ".hermes" / "webui" / "attachments"))
    for ref in ("MEDIA:img.png", f"MEDIA:{att}/img.png"):
        assert _payloads(_share(home, ref)) == [LEGIT], ref


def test_symlinked_workspace_root_still_embeds(tmp_path, monkeypatch):
    real = tmp_path / "data" / "ws"
    _put(real, LEGIT)
    link = tmp_path / "home" / "ws"
    link.parent.mkdir()
    os.symlink(real, link)  # ~/ws -> /data/ws
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path / "att"))
    for ref in ("MEDIA:img.png", f"MEDIA:{link}/img.png", f"MEDIA:{real}/img.png"):
        assert _payloads(_share(link, ref)) == [LEGIT], ref


def test_attachment_override_through_symlink_outside_workspace_still_embeds(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    real = tmp_path / "data" / "att"
    _put(real, LEGIT)
    os.symlink(real, tmp_path / "att-link")
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path / "att-link"))
    for ref in ("MEDIA:img.png", f"MEDIA:{real}/img.png"):
        assert _payloads(_share(ws, ref)) == [LEGIT], ref


@pytest.mark.parametrize("nested", [True, False])
def test_symlinked_ancestor_of_both_roots_still_embeds(tmp_path, monkeypatch, nested):
    # macOS /tmp -> /private/tmp shape: both roots configured via the link.
    private = tmp_path / "private" / "tmp"
    private.mkdir(parents=True)
    os.symlink(private, tmp_path / "tmp")
    ws = tmp_path / "tmp" / "ws"
    att = (ws / "up") if nested else (tmp_path / "tmp" / "hermes" / "attachments")
    _put(private / att.relative_to(tmp_path / "tmp"), LEGIT)
    (private / "ws").mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    real_att = private / att.relative_to(tmp_path / "tmp")
    for ref in ("MEDIA:img.png", f"MEDIA:{real_att}/img.png"):
        assert _payloads(_share(ws, ref)) == [LEGIT], ref
    assert _payloads(_embed("MEDIA:img.png", [ws, real_att])) == [LEGIT]


@pytest.mark.parametrize("nested", [True, False])
def test_default_layout_still_embeds(tmp_path, monkeypatch, nested):
    ws = tmp_path / "home"
    state = (ws / ".hermes" / "webui") if nested else (tmp_path / "state" / "webui")
    _put(state / "attachments", LEGIT)
    ws.mkdir(exist_ok=True)
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    monkeypatch.setattr(upload, "STATE_DIR", state)
    for ref in ("MEDIA:img.png", f"MEDIA:{state}/attachments/img.png"):
        assert _payloads(_share(ws, ref)) == [LEGIT], ref
