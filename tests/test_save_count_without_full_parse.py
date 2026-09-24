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

What the check needs is ONE integer -- how many messages the file on disk
holds -- and `save()` already writes that integer into the metadata prefix,
before `messages`, as `message_count`, exactly so readers can have it without
parsing the body (that is how `load_metadata_only()` and the sidebar freshness
check work). So `save()` reads that, through the same bounded 64 KiB prefix
reader, and falls back to the full read + parse whenever the prefix does not
carry a usable count. The `.bak` body is the one thing that still needs the
full text, and a shrink is the one time it is needed.

The count is read from the FILE, not cached in the object, and that is the
point. An earlier revision of this PR trusted an in-memory (inode, size,
mtime_ns) signature: "this object wrote the file and stat says nothing changed,
so its length is already known". Review (nesquena-hermes, 2026-09-21) showed
that is not a content identity -- a same-length in-place rewrite inside one
mtime tick keeps all three fields, because ext4 stamps mtime from a coarse
clock, so a stale cached count reads a real shrink as a growth and skips the
#1558 backup. That is the test at the bottom of this file. A count read from
the bytes cannot be fooled that way: any rewrite of the bytes rewrites it.

Fail-open is the contract these tests pin -- whenever the prefix has no usable
count (a legacy pre-#5854 sidecar, a file with no top-level `messages` key, a
corrupt or truncated prefix, metadata that overflows the budget), the behaviour
must be exactly today's full parse, never "assume no shrink".

`session_recovery._msg_count()` pays the same cost at boot and is left alone
on purpose: its "torn file -> -1" contract is what makes recovery restore a
`.bak` over a truncated live file, and no bounded read can prove a file is
not truncated.
"""
import json
import os
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
    empty = M.Session.load("r1")
    assert empty is not None
    empty.messages = []
    empty.active_stream_id = "a" * 32
    empty.pending_user_message = "still typing"
    calls = _spy_full_reads(monkeypatch, empty.path)

    empty.save()

    reads_by_save = calls["n"]  # taken before load() below, which reads the file too
    assert len(M.Session.load("r1").messages) == 5, "the populated file must survive"
    assert reads_by_save <= 1, f"refusing read the file {reads_by_save}x; an unknown identity costs at most one"


# ── save(): fail-open whenever the prefix carries no usable count ───────────

def _write_legacy_without_count(session_store, sid, n):
    """A pre-#5854 sidecar: no persisted `message_count` at all."""
    doc = {"session_id": sid, "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "messages": _msgs(n)}
    p = session_store / f"{sid}.json"
    p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return p


def test_legacy_sidecar_without_message_count_still_backs_up_on_shrink(session_store):
    """The prefix has no count to give, so this must fall back to the full
    parse and keep the safeguard. Silently treating "no count" as "no shrink"
    would reopen #1558."""
    _write_legacy_without_count(session_store, "l1", 5)
    s = M.Session.load("l1")
    assert s is not None
    s.messages = _msgs(3)

    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "fail-open: a legacy file must still be backed up on shrink"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 5


def test_file_changed_on_disk_since_last_save_falls_back_and_still_backs_up(session_store):
    """An externally grown file. This process saved 3 messages; something else
    then rewrote the file with 6 (an external appender, another process, a
    restore). The next save with 4 is a real shrink relative to DISK, and the
    count that governs it is the one the file now carries (6), not the 3 this
    process remembers: the `.bak` must hold the 6-message array."""
    s = _make(session_store, "x1", 3)
    external = {"session_id": "x1", "title": "T", "workspace": str(session_store.parent),
                "model": "glm", "created_at": 1.0, "updated_at": 2.0,
                "message_count": 6, "messages": _msgs(6)}
    s.path.write_text(json.dumps(external, indent=2), encoding="utf-8")

    s.messages = _msgs(4)
    with pytest.raises(M.StaleSessionGenerationError):
        s.save()
    assert json.loads(s.path.read_text(encoding="utf-8"))["messages"] == _msgs(6)
    s = M.Session.load("x1")
    assert s is not None
    s.messages = _msgs(4)
    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "an externally grown file must still be backed up on shrink"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 6


def test_file_changed_on_disk_without_a_count_still_backs_up_on_shrink(session_store):
    """The same external rewrite, but the replacement carries no
    `message_count` (an older writer, a hand-edited file). No prefix count ->
    the full parse runs and the 6-message array is still backed up."""
    s = _make(session_store, "x2", 3)
    external = {"session_id": "x2", "title": "T", "workspace": str(session_store.parent),
                "model": "glm", "created_at": 1.0, "updated_at": 2.0,
                "messages": _msgs(6)}
    s.path.write_text(json.dumps(external, indent=2), encoding="utf-8")

    s.messages = _msgs(4)
    with pytest.raises(M.StaleSessionGenerationError):
        s.save()
    assert json.loads(s.path.read_text(encoding="utf-8"))["messages"] == _msgs(6)
    s = M.Session.load("x2")
    assert s is not None
    s.messages = _msgs(4)
    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "fail-open: a count-less external rewrite must still be backed up"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 6


def test_unmarked_stale_count_from_a_foreign_writer_still_backs_up_on_shrink(session_store):
    """A persisted count with no `_mc_v` marker is not trusted.

    The exact shape review (nesquena-hermes, 2026-09-22) pinned: a sidecar
    materialized by an OLDER writer can carry a count stale against the
    messages array beside it (the recovery writer used to copy a denormalized
    state.db count over real rows). Here the foreign file says 2 but really
    holds 6; a save of 4 must not read that 2 as "the save grows the file" and
    skip the #1558 backup. The marker gate makes the prefix reader refuse the
    unmarked count, the full parse sees the real array, and the 6 messages
    land in the `.bak`. Remove the gate in `_prefix_message_count` and this
    test fails with no backup written.
    """
    s = _make(session_store, "x3", 3)
    foreign = {"session_id": "x3", "title": "T", "workspace": str(session_store.parent),
               "model": "glm", "created_at": 1.0, "updated_at": 2.0,
               "message_count": 2, "messages": _msgs(6)}  # stale count, NO _mc_v
    s.path.write_text(json.dumps(foreign, indent=2), encoding="utf-8")

    s.messages = _msgs(4)
    with pytest.raises(M.StaleSessionGenerationError):
        s.save()
    assert json.loads(s.path.read_text(encoding="utf-8"))["messages"] == _msgs(6)
    s = M.Session.load("x3")
    assert s is not None
    s.messages = _msgs(4)
    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "an unmarked stale count must not hide the shrink"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 6
    assert len(M.Session.load("x3").messages) == 4


def test_wrong_marker_version_still_backs_up_on_shrink(session_store):
    """A count marked by a DIFFERENT writer contract is equally untrusted."""
    s = _make(session_store, "x4", 3)
    foreign = {"session_id": "x4", "title": "T", "workspace": str(session_store.parent),
               "model": "glm", "created_at": 1.0, "updated_at": 2.0,
               "_mc_v": M._MESSAGE_COUNT_MARKER + 1,
               "message_count": 2, "messages": _msgs(6)}
    s.path.write_text(json.dumps(foreign, indent=2), encoding="utf-8")

    s.messages = _msgs(4)
    with pytest.raises(M.StaleSessionGenerationError):
        s.save()
    assert json.loads(s.path.read_text(encoding="utf-8"))["messages"] == _msgs(6)
    s = M.Session.load("x4")
    assert s is not None
    s.messages = _msgs(4)
    s.save()

    bak = s.path.with_suffix(".json.bak")
    assert bak.exists(), "a count from another writer version must not hide the shrink"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 6


def test_first_save_remarks_the_file_and_the_fast_path_resumes(session_store, monkeypatch):
    """The gate costs one full parse, once. After a save by the current writer
    the file carries `_mc_v`, and the cheap prefix path is back in service."""
    s = _make(session_store, "x5", 3)
    foreign = {"session_id": "x5", "title": "T", "workspace": str(session_store.parent),
               "model": "glm", "created_at": 1.0, "updated_at": 2.0,
               "message_count": 2, "messages": _msgs(6)}  # unmarked foreign file
    s.path.write_text(json.dumps(foreign, indent=2), encoding="utf-8")

    s.messages = _msgs(6)
    with pytest.raises(M.StaleSessionGenerationError):
        s.save()
    s = M.Session.load("x5")
    s.save()  # authorized legacy file becomes marked; no shrink

    assert json.loads(s.path.read_text(encoding="utf-8"))["_mc_v"] == M._MESSAGE_COUNT_MARKER

    calls = _spy_full_reads(monkeypatch, s.path)
    s.messages = _msgs(8)
    s.save()
    assert calls["n"] == 0, "a marked file must take the bounded-prefix path again"
    assert not s.path.with_suffix(".json.bak").exists()


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
    the array on purpose. The count that decides it comes from the file's own
    prefix, which the writer stamped with the length it wrote (5) -- the
    ON-DISK length, not the post-collapse length of the object (3). Read the
    object's own length instead and this backup silently disappears, so the
    fixture carries the count a real save() would have written."""
    doc = {"session_id": "h1", "title": "T", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "_mc_v": M._MESSAGE_COUNT_MARKER,
           "message_count": 5,
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


# ── the collision a stat identity cannot see (review, 2026-09-21) ───────────

def test_same_length_in_place_rewrite_inside_one_mtime_tick_still_backs_up(session_store):
    """A same-length, same-mtime, same-inode rewrite must not hide a shrink.

    The removed revision of this PR trusted an in-memory (inode, size,
    mtime_ns) identity: "this object wrote the file, stat says nothing changed,
    so the on-disk length is the one I remember". A stat tuple is not a content
    identity. Another writer replaces the sidecar IN PLACE with a longer
    transcript that occupies the same number of bytes, inside one mtime tick,
    so all three fields still match -- and the next shrinking save then reads
    its stale count as "growing", overwrites the longer transcript and writes
    no `.bak`. That is the #1558 data-loss shape the safeguard exists to
    prevent, reachable on the branch, which is why the identity cache is gone.

    The count now comes from the file's own metadata prefix, which any rewrite
    of the bytes has to carry, so the shrink is seen for what it is.
    """
    p = session_store / "i1.json"
    base = {"session_id": "i1", "title": "T", "workspace": str(session_store.parent),
            "model": "glm", "created_at": 1.0, "updated_at": 2.0}

    def _render(n):
        return json.dumps({**base, "_mc_v": M._MESSAGE_COUNT_MARKER,
                           "message_count": n, "messages": _msgs(n)},
                          indent=2, ensure_ascii=False)

    width = max(len(_render(2)), len(_render(5)))
    short, grown = _render(2).ljust(width), _render(5).ljust(width)
    assert len(short) == len(grown) == width
    p.write_text(short, encoding="utf-8")

    loaded = M.Session.load("i1")          # the only version this object knows
    before = p.stat()

    p.write_text(grown, encoding="utf-8")  # same inode, same byte length...
    os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = p.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == \
        (before.st_ino, before.st_size, before.st_mtime_ns), \
        "premise: the rewrite must be invisible to (inode, size, mtime_ns)"
    assert len(loaded.messages) == 2, "premise: this object remembers 2 messages"

    loaded.messages = _msgs(3)
    with pytest.raises(M.StaleSessionGenerationError):
        loaded.save()
    assert json.loads(p.read_text(encoding="utf-8"))["messages"] == _msgs(5)
    loaded = M.Session.load("i1")
    loaded.messages = _msgs(3)
    loaded.save()

    bak = p.with_suffix(".json.bak")
    assert bak.exists(), "the 5-message transcript must be recoverable"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 5
    assert len(M.Session.load("i1").messages) == 3
