"""#7647 re-review (2026-11-18T01:38:01Z) — the restore itself must be crash-safe.

Both earlier fixes are accepted: a store cut inside a multi-byte character now
reaches the verified rollback, and a rolled-back delete no longer rotates an
existing `.bak`. The remaining finding is about the *order* inside the ambiguous
arm: restore-first means that during the restore the store is partial and the
preimage lives only in `saved_prompts.json.bak.pending.<uuid>`, which the loader
never reads. If the process exits during that restore:

* with an older `.bak` from a previous delete, the next load falls back to it, so
  a prompt deleted earlier comes back and the prompt saved after that delete is
  lost (`[keep, older, gone]` instead of `[keep, gone, newer]`);
* with no `.bak` yet, the next load returns `[]`.

Requested change: publish the staged preimage to `.bak` before the restore
(keeping the previous `.bak` bytes), put the previous generation back after a
**verified** restore, and leave the preimage in `.bak` if that put-back fails.

These tests run each step in its own process, because "the process exits during
the restore write" cannot be simulated in-process. The seam is the file object's
`write` on the store's inode — the same in-place branch of `api/paths` the
certificate used; staging, classification, publication and rollback are the real
code paths, and `handle_delete` / `handle_post` are the real handlers.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_NAME = "saved_prompts.json.bak"
STORE_NAME = "saved_prompts.json"

GENERATION = [
    {"id": "keep", "label": "keep", "text": "keep this", "created_at": 1.0},
    {"id": "older", "label": "older", "text": "deleted earlier", "created_at": 2.0},
    {"id": "gone", "label": "gone", "text": "deleted now", "created_at": 3.0},
]

_CHILD_SOURCE = '''
"""One step of the #7647 restore-crash schedule, in its own process."""
import errno
import io
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.environ["PROBE_REPO"])

home = Path(sys.argv[1])
op = sys.argv[2]
args = sys.argv[3:]

import api.routes as routes  # noqa: E402
from api import profiles  # noqa: E402

profiles.get_active_hermes_home = lambda: home


class _FakeHandler:
    def __init__(self, command="DELETE"):
        self.status = None
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = command
        self.path = "/api/prompts"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass


def _wire(body, captured):
    def _bad(_h, msg, status=400, *_a, **_k):
        captured["bad"] = (msg, status)
        return True

    def _j(_h, obj, *_a, **_k):
        captured["payload"] = obj
        return True

    routes._check_csrf = lambda _h: True
    routes._handle_extension_sidecar_proxy = lambda *a, **k: False
    routes.read_body = lambda _h: body
    routes._guard_request_session_visibility = lambda *a, **k: True
    routes.bad = _bad
    routes.j = _j


class _SeamWriter:
    """Proxy for the store's in-place write (text mode, "w")."""

    def __init__(self, raw, index, mode):
        self._raw = raw
        self._index = index
        self._mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        try:
            self._raw.close()
        except Exception:
            pass
        return False

    def write(self, text):
        if self._mode == "fail_once" and self._index > 1:
            return self._raw.write(text)
        # One character: the store is left a decodable but incomplete prefix.
        self._raw.write(text[:1])
        self._raw.flush()
        if self._mode == "crash" and self._index > 1:
            os._exit(77)
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        self._raw.flush()

    def fileno(self):
        return self._raw.fileno()


def _arm(store, mode):
    st = store.stat()
    real_fdopen = os.fdopen
    seen = {"n": 0}

    def _fdopen(fd, *a, **kw):
        file_mode = kw.get("mode", a[0] if a else "")
        try:
            s = os.fstat(fd)
            same = (s.st_dev, s.st_ino) == (st.st_dev, st.st_ino)
        except OSError:
            same = False
        raw = real_fdopen(fd, *a, **kw)
        if same and file_mode == "w":
            seen["n"] += 1
            return _SeamWriter(raw, seen["n"], mode)
        return raw

    os.fdopen = _fdopen


store = home / "webui" / "saved_prompts.json"
captured = {}

if op == "delete":
    mode = None
    if "--crash" in args:
        mode = "crash"
        _arm(store, mode)
    elif "--fail-first-write" in args:
        mode = "fail_once"
        _arm(store, mode)
    _wire({"id": args[0]}, captured)
    handled = routes.handle_delete(_FakeHandler(), urlparse("/api/prompts"))
    print(json.dumps({"handled": handled, "captured": captured}, default=str))
elif op == "post":
    _wire({"text": args[0], "label": args[0]}, captured)
    handled = routes.handle_post(_FakeHandler("POST"), urlparse("/api/prompts"))
    print(json.dumps({"handled": handled, "captured": captured}, default=str))
elif op == "load":
    print(json.dumps([entry.get("id") for entry in routes._load_saved_prompts()]))
else:
    raise SystemExit("unknown op: " + op)
'''


@pytest.fixture
def child(tmp_path):
    """Materialize the child driver once per test (#7647 restore-crash schedule)."""
    script = tmp_path / "restore_crash_child.py"
    script.write_text(_CHILD_SOURCE, encoding="utf-8")
    return script


def _make_home(tmp_path: Path, entries: list) -> Path:
    home = tmp_path / "hermes-home"
    (home / "webui").mkdir(parents=True)
    store = home / "webui" / STORE_NAME
    store.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    # nlink > 1: api.paths takes its in-place branch, which truncates and then
    # writes the payload through a file object -- the certificate's seam.
    os.link(store, home / "webui" / "store-hardlink")
    return home


def _run(script: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PROBE_REPO"] = str(REPO_ROOT)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    return subprocess.run(
        [sys.executable, str(script), str(home), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(tmp_path_of(script)),
    )


def tmp_path_of(script: Path) -> Path:
    return script.parent


def _last_json(proc: subprocess.CompletedProcess):
    lines = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    assert lines, f"child produced no output (rc={proc.returncode}, stderr={proc.stderr[-800:]})"
    return json.loads(lines[-1])


def _delete(script: Path, home: Path, prompt_id: str, *flags: str) -> subprocess.CompletedProcess:
    return _run(script, home, "delete", prompt_id, *flags)


def _post_newer(script: Path, home: Path) -> str:
    proc = _run(script, home, "post", "newer")
    payload = _last_json(proc).get("captured", {})
    assert payload.get("payload", {}).get("ok"), payload
    return payload["payload"]["prompt"]["id"]


def _load_ids(script: Path, home: Path) -> list:
    return _last_json(_run(script, home, "load"))


def _backup_bytes(home: Path) -> bytes:
    return (home / "webui" / BACKUP_NAME).read_bytes()


def _store_bytes(home: Path) -> bytes:
    return (home / "webui" / STORE_NAME).read_bytes()


def test_crash_during_the_restore_keeps_the_newer_generation(child, tmp_path):
    """Schedule 1: an earlier `.bak` must not swallow the newer store generation."""
    home = _make_home(tmp_path, GENERATION)

    assert _last_json(_delete(child, home, "older")).get("captured", {}).get("payload") == {"ok": True}
    assert _load_ids(child, home) == ["keep", "gone"], "sanity: the first delete lands"
    assert _backup_bytes(home), "sanity: the first delete publishes the older generation"

    newer_id = _post_newer(child, home)
    assert _load_ids(child, home) == ["keep", "gone", newer_id]

    crash = _delete(child, home, "gone", "--crash")
    assert crash.returncode == 77, (
        "the seam must exit inside the restore write, not before it: "
        f"rc={crash.returncode} stdout={crash.stdout[-400:]} stderr={crash.stderr[-400:]}"
    )

    loaded = _load_ids(child, home)
    assert loaded == ["keep", "gone", newer_id], (
        "a crash during the restore must still leave the newest generation loadable: the "
        "staged preimage has to be published to the recovery copy BEFORE the store is "
        f"restored, otherwise the older `.bak` wins (loaded={loaded}, "
        f"bak={_backup_bytes(home)!r}, store={_store_bytes(home)!r})"
    )


def test_crash_during_the_restore_without_an_earlier_backup_keeps_the_preimage(child, tmp_path):
    """Schedule 2: with no earlier `.bak`, the load must not collapse to an empty list."""
    home = _make_home(tmp_path, [GENERATION[0], GENERATION[2]])

    newer_id = _post_newer(child, home)
    assert _load_ids(child, home) == ["keep", "gone", newer_id]
    assert not (home / "webui" / BACKUP_NAME).exists(), "sanity: no recovery copy yet"

    crash = _delete(child, home, "gone", "--crash")
    assert crash.returncode == 77, (
        "the seam must exit inside the restore write: "
        f"rc={crash.returncode} stdout={crash.stdout[-400:]}"
    )

    loaded = _load_ids(child, home)
    assert loaded == ["keep", "gone", newer_id], (
        "with no earlier recovery copy, publishing the staged preimage before the restore is "
        "the only thing that keeps the store loadable after a crash mid-restore "
        f"(loaded={loaded}, store={_store_bytes(home)!r})"
    )


def test_verified_rollback_restores_the_previous_backup_bytes(child, tmp_path):
    """Control: a completed rollback leaves the previous `.bak` byte-identical."""
    home = _make_home(tmp_path, GENERATION)

    assert _last_json(_delete(child, home, "older")).get("captured", {}).get("payload") == {"ok": True}
    newer_id = _post_newer(child, home)
    previous_backup = _backup_bytes(home)
    assert previous_backup, "sanity: the earlier generation is on disk"

    rolled_back = _delete(child, home, "gone", "--fail-first-write")
    answer = _last_json(rolled_back).get("captured", {})
    message, status = answer.get("bad", (None, None))
    assert status == 500, answer
    assert "nothing was deleted" in message, message

    assert _load_ids(child, home) == ["keep", "gone", newer_id], (
        f"the verified rollback must restore the preimage (store={_store_bytes(home)!r})"
    )
    assert _backup_bytes(home) == previous_backup, (
        "after a verified restore the previous recovery generation has to come back "
        "byte-identical: an implementation that publishes the staged preimage and forgets "
        "to put the old one back silently drops the prompt a previous delete made recoverable"
    )
