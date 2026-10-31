"""Regression tests for #6126: public-share image reads must be one file object.

``_embed_share_media`` used to resolve + allow-list a pathname, ``stat()`` it,
and then reopen the same pathname with ``read_bytes()``.  Those were three
separate filesystem lookups, so a concurrent writer inside an allowed root
could swap an intermediate directory (or the file itself) for a symlink to an
outside location between validation and read, and the public snapshot would
embed the OUTSIDE bytes.

The fix walks from an opened allowed-root directory descriptor one component
at a time with ``O_NOFOLLOW`` and validates (``fstat``) and reads (bounded)
the SAME final descriptor.  Where that primitive is unavailable the share
embedder fails closed.

The race tests below are deterministic: they intercept every filesystem
primitive the embedder calls (``os.open/stat/lstat/fstat/read`` and
``io.open``) and perform the swap immediately before the N-th call, for EVERY
N observed in an unswapped dry run.  No timing, no threads.
"""

from __future__ import annotations

import base64
import contextlib
import io
import mimetypes
import os
import socket as socket_mod
import re
import sys
import threading
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from api import shares

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX symlink / descriptor semantics"
)

# mimetypes lazily reads system mime.types files on first use; warm it so the
# FS-event counts below are identical between the dry run and the swap runs.
mimetypes.init()

PNG = b"\x89PNG\r\n\x1a\n"
INSIDE = PNG + b"INSIDE-ROOT-IMAGE" + b"\x00" * 32
OUTSIDE_MARK = b"OUTSIDE-SECRET-BYTES"
OUTSIDE = PNG + OUTSIDE_MARK + b"\x00" * 32

_DATA_RE = re.compile(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)")


def _payloads(out: str) -> list[bytes]:
    return [base64.b64decode(m) for m in _DATA_RE.findall(out)]


def _embed(text: str, roots) -> str:
    return shares._embed_share_media(text, allowed_roots=tuple(roots))


class _FsEvents:
    """Intercept the filesystem primitives the embedder can use.

    Records each call made by THIS thread (other threads in a shared pytest
    process pass straight through, so counts stay deterministic); when
    ``fire_at`` is set, runs ``action`` immediately BEFORE the
    ``fire_at``-th call executes (a deterministic race window).
    """

    OS_NAMES = ("open", "stat", "lstat", "fstat", "read")

    def __init__(self, fire_at: int | None = None, action=None):
        self.fire_at = fire_at
        self.action = action
        self.fired = False
        self.events: list[str] = []
        self.owner = threading.get_ident()

    def _wrap(self, label, real):
        def wrapper(*args, **kwargs):
            if threading.get_ident() != self.owner:
                return real(*args, **kwargs)
            index = len(self.events)
            self.events.append(label)
            if self.fire_at == index and not self.fired:
                self.fired = True
                self.action()
            return real(*args, **kwargs)

        return wrapper

    @contextlib.contextmanager
    def installed(self, monkeypatch):
        with monkeypatch.context() as mp:
            for name in self.OS_NAMES:
                real = getattr(os, name)
                wrapped = self._wrap(f"os.{name}", real)
                mp.setattr(os, name, wrapped)
                if name == "open" and real in os.supports_dir_fd:
                    # The wrapper forwards dir_fd= to the real os.open, so it
                    # genuinely supports it; keep the platform capability set
                    # truthful for the wrapped function.
                    mp.setattr(
                        os, "supports_dir_fd", set(os.supports_dir_fd) | {wrapped}
                    )
            mp.setattr(io, "open", self._wrap("io.open", io.open))
            yield self


def _build_tree(base: Path) -> dict:
    ws = base / "workspace"
    slot = ws / "slot"
    slot.mkdir(parents=True)
    (slot / "image.png").write_bytes(INSIDE)
    outside = base / "outside"
    outside.mkdir()
    (outside / "image.png").write_bytes(OUTSIDE)
    return {"ws": ws, "slot": slot, "outside": outside}


def _swap_dir(tree):
    def action():
        os.rename(tree["slot"], tree["ws"] / "slot.orig")
        os.symlink(tree["outside"], tree["slot"])

    return action


def _swap_file(tree):
    def action():
        os.rename(tree["slot"] / "image.png", tree["slot"] / "image.orig.png")
        os.symlink(tree["outside"] / "image.png", tree["slot"] / "image.png")

    return action


_SWAPS = {"intermediate-dir": _swap_dir, "final-file": _swap_file}


def _ref(kind: str, tree) -> str:
    if kind == "relative":
        return "MEDIA:slot/image.png"
    return f"MEDIA:{tree['ws']}/slot/image.png"


@pytest.mark.parametrize("ref_kind", ["relative", "absolute"])
@pytest.mark.parametrize("swap", sorted(_SWAPS))
def test_symlink_swap_at_every_fs_step_never_embeds_outside_bytes(
    tmp_path, monkeypatch, ref_kind, swap
):
    # Dry run: the unswapped tree embeds the in-root image (non-vacuous) and
    # tells us how many filesystem steps the embedder takes.
    dry_tree = _build_tree(tmp_path / "dry")
    dry = _FsEvents()
    with dry.installed(monkeypatch):
        out = _embed(_ref(ref_kind, dry_tree), [dry_tree["ws"]])
    assert _payloads(out) == [INSIDE], out[:200]
    assert dry.events, "embedder performed no filesystem steps"

    leaks = []
    for n in range(len(dry.events)):
        tree = _build_tree(tmp_path / f"run{n}")
        hook = _FsEvents(fire_at=n, action=_SWAPS[swap](tree))
        with hook.installed(monkeypatch):
            out = _embed(_ref(ref_kind, tree), [tree["ws"]])
        assert hook.fired, f"swap point {n} ({dry.events[n]}) never reached"
        payloads = _payloads(out)
        if any(OUTSIDE_MARK in p for p in payloads):
            leaks.append((n, dry.events[n]))
            continue
        # Either the original in-root file object or a fail-closed placeholder.
        assert out == shares._PLACEHOLDER or payloads == [INSIDE], (n, out[:200])
    assert not leaks, f"outside bytes embedded when swapped before: {leaks}"


def test_file_growing_past_limit_after_validation_is_never_embedded(
    tmp_path, monkeypatch
):
    limit = shares._SHARE_EMBED_MAX_BYTES

    def make(base: Path) -> Path:
        base.mkdir()
        (base / "grow.png").write_bytes(INSIDE)
        return base

    dry_root = make(tmp_path / "dry")
    dry = _FsEvents()
    with dry.installed(monkeypatch):
        out = _embed("MEDIA:grow.png", [dry_root])
    assert _payloads(out) == [INSIDE]

    oversized = []
    for n in range(len(dry.events)):
        root = make(tmp_path / f"run{n}")

        def grow(root=root):
            with open(root / "grow.png", "ab") as fh:
                fh.write(b"\x00" * (limit + 4096))

        hook = _FsEvents(fire_at=n, action=grow)
        with hook.installed(monkeypatch):
            out = _embed("MEDIA:grow.png", [root])
        assert hook.fired, f"growth point {n} ({dry.events[n]}) never reached"
        sizes = [len(p) for p in _payloads(out)]
        if any(size > limit for size in sizes):
            oversized.append((n, dry.events[n], max(sizes)))
            continue
        assert out == shares._PLACEHOLDER or _payloads(out) == [INSIDE], (n, out[:120])
    assert not oversized, f"embedded more than the byte cap: {oversized}"


# --- Static symlink components (no race) ------------------------------------


@pytest.fixture()
def tree(tmp_path):
    t = _build_tree(tmp_path)
    real = t["ws"] / "real"
    real.mkdir()
    (real / "img.png").write_bytes(INSIDE)
    (t["ws"] / "real.png").write_bytes(INSIDE)
    os.symlink("real", t["ws"] / "alias")  # in-root dir alias
    os.symlink("real.png", t["ws"] / "link.png")  # in-root file alias
    os.symlink(t["outside"], t["ws"] / "escape")  # dir escaping the root
    return t


@pytest.mark.parametrize(
    "ref",
    ["alias/img.png", "link.png", "escape/image.png"],
)
def test_symlinked_component_is_refused_relative(tree, ref):
    out = _embed(f"MEDIA:{ref}", [tree["ws"]])
    assert out == shares._PLACEHOLDER


@pytest.mark.parametrize(
    "ref",
    ["alias/img.png", "link.png", "escape/image.png"],
)
def test_symlinked_component_is_refused_absolute(tree, ref):
    out = _embed(f"MEDIA:{tree['ws']}/{ref}", [tree["ws"]])
    assert out == shares._PLACEHOLDER


def test_absolute_ref_through_outside_alias_of_root_is_refused(tree, tmp_path):
    os.symlink(tree["ws"], tmp_path / "ws-alias")
    out = _embed(f"MEDIA:{tmp_path}/ws-alias/real.png", [tree["ws"]])
    assert out == shares._PLACEHOLDER


def test_dotdot_component_is_refused_even_when_lexically_inside(tree):
    assert _embed("MEDIA:slot/../real.png", [tree["ws"]]) == shares._PLACEHOLDER
    assert (
        _embed(f"MEDIA:{tree['ws']}/slot/../real.png", [tree["ws"]])
        == shares._PLACEHOLDER
    )


def test_root_configured_through_symlink_is_still_served(tree, tmp_path):
    # The allowed root itself is trusted configuration: a workspace configured
    # through a symlinked spelling keeps working, for both spellings.
    os.symlink(tree["ws"], tmp_path / "ws-link")
    roots = [tmp_path / "ws-link"]
    for ref in (
        "MEDIA:real.png",
        f"MEDIA:{tmp_path}/ws-link/real.png",
        f"MEDIA:{tree['ws']}/real.png",
    ):
        assert _payloads(_embed(ref, roots)) == [INSIDE], ref


# --- Normal images keep working ---------------------------------------------


_FORMATS = {
    "a.png": (PNG + b"\x00" * 16, "image/png"),
    "b.jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg"),
    "c.gif": (b"GIF89a" + b"\x00" * 16, "image/gif"),
    "d.webp": (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16, "image/webp"),
}


@pytest.mark.parametrize("name", sorted(_FORMATS))
def test_normal_images_are_embedded_byte_exact(tmp_path, name):
    data, mime = _FORMATS[name]
    ws = tmp_path / "ws"
    (ws / "deep" / "er").mkdir(parents=True)
    (ws / name).write_bytes(data)
    (ws / "deep" / "er" / name).write_bytes(data)
    for ref in (
        name,
        f"./{name}",
        f"deep/er/{name}",
        f"deep//er/./{name}",
        f"{ws}/{name}",
        f"{ws}/deep/er/{name}",
    ):
        out = _embed(f"MEDIA:{ref}", [ws])
        assert f"data:{mime};base64," in out, ref
        assert _payloads(out) == [data], ref
        assert f'alt="{name}"' in out, ref


def test_second_allowed_root_is_searched_for_relative_refs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "up.png").write_bytes(INSIDE)
    assert _payloads(_embed("MEDIA:up.png", [ws, att])) == [INSIDE]
    assert _payloads(_embed(f"MEDIA:{att}/up.png", [ws, att])) == [INSIDE]


def test_non_regular_files_are_refused_without_blocking(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dir.png").mkdir()
    assert _embed("MEDIA:dir.png", [ws]) == shares._PLACEHOLDER

    fifo = ws / "pipe.png"
    os.mkfifo(fifo)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("out", _embed("MEDIA:pipe.png", [ws])),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        # Release a reader blocked in open() so the daemon thread can exit.
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        worker.join(timeout=5)
        pytest.fail("opening a FIFO in an allowed root blocked the share builder")
    assert result["out"] == shares._PLACEHOLDER


# --- Empty roots / unsupported platform: fail closed, touch nothing ---------


def test_empty_allowed_roots_refuse_without_opening_anything(tree, monkeypatch):
    hook = _FsEvents()
    with hook.installed(monkeypatch):
        assert _embed("MEDIA:real.png", []) == shares._PLACEHOLDER
        assert _embed(f"MEDIA:{tree['ws']}/real.png", []) == shares._PLACEHOLDER
    assert not [e for e in hook.events if e.endswith("open")], hook.events


def _assert_refuses_without_opening(tree, monkeypatch):
    hook = _FsEvents()
    with hook.installed(monkeypatch):
        for ref in ("real.png", f"{tree['ws']}/real.png"):
            assert _embed(f"MEDIA:{ref}", [tree["ws"]]) == shares._PLACEHOLDER, ref
    assert not [e for e in hook.events if e.endswith("open")], hook.events


def test_platform_without_dir_fd_open_fails_closed(tree, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    _assert_refuses_without_opening(tree, monkeypatch)


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_platform_without_required_open_flag_fails_closed(tree, monkeypatch, flag):
    monkeypatch.delattr(os, flag)
    _assert_refuses_without_opening(tree, monkeypatch)


def test_capability_check_false_fails_closed(tree, monkeypatch):
    monkeypatch.setattr(
        shares, "_share_nofollow_open_supported", lambda: False, raising=False
    )
    _assert_refuses_without_opening(tree, monkeypatch)


def test_inline_data_uri_is_unaffected_by_unsupported_platform(monkeypatch):
    # The data: branch never reaches the descriptor reader, so the platform
    # switch cannot change its result. Since #8465/#8477 a content-supplied
    # data: image is placeholdered in public shares (never embedded), which is
    # the same answer a supported platform gives.
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    b64 = base64.b64encode(INSIDE).decode("ascii")
    out = _embed(f"MEDIA:data:image/png;base64,{b64}", [])
    assert out == shares._INLINE_DATA_PLACEHOLDER
    assert _payloads(out) == []
    monkeypatch.undo()
    assert _embed(f"MEDIA:data:image/png;base64,{b64}", []) == out


# --- Descriptor hygiene -------------------------------------------------------


def _fds_into(base: Path) -> list[str]:
    """Open descriptors of this process that point into *base* (Linux)."""
    found = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target == str(base) or target.startswith(str(base) + os.sep):
            found.append(target)
    return found


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_no_descriptor_leak_across_success_and_refusal_paths(tree, tmp_path):
    refs = [
        "slot/image.png",
        "alias/img.png",
        "link.png",
        "escape/image.png",
        "missing/none.png",
        "slot/missing.png",
        "slot",
        "real.png",
    ]
    assert _fds_into(tmp_path) == []
    for _ in range(25):
        for ref in refs:
            _embed(f"MEDIA:{ref}", [tree["ws"], tree["outside"]])
            _embed(f"MEDIA:{tree['ws']}/{ref}", [tree["ws"], tree["outside"]])
    assert _fds_into(tmp_path) == []


# --- Codex round 1 (#6126) ----------------------------------------------------
#
# R1-1  The allowed root is now opened from "/" one component at a time with
#       O_NOFOLLOW, so an ANCESTOR of a nested root (an attachments root inside
#       a writable workspace) swapped for a symlink after root resolution can no
#       longer redirect the root descriptor outside.


def _nested_root_tree(base: Path) -> dict:
    ws = base / "workspace"
    inbox = ws / "inbox"
    att = inbox / "attachments"
    att.mkdir(parents=True)
    (att / "img.png").write_bytes(INSIDE)
    outside = base / "outside"
    (outside / "attachments").mkdir(parents=True)
    (outside / "attachments" / "img.png").write_bytes(OUTSIDE)
    return {"ws": ws, "inbox": inbox, "att": att, "outside": outside}


def _swap_inbox(tree):
    def action():
        os.rename(tree["inbox"], tree["ws"] / "inbox.saved")
        os.symlink(tree["outside"], tree["inbox"])

    return action


def _nested_roots(kind: str, tree) -> list[Path]:
    return [tree["att"]] if kind == "attachments-only" else [tree["ws"], tree["att"]]


def _nested_ref(kind: str, tree) -> str:
    return "MEDIA:img.png" if kind == "relative" else f"MEDIA:{tree['att']}/img.png"


@pytest.mark.parametrize("ref_kind", ["relative", "absolute"])
@pytest.mark.parametrize("roots_kind", ["attachments-only", "workspace+attachments"])
def test_nested_root_ancestor_swap_after_resolution_never_embeds_outside_bytes(
    tmp_path, monkeypatch, ref_kind, roots_kind
):
    # Dry run: unswapped, the nested-root image embeds (non-vacuous) and we
    # learn every filesystem step.  Root resolution (realpath's lstat calls)
    # happens before the first os.open; a swap BEFORE resolution redirects
    # the trusted configuration itself (pre-existing policy, out of scope), so
    # the swap is applied at every step from the first open onwards.
    dry_tree = _nested_root_tree(tmp_path / "dry")
    dry = _FsEvents()
    with dry.installed(monkeypatch):
        out = _embed(_nested_ref(ref_kind, dry_tree), _nested_roots(roots_kind, dry_tree))
    assert _payloads(out) == [INSIDE], out[:200]
    first_open = dry.events.index("os.open")

    leaks = []
    for n in range(first_open, len(dry.events)):
        tree = _nested_root_tree(tmp_path / f"run{n}")
        hook = _FsEvents(fire_at=n, action=_swap_inbox(tree))
        with hook.installed(monkeypatch):
            out = _embed(_nested_ref(ref_kind, tree), _nested_roots(roots_kind, tree))
        assert hook.fired, f"swap point {n} ({dry.events[n]}) never reached"
        payloads = _payloads(out)
        if any(OUTSIDE_MARK in p for p in payloads):
            leaks.append((n, dry.events[n]))
            continue
        assert out == shares._PLACEHOLDER or payloads == [INSIDE], (n, out[:200])
    assert not leaks, f"outside bytes embedded when swapped before: {leaks}"


@pytest.mark.parametrize("ref_kind", ["relative", "absolute"])
def test_nested_root_ancestor_already_swapped_is_placeholder(tmp_path, monkeypatch, ref_kind):
    # Deterministic single-shot shape of the Codex probe: resolve the roots,
    # then swap workspace/inbox for a symlink before the reader's first open.
    tree = _nested_root_tree(tmp_path)
    swap = _swap_inbox(tree)
    real_open = os.open
    fired = []
    owner = threading.get_ident()

    def open_after_swap(*args, **kwargs):
        if threading.get_ident() == owner and not fired:
            fired.append(True)
            swap()
        return real_open(*args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(os, "open", open_after_swap)
        mp.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {open_after_swap})
        out = _embed(_nested_ref(ref_kind, tree), [tree["ws"], tree["att"]])
    assert fired, "the reader never opened anything"
    assert OUTSIDE_MARK not in b"".join(_payloads(out))
    assert out == shares._PLACEHOLDER, out[:200]


def test_root_with_symlinked_ancestor_is_still_served(tmp_path):
    # Configured roots are resolved first, then walked no-follow: a symlink
    # ANYWHERE in the configured spelling of the root keeps working.
    real = tmp_path / "real-parent"
    (real / "ws" / "sub").mkdir(parents=True)
    (real / "ws" / "sub" / "img.png").write_bytes(INSIDE)
    os.symlink(real, tmp_path / "parent-link")
    root = tmp_path / "parent-link" / "ws"
    for ref in (
        "MEDIA:sub/img.png",
        f"MEDIA:{root}/sub/img.png",
        f"MEDIA:{real}/ws/sub/img.png",
    ):
        assert _payloads(_embed(ref, [root])) == [INSIDE], ref


# R1-2  A malformed reference (lone surrogate in a ``~user`` name or in a
#       path) is a placeholder, never an exception that fails the whole share
#       snapshot (the route would turn it into HTTP 400).

_MALFORMED_REFS = [
    "~\ud800/img.png",  # expanduser -> pwd.getpwnam -> UnicodeEncodeError
    "~\ud800",
    "\ud800.png",  # relative: os.open(name) -> UnicodeEncodeError
    "sub\ud800/img.png",
]


@pytest.mark.parametrize("ref", _MALFORMED_REFS)
def test_malformed_ref_is_placeholder(tmp_path, ref):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _embed(f"see MEDIA:{ref}", [ws]) == "see " + shares._PLACEHOLDER
    absolute = f"MEDIA:{ws}/{ref.lstrip('~')}"
    assert _embed(absolute, [ws]) == shares._PLACEHOLDER


def test_malformed_tilde_ref_does_not_fail_share_snapshot(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.png").write_bytes(INSIDE)
    session = SimpleNamespace(
        workspace=str(ws),
        title="malformed media ref",
        messages=[
            {"role": "user", "content": "share it"},
            {"role": "assistant", "content": "bad MEDIA:~\ud800/img.png good MEDIA:ok.png"},
        ],
    )
    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][-1]["content"]
    assert content.startswith("bad " + shares._PLACEHOLDER + " good <img "), content[:200]
    assert _payloads(content) == [INSIDE]


# R1-3  Permission denied below a root is TERMINAL: the ref becomes the
#       placeholder and a namesake in a later root is never substituted.

_as_root = hasattr(os, "geteuid") and os.geteuid() == 0
needs_non_root = pytest.mark.skipif(_as_root, reason="root bypasses DAC permission bits")


@contextlib.contextmanager
def _chmod(path: Path, mode: int):
    old = path.stat().st_mode & 0o7777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(old)


@needs_non_root
@pytest.mark.parametrize(
    "denied", ["final-file", "intermediate-dir"]
)
def test_permission_denied_in_first_root_never_substitutes_later_root(tmp_path, denied):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    for root, payload in ((root_a, INSIDE), (root_b, OUTSIDE)):
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "img.png").write_bytes(payload)
    target = root_a / "sub" / "img.png" if denied == "final-file" else root_a / "sub"
    with _chmod(target, 0):
        out = _embed("MEDIA:sub/img.png", [root_a, root_b])
    assert OUTSIDE_MARK not in b"".join(_payloads(out)), "root B namesake substituted"
    assert out == shares._PLACEHOLDER, out[:200]
    # Control: a plain miss in root A still falls through to root B.
    (root_a / "sub" / "img.png").unlink()
    assert _payloads(_embed("MEDIA:sub/img.png", [root_a, root_b])) == [OUTSIDE]


# R1-4  Directory descriptors only need SEARCH permission: on Linux they are
#       O_PATH descriptors, so an image under an execute-only (0111) directory
#       (or root) still embeds, as it did with pathname reads.

_linux_o_path = os.uname().sysname == "Linux" and hasattr(os, "O_PATH")


@needs_non_root
@pytest.mark.skipif(not _linux_o_path, reason="O_PATH directory descriptors are Linux-only")
@pytest.mark.parametrize("execute_only", ["root", "intermediate-dir"])
def test_image_under_execute_only_directory_still_embeds(tmp_path, execute_only):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "img.png").write_bytes(INSIDE)
    os.symlink("img.png", ws / "sub" / "link.png")
    target = ws if execute_only == "root" else ws / "sub"
    with _chmod(target, 0o111):
        for ref in ("MEDIA:sub/img.png", f"MEDIA:{ws}/sub/img.png"):
            assert _payloads(_embed(ref, [ws])) == [INSIDE], ref
        # Symlinks stay refused through O_PATH directory descriptors.
        assert _embed("MEDIA:sub/link.png", [ws]) == shares._PLACEHOLDER
        assert _embed("MEDIA:sub/missing.png", [ws]) == shares._PLACEHOLDER


@pytest.mark.skipif(not _linux_o_path, reason="O_PATH directory descriptors are Linux-only")
def test_linux_directory_descriptors_are_o_path_nofollow():
    flags = shares._share_dir_open_flags()
    for flag in ("O_PATH", "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        assert flags & getattr(os, flag), flag


def test_directory_flags_without_o_path_keep_rdonly_and_still_work(tree, monkeypatch):
    monkeypatch.delattr(os, "O_PATH", raising=False)
    flags = shares._share_dir_open_flags()
    assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
    assert flags & os.O_ACCMODE == os.O_RDONLY
    assert _payloads(_embed("MEDIA:real/img.png", [tree["ws"]])) == [INSIDE]
    assert _payloads(_embed(f"MEDIA:{tree['ws']}/real/img.png", [tree["ws"]])) == [INSIDE]
    for ref in ("alias/img.png", "link.png", "escape/image.png"):
        assert _embed(f"MEDIA:{ref}", [tree["ws"]]) == shares._PLACEHOLDER, ref


# R2-1  Without O_PATH (macOS), prefer O_SEARCH when the platform exposes it:
#       a search-only directory descriptor needs only search permission, like
#       a pathname lookup, so execute-only (0111) directories keep working.
#       Simulated on Linux by hiding O_PATH and exposing its value as O_SEARCH.


@needs_non_root
@pytest.mark.skipif(not _linux_o_path, reason="simulation reuses the Linux O_PATH bit")
@pytest.mark.parametrize("execute_only", ["root", "intermediate-dir"])
def test_directory_flags_prefer_o_search_without_o_path(tmp_path, monkeypatch, execute_only):
    search_bit = os.O_PATH
    monkeypatch.delattr(os, "O_PATH")
    monkeypatch.setattr(os, "O_SEARCH", search_bit, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    flags = shares._share_dir_open_flags()
    assert flags & search_bit and flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "img.png").write_bytes(INSIDE)
    target = ws if execute_only == "root" else ws / "sub"
    with _chmod(target, 0o111):
        assert _payloads(_embed("MEDIA:sub/img.png", [ws])) == [INSIDE]
        # A plain miss under a searchable directory is still a miss, not a refusal.
        assert _embed("MEDIA:sub/missing.png", [ws]) == shares._PLACEHOLDER


# R2-2  A root that can be opened but not SEARCHED (mode 000) is a miss, so
#       the lookup falls through to the next allowed root exactly as pathname
#       reads did; a permission error BELOW a searchable root stays terminal
#       (R1-3).


@needs_non_root
def test_unsearchable_root_is_a_miss_and_falls_through_to_next_root(tmp_path):
    root_a = tmp_path / "ws-a"
    root_b = tmp_path / "attachments-b"
    for root in (root_a, root_b):
        (root / "sub").mkdir(parents=True)
    (root_a / "sub" / "img.png").write_bytes(INSIDE)
    (root_b / "sub" / "img.png").write_bytes(OUTSIDE)
    with _chmod(root_a, 0):
        assert _payloads(_embed("MEDIA:sub/img.png", [root_a, root_b])) == [OUTSIDE]
    # Control: an unreadable directory BELOW a searchable root stays terminal.
    with _chmod(root_a / "sub", 0):
        assert _embed("MEDIA:sub/img.png", [root_a, root_b]) == shares._PLACEHOLDER


# R3-1  Only "nothing is there" errors (ENOENT, ENOTDIR, a refused symlink, and
#       a permission error AT the root) may fall through to the next allowed
#       root.  Any other error (EMFILE, EIO, ESTALE, ...) at the root walk, the
#       root search probe or below the root is TERMINAL: falling through would
#       substitute another root's namesake for an image that exists.


def _two_roots(tmp_path):
    root_a = tmp_path / "ws-a"
    root_b = tmp_path / "attachments-b"
    for root, payload in ((root_a, INSIDE), (root_b, OUTSIDE)):
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "img.png").write_bytes(payload)
    return root_a, root_b


@pytest.mark.parametrize("err", ["EMFILE", "EIO", "ESTALE", "ENFILE"])
@pytest.mark.parametrize("where", ["slash-open", "root-walk", "root-probe", "below-root-dir", "final-file"])
def test_unexpected_open_error_never_substitutes_a_later_root(tmp_path, monkeypatch, where, err):
    import errno as _errno

    root_a, root_b = _two_roots(tmp_path)
    real_open = os.open
    fired = []

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        name = os.fspath(path)
        hit = (
            (where == "slash-open" and name == "/" and dir_fd is None)
            or (where == "root-walk" and name == root_a.name and dir_fd is not None)
            or (where == "root-probe" and name == "." and dir_fd is not None)
            or (where == "below-root-dir" and name == "sub" and dir_fd is not None)
            or (where == "final-file" and name == "img.png" and dir_fd is not None)
        )
        if hit and not fired:
            fired.append(name)
            raise OSError(getattr(_errno, err), os.strerror(getattr(_errno, err)))
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fake_open)
    # Register the wrapper as dir_fd-capable, as the real os.open is; otherwise
    # the capability gate refuses everything and the test proves nothing.
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {fake_open})
    assert shares._share_nofollow_open_supported()
    out = _embed("MEDIA:sub/img.png", [root_a, root_b])
    assert fired, "the injected error never fired"
    assert OUTSIDE_MARK not in b"".join(_payloads(out)), f"{err} at {where} substituted root B"
    assert out == shares._PLACEHOLDER, out[:200]


@pytest.mark.parametrize("err", ["ENOENT", "ENOTDIR", "ELOOP", "ENAMETOOLONG"])
def test_nothing_there_errors_at_the_root_still_fall_through(tmp_path, monkeypatch, err):
    import errno as _errno

    root_a, root_b = _two_roots(tmp_path)
    real_open = os.open

    fired = []

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        if os.fspath(path) == root_a.name and dir_fd is not None:
            fired.append(err)
            raise OSError(getattr(_errno, err), os.strerror(getattr(_errno, err)))
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {fake_open})
    assert shares._share_nofollow_open_supported()
    assert _payloads(_embed("MEDIA:sub/img.png", [root_a, root_b])) == [OUTSIDE]
    assert fired, "the injected error never fired"


# R4-2  A non-regular entry (socket, FIFO, device) where the image should be is
#       a miss, as master's ``is_file()`` probe treated it, and is never
#       opened: opening a socket fails (ENXIO on Linux, EOPNOTSUPP on macOS)
#       and opening a device node can have side effects.


@pytest.mark.skipif(not hasattr(socket_mod, "AF_UNIX"), reason="needs Unix sockets")
def test_socket_in_first_root_is_a_miss_and_never_opened(tmp_path, monkeypatch):
    root_a, root_b = _two_roots(tmp_path)
    (root_a / "sub" / "img.png").unlink()
    monkeypatch.chdir(root_a / "sub")  # bind by a short relative name (AF_UNIX path limit)
    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    try:
        sock.bind("img.png")
        monkeypatch.chdir(tmp_path)
        real_open = os.open
        opened = []

        def spy_open(path, flags, mode=0o777, *, dir_fd=None):
            opened.append(os.fspath(path))
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {spy_open})
        assert _payloads(_embed("MEDIA:sub/img.png", [root_a, root_b])) == [OUTSIDE]
        # root A's socket was never opened; only root B's regular file was
        assert opened.count("img.png") == 1, opened
    finally:
        sock.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_in_first_root_is_a_miss(tmp_path):
    root_a, root_b = _two_roots(tmp_path)
    (root_a / "sub" / "img.png").unlink()
    os.mkfifo(root_a / "sub" / "img.png")
    assert _payloads(_embed("MEDIA:sub/img.png", [root_a, root_b])) == [OUTSIDE]


# R1-5  Windows: verify-after-open.  Native Windows is NOT available to this
#       suite, so the Windows branch is exercised on POSIX with the platform
#       switch, ``msvcrt.get_osfhandle`` and the ``GetFinalPathNameByHandleW``
#       ctypes call mocked.  The fake ``os.open`` maps Windows paths onto real
#       temp files, so ``fstat`` and the capped read run for real on a real
#       descriptor; each opened handle reports a configurable final path (what
#       a junction/symlink swap would make the kernel report).


class _FakeWin32:
    HANDLE_BASE = 0x1000

    def __init__(self, monkeypatch):
        self.files: dict[str, tuple[object, str]] = {}
        self.handles: dict[int, str] = {}
        self.open_calls: list[str] = []
        self.stat_calls: list[str] = []
        self.opened: list[int] = []
        self.closed: list[int] = []
        owner = threading.get_ident()
        real_open, real_close, real_stat = os.open, os.close, os.stat

        def fake_open(path, flags, *args, **kwargs):
            if threading.get_ident() != owner or not isinstance(path, str) or "\\" not in path:
                return real_open(path, flags, *args, **kwargs)
            assert not args and not kwargs, "the Windows reader must not use dir_fd"
            self.open_calls.append(path)
            if path not in self.files:
                raise FileNotFoundError(2, "No such file or directory", path)
            target, final = self.files[path]
            if isinstance(target, BaseException):
                raise target
            if os.path.isdir(str(target)):
                # CPython opens with _wopen, which reports EACCES for a directory (Codex r5)
                raise PermissionError(13, "Permission denied", path)
            fd = real_open(str(target), os.O_RDONLY | os.O_NONBLOCK)
            self.opened.append(fd)
            self.handles[self.HANDLE_BASE + fd] = final
            return fd

        def fake_close(fd):
            if threading.get_ident() == owner:
                self.closed.append(fd)
            return real_close(fd)

        def fake_stat(path, *args, **kwargs):
            if threading.get_ident() != owner or not isinstance(path, str) or "\\" not in path:
                return real_stat(path, *args, **kwargs)
            self.stat_calls.append(path)
            if path not in self.files:
                raise FileNotFoundError(2, "No such file or directory", path)
            target, _final = self.files[path]
            if isinstance(target, BaseException):
                raise target
            return real_stat(str(target))

        monkeypatch.setattr(os, "open", fake_open)
        monkeypatch.setattr(os, "close", fake_close)
        monkeypatch.setattr(os, "stat", fake_stat)
        self.msvcrt = SimpleNamespace(get_osfhandle=lambda fd: self.HANDLE_BASE + fd)
        monkeypatch.setitem(sys.modules, "msvcrt", self.msvcrt)
        monkeypatch.setattr(
            shares, "_share_win_final_path_api", lambda: self.final_path_api, raising=False
        )

    def final_path_api(self, handle, buf, size, flags):
        assert flags == 0, "FILE_NAME_NORMALIZED | VOLUME_NAME_DOS"
        final = self.handles.get(handle)
        if final is None:
            return 0
        if len(final) + 1 > size:
            return len(final) + 1  # buffer too small: required size incl. NUL
        buf.value = final
        return len(final)

    def add(self, path: str, target, final: str) -> None:
        self.files[path] = (target, final)

    def assert_all_closed(self) -> None:
        assert set(self.opened) <= set(self.closed), (self.opened, self.closed)


@pytest.fixture()
def win(monkeypatch):
    return _FakeWin32(monkeypatch)


@pytest.fixture()
def win_files(tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(INSIDE)
    b = tmp_path / "b.png"
    b.write_bytes(OUTSIDE)
    return {"inside": a, "other": b}


def _read_win(candidates):
    return shares._read_share_media_windows(candidates, shares._SHARE_EMBED_MAX_BYTES)


@pytest.mark.parametrize(
    "root, final",
    [
        ("C:\\a\\b", "\\\\?\\C:\\a\\b\\x.png"),
        ("C:\\a\\b", "C:\\a\\b\\x.png"),
        ("C:\\a\\b", "\\\\?\\c:\\A\\B\\X.PNG"),  # normcase on both sides
        ("C:\\a\\b", "\\\\?\\C:\\a\\b\\deeper\\x.png"),  # in-root junction target
        ("\\\\?\\C:\\a\\b", "\\\\?\\C:\\a\\b\\x.png"),  # \\?\ long-path root
        ("C:\\", "\\\\?\\C:\\x.png"),  # drive root
        ("\\\\server\\share\\ws", "\\\\?\\UNC\\server\\share\\ws\\x.png"),
        ("\\\\server\\share\\ws", "\\\\server\\share\\ws\\x.png"),
    ],
)
def test_windows_reader_embeds_file_verified_inside_root(win, win_files, root, final):
    path = root.rstrip("\\") + "\\x.png"
    win.add(path, win_files["inside"], final)
    assert _read_win([(root, ["x.png"])]) == INSIDE
    assert win.open_calls == [path]
    win.assert_all_closed()


_UNVERIFIED_FINALS = {
    "outside": "\\\\?\\C:\\outside\\x.png",
    "prefix-sibling": "\\\\?\\C:\\a\\bc\\x.png",
    "parent": "\\\\?\\C:\\a\\x.png",
    "the-root-itself": "\\\\?\\C:\\a\\b",
    "other-drive": "\\\\?\\D:\\a\\b\\x.png",
    "unc-outside": "\\\\?\\UNC\\server\\share\\a\\b\\x.png",
    "device": "\\\\.\\PhysicalDrive0",
    "device-in-root-shape": "\\\\.\\C:\\a\\b\\x.png",
    "globalroot": "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\a\\b\\x.png",
    "volume-guid": "\\\\?\\Volume{0b0e8c3a-0000-0000-0000-100000000000}\\a\\b\\x.png",
    "nt-namespace": "\\??\\C:\\a\\b\\x.png",
    "dotdot": "\\\\?\\C:\\a\\b\\..\\x.png",
    "relative": "a\\b\\x.png",
    "rooted-no-drive": "\\a\\b\\x.png",
    "empty": "",
}


@pytest.mark.parametrize("case", sorted(_UNVERIFIED_FINALS))
def test_windows_reader_refuses_unverified_final_path_and_stops(win, win_files, case):
    win.add("C:\\a\\b\\x.png", win_files["other"], _UNVERIFIED_FINALS[case])
    # A verifiable namesake in a later root must NOT be substituted.
    win.add("C:\\z\\x.png", win_files["inside"], "\\\\?\\C:\\z\\x.png")
    assert _read_win([("C:\\a\\b", ["x.png"]), ("C:\\z", ["x.png"])]) is None
    assert win.open_calls == ["C:\\a\\b\\x.png"]
    win.assert_all_closed()


@pytest.mark.parametrize(
    "final",
    [
        "\\\\?\\UNC\\server\\share\\wsx\\x.png",  # prefix sibling
        "\\\\?\\UNC\\other\\share\\ws\\x.png",
        "\\\\?\\UNC\\server\\share2\\ws\\x.png",
        "\\\\?\\UNC\\server\\share",
    ],
)
def test_windows_reader_unc_root_component_boundary(win, win_files, final):
    root = "\\\\server\\share\\ws"
    win.add(root + "\\x.png", win_files["other"], final)
    assert _read_win([(root, ["x.png"])]) is None
    win.assert_all_closed()


def test_windows_reader_long_final_path_uses_resized_buffer(win, win_files):
    root = "C:\\" + "\\".join(["d" * 60] * 12)  # final path > the first buffer
    win.add(root + "\\x.png", win_files["inside"], "\\\\?\\" + root + "\\x.png")
    assert len(root) > 512
    assert _read_win([(root, ["x.png"])]) == INSIDE
    win.assert_all_closed()


def test_windows_reader_api_failure_is_placeholder(win, win_files, monkeypatch):
    win.add("C:\\a\\b\\x.png", win_files["inside"], "\\\\?\\C:\\a\\b\\x.png")
    # GetFinalPathNameByHandleW returns 0 on failure.
    monkeypatch.setattr(shares, "_share_win_final_path_api", lambda: lambda *_a: 0)
    assert _read_win([("C:\\a\\b", ["x.png"])]) is None
    assert win.open_calls == ["C:\\a\\b\\x.png"]
    win.assert_all_closed()


@pytest.mark.parametrize("failure", ["get_osfhandle", "api-lookup", "api-call", "no-msvcrt"])
def test_windows_reader_any_exception_is_placeholder(win, win_files, monkeypatch, failure):
    win.add("C:\\a\\b\\x.png", win_files["inside"], "\\\\?\\C:\\a\\b\\x.png")

    def boom(*_args, **_kwargs):
        raise OSError(22, "simulated Win32 failure")

    if failure == "get_osfhandle":
        monkeypatch.setattr(win.msvcrt, "get_osfhandle", boom)
    elif failure == "api-lookup":
        monkeypatch.setattr(shares, "_share_win_final_path_api", boom)
    elif failure == "api-call":
        monkeypatch.setattr(shares, "_share_win_final_path_api", lambda: boom)
    else:
        monkeypatch.setitem(sys.modules, "msvcrt", None)  # import -> ImportError
    assert _read_win([("C:\\a\\b", ["x.png"])]) is None
    assert win.open_calls == ["C:\\a\\b\\x.png"]
    win.assert_all_closed()


@pytest.mark.parametrize("kind", ["char-device", "directory"])
def test_windows_reader_requires_regular_file(win, tmp_path, kind):
    target = Path(os.devnull) if kind == "char-device" else tmp_path
    win.add("C:\\a\\b\\x.png", target, "\\\\?\\C:\\a\\b\\x.png")
    assert _read_win([("C:\\a\\b", ["x.png"])]) is None
    win.assert_all_closed()


def test_windows_reader_size_cap_on_same_descriptor(win, tmp_path):
    limit = shares._SHARE_EMBED_MAX_BYTES
    exact = tmp_path / "exact.png"
    exact.write_bytes(PNG + b"\x00" * (limit - len(PNG)))
    over = tmp_path / "over.png"
    over.write_bytes(PNG + b"\x00" * (limit + 1 - len(PNG)))
    win.add("C:\\a\\b\\exact.png", exact, "\\\\?\\C:\\a\\b\\exact.png")
    win.add("C:\\a\\b\\over.png", over, "\\\\?\\C:\\a\\b\\over.png")
    data = _read_win([("C:\\a\\b", ["exact.png"])])
    assert data is not None and len(data) == limit
    assert _read_win([("C:\\a\\b", ["over.png"])]) is None
    win.assert_all_closed()


def test_windows_reader_missing_falls_through_but_denied_is_terminal(win, win_files):
    win.add("C:\\z\\x.png", win_files["other"], "\\\\?\\C:\\z\\x.png")
    candidates = [("C:\\a\\b", ["x.png"]), ("C:\\z", ["x.png"])]
    assert _read_win(candidates) == OUTSIDE  # missing in root A: root B is used
    win.add("C:\\a\\b\\x.png", PermissionError(13, "Access is denied"), "")
    assert _read_win(candidates) is None  # denied in root A: never root B
    win.assert_all_closed()


def test_windows_reader_directory_in_first_root_is_a_miss_and_never_opened(win, win_files, tmp_path):
    # Codex r5: _wopen reports EACCES for a directory, so opening it first would end the lookup with the
    # placeholder where master's is_file() fell through to root B.  An advisory stat skips it unopened.
    (tmp_path / "adir").mkdir()
    win.add("C:\\ws\\sub\\x.png", tmp_path / "adir", "\\\\?\\C:\\ws\\sub\\x.png")
    win.add("C:\\att\\sub\\x.png", win_files["other"], "\\\\?\\C:\\att\\sub\\x.png")
    candidates = [("C:\\ws", ["sub", "x.png"]), ("C:\\att", ["sub", "x.png"])]
    assert _read_win(candidates) == OUTSIDE
    assert win.open_calls == ["C:\\att\\sub\\x.png"], win.open_calls
    win.assert_all_closed()


@pytest.mark.parametrize("err", [PermissionError(13, "denied"), OSError(5, "EIO"), OSError(24, "EMFILE")])
def test_windows_reader_stat_errors_other_than_missing_are_terminal(win, win_files, err):
    win.add("C:\\ws\\x.png", err, "")
    win.add("C:\\att\\x.png", win_files["other"], "\\\\?\\C:\\att\\x.png")
    assert _read_win([("C:\\ws", ["x.png"]), ("C:\\att", ["x.png"])]) is None
    assert win.open_calls == []
    win.assert_all_closed()


@pytest.mark.parametrize(
    "names",
    [["nul.png"], ["CON"], ["com1.png"], ["LPT\u00b9.png"], ["x.png:secret"], ["sub", "D:"], ["a\\b.png"]],
)
def test_windows_reader_refuses_device_and_stream_names_without_opening(win, names):
    assert _read_win([("C:\\a\\b", names)]) is None
    assert win.open_calls == []


def test_windows_unverifiable_root_fails_closed_without_opening(win):
    for root in ("\\\\?\\Volume{0b0e8c3a}\\ws", "\\\\.\\C:\\ws", "relative\\ws", "/tmp/posix-root"):
        assert _read_win([(root, ["x.png"])]) is None, root
    assert win.open_calls == []


def test_windows_canonical_and_containment_helpers():
    canon = shares._share_win_canonical
    assert canon("\\\\?\\C:\\A\\B\\") == "c:\\a\\b"
    assert canon("C:/A/B") == "c:\\a\\b"
    assert canon("\\\\?\\UNC\\Srv\\Share\\X") == "\\\\srv\\share\\x"
    for bad in ("\\\\.\\C:\\x", "\\\\?\\Volume{1}\\x", "\\??\\C:\\x", "C:x", "\\x", "\\\\srv", "C:\\a\\..\\b"):
        assert canon(bad) is None, bad
    within = shares._share_win_is_within
    assert within("\\\\?\\C:\\a\\b\\x.png", "c:\\a\\b")
    assert not within("\\\\?\\C:\\a\\bc\\x.png", "c:\\a\\b")
    assert not within("\\\\?\\C:\\a\\b", "c:\\a\\b")


# -- Windows end-to-end through _embed_share_media (lexical mapping included) --


class _ResolvedWinPath(PureWindowsPath):
    """A PureWindowsPath whose resolve() is the identity (already canonical)."""

    def resolve(self, strict=False):  # noqa: ARG002 - pathlib signature
        return self


@pytest.fixture()
def win_e2e(win_files, win, monkeypatch):
    # The platform switch plus Windows' platform facts: os.open has no dir_fd
    # support and there is no O_NOFOLLOW / O_DIRECTORY.  (raising=False keeps
    # the test runnable against the pre-round-1 code, where it must go RED.)
    monkeypatch.setattr(shares, "_share_platform_is_windows", lambda: True, raising=False)
    monkeypatch.setattr(shares, "Path", _ResolvedWinPath)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    return win


WS = "C:\\Users\\Me\\ws"
ATT = WS + "\\inbox\\attachments"


@pytest.mark.parametrize(
    "ref",
    [
        "sub\\img.png",
        "sub/img.png",
        WS + "\\sub\\img.png",
        "C:/Users/Me/ws/sub/img.png",
        "c:\\users\\me\\WS\\sub\\img.png",  # case-insensitive root match
        "\\\\?\\C:\\Users\\Me\\ws\\sub\\img.png",
    ],
)
def test_windows_embeds_in_root_image_end_to_end(win_e2e, win_files, ref):
    win_e2e.add(WS + "\\sub\\img.png", win_files["inside"], "\\\\?\\" + WS + "\\sub\\img.png")
    out = _embed(f"MEDIA:{ref}", [WS])
    assert _payloads(out) == [INSIDE], (ref, out[:200])
    assert 'alt="img.png"' in out
    win_e2e.assert_all_closed()


@pytest.mark.parametrize(
    "ref",
    [
        "C:\\Users\\Me\\wsx\\sub\\img.png",  # sibling sharing the root's prefix
        "C:\\Users\\Me\\ws\\..\\secret\\img.png",
        "C:sub\\img.png",  # drive-relative
        "\\Users\\Me\\ws\\sub\\img.png",  # rooted without a drive
        "D:\\Users\\Me\\ws\\sub\\img.png",
        "file://C:/Users/Me/ws/sub/img.png",
        "\\\\.\\C:\\Users\\Me\\ws\\sub\\img.png",  # device namespace
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\Users\\Me\\ws\\sub\\img.png",
    ],
)
def test_windows_refuses_lexically_outside_refs_without_opening(win_e2e, win_files, ref):
    win_e2e.add(WS + "\\sub\\img.png", win_files["inside"], "\\\\?\\" + WS + "\\sub\\img.png")
    assert _embed(f"MEDIA:{ref}", [WS]) == shares._PLACEHOLDER
    assert win_e2e.open_calls == []


@pytest.mark.parametrize("ref", ["img.png", ATT + "\\img.png"])
def test_windows_nested_root_redirect_is_placeholder_end_to_end(win_e2e, win_files, ref):
    # inbox swapped for a junction to C:\outside after root resolution: the
    # opened handle's final path is outside every root -> placeholder.
    win_e2e.add(ATT + "\\img.png", win_files["other"], "\\\\?\\C:\\outside\\attachments\\img.png")
    out = _embed(f"MEDIA:{ref}", [WS, ATT])
    assert OUTSIDE_MARK not in b"".join(_payloads(out))
    assert out == shares._PLACEHOLDER
    win_e2e.assert_all_closed()


def test_windows_switch_with_posix_roots_fails_closed(tree, monkeypatch):
    # On the real POSIX tree the Windows reader cannot verify anything (the
    # roots are not drive/UNC paths and there is no msvcrt): placeholder, and
    # nothing is opened.
    monkeypatch.setattr(shares, "_share_platform_is_windows", lambda: True, raising=False)
    _assert_refuses_without_opening(tree, monkeypatch)
