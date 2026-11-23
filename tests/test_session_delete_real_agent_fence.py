"""Session delete with gateway runtime rows, against the active Agent's real store.

The store, its schema and triggers, and the runtime rows all come from the Agent's
own producer API (admit/claim/settle, worker register/finish), and the delete runs
through WebUI's real ``delete_cli_session``. Expectations are keyed on which
retirement fence the active Agent ships (the current store module, the pre-refactor
module, or none), never on a version string. Without an Agent that has the runtime
ledger, every test here skips with a reason; the WebUI-only logic is covered by
``test_session_delete_runtime_ledger.py``.
"""

import hashlib
import importlib
import importlib.util
import inspect
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


CURRENT_FENCE = "persistence.sqlite.conversation._store.retirement"
LEGACY_FENCE = "hermes_state_mutation_retirement"
MEDIA_ROOT_MODULE = "gateway.session_ingress_media"
RETIRED_PREFIX = "gateway.retired_session.v1."
ADMISSION_PREFIX = "gateway.terminal_admission.v1."
WORKER_PREFIX = "gateway.terminal_worker.v1."
MEDIA_PREFIX = "gateway.media.retired.v1."
HOSTED_DOCUMENT_LINE = "\n[Shared attachment] file: "
PRINCIPAL = "webui-test"


def _module_present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _fence_kind():
    """Which retirement fence the active Agent ships: ``current``, ``legacy`` or None."""
    if _module_present(CURRENT_FENCE):
        return "current"
    if _module_present(LEGACY_FENCE):
        return "legacy"
    return None


class _StoreWriter:
    """Writes one profile's store through the Agent's persistence assembly."""

    def __init__(self, home):
        from persistence_assembly import open_persistence, profile_scope

        self.home = home
        self._handle = open_persistence(profile_scope(home=home))
        self.epoch = self._handle.runtime.begin_epoch(instance_id=PRINCIPAL)

    def session(self, sid):
        from persistence.conversation.sessions import NewSession

        self._handle.sessions.create(NewSession(session_id=sid, source="cli"))

    def admit(self, sid, request_id, text):
        record, _created = self._handle.admissions.admit(
            epoch=self.epoch, principal_id=PRINCIPAL, session_id=sid,
            request_id=request_id, payload={"text": text},
        )
        return record.admission_id

    def claim(self, sid):
        claimed = self._handle.admissions.claim(epoch=self.epoch, session_id=sid)
        return claimed.admission_id, claimed.generation

    def settle(self, admission_id, generation):
        self._handle.admissions.settle(
            epoch=self.epoch, admission_id=admission_id, generation=generation,
            outcome="completed", result={"final_response": "done"},
        )

    def restart(self):
        """A new owner epoch: started work of the old one becomes unknown."""
        self.epoch = self._handle.runtime.begin_epoch(instance_id=PRINCIPAL + "-restart")
        self._handle.admissions.recover(epoch=self.epoch)

    def register_worker(self, sid, execution_id, generation):
        self._handle.worker_executions.register(
            epoch=self.epoch, execution_id=execution_id, session_id=sid,
            generation=generation, kind="compute", adoption_secret="webui-test-secret",
        )

    def finish_worker(self, sid, execution_id, generation):
        self._handle.worker_executions.finish(
            epoch=self.epoch, execution_id=execution_id, session_id=sid,
            generation=generation, sequence=1,
        )

    def close(self):
        from persistence_assembly import close_stores_under

        self._handle.close()
        close_stores_under(self.home)


class _LegacyWriter:
    """Writes one profile's store through the pre-refactor runtime functions."""

    def __init__(self, home):
        from hermes_state import SessionDB
        import hermes_state_runtime

        self.home = home
        self._rt = hermes_state_runtime
        self._db = SessionDB(db_path=home / "state.db")
        self.epoch = self._rt.begin_runtime_epoch(self._db, instance_id=PRINCIPAL)

    def session(self, sid):
        self._db.create_session(sid, "cli")

    def admit(self, sid, request_id, text):
        row = self._rt.admit_session_input(
            self._db, epoch=self.epoch, principal_id=PRINCIPAL, session_id=sid,
            request_id=request_id, payload={"text": text},
        )
        return row["admission_id"]

    def claim(self, sid):
        claimed = self._rt.claim_session_input(self._db, epoch=self.epoch, session_id=sid)
        return claimed["admission_id"], claimed["generation"]

    def settle(self, admission_id, generation):
        self._rt.settle_session_input(
            self._db, epoch=self.epoch, admission_id=admission_id, generation=generation,
            outcome="completed", result={"final_response": "done"},
        )

    def restart(self):
        self.epoch = self._rt.begin_runtime_epoch(self._db, instance_id=PRINCIPAL + "-restart")
        self._rt.recover_session_inputs(self._db, epoch=self.epoch)

    def register_worker(self, sid, execution_id, generation):
        self._rt.register_worker_execution(
            self._db, epoch=self.epoch, execution_id=execution_id, session_id=sid,
            generation=generation, kind="compute", adoption_secret="webui-test-secret",
        )

    def finish_worker(self, sid, execution_id, generation):
        self._rt.mutate_worker_execution(
            self._db, epoch=self.epoch, execution_id=execution_id, session_id=sid,
            generation=generation, sequence=1, operation="execution.finish", payload={},
        )

    def close(self):
        self._db.close()


_LEGACY_PRODUCERS = (
    "begin_runtime_epoch", "admit_session_input", "claim_session_input",
    "settle_session_input", "recover_session_inputs", "register_worker_execution",
    "mutate_worker_execution",
)


def _writer_class():
    if _module_present("persistence_assembly") and _module_present("persistence.conversation.sessions"):
        return _StoreWriter
    if _module_present("hermes_state_runtime") and _module_present("hermes_state"):
        runtime = importlib.import_module("hermes_state_runtime")
        if all(callable(getattr(runtime, name, None)) for name in _LEGACY_PRODUCERS):
            return _LegacyWriter
    return None


@contextmanager
def _agent_store(home):
    writer_class = _writer_class()
    if writer_class is None:
        pytest.skip("the active Agent has no runtime-ledger producer (admissions / worker executions)")
    home.mkdir(parents=True, exist_ok=True)
    writer = writer_class(home)
    try:
        yield writer
    finally:
        writer.close()


def _runtime_generation(home, sid):
    conn = sqlite3.connect(home / "state.db")
    try:
        return conn.execute("SELECT runtime_generation FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    finally:
        conn.close()


def _terminal_turn(writer, sid, request_id, text="hello"):
    writer.admit(sid, request_id, text)
    admission_id, generation = writer.claim(sid)
    writer.settle(admission_id, generation)
    return admission_id


def _terminal_worker(writer, sid, execution_id):
    generation = _runtime_generation(writer.home, sid)
    writer.register_worker(sid, execution_id, generation)
    writer.finish_worker(sid, execution_id, generation)


def _snapshot(home):
    conn = sqlite3.connect(home / "state.db")
    try:
        return {
            "sessions": sorted(r[0] for r in conn.execute("SELECT id FROM sessions")),
            "admissions": sorted(conn.execute(
                "SELECT admission_id, target_session_id, status FROM session_admissions").fetchall()),
            "workers": sorted(conn.execute(
                "SELECT execution_id, session_id, status FROM worker_executions").fetchall()),
            "receipts": conn.execute("SELECT COUNT(*) FROM worker_receipts").fetchone()[0],
            "meta": sorted(r[0] for r in conn.execute("SELECT key FROM state_meta")),
        }
    finally:
        conn.close()


def _use_profile(monkeypatch, home):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)


def _hide(monkeypatch, *names):
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)


def _media_key(path):
    return MEDIA_PREFIX + hashlib.sha256(str(path).encode()).hexdigest()


def test_terminal_runtime_rows_retire_through_the_agent_fence(tmp_path, monkeypatch):
    import api.models as models

    home = tmp_path / "profile"
    with _agent_store(home) as writer:
        writer.session("sid")
        writer.session("bystander")
        admission_id = _terminal_turn(writer, "sid", "req-sid")
        _terminal_worker(writer, "sid", "exec-sid")
        _terminal_turn(writer, "bystander", "req-bystander")
    _use_profile(monkeypatch, home)
    before = _snapshot(home)
    assert ("exec-sid", "sid", "terminal") in before["workers"]
    assert before["receipts"] == 1

    if _fence_kind() is None:
        with pytest.raises(models.SessionDeleteRefused) as excinfo:
            models.delete_cli_session("sid")
        assert excinfo.value.code == "runtime_ledger_unavailable"
        assert _snapshot(home) == before
        return

    assert models.delete_cli_session("sid") is True
    after = _snapshot(home)
    assert after["sessions"] == ["bystander"]
    assert [row[1] for row in after["admissions"]] == ["bystander"]
    assert after["workers"] == []
    assert after["receipts"] == 0
    meta = set(after["meta"])
    # The fence is published: the retired-id marker, and the terminal receipts as
    # exact-retry evidence.
    assert RETIRED_PREFIX + "sid" in meta
    assert RETIRED_PREFIX + "bystander" not in meta
    assert ADMISSION_PREFIX + admission_id in meta
    assert WORKER_PREFIX + "exec-sid" in meta


@pytest.mark.parametrize(
    "live,code",
    [
        ("queued", "session_busy"),
        ("started", "session_busy"),
        ("unknown", "unknown_execution"),
        ("registered_worker", "session_busy"),
    ],
)
def test_live_runtime_work_refuses_and_deletes_nothing(tmp_path, monkeypatch, live, code):
    import api.models as models

    home = tmp_path / "profile"
    with _agent_store(home) as writer:
        writer.session("sid")
        if live == "registered_worker":
            _terminal_turn(writer, "sid", "req-done")
            writer.register_worker("sid", "exec-live", _runtime_generation(home, "sid"))
        else:
            writer.admit("sid", "req-live", "hello")
            if live in ("started", "unknown"):
                writer.claim("sid")
            if live == "unknown":
                writer.restart()
    _use_profile(monkeypatch, home)
    sessions_dir = home / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    (sessions_dir / "sid.json").write_text("{}", encoding="utf-8")
    before = _snapshot(home)

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    expected = code if _fence_kind() is not None else "runtime_ledger_unavailable"
    assert excinfo.value.code == expected
    assert _snapshot(home) == before
    assert (sessions_dir / "sid.json").exists()


def _current_fence_takes_media_root():
    if not _module_present(CURRENT_FENCE):
        return False
    fence = importlib.import_module(CURRENT_FENCE)
    return "media_root" in inspect.signature(fence.retire_sessions).parameters


def test_named_profile_delete_uses_that_profiles_media_root(tmp_path, monkeypatch):
    if not _current_fence_takes_media_root():
        pytest.skip("the active Agent's fence takes no media_root (it resolves its own root)")
    import api.models as models
    from gateway.session_ingress_media import native_inputs_root

    # The server process's own profile root: what a delete must NOT use for another profile.
    launch_root = Path(native_inputs_root())
    named_home = (tmp_path / "profiles" / "named").resolve()
    named_root = (named_home / "cache" / "documents" / "native-inputs").resolve()
    assert launch_root != named_root
    named_doc = named_root / ("b" * 64) / "named-profile.pdf"
    launch_doc = launch_root / ("c" * 64) / "launch-profile.pdf"
    planted = []
    try:
        for doc in (named_doc, launch_doc):
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_bytes(b"%PDF-1.4\n")
            planted.append(doc)
        prompt = ("please read" + HOSTED_DOCUMENT_LINE + str(named_doc) + "\n"
                  + HOSTED_DOCUMENT_LINE + str(launch_doc) + "\n")
        with _agent_store(named_home) as writer:
            writer.session("sid")
            _terminal_turn(writer, "sid", "hosted:req-doc", text=prompt)
        _use_profile(monkeypatch, named_home)

        assert models.delete_cli_session("sid") is True

        meta = set(_snapshot(named_home)["meta"])
        assert RETIRED_PREFIX + "sid" in meta
        # Only a document under the deleted profile's own root becomes a deletion
        # candidate; the launch profile's file gains no deletion authority.
        assert _media_key(named_doc) in meta
        assert _media_key(launch_doc) not in meta
        # Retirement records candidates; it never unlinks inside the delete.
        assert named_doc.exists() and launch_doc.exists()
    finally:
        for doc in planted:
            doc.unlink(missing_ok=True)
            try:
                doc.parent.rmdir()
            except OSError:
                pass


def test_unresolvable_media_root_fails_closed(tmp_path, monkeypatch):
    if not _current_fence_takes_media_root():
        pytest.skip("the active Agent's fence takes no media_root")
    import api.models as models

    home = tmp_path / "profile"
    with _agent_store(home) as writer:
        writer.session("sid")
        writer.session("idle")
        _terminal_turn(writer, "sid", "req-sid")
    _use_profile(monkeypatch, home)
    _hide(monkeypatch, MEDIA_ROOT_MODULE)
    before = _snapshot(home)

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == "runtime_ledger_unavailable"
    assert _snapshot(home) == before
    # A session without runtime rows still deletes (the unchanged degrade).
    assert models.delete_cli_session("idle") is True
    assert _snapshot(home)["sessions"] == ["sid"]


def test_no_importable_fence_keeps_the_refusal_and_the_degrade(tmp_path, monkeypatch):
    import api.models as models

    home = tmp_path / "profile"
    with _agent_store(home) as writer:
        writer.session("sid")
        writer.session("idle")
        _terminal_turn(writer, "sid", "req-sid")
    _use_profile(monkeypatch, home)
    _hide(monkeypatch, CURRENT_FENCE, LEGACY_FENCE)
    before = _snapshot(home)

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == "runtime_ledger_unavailable"
    assert _snapshot(home) == before
    assert models.delete_cli_session("idle") is True
    after = _snapshot(home)
    assert after["sessions"] == ["sid"]
    assert not any(key.startswith(RETIRED_PREFIX) for key in after["meta"])
