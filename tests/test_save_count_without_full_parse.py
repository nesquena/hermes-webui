"""A save must not read and parse the whole sidecar just to count its messages.

The #1558 backup safeguard in `Session.save()` compares the on-disk message
count with the incoming one, and writes `<sid>.json.bak` only when the save
would SHRINK the array. Sound. But it obtained the on-disk count by
`json.loads(self.path.read_text())` -- the entire file -- on EVERY save,
including the 99.9% of saves that grow the conversation and never back
anything up. `session_recovery._msg_count()` did the same on every boot, for
every live file that has a `.bak`.

Measured in production on 2026-09-15, in the running container:

    sessions/fa3bca34a0c6.json   203,439,398 bytes / 266,940 messages
      read_text     17,453 ms
      json.loads     2,924 ms
      TOTAL to obtain one integer: 20,377 ms   <- paid on every save

That was the 39,357 ms `/api/session` request stuck at stage
`t6_after_json_write`, after the read-side re-parse (#4633 recurrence) had
already been fixed. The cost scales linearly with the file, and the file grows
for as long as the conversation does.

`save()` is the one that wrote the previous version. If nothing has touched
the file since -- a `stat()` answers that: same inode, size and mtime_ns as the
payload it wrote -- then the on-disk array is exactly the one it wrote, and its
length is already known. No read at all. The full text is still needed for ONE
thing: the `.bak` body when a shrink is detected, and only then.

This deliberately does NOT trust the persisted `message_count` in the metadata
prefix. Three comments in models.py anticipate "external sidecar appends", and
a writer that appends to the array without bumping the count would make a
prefix-based check miss a real shrink. A stat signature cannot be fooled that
way: any external write changes it, and the code falls back to today's full
parse. Fail-open is the contract these tests pin -- whenever the signature is
unknown or does not match, the behaviour must be exactly today's, never
"assume no shrink".

`session_recovery._msg_count()` pays the same cost at boot and is left alone
on purpose: its "torn file -> -1" contract is what makes recovery restore a
`.bak` over a truncated live file, and no bounded read can prove a file is
not truncated.
"""
import json
import pathlib
from collections import OrderedDict

import pytest

import api.models as M


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "SESSION_DIR", sdir)
    monkeypatch.setattr(M, "SESSIONS", OrderedDict())
    return sdir


def _msgs(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(n)]


def _make(session_store, sid, n):
    s = M.Session(session_id=sid, title="T", workspace=str(session_store.parent),
                  model="glm", messages=_msgs(n))
    s.save()
    return s


def _spy_full_reads(monkeypatch, target):
    """Count `Path.read_text()` calls on ONE path.

    `read_text` is the whole-file read; the metadata-prefix reader uses `open()`
    with a byte budget and never goes through it. So this counter is exactly
    "how many times was this file read in full", which is the cost being removed.
    """
    calls = {"n": 0}
    real = pathlib.Path.read_text
    target = pathlib.Path(target)

    def counting(self, *a, **k):
        if pathlib.Path(self) == target:
            calls["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(type(pathlib.Path()), "read_text", counting)
    return calls


# ── save(): the grow path must be free of full reads ────────────────────────

def test_grow_save_does_not_read_the_existing_file_in_full(session_store, monkeypatch):
    """The common case. Appending messages must not read the previous file
    in full -- this object wrote that file and it is untouched, so the on-disk
    count is already known."""
    s = _make(session_store, "g1", 5)
    calls = _spy_full_reads(monkeypatch, s.path)

    s.messages = _msgs(7)
    s.save()

    assert calls["n"] == 0, f"grow-save read the existing sidecar in full {calls['n']}x"
    assert not s.path.with_suffix(".json.bak").exists(), "a grow-save must not produce a backup"
    assert len(M.Session.load("g1").messages) == 7


def test_same_size_save_does_not_read_the_existing_file_in_full(session_store, monkeypatch):
    """Metadata-only saves (title, flags, stream state) keep the array as-is.
    They are the most frequent save of all and must be just as cheap."""
    s = _make(session_store, "g2", 5)
    calls = _spy_full_reads(monkeypatch, s.path)

    s.title = "renamed"
    s.save()

    assert calls["n"] == 0
    assert not s.path.with_suffix(".json.bak").exists()


# ── save(): the safeguards must be untouched ────────────────────────────────

def test_shrink_save_still_writes_bak_with_the_pre_shrink_content(session_store, monkeypatch):
    """#1558 must keep working. A shrink still produces a `.bak` holding the
    pre-shrink array -- and reads the file in full exactly once, for that body.
    Not twice: knowing the count must not be followed by a redundant parse."""
    s = _make(session_store, "s1", 5)
    calls = _spy_full_reads(monkeypatch, s.path)

    s.messages = _msgs(3)
    s.save()

    reads_by_save = calls["n"]  # taken before load() below, which reads the file too
    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "a shrinking save must leave a recoverable backup"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 5
    assert len(M.Session.load("s1").messages) == 3
    assert reads_by_save == 1, f"shrink-save read the sidecar in full {reads_by_save}x; the .bak body needs exactly one"


def test_empty_active_snapshot_is_still_refused(session_store, monkeypatch):
    """The other guard in the same block: an empty array with a live stream or
    pending prompt must NOT overwrite a populated file (the #1558 data-loss
    shape). This object was neither loaded nor saved by this process, so it has
    no signature to trust and refusing may cost today's single full read --
    never more, and it must still refuse."""
    _make(session_store, "r1", 5)
    empty = M.Session(session_id="r1", title="T", workspace=str(session_store.parent),
                      model="glm", messages=[], active_stream_id="a" * 32,
                      pending_user_message="still typing")
    calls = _spy_full_reads(monkeypatch, empty.path)

    empty.save()

    reads_by_save = calls["n"]  # taken before load() below, which reads the file too
    assert len(M.Session.load("r1").messages) == 5, "the populated file must survive"
    assert reads_by_save <= 1, f"refusing read the file {reads_by_save}x; an unknown identity costs at most one"


# ── save(): fail-open whenever the identity is unknown or stale ─────────────

def _write_legacy_without_count(session_store, sid, n):
    """A pre-#5854 sidecar: no persisted `message_count` at all."""
    doc = {"session_id": sid, "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "messages": _msgs(n)}
    p = session_store / f"{sid}.json"
    p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return p


def test_legacy_sidecar_without_message_count_still_backs_up_on_shrink(session_store):
    """This object never loaded or saved the file, so it has no identity to
    compare against: fall back to the full parse and keep the safeguard.
    Silently treating "unknown" as "no shrink" would reopen #1558."""
    _write_legacy_without_count(session_store, "l1", 5)
    s = M.Session(session_id="l1", title="Legacy", workspace=str(session_store.parent),
                  model="glm", messages=_msgs(3))

    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "fail-open: a legacy file must still be backed up on shrink"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 5


def test_file_changed_on_disk_since_last_save_falls_back_and_still_backs_up(session_store):
    """The fail-open case the design exists for. This process saved 3 messages
    and remembers that. Something else then rewrote the file with 6 (an
    external appender, another process, a restore). The next save with 4 is a
    real shrink relative to DISK, and the remembered count must not be trusted
    over it: the signature no longer matches, so the full parse runs and the
    `.bak` holds the 6-message array."""
    s = _make(session_store, "x1", 3)
    external = {"session_id": "x1", "title": "T", "workspace": str(session_store.parent),
                "model": "glm", "created_at": 1.0, "updated_at": 2.0,
                "message_count": 6, "messages": _msgs(6)}
    s.path.write_text(json.dumps(external, indent=2), encoding="utf-8")

    s.messages = _msgs(4)
    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "an externally grown file must still be backed up on shrink"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 6


def _tool_partial(ts=123):
    """The exact shape the #2592 collapse recognises (copied from its test)."""
    return {"role": "assistant", "content": "", "_partial": True, "timestamp": ts,
            "reasoning": "same reasoning",
            "_partial_tool_calls": [{"name": "execute_code",
                                     "args": {"code": "raise RuntimeError('boom')"},
                                     "done": True, "is_error": True, "duration": 3.87}]}


def test_collapse_self_heal_on_load_still_backs_up_the_pre_collapse_array(session_store):
    """#2592: load() de-duplicates adjacent partials and immediately saves the
    shorter transcript, and that save must produce a `.bak` because it shrinks
    the array on purpose. The identity load() records matches the file, so the
    save skips the read -- which means the remembered count has to be the
    ON-DISK length load() saw (5), not the post-collapse length of the object
    (3). Record the wrong one and this backup silently disappears."""
    doc = {"session_id": "h1", "title": "T", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "messages": [{"role": "user", "content": "run this"},
                        _tool_partial(), _tool_partial(), _tool_partial(),
                        {"role": "assistant", "content": "**Task cancelled.**", "_error": True}]}
    (session_store / "h1.json").write_text(json.dumps(doc), encoding="utf-8")

    loaded = M.Session.load("h1")

    assert sum(1 for m in loaded.messages if m.get("_partial")) == 1, "the collapse must have fired"
    assert len(loaded.messages) == 3
    bak = (session_store / "h1.json").with_suffix(".json.bak")
    assert bak.exists(), "the self-heal shrink must leave the pre-collapse array recoverable"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 5
