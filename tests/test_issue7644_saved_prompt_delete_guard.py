"""#7644 saved prompts — deliberate two-step delete + recoverable persistence.

A single click on the popup's 12px ✕ fired ``DELETE /api/prompts`` immediately
(empty ``catch``, no confirmation), and the server rewrote ``saved_prompts.json``
in place with no backup, so a mis-click destroyed a prompt permanently.

These tests pin the fix on both layers:
  * the frontend DELETE must be gated behind an armed/confirm state that
    auto-expires, and a failed delete must keep the row and show a toast;
  * the persistence layer must publish atomically (temp + rename) and keep one
    known-good generation that the loader falls back to when the primary file
    cannot be parsed.
"""
import errno
import io
import json
import os

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _body(source: str, header: str) -> str:
    """Return the brace-matched block that starts at `header`."""
    start = source.index(header)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unterminated block: {header}")


def test_delete_needs_a_second_click_within_a_window() -> None:
    """First click only arms the row's ✕ (auto-disarms after ~3s); the DELETE
    request must be unreachable from that first click."""
    js = read("static/messages.js")
    handler = _body(js, "del.onclick=async(e)=>")
    delete_at = handler.index("method:'DELETE'")
    guard_at = handler.index("if(!del.classList.contains('is-armed')){")
    arm_at = handler.index("add('is-armed')", guard_at)
    bail_at = handler.index("return;", arm_at)
    assert guard_at < arm_at < bail_at < delete_at, (
        "the first click must add the armed state and return before any request"
    )
    assert "setTimeout" in handler and "clearTimeout" in handler, (
        "the armed state must auto-expire so a stale ✕ can't delete later"
    )


def test_delete_failure_is_surfaced_and_keeps_the_row() -> None:
    """Errors used to vanish into catch(_e){}; now the popup must toast and
    not re-render (row stays) when the DELETE fails."""
    js = read("static/messages.js")
    handler = _body(js, "del.onclick=async(e)=>")
    assert "catch(_e){}" not in handler, "delete errors must not be silently discarded"
    failure_path = handler.split("catch(_e){", 1)[1]
    assert "showToast" in failure_path, "a failed delete must surface a toast"
    assert "return;" in failure_path.split("}catch", 1)[0], (
        "a failed delete must bail out before the popup re-renders"
    )


def test_delete_control_has_a_44px_hit_target() -> None:
    """The 12px icon must keep a >=44x44 target: 8px padding box (28px) plus an
    8px invisible halo on every side."""
    css = read("static/style.css")
    rule = _body(css, ".saved-prompt-delete{")
    assert "padding:8px" in rule, "delete button padding box must be 28px"
    halo = _body(css, ".saved-prompt-delete::after{")
    assert "inset:-8px" in halo, "the ::after halo must add 8px per side (44px total)"
    assert ".saved-prompt-delete.is-armed{" in css, "armed state needs its own style"
    assert "animation:none" in _body(
        css, "@media (prefers-reduced-motion:reduce){.saved-prompt-delete.is-armed{"
    ), "the armed confirmation must respect prefers-reduced-motion"


def test_save_is_atomic_and_keeps_one_generation(tmp_path, monkeypatch) -> None:
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert not Path(str(p) + ".tmp").exists(), "temp file must be renamed away"

    routes._save_saved_prompts([{"id": "b", "text": "two"}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "b", "text": "two"}]
    bak = json.loads(Path(str(p) + ".bak").read_text(encoding="utf-8"))
    assert bak == [{"id": "a", "text": "one"}], (
        "the previous generation must survive as saved_prompts.json.bak"
    )
    assert routes._load_saved_prompts() == [{"id": "b", "text": "two"}]


def test_loader_recovers_from_a_corrupt_primary(tmp_path, monkeypatch) -> None:
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    p.write_text("{torn write", encoding="utf-8")  # crash mid-rewrite
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}], (
        "a corrupt primary must fall back to the .bak generation, not []"
    )


def test_loader_returns_empty_when_nothing_exists(tmp_path, monkeypatch) -> None:
    from api import routes

    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: tmp_path / "webui" / "saved_prompts.json")
    assert routes._load_saved_prompts() == []


def test_save_uses_the_shared_atomic_writer(tmp_path, monkeypatch) -> None:
    """The publish must ride api.paths._atomic_write_text (the repo's shared
    temp+fsync+os.replace chokepoint that preserves mode/uid/gid and handles
    symlinks/hard links), not a bespoke truncate of the live file."""
    import inspect

    from api import routes

    source = inspect.getsource(routes._save_saved_prompts)
    assert "from api.paths import" in source, (
        "_save_saved_prompts must delegate its publications to api.paths"
    )
    assert "_atomic_write_text(" in source, (
        "the primary publish must ride the shared atomic writer"
    )
    in_place = source.replace("_atomic_write_text(", "")
    assert "write_text(" not in in_place and "open(" not in in_place, (
        "an in-place write_text/open is the #7644 truncation bug"
    )


def test_a_corrupt_primary_never_overwrites_the_good_backup(tmp_path, monkeypatch) -> None:
    """Rotating blindly would let a torn saved_prompts.json clobber the one
    known-good .bak generation, destroying the only recovery source."""
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    p.write_text("{torn write", encoding="utf-8")  # crash mid-rewrite

    routes._save_saved_prompts([{"id": "c", "text": "three"}])

    bak = Path(str(p) + ".bak")
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}], (
        "the good backup generation must survive a corrupt primary rotation"
    )
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "c", "text": "three"}]


# ── The primary's own authority across a publish ─────────────────────────
#
# Rotating with `os.replace(p, p.bak)` handed the shared writer a brand-new
# path: mode, owner, symlinks, hard links and xattrs of the live file were no
# longer there to preserve. The recovery copy is now published as an
# independent snapshot while the primary stays exactly where it is.

def _paths(tmp_path, monkeypatch):
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)
    return routes, p


@pytest.mark.parametrize("mode", [0o600, 0o664], ids=["0600", "0664"])
def test_a_private_primary_stays_private_across_a_save(tmp_path, monkeypatch, mode):
    import os as _os

    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    _os.chmod(p, mode)
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    assert _os.stat(p).st_mode & 0o777 == mode, (
        f"a {mode:04o} saved-prompt file must not be re-moded by a save"
    )
    assert json.loads(p.read_text(encoding="utf-8"))[1]["id"] == "b"


def test_a_read_only_primary_refuses_the_write(tmp_path, monkeypatch):
    import os as _os

    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    _os.chmod(p, 0o444)
    before = p.read_text(encoding="utf-8")
    try:
        with pytest.raises(PermissionError):
            routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b"}])
    finally:
        _os.chmod(p, 0o644)
    assert p.read_text(encoding="utf-8") == before, "a 0444 primary must not be overwritten"


def test_a_symlinked_primary_keeps_the_link_and_updates_the_referent(tmp_path, monkeypatch):
    import os as _os

    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    real = p.parent / "prompts-real.json"
    _os.replace(p, real)
    p.symlink_to(real.name)

    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])

    assert p.is_symlink(), "saving must not sever the primary's symlink"
    assert json.loads(real.read_text(encoding="utf-8"))[1]["id"] == "b", (
        "the referent is what a symlinked primary writes through"
    )
    assert json.loads(p.read_text(encoding="utf-8"))[1]["id"] == "b"


def test_the_recovery_copy_gets_its_own_inode(tmp_path, monkeypatch):
    """A compatibility write goes in place so aliases stay valid; if the backup
    were an alias of the primary, that in-place write would rewrite the live
    file through it and the two generations would collapse into one."""
    import os as _os

    routes, p = _paths(tmp_path, monkeypatch)
    bak = Path(str(p) + ".bak")
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    _os.link(p, bak)  # user-made hard-linked backup

    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])

    assert json.loads(p.read_text(encoding="utf-8"))[1]["id"] == "b"
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}], (
        "the recovery generation must survive as its own inode, not the primary's alias"
    )
    assert _os.stat(p).st_ino != _os.stat(bak).st_ino


def test_an_unusable_recovery_copy_refuses_the_destructive_save(tmp_path, monkeypatch):
    """`.bak` that cannot be published (here: a directory) must stop the save
    before the primary is touched — the old code swallowed the failure and
    answered 200 over a destroyed file."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    before = p.read_text(encoding="utf-8")
    Path(str(p) + ".bak").mkdir()

    with pytest.raises(routes.SavedPromptPersistenceError):
        routes._save_saved_prompts([])

    assert p.read_text(encoding="utf-8") == before, "the primary must be untouched"
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}]


def test_the_payload_is_serialized_before_anything_is_published(tmp_path, monkeypatch):
    """Encoding the whole payload first: a failure there can never leave a
    half-published generation behind."""
    import inspect

    from api import routes

    source = inspect.getsource(routes._save_saved_prompts)
    dump_at = source.index("payload = json.dumps(")
    assert dump_at < source.index("_stage_atomic_write("), (
        "serialize before staging the recovery copy"
    )
    assert dump_at < source.index("_atomic_write_text(p, payload)"), (
        "serialize before publishing the primary"
    )
    assert source.index("_stage_atomic_write(") < source.index("_publish_staged_write("), (
        "the recovery copy is staged before it is published, so a preparation "
        "failure never removes the pre-existing backup"
    )


# ── Route behaviour: replayed DELETEs ────────────────────────────────────

class _PromptDeleteHandler:
    def __init__(self, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _delete_via_route(monkeypatch, p, prompt_id: str):
    from urllib.parse import urlparse

    from api import routes

    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda handler, parsed, body=None, method="DELETE": True,
    )
    handler = _PromptDeleteHandler({"id": prompt_id})
    handled = routes.handle_delete(handler, urlparse("/api/prompts"))
    assert handled is not False, "DELETE /api/prompts must be handled"
    return handler


def test_a_repeated_delete_of_the_same_id_never_erases_the_recovery_generation(
    tmp_path, monkeypatch
):
    """The frontend helper can replay a DELETE after a network error. The first
    one publishes primary=[] and backup=[old]; the replay used to publish
    backup=[] too, so corrupting the primary afterwards recovered nothing."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    bak = Path(str(p) + ".bak")

    first = _delete_via_route(monkeypatch, p, "a")
    assert first.status == 200 and first.json_body() == {"ok": True}
    assert json.loads(p.read_text(encoding="utf-8")) == []
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]

    second = _delete_via_route(monkeypatch, p, "a")
    assert second.status == 200 and second.json_body() == {"ok": True}
    assert json.loads(p.read_text(encoding="utf-8")) == [], "the primary is still []"
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}], (
        "a replayed DELETE must not rotate the recovery copy down to []"
    )

    p.write_text("{torn write", encoding="utf-8")
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}]


def test_deleting_an_unknown_id_creates_no_files(tmp_path, monkeypatch):
    routes, p = _paths(tmp_path, monkeypatch)
    handler = _delete_via_route(monkeypatch, p, "never-there")
    assert handler.status == 200 and handler.json_body() == {"ok": True}
    assert not p.exists() and not Path(str(p) + ".bak").exists()


def test_a_blocked_recovery_copy_surfaces_an_error_instead_of_success(
    tmp_path, monkeypatch
):
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    before = p.read_text(encoding="utf-8")
    Path(str(p) + ".bak").mkdir()

    handler = _delete_via_route(monkeypatch, p, "a")

    assert handler.status == 500
    assert "error" in handler.json_body()
    assert p.read_text(encoding="utf-8") == before, "the prompt must still be there"


# ── Recovery-copy confidentiality and alias handling ─────────────────────
#
# The primary may be 0600, so publishing its bytes as a 0644 .bak hands the
# whole prompt list to anyone who can traverse the state directory. And a
# `.bak` that is an alias belongs to somebody else — an unrelated file, or the
# primary itself — so publication must break the alias and land the bytes on a
# fresh inode carrying the primary's restrictions *before* they exist on disk,
# never via a chmod after the rename.


def _fail_backup_publish(monkeypatch):
    """Make only the recovery publication fail, as an fsync/preparation
    failure would."""
    from api import paths

    original = paths._atomic_write_text

    def failing(path, text, **kwargs):
        if Path(path).name.endswith(".bak"):
            raise OSError("injected preparation failure")
        return original(path, text, **kwargs)

    monkeypatch.setattr(paths, "_atomic_write_text", failing)


def _fail_recovery_publication(monkeypatch):
    """Fail the recovery publication *after* preparation, at its rename.

    This is the failure that used to strand the process: the shared writer had
    already written and fsynced the replacement temp, so the pre-existing
    `.bak` must still be intact when this raises.
    """
    from api import paths

    original_replace = os.replace

    def failing_replace(src, dst, **kwargs):
        if str(dst).endswith(".bak"):
            raise OSError(errno.EIO, "injected recovery publication failure")
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr(paths.os, "replace", failing_replace)


@pytest.mark.parametrize(
    "backup_state",
    ["missing", "permissive", "hardlinked"],
    ids=["missing", "permissive-0644", "hardlinked-to-primary"],
)
def test_the_recovery_copy_never_widens_a_private_primary(
    tmp_path, monkeypatch, backup_state
):
    """0600 primary + 0022 umask must not publish a readable `.bak` — whether
    the backup is absent, already 0644, or a hard link of the primary."""
    routes, p = _paths(tmp_path, monkeypatch)
    bak = Path(str(p) + ".bak")
    routes._save_saved_prompts([{"id": "a", "text": "one"}])

    old_umask = os.umask(0o022)
    try:
        os.chmod(p, 0o600)
        if backup_state == "permissive":
            bak.write_text("[]", encoding="utf-8")
            os.chmod(bak, 0o644)
        elif backup_state == "hardlinked":
            os.link(p, bak)

        routes._save_saved_prompts(
            [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
        )

        assert os.stat(bak).st_mode & 0o777 == 0o600, (
            "the recovery inode must carry the primary's restrictions, got "
            f"{os.stat(bak).st_mode & 0o777:04o}"
        )
    finally:
        os.umask(old_umask)

    assert os.stat(p).st_mode & 0o777 == 0o600, "the primary keeps 0600"
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert json.loads(p.read_text(encoding="utf-8"))[1]["id"] == "b"
    assert os.stat(p).st_ino != os.stat(bak).st_ino, "independent recovery inode"


def test_a_primary_symlinked_onto_its_backup_is_refused_before_any_mutation(
    tmp_path, monkeypatch
):
    """Alias direction 1 — saved_prompts.json -> saved_prompts.json.bak: the
    backup IS the primary's referent, so publishing a fresh recovery inode
    would delete it, and the replacement would be written through the primary
    link into the recovery path, collapsing both generations. Preparation
    failure or not, the topology must be refused with everything intact."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    bak = Path(str(p) + ".bak")
    p.replace(bak)  # the live generation sits at the recovery path
    p.symlink_to(bak.name)
    before = bak.read_text(encoding="utf-8")
    _fail_backup_publish(monkeypatch)

    with pytest.raises(routes.SavedPromptPersistenceError) as exc:
        routes._save_saved_prompts(
            [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
        )

    assert "symlinks onto its own backup" in str(exc.value), (
        "the topology gate must fire before any publication attempt"
    )
    assert p.is_symlink(), "the refusal must not repoint or replace the primary"
    assert bak.is_file() and bak.read_text(encoding="utf-8") == before, (
        "the primary's referent must survive the refusal byte-for-byte"
    )
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}]


def test_a_backup_symlinked_onto_the_primary_gets_its_own_inode(
    tmp_path, monkeypatch
):
    """Alias direction 2 — saved_prompts.json.bak -> saved_prompts.json: the
    shared writer follows that alias, so publishing through it would write the
    old prompts over the live primary and collapse the generations."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    bak = Path(str(p) + ".bak")
    bak.symlink_to(p.name)

    routes._save_saved_prompts(
        [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
    )

    assert not bak.is_symlink(), "the recovery copy must break the alias, not follow it"
    assert os.stat(p).st_ino != os.stat(bak).st_ino
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert json.loads(p.read_text(encoding="utf-8"))[1]["id"] == "b"


def test_a_backup_aliasing_the_primary_survives_a_publication_failure(
    tmp_path, monkeypatch
):
    """Alias direction 2 with the recovery publication failing at its rename.

    The primary's own publication has already committed at that point, so the
    invariant here is not "nothing changed" but that the failed recovery
    publication destroyed nothing: the pre-existing backup entry survives
    byte-for-byte, its alias is not written through, and no staged temp is
    left behind."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    bak = Path(str(p) + ".bak")
    before_backup = bak.read_text(encoding="utf-8")
    _fail_recovery_publication(monkeypatch)

    with pytest.raises(routes.SavedPromptPersistenceError):
        routes._save_saved_prompts([{"id": "c", "text": "three"}])

    assert bak.is_file() and bak.read_text(encoding="utf-8") == before_backup, (
        "a failed recovery publication must leave the previous backup intact"
    )
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}], (
        "the previous recovery generation is still readable"
    )
    assert routes._load_saved_prompts() == [{"id": "c", "text": "three"}], (
        "the primary's own publication had already committed"
    )
    assert [q.name for q in p.parent.iterdir() if ".tmp" in q.name] == [], (
        "the staged recovery temp must be cleaned up"
    )


@pytest.mark.parametrize("kind", ["symlink", "hardlink"], ids=["symlink", "hardlink"])
def test_an_unrelated_backup_alias_is_never_written_through(
    tmp_path, monkeypatch, kind
):
    """A `.bak` pointing at an unrelated file is somebody else's data: the
    publication must give the recovery path its own inode and leave the
    unrelated file byte-identical."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    stranger = tmp_path / "stranger.json"
    stranger.write_text('["stranger"]', encoding="utf-8")
    bak = Path(str(p) + ".bak")
    if kind == "symlink":
        bak.symlink_to(stranger)
    else:
        os.link(stranger, bak)

    routes._save_saved_prompts(
        [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
    )

    assert stranger.read_text(encoding="utf-8") == '["stranger"]', (
        "recovery publication must not write through an unrelated alias"
    )
    assert not bak.is_symlink() and os.stat(bak).st_nlink == 1, (
        "the recovery path must end up on its own inode"
    )
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert json.loads(p.read_text(encoding="utf-8"))[1]["id"] == "b"


# ── Failure ordering: preparation must never destroy the recovery copy ──────
#
# Unlinking `.bak` *before* the replacement is prepared and fsynced means a
# failed preparation erases the only recovery generation. The replacement must
# be fully staged on its own temp first, so the pre-existing backup stays
# readable until the successful publication boundary replaces it.


def _fail_recovery_fsync(monkeypatch):
    """Fail the real fsync of the staged recovery temp — i.e. a preparation
    failure that happens after the bytes are written but before publication."""
    from api import paths

    real_fsync = os.fsync

    def failing_fsync(fd):
        try:
            staged = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            staged = ""
        if ".bak" in Path(staged).name:
            raise OSError(errno.EIO, "injected recovery fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(paths.os, "fsync", failing_fsync)


def test_a_failed_recovery_preparation_keeps_the_previous_backup(
    tmp_path, monkeypatch
):
    """A failed save must not cost the recovery generation: the pre-existing
    `.bak` has to still be there, byte-identical, afterwards."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    bak = Path(str(p) + ".bak")
    before_backup = bak.read_text(encoding="utf-8")
    before_primary = p.read_text(encoding="utf-8")
    _fail_recovery_fsync(monkeypatch)

    with pytest.raises(routes.SavedPromptPersistenceError):
        routes._save_saved_prompts([{"id": "c", "text": "three"}])

    assert bak.is_file(), "the pre-existing recovery copy must not be destroyed"
    assert bak.read_text(encoding="utf-8") == before_backup
    assert p.read_text(encoding="utf-8") == before_primary, "the primary must be untouched"


def test_a_failed_recovery_preparation_leaves_no_staged_temps(tmp_path, monkeypatch):
    """Only its own staged temps are cleaned up — never somebody else's."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    _fail_recovery_fsync(monkeypatch)

    with pytest.raises(routes.SavedPromptPersistenceError):
        routes._save_saved_prompts([{"id": "c", "text": "three"}])

    leftovers = [q.name for q in p.parent.iterdir() if ".tmp" in q.name]
    assert leftovers == [], f"staged temps must be cleaned up: {leftovers}"


def test_a_failed_recovery_publication_still_refuses_the_delete(
    tmp_path, monkeypatch
):
    """Route level: a recovery publication that fails after preparation answers
    500 with the prompt still on disk, rather than reporting a success over a
    mutation whose recovery copy is gone."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    bak = Path(str(p) + ".bak")
    before_backup = bak.read_text(encoding="utf-8")
    _fail_recovery_fsync(monkeypatch)

    handler = _delete_via_route(monkeypatch, p, "a")

    assert handler.status == 500
    assert "error" in handler.json_body()
    assert bak.is_file() and bak.read_text(encoding="utf-8") == before_backup
    assert routes._load_saved_prompts()[1]["id"] == "b", "the delete must not have landed"


# ── Alias classification: which direction actually depends on which ────────
#
# `p.is_symlink() and p.resolve() == backup.resolve()` matches all four
# topologies, so it refuses reverse aliases that are actually safe. The
# dependency has to be read from the link targets themselves.


def _make_symlinked_primary(routes, p):
    """Publish a generation, then make the primary a symlink onto its own
    referent file. Returns the referent path."""
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    real = p.parent / "prompts-real.json"
    os.replace(p, real)
    p.symlink_to(real.name)
    return real


def test_a_backup_aliased_onto_a_symlinked_primary_is_permitted(
    tmp_path, monkeypatch
):
    """Safe reverse alias: `primary -> real.json` with `backup -> primary`.

    Replacing the *backup* entry leaves the primary link and its referent
    intact, so this save must succeed. The guard must classify direction, not
    merely test `primary is a symlink` with an equal referent."""
    routes, p = _paths(tmp_path, monkeypatch)
    real = _make_symlinked_primary(routes, p)
    bak = Path(str(p) + ".bak")
    bak.symlink_to(p.name)

    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])

    assert p.is_symlink(), "the primary must keep its symlink"
    assert json.loads(real.read_text(encoding="utf-8"))[1]["id"] == "b"
    assert not bak.is_symlink(), "the recovery copy lands on its own inode"
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert routes._load_saved_prompts()[1]["id"] == "b"


def test_a_backup_aliased_onto_the_referent_is_permitted(tmp_path, monkeypatch):
    """Safe reverse alias variant: `primary -> real.json` with
    `backup -> real.json`. Both entries share a referent, but replacing the
    backup entry cannot damage the primary's link."""
    routes, p = _paths(tmp_path, monkeypatch)
    real = _make_symlinked_primary(routes, p)
    bak = Path(str(p) + ".bak")
    bak.symlink_to(real.name)

    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])

    assert p.is_symlink()
    assert not bak.is_symlink(), "the recovery copy lands on its own inode"
    assert json.loads(real.read_text(encoding="utf-8"))[1]["id"] == "b"
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]


def test_a_primary_symlinked_onto_a_backup_with_its_own_target_is_refused(
    tmp_path, monkeypatch
):
    """Unsafe forward alias, chained: `primary -> backup -> real.json`.

    The primary's referent chain runs *through* the backup entry, so replacing
    the backup would strand the primary and the replacement would be published
    through it. Still refused, with everything intact."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    real = p.parent / "prompts-real.json"
    os.replace(p, real)
    bak = Path(str(p) + ".bak")
    bak.symlink_to(real.name)          # backup -> real.json
    p.symlink_to(bak.name)             # primary -> backup -> real.json

    with pytest.raises(routes.SavedPromptPersistenceError):
        routes._save_saved_prompts([{"id": "b", "text": "two"}])

    assert p.is_symlink() and bak.is_symlink(), "the refusal must not touch either link"
    assert json.loads(real.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}]


def test_a_forward_alias_is_refused_at_the_route_level(tmp_path, monkeypatch):
    """The forward topology answers 500 at the route, with the prompt intact."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    bak = Path(str(p) + ".bak")
    p.replace(bak)
    p.symlink_to(bak.name)

    handler = _delete_via_route(monkeypatch, p, "a")

    assert handler.status == 500
    assert "error" in handler.json_body()
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}]


def test_a_safe_reverse_alias_succeeds_through_the_route(tmp_path, monkeypatch):
    """The reverse topology the guard used to refuse must now publish cleanly
    end-to-end through the real DELETE dispatcher."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "z", "text": "keep"}])
    real = p.parent / "prompts-real.json"
    os.replace(p, real)
    p.symlink_to(real.name)
    Path(str(p) + ".bak").symlink_to(p.name)

    handler = _delete_via_route(monkeypatch, p, "z")

    assert handler.status == 200 and handler.json_body() == {"ok": True}
    assert [row["id"] for row in routes._load_saved_prompts()] == ["a"]
    assert json.loads(Path(str(p) + ".bak").read_text(encoding="utf-8")) == [
        {"id": "a", "text": "one"},
        {"id": "z", "text": "keep"},
    ]


# ── Recovery-copy ownership: mode AND group ────────────────────────────────


def _restricted_group() -> int:
    """A group this process is a member of but whose gid is not its egid, so a
    recovery copy published with the process's default group is detectable."""
    alternatives = [gid for gid in sorted(os.getgroups()) if gid != os.getegid()]
    if not alternatives:
        pytest.skip("no supplementary group available to distinguish the recovery group")
    return alternatives[0]


def test_the_recovery_copy_keeps_the_primarys_group(tmp_path, monkeypatch):
    """A `0660` primary shared with a restricted group must not publish a
    recovery copy the process's default group can read."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    bak = Path(str(p) + ".bak")
    os.chown(p, os.stat(p).st_uid, _restricted_group())
    os.chmod(p, 0o660)
    primary_stat = os.stat(p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])

    backup_stat = os.stat(bak)
    assert backup_stat.st_gid == primary_stat.st_gid, (
        f"the recovery copy must keep the primary's group, got "
        f"{backup_stat.st_gid} != {primary_stat.st_gid}"
    )
    assert backup_stat.st_mode & 0o777 == 0o660
    assert backup_stat.st_uid == primary_stat.st_uid


def test_the_recovery_copy_never_widens_a_group_only_primary(tmp_path, monkeypatch):
    """If the primary is group-only or group+world readable, the recovery copy
    must be at most as open as the primary — never more."""
    routes, p = _paths(tmp_path, monkeypatch)
    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    bak = Path(str(p) + ".bak")
    os.chown(p, os.stat(p).st_uid, _restricted_group())

    old_umask = os.umask(0o022)
    try:
        os.chmod(p, 0o640)
        routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    finally:
        os.umask(old_umask)

    assert os.stat(bak).st_mode & 0o777 == 0o640
