"""#7644 — the saved-prompts ✕ must confirm before deleting, and the delete must
stay recoverable on disk.

The reporter lost a carefully written prompt to a single mis-click on the
12x12 px ✕ in the saved-prompts popup: the client fired
`DELETE /api/prompts {id}` on the first click, `_save_saved_prompts()` rewrote
`saved_prompts.json` in place, and nothing was kept, so the text existed
nowhere else.

Two invariants this pins:

1. Frontend (static/messages.js): the first click on the ✕ only *arms* a
   confirmation state (auto-disarmed after a few seconds); only a second,
   deliberate click sends the DELETE. A failed delete is surfaced through a
   toast instead of being swallowed by `catch(_e){}`.
2. Backend (api/routes.py): every rewrite of the store is atomic
   (temp file + fsync + os.replace) and the previous generation is copied to
   `saved_prompts.json.bak` *before* the rewrite, so a deleted prompt is
   recoverable from disk.
3. Backend durability of that backup (#7647 CR): if the backup cannot be
   committed the DELETE aborts with an error and the live store is untouched,
   and a repeat DELETE of an already-deleted id rewrites nothing — the first
   (oldest) backup is never rotated away by a retry.
4. Backend permissions (#7647 CR round 3): the backup is a copy of private
   text, so it is born with — and never wider than — the store's mode, even
   under a permissive umask; an already-wider backup is tightened.
5. Rotation ordering (#7647 CR round 3, minors): the backup only rotates for
   destructive writes (POST never rotates it), and never before a store write
   that cannot succeed (a read-only store must leave the older .bak alone).
6. Dismissal (#7647 CR round 3, minor): Escape (keydown) and click-away close
   the popup *and* disarm the pending ✕ confirmation.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlparse

import pytest

import api.routes as routes
from api import profiles

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

BACKUP_NAME = "saved_prompts.json.bak"


def _brace_body_after(src: str, marker: str) -> str:
    """Return the `{...}` body that follows *marker* (naive, balanced-brace walk)."""
    try:
        start = src.index(marker)
        brace = src.index("{", start)
    except ValueError:
        raise AssertionError(f"marker not found for body extraction: {marker!r}") from None
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:i]
        i += 1
    raise AssertionError(f"unclosed body for: {marker}")


def _brace_bodies_after(src: str, marker: str) -> list[str]:
    """Return every ``{...}`` body that follows *marker* (all occurrences)."""
    bodies: list[str] = []
    start = 0
    while True:
        idx = src.find(marker, start)
        if idx < 0:
            return bodies
        bodies.append(_brace_body_after(src[idx:], marker))
        start = idx + len(marker)


# ── frontend ──────────────────────────────────────────────────────────────────

def test_delete_fires_only_on_the_second_click():
    """The ✕ handler must gate the DELETE behind an armed (first-click) state."""
    body = _brace_body_after(MESSAGES_JS, "del.onclick=async(e)=>{")

    armed = re.search(r"if\s*\(\s*!\s*del\.classList\.contains\(\s*'is-confirming'\s*\)\s*\)", body)
    assert armed, (
        "the saved-prompt ✕ must have a first-click 'armed' branch (#7644): one "
        "mis-click on a 12x12 px button must not delete a prompt"
    )

    delete_call = body.index("method:'DELETE'")
    assert armed.start() < delete_call, "the confirmation guard must precede the DELETE call"
    assert "return;" in body[armed.start():delete_call], (
        "the first click must return before issuing the DELETE request"
    )
    assert re.search(r"setTimeout\s*\(\s*disarmDelete", body), (
        "the armed state must auto-disarm after a timeout so it cannot linger"
    )
    # the disarm helper clears the timer, the armed class and the pending row class
    disarm = _brace_body_after(MESSAGES_JS, "const disarmDelete=()=>{")
    assert "clearTimeout" in disarm
    assert "is-confirming" in disarm and "is-confirm-pending" in disarm
    assert "del.title=delTitle" in disarm and "aria-label" in disarm, (
        "disarming must restore the original tooltip and label"
    )


def test_delete_failure_is_surfaced_not_swallowed():
    """A failed DELETE must toast, not silently leave the row on screen."""
    body = _brace_body_after(MESSAGES_JS, "del.onclick=async(e)=>{")
    assert "e.stopPropagation()" in body, "the ✕ must not also trigger the row click"
    assert not re.search(r"catch\s*\(\s*\w+\s*\)\s*\{\s*\}", body), (
        "the delete handler must not swallow errors with an empty catch (#7644)"
    )
    assert "showToast" in body, "a failed delete must be reported to the user"
    assert "Failed to delete prompt" in body


def test_armed_state_is_styled_and_translated():
    """The confirmation state needs a visible style and a copy string."""
    assert ".saved-prompt-delete.is-confirming" in STYLE_CSS, (
        "the armed ✕ needs a distinct (danger-coloured) style"
    )
    assert ".saved-prompt-row.is-confirm-pending" in STYLE_CSS, (
        "the pending row needs a distinct style"
    )
    assert "saved_prompts_delete_confirm" in MESSAGES_JS
    assert re.search(r"saved_prompts_delete_confirm:\s*'[^']+'", I18N_JS), (
        "the en locale must carry a non-empty saved_prompts_delete_confirm string"
    )


def test_escape_and_click_away_close_the_armed_popup():
    """Escape (keydown) and click-away must close the popup AND disarm the ✕ (#7647).

    Escape used to be a no-op here and click-away only hid the popup: the ✕
    kept its armed (first-click) state until the 4 s timer, so a keyboard user
    who armed the delete, pressed Escape to cancel and pressed Enter again
    deleted the prompt anyway — exactly the mis-click the confirmation exists
    to prevent.
    """
    if "function _closeSavedPromptsPopup()" not in MESSAGES_JS:
        pytest.fail(
            "the saved-prompts popup needs a single close routine that Escape and "
            "click-away can both go through (#7647)"
        )
    close_body = _brace_body_after(MESSAGES_JS, "function _closeSavedPromptsPopup()")
    assert "_disarmSavedPromptDeletes()" in close_body, (
        "closing the popup must disarm every ✕ still armed for its second click"
    )
    assert "popup.style.display='none'" in close_body, "closing must hide the popup"
    assert "'aria-expanded','false'" in close_body, (
        "closing must collapse the trigger button's aria-expanded state"
    )

    disarm_body = _brace_body_after(MESSAGES_JS, "function _disarmSavedPromptDeletes()")
    assert "is-confirming" in disarm_body and "is-confirm-pending" in disarm_body, (
        "disarming must clear the armed ✕ class and the pending row class"
    )
    assert "_disarmDelete" in disarm_body, (
        "the disarm must go through the row's own closure so the 4 s arm timer "
        "is cleared, not just the classes"
    )
    assert "del._disarmDelete=disarmDelete" in MESSAGES_JS, (
        "each row must publish its disarm closure for the dismissal paths"
    )

    keydown_bodies = _brace_bodies_after(MESSAGES_JS, "popup.addEventListener('keydown'")
    assert keydown_bodies, (
        "the saved-prompts popup needs a keydown handler so Escape reaches it (#7647)"
    )
    keydown_body = keydown_bodies[0]
    assert "'Escape'" in keydown_body, "the popup keydown handler must listen for Escape"
    assert "_closeSavedPromptsPopup()" in keydown_body, (
        "Escape must route through the shared close routine (hide + disarm)"
    )

    click_bodies = [
        b
        for b in _brace_bodies_after(MESSAGES_JS, "document.addEventListener('click'")
        if "savedPromptsPopup" in b
    ]
    assert click_bodies, "the click-away dismissal listener must still exist"
    assert "_closeSavedPromptsPopup()" in click_bodies[0], (
        "click-away must disarm the armed ✕ instead of leaving it pending behind a hidden popup"
    )


# ── backend ───────────────────────────────────────────────────────────────────

class _FakeHandler:
    """Minimal stand-in for the BaseHTTPRequestHandler (see test_465)."""

    def __init__(self, path: str = "/api/prompts", command: str = "DELETE"):
        self.status = None
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = command
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass


@pytest.fixture
def prompt_store(monkeypatch, tmp_path):
    """Point the store at a tmp HERMES_HOME holding two saved prompts."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    store = tmp_path / "webui" / "saved_prompts.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            [
                {"id": "keepme", "label": "keep", "text": "keep this one", "created_at": 1.0},
                {"id": "gone", "label": "gone", "text": "the prompt the reporter lost", "created_at": 2.0},
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return store


def test_deleted_prompt_stays_recoverable_in_backup(prompt_store, monkeypatch):
    """DELETE /api/prompts must leave the previous generation on disk."""
    captured: dict = {}

    def _bad(_handler, msg, code=400):
        captured["bad"] = (msg, code)
        return True

    def _j(_handler, obj, *_args, **kwargs):
        captured["payload"] = obj
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **k: False)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"id": "gone"})
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "j", _j)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "bad" not in captured, "a valid delete must not be rejected"

    remaining = json.loads(prompt_store.read_text(encoding="utf-8"))
    assert [p["id"] for p in remaining] == ["keepme"]

    backup = prompt_store.with_name(BACKUP_NAME)
    assert backup.exists(), (
        "DELETE rewrote the store with no backup — the deleted prompt is gone " "for good (#7644)"
    )
    recovered = json.loads(backup.read_text(encoding="utf-8"))
    assert [p["id"] for p in recovered] == ["keepme", "gone"], (
        "the backup must hold the full previous generation, including the deleted prompt"
    )
    assert recovered[1]["text"] == "the prompt the reporter lost"
    assert remaining == [recovered[0]], "the live store keeps the surviving prompt unchanged"

    assert list(prompt_store.parent.glob("*.tmp")) == [], "atomic write left a temp file behind"
    assert captured.get("payload") == {"ok": True}, "the DELETE response shape must not change"


def _wire_delete(monkeypatch, captured: dict, prompt_id: str) -> None:
    """Stub the DELETE pipeline; whatever the handler answers lands in *captured*.

    `bad` fills `captured["bad"] = (msg, status)` and `j` fills
    `captured["payload"]`, so a test can tell "aborted" from "succeeded".
    """

    def _bad(_handler, msg, status: int = 400, *_args, **_kwargs):
        captured["bad"] = (msg, status)
        return True

    def _j(_handler, obj, *_args, **_kwargs):
        captured["payload"] = obj
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **k: False)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"id": prompt_id})
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "j", _j)


def test_delete_aborts_when_the_backup_cannot_be_written(prompt_store, monkeypatch):
    """No committed backup → the DELETE must fail with the store untouched (#7647).

    The backup write is simulated as failing (no space left). Swallowing that
    failure — the old behaviour — deleted the prompt with no recovery copy
    anywhere while still answering `{"ok": True}`.
    """
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    real_write = routes._write_text_atomic

    def _fail_backup_write(path, text, mode=None):
        if path.name.endswith(".bak"):
            raise OSError("simulated: no space left on device")
        real_write(path, text)

    monkeypatch.setattr(routes, "_write_text_atomic", _fail_backup_write)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True

    assert "payload" not in captured, (
        "the delete reported success although the recovery backup was never written — "
        "the prompt is now deleted with no copy anywhere (#7644)"
    )
    assert captured.get("bad", (None, None))[1] == 500, (
        "an uncommittable backup must abort the delete with a server error"
    )

    remaining = json.loads(prompt_store.read_text(encoding="utf-8"))
    assert [p["id"] for p in remaining] == ["keepme", "gone"], (
        "the live store must not be modified when the backup cannot be committed"
    )
    backup = prompt_store.with_name(BACKUP_NAME)
    assert not backup.exists(), "a failed backup write must not leave a partial .bak behind"
    assert list(prompt_store.parent.glob("*.tmp")) == [], "the aborted write left a temp file behind"


def test_repeat_delete_never_rotates_the_first_backup(prompt_store, monkeypatch):
    """A second DELETE of the same id must preserve the first (oldest) backup (#7647).

    Old behaviour: the repeat found nothing to delete but rewrote the store
    anyway, rotating `.bak` onto a generation that no longer contains the
    deleted prompt — the recovery path eating itself on a stale retry.
    """
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert captured.get("payload") == {"ok": True}
    backup = prompt_store.with_name(BACKUP_NAME)
    assert backup.exists(), "the first delete must leave a backup"
    first_backup = backup.read_text(encoding="utf-8")
    assert [p["id"] for p in json.loads(first_backup)] == ["keepme", "gone"]

    captured.clear()
    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "bad" not in captured, "a repeat delete of an already-deleted id is not an error"
    assert captured.get("payload") == {"ok": True}, "DELETE stays idempotent for the client"

    assert backup.read_text(encoding="utf-8") == first_backup, (
        "the repeat DELETE rotated .bak onto a generation without the deleted prompt — "
        "the recovery copy ate itself"
    )
    remaining = json.loads(prompt_store.read_text(encoding="utf-8"))
    assert [p["id"] for p in remaining] == ["keepme"], "the store keeps only the surviving prompt"
    assert list(prompt_store.parent.glob("*.tmp")) == [], "atomic write left a temp file behind"


def test_store_write_is_atomic_and_backs_up_before_replacing(prompt_store):
    """No in-place `write_text` on the store; the backup lands before the swap."""
    save_src = inspect.getsource(routes._save_saved_prompts)
    assert "_write_text_atomic" in save_src, "the store must be written atomically"
    assert not re.search(r"\.write_text\(", save_src), (
        "`Path.write_text` truncates in place; a crash mid-write used to lose every prompt"
    )
    assert save_src.index("_saved_prompts_backup_path") < save_src.index("_write_text_atomic(p,"), (
        "the previous generation must be copied to .bak before the store is replaced"
    )

    helper_src = inspect.getsource(routes._write_text_atomic)
    assert "os.replace" in helper_src and "fsync" in helper_src

    # A plain save is append-only (#7647 minor): it must NOT rotate the
    # recovery backup, otherwise a deleted prompt stays recoverable only until
    # the next save overwrites .bak with a generation that no longer has it.
    backup = prompt_store.with_name(BACKUP_NAME)
    previous_generation = json.dumps(
        [{"id": "older", "label": "older", "text": "previous generation", "created_at": 0.5}],
        ensure_ascii=False,
        indent=2,
    )
    backup.write_text(previous_generation, encoding="utf-8")

    routes._save_saved_prompts([{"id": "keepme", "label": "keep", "text": "keep this one"}])
    assert backup.read_text(encoding="utf-8") == previous_generation, (
        "a plain (non-destructive) save rotated the recovery backup — the "
        "deleted prompt is recoverable only until the next save (#7647)"
    )
    assert [p["id"] for p in json.loads(prompt_store.read_text(encoding="utf-8"))] == ["keepme"]


def test_delete_aborts_when_store_corrupted_or_unreadable(prompt_store, monkeypatch):
    """Corrupt JSON in saved prompts store must abort DELETE with 500 without modifying file."""
    prompt_store.write_text("{corrupted-json: invalid", encoding="utf-8")
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "keepme")

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured
    assert captured.get("bad", (None, None))[1] == 500
    assert prompt_store.read_text(encoding="utf-8") == "{corrupted-json: invalid"
    backup = prompt_store.with_name(BACKUP_NAME)
    assert not backup.exists()


def _wire_post(monkeypatch, captured: dict, payload: dict) -> None:
    """Stub the POST pipeline; whatever `handle_post` answers lands in *captured*."""

    def _bad(_handler, msg, status: int = 400, *_args, **_kwargs):
        captured["bad"] = (msg, status)
        return True

    def _j(_handler, obj, *_args, **_kwargs):
        captured["payload"] = obj
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **k: False)
    monkeypatch.setattr(routes, "read_body", lambda _handler: payload)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "j", _j)


def test_backup_is_not_wider_than_the_store_under_umask_002(prompt_store, monkeypatch):
    """A `0600` store must yield a `0600` backup even under a permissive umask (#7647).

    The atomic writer creates brand-new files through ``os.open(..., 0666)``
    so the kernel applies the process umask: under umask ``002`` the recovery
    backup was born ``0664`` while the store it protects stayed ``0600`` —
    the file holding the deleted prompt's text readable by group/other.

    Measured red→green on this same file: before the fix the assertion below
    reported ``0o664``.
    """
    os.chmod(prompt_store, 0o600)
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    previous_umask = os.umask(0o002)
    try:
        assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    finally:
        os.umask(previous_umask)

    assert captured.get("payload") == {"ok": True}
    backup = prompt_store.with_name(BACKUP_NAME)
    assert backup.exists(), "the delete must still commit a recovery backup"
    store_mode = stat.S_IMODE(prompt_store.stat().st_mode)
    backup_mode = stat.S_IMODE(backup.stat().st_mode)
    assert store_mode == 0o600, "the live store must keep its 0600 mode"
    assert backup_mode == store_mode, (
        f"the recovery backup is {oct(backup_mode)} under umask 002 while the "
        f"store it copies is {oct(store_mode)} — the deleted prompt's text is "
        "now readable beyond the store's permission"
    )
    assert list(prompt_store.parent.glob("*.tmp")) == [], "atomic write left a temp file behind"


def test_wider_existing_backup_is_tightened_to_the_store_mode(prompt_store, monkeypatch):
    """A `0664` leftover backup must be tightened to the store's `0600` (#7647).

    The backup writer preserves an existing inode's mode, so a backup created
    earlier (by an older build, or by hand) stayed group/other-readable for
    every later generation.
    """
    os.chmod(prompt_store, 0o600)
    backup = prompt_store.with_name(BACKUP_NAME)
    backup.write_text(
        json.dumps([{"id": "older", "label": "older", "text": "previous", "created_at": 0.5}]),
        encoding="utf-8",
    )
    os.chmod(backup, 0o664)
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert captured.get("payload") == {"ok": True}

    backup_mode = stat.S_IMODE(backup.stat().st_mode)
    assert backup_mode == stat.S_IMODE(prompt_store.stat().st_mode) == 0o600, (
        f"the pre-existing backup stayed {oct(backup_mode)} — replacing it must "
        "tighten it to the store's mode, not inherit the wider one"
    )
    # ...and the rotation still happened: .bak now holds the deleted prompt.
    assert [p["id"] for p in json.loads(backup.read_text(encoding="utf-8"))] == ["keepme", "gone"]


def test_post_save_does_not_rotate_the_recovery_backup(prompt_store, monkeypatch):
    """POST /api/prompts is append-only: it must never rotate `.bak` (#7647).

    Old behaviour: every save rewrote the backup from the live store, so a
    deleted prompt stayed recoverable only until the user's next save — the
    next POST silently replaced the recovery copy with a generation that no
    longer contained it.
    """
    backup = prompt_store.with_name(BACKUP_NAME)
    previous_generation = json.dumps(
        [{"id": "older", "label": "older", "text": "previous generation", "created_at": 0.5}],
        ensure_ascii=False,
        indent=2,
    )
    backup.write_text(previous_generation, encoding="utf-8")

    captured: dict = {}
    _wire_post(monkeypatch, captured, {"text": "a brand new prompt"})

    assert routes.handle_post(_FakeHandler(command="POST"), urlparse("/api/prompts")) is True
    assert captured.get("bad") is None, f"the save was rejected: {captured.get('bad')}"
    assert captured.get("payload", {}).get("ok") is True

    assert backup.read_text(encoding="utf-8") == previous_generation, (
        "POST rotated the recovery backup — a deleted prompt is now "
        "recoverable only until the next save (#7647)"
    )
    saved = json.loads(prompt_store.read_text(encoding="utf-8"))
    assert [p["text"] for p in saved][-1] == "a brand new prompt", (
        "the append itself must still land in the live store"
    )


def test_read_only_store_aborts_before_the_backup_rotates(prompt_store, monkeypatch):
    """A store that refuses writes must abort the DELETE *before* `.bak` rotates (#7647).

    Old behaviour: `.bak` was rotated from the live store first and only then
    did the store write fail — the older, useful recovery copy was destroyed
    for nothing, and the 500 blamed the backup step.

    The write refusal is simulated through the writer's own probe because this
    suite also runs as root, where ``0444`` bits do not block a write.
    """
    backup = prompt_store.with_name(BACKUP_NAME)
    previous_generation = json.dumps(
        [{"id": "older", "label": "older", "text": "previous generation", "created_at": 0.5}],
        ensure_ascii=False,
        indent=2,
    )
    backup.write_text(previous_generation, encoding="utf-8")

    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    def _refuse_write(_path):
        raise PermissionError("simulated: saved prompts store is read-only")

    monkeypatch.setattr(routes, "_require_writable_target", _refuse_write, raising=False)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured, "a read-only store cannot be deleted from"
    message, status = captured.get("bad", (None, None))
    assert status == 500
    assert "not writable" in str(message), (
        f"the 500 must name the real blocker (read-only store), got: {message!r}"
    )
    assert backup.read_text(encoding="utf-8") == previous_generation, (
        "the backup rotated ahead of a store write that could never succeed — "
        "the older recovery copy was destroyed for nothing (#7647)"
    )
    assert [p["id"] for p in json.loads(prompt_store.read_text(encoding="utf-8"))] == [
        "keepme",
        "gone",
    ], "the live store must be untouched"


def test_delete_fails_closed_when_the_backup_file_cannot_be_created(prompt_store, monkeypatch):
    """Declared behaviour change: no committable backup anywhere → 500, store untouched (#7647).

    Simulates a read-only directory with no ``.bak`` yet (temp creation for
    the backup is denied — exactly what ``EACCES`` on the directory produces).
    ``master`` deleted the prompt and answered 200 because it kept no backup
    at all; this PR keeps fail-closed on purpose, as declared in the PR body
    for hardened read-only deployments: a "recoverable delete" must not be
    able to become a delete with no copy anywhere.
    """
    from api import paths as api_paths

    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    real_create = api_paths._create_atomic_temp_file

    def _deny_backup_temp(write_path, *, existing):
        if Path(write_path).name.endswith(".bak"):
            raise PermissionError("simulated: directory is not writable")
        return real_create(write_path, existing=existing)

    monkeypatch.setattr(api_paths, "_create_atomic_temp_file", _deny_backup_temp)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured, (
        "the delete succeeded although no recovery backup could be committed"
    )
    assert captured.get("bad", (None, None))[1] == 500

    assert [p["id"] for p in json.loads(prompt_store.read_text(encoding="utf-8"))] == [
        "keepme",
        "gone",
    ], "fail-closed means the live store is untouched"
    assert not prompt_store.with_name(BACKUP_NAME).exists()
    assert list(prompt_store.parent.glob("*.tmp")) == [], "the aborted write left a temp file behind"

