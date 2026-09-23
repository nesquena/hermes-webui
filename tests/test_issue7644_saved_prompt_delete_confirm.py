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
"""
from __future__ import annotations

import inspect
import io
import json
import re
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
    start = src.index(marker)
    brace = src.index("{", start)
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


# ── backend ───────────────────────────────────────────────────────────────────

class _FakeHandler:
    """Minimal stand-in for the BaseHTTPRequestHandler (see test_465)."""

    def __init__(self, path: str = "/api/prompts"):
        self.status = None
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "DELETE"
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

    def _fail_backup_write(path, text):
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

    # A second delete must still leave the *previous* (post-first-delete) state.
    routes._save_saved_prompts([{"id": "keepme", "label": "keep", "text": "keep this one"}])
    assert json.loads(prompt_store.with_name(BACKUP_NAME).read_text(encoding="utf-8")) == [
        {"id": "keepme", "label": "keep", "text": "keep this one", "created_at": 1.0},
        {"id": "gone", "label": "gone", "text": "the prompt the reporter lost", "created_at": 2.0},
    ]
    assert [p["id"] for p in json.loads(prompt_store.read_text(encoding="utf-8"))] == ["keepme"]
