"""Cold HTTP and persisted copies retain successors of fractional Stop owners."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from api import models
from tests.test_recovered_export_share_and_gateway_http import _request
from tests.test_recovered_surrogate_http import _new_server

FRACTION_SEED = r"""
import json,sys
from pathlib import Path

from tests.test_cancel_restart_journal_recovery import _start_cancelled_turn
from tests.test_webui_state_db_reconciliation import _make_state_db
from api import models

from api.run_journal import RunJournalWriter
from api.streaming import cancel_stream
sid,stream,stamp=sys.argv[1:];stamp=float(stamp)
models.SESSION_DIR.mkdir(parents=True,exist_ok=True)
s=_start_cancelled_turn(sid,stream);s.pending_started_at=stamp;s.pending_user_message='FRACTION_STOP';s.save()
assert cancel_stream(stream)
s=models.Session.load(sid)
marker=next(r for r in s.messages if r.get('_error'));clock=marker['timestamp']
writer=RunJournalWriter(sid,stream);writer.append_sse_event('token',{'text':'STOP_OUTPUT'});writer.append_sse_event('stream_end',{})
db=Path(models._active_state_db_path());db.parent.mkdir(parents=True,exist_ok=True)
_make_state_db(db,sid,[{'role':'user','content':'FRACTION_STOP','timestamp':stamp},
 {'role':'assistant','content':'CANCELLED_RAW_REPLAY','timestamp':stamp+1},
 {'role':'user','content':'QUEUED_GATEWAY_Q','timestamp':clock-.125},
 {'role':'assistant','content':'QUEUED_GATEWAY_A','timestamp':clock+.125}])
print(json.dumps({'saved_owner':[r for r in s.messages if r.get('role')=='user'],'pending':stamp,'carrier':clock}))
"""


@pytest.mark.parametrize("start", [10.0, 10.25, 10.75])
def test_actual_http_fractional_deferred_stop_keeps_queued_exchange(tmp_path, start):
    root = Path(models.__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "GH_", "GITHUB_", "OPENAI_", "ANTHROPIC_"))
        and not k.endswith(("_API_KEY", "_AUTH_TOKEN", "_BOT_TOKEN", "_ACCESS_TOKEN"))
    }
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(
        HERMES_HOME=str(home),
        HERMES_BASE_HOME=str(home),
        HERMES_CONFIG_PATH=str(home / "config.yaml"),
        HERMES_WEBUI_STATE_DIR=str(tmp_path / "state"),
        HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
        HERMES_WEBUI_TEST_NETWORK_BLOCK="1",
    )
    sid = "r21-fraction-" + str(start).replace(".", "-")
    stream = sid + "-run"
    seeded = subprocess.run(
        [sys.executable, "-c", FRACTION_SEED, sid, stream, str(start)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert seeded.returncode == 0, seeded.stderr
    journal = next((tmp_path / "state").rglob(stream + ".jsonl"))
    raw = journal.read_bytes()
    snapshots = {}
    saved = {}
    with _new_server(env, root, tmp_path / "server.log") as base:
        status, body, _ = _request(
            base, "/api/session?session_id=" + sid + "&msg_limit=all"
        )
        assert status == 200
        snapshots["GET"] = json.loads(body)["session"]
        for endpoint in ["branch", "duplicate"]:
            status, body, _ = _request(
                base, "/api/session/" + endpoint, {"session_id": sid}
            )
            assert status == 200, body
            payload = json.loads(body)
            child = (
                payload["session_id"]
                if endpoint == "branch"
                else payload["session"]["session_id"]
            )
            code = "import json,sys;from api.models import Session;s=Session.load(sys.argv[1]);print(json.dumps({'messages':s.messages,'context':s.context_messages}))"
            result = subprocess.run(
                [sys.executable, "-c", code, child],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            saved[endpoint] = json.loads(result.stdout)
    assert journal.read_bytes() == raw
    for name, rows in [
        ("GET", snapshots["GET"]["messages"]),
        *[
            (op + "." + layer, data[layer])
            for op, data in saved.items()
            for layer in ["messages", "context"]
        ],
    ]:
        text = [r.get("content") for r in rows]
        assert text.count("QUEUED_GATEWAY_Q") == text.count("QUEUED_GATEWAY_A") == 1, (
            name,
            text,
        )
        assert "CANCELLED_RAW_REPLAY" not in text
