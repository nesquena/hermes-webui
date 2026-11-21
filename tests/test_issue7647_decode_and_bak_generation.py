"""#7647 gate certificate (2026-11-18T00:04:36Z) — decode failures and `.bak` generations.

Certificate, reproduced against `4a70f411f`:

1. *A partial write that ends inside a multi-byte character escapes the recovery
   ladder.* The post-failure read-back in `_save_saved_prompts()` catches only
   `OSError`, so a store cut mid-character raises `UnicodeDecodeError` out of
   `handle_delete`: no rollback runs, the store is left undecodable and the only
   copy of the pre-delete generation sits in an orphaned
   `saved_prompts.json.bak.pending.<uuid>` file that nothing reads. The next load
   falls back to `.bak`, resurrecting a prompt the user had already deleted, and
   the next delete writes it back into the store.
2. *A rolled-back delete still rotates `.bak`.* In the ambiguous-store arm the
   staged preimage is published over `.bak` *before* the restore. After a
   successful delete, a second delete that fails partway is rolled back and
   answered "nothing was deleted", yet the first deleted prompt no longer exists
   in any file.

The umbrella case is the in-place store write (a hard-linked store, extended
attributes, or an ownership/temp-file refusal). Both regressions drive the real
in-place writer through the real `handle_delete`, staging and classification; the
only substituted edge is the file object's `write`, which emits the first N bytes
of the payload and then raises `ENOSPC` — exactly the certificate's seam, with the
real `os.fdopen`, `os.ftruncate` and hard-link branch of `api/paths`.

Controls keep the two legitimate endings honest: cutting one byte earlier (before
the character) still reaches the verified rollback and restores the store, and a
clean delete still publishes the previous generation.
"""
from __future__ import annotations

import errno
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

# Non-ASCII text so a byte offset can land inside a multi-byte character.
GENERATION = [
    {"id": "gone", "label": "gone", "text": "ação apagada pelo usuário", "created_at": 1.0},
    {"id": "keepme", "label": "keep", "text": "café que fica na loja", "created_at": 2.0},
]
PREIMAGE_IDS = [entry["id"] for entry in GENERATION]


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


def _write_store(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def prompt_store(monkeypatch, tmp_path):
    """A hard-linked store: the in-place writer is the real code path here."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    store = tmp_path / "webui" / STORE_NAME
    _write_store(store, GENERATION)
    # nlink > 1 makes api.paths._atomic_write_text take its in-place branch,
    # which truncates and then writes through a file object.
    os.link(store, tmp_path / "webui" / "store-hardlink")
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
    try:
        return [entry.get("id") for entry in json.loads(Path(path).read_text(encoding="utf-8"))]
    except Exception:
        return []


def _recovery_ids_elsewhere(directory: Path) -> set:
    """Ids recoverable from files next to the store (the live store excluded)."""
    found: set = set()
    for candidate in sorted(Path(directory).iterdir()):
        if candidate.name in (STORE_NAME, "store-hardlink") or not candidate.is_file():
            continue
        found.update(item for item in _ids(candidate) if item)
    return found


class _CuttingWriter:
    """File-object proxy: write the first *cut_bytes* bytes of the payload, then fail."""

    def __init__(self, raw, cut_bytes: int):
        self._raw = raw
        self._cut_bytes = cut_bytes
        self._spent = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        try:
            self._raw.close()
        except Exception:
            pass
        return False

    def write(self, text):
        if self._spent:  # a later write (the rollback) is not this cut
            return self._raw.write(text)
        self._spent = True
        self._raw.write(text.encode("utf-8")[: self._cut_bytes])
        self._raw.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        self._raw.flush()

    def fileno(self):
        return self._raw.fileno()


def _arm_store_cut(monkeypatch, store: Path, *, cut_bytes: int, armed: dict) -> None:
    """Cut the store's in-place write after *cut_bytes* bytes, once ``armed`` is set."""
    real_fdopen = os.fdopen
    stat = store.stat()
    spent = {"done": False}

    def _fdopen(fd, *args, **kwargs):
        mode = kwargs.get("mode", args[0] if args else "")
        try:
            opened = os.fstat(fd)
            same_inode = (opened.st_dev, opened.st_ino) == (stat.st_dev, stat.st_ino)
        except OSError:
            same_inode = False
        if same_inode and mode == "w" and armed.get("on") and not spent["done"]:
            spent["done"] = True
            return _CuttingWriter(real_fdopen(fd, "wb"), cut_bytes)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", _fdopen)


def _mid_character_cut(payload: str) -> int:
    """Byte offset that lands one byte inside the first multi-byte character."""
    data = payload.encode("utf-8")
    start = next(index for index, byte in enumerate(data) if byte >= 0xC0)
    return start + 1


def _boundary_cut(payload: str) -> int:
    """Byte offset just before the first multi-byte character: a decodable prefix."""
    data = payload.encode("utf-8")
    return next(index for index, byte in enumerate(data) if byte >= 0xC0)


def test_mid_character_cut_is_classified_as_an_unreadable_store(prompt_store, monkeypatch):
    """Defect 1: a store cut inside a character must not escape as UnicodeDecodeError."""
    payload = json.dumps([GENERATION[1]], ensure_ascii=False, indent=2)
    armed = {"on": False}
    _arm_store_cut(monkeypatch, prompt_store, cut_bytes=_mid_character_cut(payload), armed=armed)

    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    armed["on"] = True

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured, "a store the classification could not read is not a success"

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    # The store has to be readable again and hold the pre-delete generation: the
    # decoder failure must reach the same verified rollback as the control below.
    assert _ids(prompt_store) == PREIMAGE_IDS, (
        "the store was left undecodable or incomplete instead of being rolled back: "
        f"{prompt_store.read_bytes()!r} (message={message!r})"
    )
    assert "nothing was deleted" in message, message
    assert not list(prompt_store.parent.glob("*.pending.*")), (
        "the classification died before it could dispose of the staged copy, leaving an "
        f"orphan that nothing reads (message={message!r})"
    )


def test_previous_recovery_generation_survives_a_rolled_back_delete(prompt_store, monkeypatch):
    """Defect 2: a failed second delete must not destroy the first one's recovery copy."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    backup = prompt_store.parent / BACKUP_NAME
    assert _ids(backup) == PREIMAGE_IDS, "sanity: the first delete publishes the generation"

    # Second destructive write fails partway, at a decodable boundary so the
    # classification (not the decoder) decides the outcome: the store payload is
    # "[]" here, so one byte leaves a readable but incomplete store.
    armed = {"on": False}
    _arm_store_cut(monkeypatch, prompt_store, cut_bytes=1, armed=armed)
    captured.clear()
    _wire_delete(monkeypatch, captured, "keepme")
    armed["on"] = True

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert "nothing was deleted" in message, message
    assert _ids(prompt_store) == [GENERATION[1]["id"]], "the rollback must restore the preimage"
    assert "gone" in _recovery_ids_elsewhere(prompt_store.parent), (
        "the rolled-back delete rotated `.bak` onto its own preimage, so the prompt a "
        f"previous successful delete made recoverable now exists in no file (message={message!r})"
    )


def test_boundary_cut_still_reaches_the_verified_rollback(prompt_store, monkeypatch):
    """Control: cutting one byte earlier reaches the rollback and restores the store."""
    original = prompt_store.read_bytes()
    payload = json.dumps([GENERATION[1]], ensure_ascii=False, indent=2)
    armed = {"on": False}
    _arm_store_cut(monkeypatch, prompt_store, cut_bytes=_boundary_cut(payload), armed=armed)

    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    armed["on"] = True

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured
    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert "nothing was deleted" in message, message
    assert prompt_store.read_bytes() == original, "the verified rollback must restore the bytes"


def test_clean_delete_still_publishes_the_generation(prompt_store, monkeypatch):
    """Control: the happy path still publishes `.bak` and answers ok."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "bad" not in captured, captured
    assert captured.get("payload") == {"ok": True}

    backup = prompt_store.parent / BACKUP_NAME
    assert backup.is_file(), "the recovery copy must still be published on a clean delete"
    assert _ids(backup) == PREIMAGE_IDS
    assert _ids(prompt_store) == ["keepme"]
