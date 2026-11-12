"""Test-only WebUI server for the #7293 real-process drain tests.

Runs the real ``server.main()`` (real HTTP routes, run admission fence, signal
handlers, shutdown drain, cancel/settle path and session persistence) with
three test seams, all applied before the server starts:

* ``api.streaming._get_ai_agent`` returns ``SlowFakeAgent``: a deterministic
  in-process agent that streams ticks for ``sleep:<seconds>`` taken from the
  user message and honours ``interrupt()`` like the real agent. No provider,
  network or Hermes Agent checkout is needed.
* ``api.updates._apply_update_inner`` / ``_apply_force_update_inner`` skip
  git and just schedule the real restart (``_schedule_restart``), so the
  update path runs its real refusal, fence, restart-pending and settle logic.
* ``os.execv`` records what the server looked like at the moment it would
  have replaced the process image (active runs, admission phase and the
  on-disk session JSON) to ``<record_dir>/execv-<n>.json`` and returns, which
  the restart code treats as "the process was not replaced" (admission
  reopens). Bytecode purging is disabled so the checkout is not touched.

Usage: ``python tests/issue7293_drain_server.py <repo_root> <record_dir>``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time


# Set by main(): where SlowFakeAgent announces that a run has streamed output.
FIRST_OUTPUT_DIR: Path | None = None


class SlowFakeAgent:
    """Streams ``tickN`` deltas until ``sleep:<s>`` elapses or it is interrupted.

    After its first delta has been delivered to the WebUI stream callback it
    writes ``<record_dir>/first-output-<session_id>``, so a test can wait until
    a run has real partial output before it stops or settles that run.
    """

    def __init__(self, **kwargs):
        self._cb = kwargs.get("stream_delta_callback")
        self._interrupted = threading.Event()
        self.session_id = kwargs.get("session_id")
        self.model = kwargs.get("model") or "fake-model"
        self.platform = "webui"
        self.context_compressor = None
        self.ephemeral_system_prompt = None
        self._session_db = None

    def interrupt(self, message=None):
        self._interrupted.set()

    def run_conversation(self, user_message, system_message=None, conversation_history=None,
                         task_id=None, persist_user_message=None, **kwargs):
        history = list(conversation_history or [])
        text = str(user_message or "")
        seconds = 1.0
        if "sleep:" in text:
            try:
                seconds = float(text.split("sleep:", 1)[1].split()[0])
            except (ValueError, IndexError):
                pass
        user = {"role": "user", "content": persist_user_message or text}
        deadline = time.monotonic() + seconds
        emitted = []
        n = 0
        while time.monotonic() < deadline:
            if self._interrupted.is_set():
                return {"completed": False, "interrupted": True, "final_response": "",
                        "messages": history + [user]}
            chunk = f"tick{n} "
            n += 1
            emitted.append(chunk)
            if self._cb:
                try:
                    self._cb(chunk)
                except Exception:
                    pass
            if n == 1 and FIRST_OUTPUT_DIR is not None and self.session_id:
                marker = FIRST_OUTPUT_DIR / f"first-output-{self.session_id}"
                tmp = marker.with_suffix(".tmp")
                tmp.write_text(chunk, encoding="utf-8")
                os.replace(tmp, marker)
            self._interrupted.wait(0.1)
        final = "".join(emitted) + "DONE"
        return {"completed": True, "final_response": final,
                "messages": history + [user, {"role": "assistant", "content": final}]}


def main():
    if len(sys.argv) != 3:
        raise SystemExit("expected repository root and record directory")
    repo_root, record_dir = (Path(value) for value in sys.argv[1:])
    record_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(repo_root))
    global FIRST_OUTPUT_DIR
    FIRST_OUTPUT_DIR = record_dir

    import server
    from api import config, run_drain, streaming, updates

    streaming._get_ai_agent = lambda: SlowFakeAgent

    # Without a dynamic-home-capable Hermes Agent installed (hosted CI has
    # none), streaming serializes whole turns on the legacy skill-module patch
    # lock, so a second concurrent fake run could never stream. The fake agent
    # touches no skill modules, so that lock protects nothing here; swap in a
    # no-op lock so two runs stream concurrently, as they do on a current Agent.
    from api import profiles as _profiles

    class _NoopLock:
        def acquire(self, *_a, **_k):
            return True

        def release(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    if hasattr(_profiles, "_SKILL_HOME_MODULE_PATCH_LOCK"):
        _profiles._SKILL_HOME_MODULE_PATCH_LOCK = _NoopLock()

    def fake_update_inner(target, *_args, **_kwargs):
        # Long enough for the test to observe the restart-pending fence.
        updates._schedule_restart(float(os.environ.get("ISSUE7293_TEST_RESTART_DELAY", "1.0")))
        return {"ok": True, "message": f"{target} updated successfully (test stub)",
                "target": target, "restart_scheduled": True}

    updates._apply_update_inner = fake_update_inner
    updates._apply_force_update_inner = fake_update_inner
    updates._purge_agent_pycache = lambda _path: None

    execv_count = [0]

    def recording_execv(path, argv):
        execv_count[0] += 1
        sessions = {}
        for session_file in sorted(Path(config.SESSION_DIR).glob("*.json")):
            if session_file.name.startswith("_"):
                continue  # _index.json and other bookkeeping files
            try:
                sessions[session_file.stem] = json.loads(session_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        record = {
            "path": str(path),
            "active_runs": run_drain.webui_owned_active_runs(),
            "admission": run_drain.admission_status(),
            "sessions": sessions,
            "at": time.time(),
        }
        out = record_dir / f"execv-{execv_count[0]}.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, default=str), encoding="utf-8")
        os.replace(tmp, out)
        # Returning means "not replaced": _schedule_restart reopens admission.

    os.execv = recording_execv
    server.main()


if __name__ == "__main__":
    main()
