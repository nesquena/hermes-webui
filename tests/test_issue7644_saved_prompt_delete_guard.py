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
import io
import json

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
    assert "from api.paths import _atomic_write_text" in source, (
        "_save_saved_prompts must delegate the atomic publish to api.paths"
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
    assert dump_at < source.index("_atomic_write_text(backup"), (
        "serialize before publishing the recovery copy"
    )
    assert dump_at < source.index("_atomic_write_text(p, payload)"), (
        "serialize before publishing the primary"
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
