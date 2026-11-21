"""#7647 round 4 — the verified preimage must outlive a failed publication or restore.

Review request (2026-11-17T14:31:07Z):

    Keep the verified preimage recoverable until publication or rollback is
    verified, including failed recovery publication and failed restore; report
    the actual outcome.

`_save_saved_prompts()` now keeps a staged recovery copy
(`saved_prompts.json.bak.pending.<uuid>`) and stages/publishes it around the
live-store write. Three arms of that transaction still *destroy* the only copy
of the generation the user wants back, or answer with a recovery claim that is
false:

* post-commit arm: the store replacement landed, publishing the staged copy to
  `.bak` raised, and the handler still promised that "the previous generation is
  in the recovery copy" — after deleting the staged preimage;
* store-write-success arm: same, plus the verified rollback also failed;
* verification arm: `os.replace` returned without publishing, so verification
  failed, the staged preimage was unlinked and the rollback then failed.

Invariant pinned here: on every failure path where the live store does **not**
verifiably hold the preimage, a file next to the store must still contain that
preimage, and the response must not claim a recovery copy that does not exist.
Controls keep the two legitimate endings honest: a verified rollback still says
nothing was deleted, and a normal DELETE still publishes `.bak`.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

import api.routes as routes
from api import profiles

STORE_NAME = "saved_prompts.json"
BACKUP_NAME = "saved_prompts.json.bak"
PREIMAGE_IDS = ["keepme", "gone"]


class _FakeHandler:
    """Minimal stand-in for the BaseHTTPRequestHandler."""

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
    store = tmp_path / "webui" / STORE_NAME
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


def _wire_delete(monkeypatch, captured: dict, prompt_id: str) -> None:
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


def _ids(path) -> list:
    return [entry.get("id") for entry in json.loads(Path(path).read_text(encoding="utf-8"))]


def _preimage_copies(directory: Path) -> list:
    """Files next to the store whose saved-prompt list is the generation to restore.

    The live store is excluded on purpose: the question is whether the preimage
    survived somewhere once the store no longer holds it.
    """
    found = []
    for candidate in sorted(Path(directory).iterdir()):
        if candidate.name == STORE_NAME or not candidate.is_file():
            continue
        try:
            if _ids(candidate) == PREIMAGE_IDS:
                found.append(candidate.name)
        except Exception:
            continue
    return found


def _fail_publication(monkeypatch) -> None:
    """`os.replace(staged, .bak)` raises: the recovery copy cannot be published."""
    real_replace = os.replace

    def _replace(src, dst):
        if Path(dst).name == BACKUP_NAME:
            raise OSError("simulated: recovery copy publication failed")
        return real_replace(src, dst)

    monkeypatch.setattr(routes.os, "replace", _replace)


def _publish_nothing(monkeypatch) -> None:
    """`os.replace(staged, .bak)` returns without publishing, so verification fails."""
    real_replace = os.replace

    def _replace(src, dst):
        if Path(dst).name == BACKUP_NAME:
            return None
        return real_replace(src, dst)

    monkeypatch.setattr(routes.os, "replace", _replace)


def _store_writer(monkeypatch, store: Path, behaviour) -> dict:
    """Wrap the live-store writer; *behaviour(n, real, path, text, mode)* decides.

    ``n`` is the 1-based index of a call whose destination is the live store, so
    a test can fail the destructive write, the rollback write, or both.
    """
    real_write = routes._write_text_atomic
    state = {"n": 0}

    def _write(path, text, mode=None):
        target = Path(path)
        if target == store:
            state["n"] += 1
            return behaviour(state["n"], real_write, target, text, mode)
        return real_write(target, text, mode=mode)

    monkeypatch.setattr(routes, "_write_text_atomic", _write)
    return state


def _assert_recovery_claim_is_true(message: str, store_dir: Path) -> None:
    """A response may only promise the recovery copy if `.bak` really holds it."""
    if (store_dir / BACKUP_NAME).is_file():
        return
    lowered = message.lower()
    for phrase in ("is in the recovery copy", "in the recovery copy and"):
        assert phrase not in lowered, (
            "the response promises a recovery copy that was never published: " f"{message!r}"
        )


def test_postcommit_publication_failure_keeps_the_preimage(prompt_store, monkeypatch):
    """Store replacement landed, publishing `.bak` failed: the preimage must survive.

    Old behaviour: the staged preimage was unlinked, so the deleted prompt
    existed nowhere, while the 500 said it was in the recovery copy.
    """
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _fail_publication(monkeypatch)

    def _commit_then_fail(_n, real_write, path, text, mode):
        real_write(path, text, mode=mode)  # rename landed, durability after it failed
        raise OSError("simulated: directory fsync after replace")

    _store_writer(monkeypatch, prompt_store, _commit_then_fail)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured, "a delete whose recovery copy was not published is not a success"

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _ids(prompt_store) == ["keepme"], "the live store keeps the replacement"
    assert _preimage_copies(prompt_store.parent), (
        "the previous generation was destroyed: no file next to the store still holds "
        f"{PREIMAGE_IDS} (message={message!r})"
    )
    _assert_recovery_claim_is_true(message, prompt_store.parent)


def test_store_write_and_publication_failure_keep_the_preimage(prompt_store, monkeypatch):
    """Store write succeeded, publication failed, rollback also failed."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _fail_publication(monkeypatch)

    def _first_ok_restore_fails(n, real_write, path, text, mode):
        if n == 1:
            return real_write(path, text, mode=mode)
        raise OSError("simulated: rollback write failed")

    _store_writer(monkeypatch, prompt_store, _first_ok_restore_fails)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _ids(prompt_store) == ["keepme"], "the destructive write cannot be rolled back here"
    assert _preimage_copies(prompt_store.parent), (
        "publication failure plus a failed rollback destroyed the only copy of the "
        f"previous generation (message={message!r})"
    )
    _assert_recovery_claim_is_true(message, prompt_store.parent)


def test_unpublished_recovery_copy_and_failed_rollback_keep_the_preimage(prompt_store, monkeypatch):
    """`os.replace` reports success without publishing; verification then fails."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _publish_nothing(monkeypatch)

    def _first_ok_restore_fails(n, real_write, path, text, mode):
        if n == 1:
            return real_write(path, text, mode=mode)
        raise OSError("simulated: rollback write failed")

    _store_writer(monkeypatch, prompt_store, _first_ok_restore_fails)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert not (prompt_store.parent / BACKUP_NAME).exists(), "sanity: nothing was published"
    assert _preimage_copies(prompt_store.parent), (
        "an unverified publication deleted the staged preimage before the rollback was "
        f"established (message={message!r})"
    )
    _assert_recovery_claim_is_true(message, prompt_store.parent)


def test_ambiguous_write_publication_failure_reports_the_actual_outcome(prompt_store, monkeypatch):
    """Truncated store + failed publication + failed rollback: no false promise."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _fail_publication(monkeypatch)

    def _truncate_then_fail(n, real_write, path, text, mode):
        if n == 1:
            path.write_text("", encoding="utf-8")
            raise OSError("simulated: in-place writer failed after truncation")
        raise OSError("simulated: rollback write failed")

    _store_writer(monkeypatch, prompt_store, _truncate_then_fail)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _preimage_copies(prompt_store.parent), (
        "the preimage must stay on disk when neither publication nor rollback was established"
    )
    _assert_recovery_claim_is_true(message, prompt_store.parent)


def test_verified_rollback_still_says_nothing_was_deleted(prompt_store, monkeypatch):
    """Control: publication failed but the rollback is verified — store is intact."""
    original_bytes = prompt_store.read_bytes()
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _fail_publication(monkeypatch)

    def _first_ok_restore_ok(n, real_write, path, text, mode):
        return real_write(path, text, mode=mode)

    _store_writer(monkeypatch, prompt_store, _first_ok_restore_ok)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert "nothing was deleted" in message, message
    assert prompt_store.read_bytes() == original_bytes, (
        "a verified rollback must leave the live store byte-identical to the preimage"
    )


def test_normal_delete_still_publishes_the_recovery_copy(prompt_store, monkeypatch):
    """Control: the happy path keeps publishing `.bak` and answers ok."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "bad" not in captured, captured
    assert captured.get("payload") == {"ok": True}

    backup = prompt_store.parent / BACKUP_NAME
    assert backup.is_file(), "the recovery copy must still be published on a clean delete"
    assert _ids(backup) == PREIMAGE_IDS
    assert _ids(prompt_store) == ["keepme"]
