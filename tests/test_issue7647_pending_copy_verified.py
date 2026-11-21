"""#7647 round 5 — a pending recovery copy may only be claimed once it is verified.

Review request (2026-11-17T16:29:30Z):

    Report recovery as unverified when pending-copy recreation fails; only claim
    a pending copy after its path and contents are verified.

`_ensure_saved_prompts_preimage_retained()` recreates the staged preimage when a
publication attempt consumed or replaced it. It swallows every failure and
returns nothing, and all four call sites then hand the *unverified* staging path
to the raised exception (`pending_path=staged_backup`) with the handler copy
answering "the previous generation is retained in a pending recovery copy".

So when the recreation itself fails there is no copy on disk at all, and when it
leaves a file whose contents are not the preimage the copy is worthless — in both
cases the 500 asserts a recovery artifact that does not exist and the user stops
looking for the bytes.

Invariant pinned here: the pending-recovery claim appears only after the path
exists, is a regular non-symlink file, and reads back byte-equal to the preimage;
otherwise the response reports recovery as unverified. Controls keep the two
legitimate endings honest: a recreation that succeeds is claimed (and readable),
and a surviving staged copy is still claimed after a failed publication.
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
PENDING_MARKER = ".pending."
PREIMAGE_IDS = ["keepme", "gone"]
UNVERIFIED_PHRASES = ("unverified", "could not be verified", "not verified")


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
    """Files next to the store whose saved-prompt list is the generation to restore."""
    found = []
    for candidate in sorted(Path(directory).iterdir()):
        if candidate.name == STORE_NAME or not candidate.is_file():
            continue
        try:
            if _ids(candidate) == PREIMAGE_IDS:
                found.append(candidate)
        except Exception:
            continue
    return found


def _wire_writer(monkeypatch, store: Path, behaviour) -> dict:
    """Wrap the single writer; *behaviour(kind, n, real_write, path, text, mode)* decides.

    ``kind`` is ``"store"`` for the live store and ``"pending"`` for any staged
    recovery copy (``<backup>.pending.<uuid>``); ``n`` is the 1-based index inside
    that kind, so the staging write can succeed while the recreation fails.
    """
    real_write = routes._write_text_atomic
    state = {"store": 0, "pending": 0}

    def _write(path, text, mode=None):
        target = Path(path)
        if target == store:
            state["store"] += 1
            return behaviour("store", state["store"], real_write, target, text, mode)
        if PENDING_MARKER in target.name:
            state["pending"] += 1
            return behaviour("pending", state["pending"], real_write, target, text, mode)
        return real_write(target, text, mode=mode)

    monkeypatch.setattr(routes, "_write_text_atomic", _write)
    return state


def _commit_then_fail(kind, _n, real_write, path, text, mode):
    """The live replacement lands; the durability step after it raises."""
    if kind == "store":
        real_write(path, text, mode=mode)
        raise OSError("simulated: directory fsync after replace")
    return real_write(path, text, mode=mode)


def _consume_then_lose(monkeypatch, leftover: "str | None" = None) -> None:
    """Publishing consumes the staged copy without producing a verified `.bak`.

    ``leftover`` writes that text at the staging path instead of removing it, i.e.
    a file exists where the preimage used to be but does not hold the preimage.
    """
    real_replace = os.replace

    def _replace(src, dst):
        if Path(dst).name == BACKUP_NAME:
            staged = Path(src)
            if leftover is None:
                staged.unlink(missing_ok=True)
            else:
                staged.write_text(leftover, encoding="utf-8")
            return None
        return real_replace(src, dst)

    monkeypatch.setattr(routes.os, "replace", _replace)


def _fail_publication(monkeypatch) -> None:
    """`os.replace(staged, .bak)` raises: the recovery copy cannot be published."""
    real_replace = os.replace

    def _replace(src, dst):
        if Path(dst).name == BACKUP_NAME:
            raise OSError("simulated: recovery copy publication failed")
        return real_replace(src, dst)

    monkeypatch.setattr(routes.os, "replace", _replace)


def _claims_pending_copy(message: str) -> bool:
    return "pending recovery copy" in message.lower()


def _reports_unverified(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in UNVERIFIED_PHRASES)


def test_failed_recreation_reports_recovery_as_unverified(prompt_store, monkeypatch):
    """Publication consumed the staged copy and the recreation failed: no claim.

    Old behaviour: the recreation failure was swallowed, `pending_path` was handed
    to the response anyway, and the 500 said the previous generation "is retained
    in a pending recovery copy" while nothing on disk held it.
    """
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _consume_then_lose(monkeypatch)

    def _staging_ok_recreation_fails(kind, n, real_write, path, text, mode):
        if kind == "pending" and n > 1:
            raise OSError("simulated: pending copy recreation failed")
        return _commit_then_fail(kind, n, real_write, path, text, mode)

    _wire_writer(monkeypatch, prompt_store, _staging_ok_recreation_fails)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured, "a delete without a recovery copy is not a success"

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _ids(prompt_store) == ["keepme"], "the live store keeps the replacement"
    assert _preimage_copies(prompt_store.parent) == [], (
        "sanity: with the recreation failing there is no copy to promise"
    )
    assert not _claims_pending_copy(message), (
        f"the response claims a pending recovery copy that was never written: {message!r}"
    )
    assert _reports_unverified(message), (
        f"the response must report recovery as unverified: {message!r}"
    )


def test_leftover_that_is_not_the_preimage_is_not_claimed(prompt_store, monkeypatch):
    """A staging file with the wrong bytes is not a recovery copy, even if it exists."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _consume_then_lose(monkeypatch, leftover='[{"id": "not-the-preimage"}]')

    def _staging_ok_recreation_fails(kind, n, real_write, path, text, mode):
        if kind == "pending" and n > 1:
            raise OSError("simulated: pending copy recreation failed")
        return _commit_then_fail(kind, n, real_write, path, text, mode)

    _wire_writer(monkeypatch, prompt_store, _staging_ok_recreation_fails)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _preimage_copies(prompt_store.parent) == [], (
        "sanity: the leftover does not hold the previous generation"
    )
    assert not _claims_pending_copy(message), (
        f"the response claims a pending recovery copy whose contents are not the preimage: {message!r}"
    )
    assert _reports_unverified(message), (
        f"the response must report recovery as unverified: {message!r}"
    )


def test_verified_recreation_is_claimed_and_readable(prompt_store, monkeypatch):
    """Control: when the recreation succeeds the claim is true and the copy reads back."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _consume_then_lose(monkeypatch)
    _wire_writer(monkeypatch, prompt_store, _commit_then_fail)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _claims_pending_copy(message), (
        f"a verified pending copy must still be reported as retained: {message!r}"
    )
    copies = _preimage_copies(prompt_store.parent)
    assert len(copies) == 1, (
        "the verified pending copy must be the only copy of the previous generation: "
        f"{[c.name for c in copies]}"
    )
    assert PENDING_MARKER in copies[0].name, (
        f"the retained copy must be the staged sibling, got {copies[0].name!r}"
    )


def test_surviving_staged_copy_is_claimed_after_failed_publication(prompt_store, monkeypatch):
    """Control: a failed publication that leaves the staged copy intact is still claimed."""
    captured: dict = {}
    _wire_delete(monkeypatch, captured, "gone")
    _fail_publication(monkeypatch)
    _wire_writer(monkeypatch, prompt_store, _commit_then_fail)

    assert routes.handle_delete(_FakeHandler(), urlparse("/api/prompts")) is True
    assert "payload" not in captured

    message, status = captured.get("bad", (None, None))
    assert status == 500, message
    assert _claims_pending_copy(message), (
        f"the staged copy survived, so the response may claim it: {message!r}"
    )
    assert _preimage_copies(prompt_store.parent), (
        "the claim must be backed by a file holding the previous generation"
    )
