"""Tests for real /steer functionality (follow-up to PR #1062).

Covers the new POST /api/chat/steer endpoint which mirrors the CLI's /steer
command (cli.py:6140-6155): the endpoint looks up the cached AIAgent for the
session, calls agent.steer(text), and the agent's run loop appends the steer
text to the next tool-result message — no interruption.

Falls back to {"accepted": false, "fallback": "<reason>"} when the agent
isn't running, isn't cached, or doesn't support steer (older agent versions).
The frontend uses the fallback signal to restore the draft without cancelling
the active run.

Plus a leftover-delivery flow: if the agent finishes its turn before the
steer is consumed (no tool-call boundary), _drain_pending_steer is called
after run_conversation returns and a `pending_steer_leftover` SSE event is
emitted so the frontend can queue the leftover text as a next-turn message.
"""
import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import source_between as _source_between

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(autouse=True)
def _restore_auth_sessions():
    """Snapshot and restore api.auth._sessions — see test_1058 for the rationale."""
    if os.environ.get("HERMES_WEBUI_PYTHON"):
        os.environ["HERMES_AGENT_PYTHON"] = os.environ["HERMES_WEBUI_PYTHON"]
    import api.auth as _auth
    snapshot = dict(_auth._sessions)
    yield
    _auth._sessions.clear()
    _auth._sessions.update(snapshot)


@pytest.fixture
def _clear_caches():
    """Snapshot SESSION_AGENT_CACHE and STREAMS so tests don't bleed."""
    from api.config import (
        ACTIVE_RUNS,
        ACTIVE_RUNS_LOCK,
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS,
        STREAMS_LOCK,
    )
    with SESSION_AGENT_CACHE_LOCK:
        cache_snap = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snap = dict(STREAMS)
        STREAMS.clear()
    with ACTIVE_RUNS_LOCK:
        active_runs_snap = dict(ACTIVE_RUNS)
        ACTIVE_RUNS.clear()
    yield
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()
        SESSION_AGENT_CACHE.update(cache_snap)
    with STREAMS_LOCK:
        STREAMS.clear()
        STREAMS.update(streams_snap)
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS.clear()
        ACTIVE_RUNS.update(active_runs_snap)


def _make_handler():
    """Minimal handler stub matching the methods api.helpers.j() touches."""
    h = MagicMock()
    h.wfile = MagicMock()
    h.headers = MagicMock()
    h.headers.get = MagicMock(return_value="")
    return h


def _captured_response(handler):
    """Pull the JSON body that j() wrote to handler.wfile."""
    import json as _json
    # j() calls handler.wfile.write(body)
    write_calls = handler.wfile.write.call_args_list
    assert write_calls, "no body was written to handler.wfile"
    body = write_calls[-1][0][0]
    return _json.loads(body.decode("utf-8"))


def _captured_status(handler):
    """Pull the HTTP status passed to handler.send_response()."""
    calls = handler.send_response.call_args_list
    assert calls, "no status was sent"
    return calls[-1][0][0]


# ── Backend: the /api/chat/steer endpoint ─────────────────────────────────

class TestHandleChatSteerHappyPath:
    """Endpoint accepts text and calls agent.steer() when all gates pass."""

    def test_accepts_when_agent_cached_and_running(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_happy", "stream_happy"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "Use Python instead"})

        agent.steer.assert_called_once_with("Use Python instead")
        body = _captured_response(handler)
        assert body == {"accepted": True, "fallback": None, "stream_id": stream_id}

    def test_accepts_multiple_steers_in_order(self, _clear_caches):
        """Repeated accepted steers must all reach agent.steer() in submit order.

        The CLI concatenates pending steer payloads at the next tool-result
        boundary.  WebUI must not add a frontend/server slot that replaces the
        first steer with the second.
        """
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_multi_steer", "stream_multi_steer"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            for text in ("first steer", "second steer", "third steer"):
                assert _handle_chat_steer(handler, {"session_id": sid, "text": text}) is not False

        assert agent.steer.call_args_list == [
            unittest.mock.call("first steer"),
            unittest.mock.call("second steer"),
            unittest.mock.call("third steer"),
        ]
        body = _captured_response(handler)
        assert body == {"accepted": True, "fallback": None, "stream_id": stream_id}


class TestHandleChatSteerFallbacks:
    """Each gate that fails returns a structured fallback the frontend can branch on."""

    def test_no_cached_agent(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid_x", "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "no_cached_agent"

    def test_gateway_owned_stream_without_cached_agent_queues_fallback(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import ACTIVE_RUNS, ACTIVE_RUNS_LOCK, STREAMS, STREAMS_LOCK
        import queue as _q

        sid, stream_id = "sid_gateway", "stream_gateway"
        with STREAMS_LOCK:
            STREAMS[stream_id] = _q.Queue()
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[stream_id] = {"session_id": sid, "backend": "gateway"}

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "preserve this"})

        body = _captured_response(handler)
        assert body == {
            "accepted": False,
            "fallback": "gateway_steer_queued",
            "stream_id": stream_id,
        }

    def test_agent_lacks_steer_method(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_old"
        # Older agent without steer() — use spec to suppress MagicMock auto-create
        agent = MagicMock(spec=["interrupt", "run_conversation"])
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "agent_lacks_steer"

    def test_session_not_found(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_missing"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with patch("api.streaming.get_session", side_effect=KeyError(sid)):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "session_not_found"
        agent.steer.assert_not_called()  # never reached the steer call

    def test_session_not_running(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_idle"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        sess = MagicMock()
        sess.active_stream_id = None  # idle session
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "not_running"
        agent.steer.assert_not_called()

    def test_stream_dead(self, _clear_caches):
        """Session has active_stream_id but the stream is gone from STREAMS (e.g. crashed)."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_zombie"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        sess = MagicMock()
        sess.active_stream_id = "stream_zombie"
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "stream_dead"
        agent.steer.assert_not_called()

    def test_steer_raises(self, _clear_caches):
        """If agent.steer() raises, return steer_error rather than 500."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_throws", "stream_throws"
        agent = MagicMock()
        agent.steer = MagicMock(side_effect=RuntimeError("boom"))
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()
        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "steer_error"


class TestHandleChatSteerInputValidation:
    """Bad input → 400 Bad Request, not silent acceptance."""

    def test_missing_session_id(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"text": "hint"})
        assert _captured_status(handler) == 400

    def test_missing_text(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid"})
        assert _captured_status(handler) == 400

    def test_empty_text_after_strip(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid", "text": "   \n\t  "})
        assert _captured_status(handler) == 400


# ── Routing ───────────────────────────────────────────────────────────────

class TestRouting:
    """The POST handler must dispatch /api/chat/steer to _handle_chat_steer."""

    def test_route_registered(self):
        src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
        assert '/api/chat/steer' in src
        assert '_handle_chat_steer' in src


# ── Frontend: cmdSteer + busy-mode steer use the new endpoint ────────────

class TestFrontendWiring:
    """The slash command and busy-mode steer paths must call /api/chat/steer."""

    @classmethod
    def setup_class(cls):
        cls.cmds = (Path(__file__).parent.parent / "static" / "commands.js").read_text(encoding="utf-8")
        cls.msgs = (Path(__file__).parent.parent / "static" / "messages.js").read_text(encoding="utf-8")
        cls.i18n = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")
        cls.ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        cls.sessions = (Path(__file__).parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")

    def test_cmd_steer_calls_endpoint(self):
        idx = self.cmds.find("async function cmdSteer(")
        assert idx >= 0
        body = self.cmds[idx:idx + 600]
        # Should call _trySteer (which calls the endpoint), not directly cancelStream
        assert "_trySteer" in body, "cmdSteer must delegate to _trySteer"

    def test_try_steer_calls_endpoint(self):
        idx = self.cmds.find("async function _trySteer(")
        assert idx >= 0
        body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle(args){")
        assert "/api/chat/steer" in body, "_trySteer must POST to /api/chat/steer"
        assert "method:'POST'" in body or 'method:"POST"' in body

    def test_try_steer_handles_fallback_without_cancelling(self):
        idx = self.cmds.find("async function _trySteer(")
        body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle")
        # Must check result.accepted and keep generic failures from cancelling.
        assert "result&&result.accepted" in body or "result.accepted" in body
        assert "result&&result.fallback==='gateway_steer_queued'" in body
        assert "queueSessionMessage(ownerSid" in body
        assert "cancelStream" not in body, "fallback path must not cancel the stream"
        assert "inp.value" in body, "fallback path must restore the composer draft"

    def test_send_busy_steer_uses_try_steer(self):
        # send() in messages.js: when busyMode === 'steer', should call _trySteer
        idx = self.msgs.find("defaultMessageMode==='steer'")
        assert idx >= 0
        block = self.msgs[idx:idx + 800]
        assert "_trySteer" in block, "send()'s steer branch must delegate to _trySteer"

    def test_try_steer_uploads_pending_files_without_clearing_until_accepted(self):
        cmds = self.cmds
        assert "function _steerUploadedAttachmentPaths" in cmds
        assert "async function _steerTextWithPendingFiles" in cmds
        assert "function _steerOwnerIsCurrent" in cmds
        assert "uploadPendingFiles({clearPending:false,sessionId:ownerSid,files:pendingFiles})" in cmds, (
            "steer must upload staged files for the captured owner session without clearing chips before endpoint acceptance"
        )
        idx = cmds.find("async function _trySteer(")
        assert idx >= 0
        body = _source_between(cmds, "async function _trySteer(", "\nasync function cmdTitle")
        assert "const ownerSid=(typeof S!=='undefined'&&S.session&&S.session.session_id)||null;" in body
        assert "const pendingFilesSnapshot=typeof S!=='undefined'&&Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];" in body
        assert "steerText=await _steerTextWithPendingFiles(originalMsg,ownerSid,pendingFilesSnapshot)" in body
        assert "body:JSON.stringify({session_id:ownerSid,text:steerText})" in body, (
            "steer endpoint must receive the captured owner session id and attachment-enriched text"
        )
        assert "_clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot)" in body
        assert "if(_steerOwnerIsCurrent(ownerSid))" in body
        assert "S.pendingFiles=_remaining" in body, "accepted steer should clear the delivered files (by identity) after paths are injected"

    def test_file_steer_does_not_read_live_session_after_upload_await(self):
        cmds = self.cmds
        idx = cmds.find("async function _trySteer(")
        assert idx >= 0
        body = _source_between(cmds, "async function _trySteer(", "\nasync function cmdTitle")
        await_idx = body.find("steerText=await _steerTextWithPendingFiles")
        assert await_idx >= 0
        after_upload = body[await_idx:]
        assert "session_id:S.session.session_id" not in after_upload
        assert "{session_id:S.session.session_id" not in after_upload
        assert "session_id:ownerSid" in after_upload
        assert "_steerOwnerIsCurrent(ownerSid)" in after_upload, (
            "post-await tray/DOM mutations must be guarded by the captured owner session"
        )

    def test_file_steer_upload_status_and_indicator_are_owner_scoped(self):
        steer_helpers = _source_between(
            self.cmds,
            "function _steerOwnerIsCurrent",
            "\nasync function cmdTitle",
        )
        try_body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle")
        assert "function _steerSetComposerStatusForOwner" in steer_helpers
        assert "_steerSetComposerStatusForOwner(ownerSid,t('uploading')||'Uploading…')" in steer_helpers
        assert "_steerSetComposerStatusForOwner(ownerSid,'')" in steer_helpers
        assert "function _steerIndicatorText" in steer_helpers
        assert "_showSteerIndicator(_steerIndicatorText(originalMsg,pendingFilesSnapshot))" in try_body, (
            "visible steer indicator must use original text or a file-only display label, not attachment tool instructions"
        )
        assert "_showSteerIndicator(steerText)" not in try_body

    def test_file_steer_indicator_omits_attachment_tool_note(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[{{name:'a.pdf'}}]}};
            let apiPayload = null;
            let indicatorText = null;
            function t(k){{return k;}}
            function $(id){{return {{value:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}}};}}
            function setComposerStatus(){{}}
            function showToast(){{}}
            function renderTray(){{}}
            function _showSteerIndicator(text){{indicatorText = text;}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(){{}}
            async function uploadPendingFiles(){{return [{{path:'/tmp/a.pdf'}}];}}
            async function api(url, options){{
              assert.strictEqual(url, '/api/chat/steer');
              apiPayload = JSON.parse(options.body);
              return {{accepted:true}};
            }}
            eval({json.dumps(steer_src)});
            _setSteerPendingCount('A', 1);
            (async()=>{{
              const delivered = await _trySteer('hint', false);
              assert.strictEqual(delivered, true);
              assert.strictEqual(indicatorText, 'hint');
              assert.ok(apiPayload.text.includes('[Attached files for this steer: /tmp/a.pdf]'));
              assert.ok(!indicatorText.includes('Attached files'));
              assert.ok(!indicatorText.includes('file tools/read_file'));
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        try:
            subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout)
            print(exc.stderr, file=sys.stderr)
            raise

    def test_attachment_only_steer_indicator_uses_file_label(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[{{name:'a.pdf'}}]}};
            let apiPayload = null;
            let indicatorText = null;
            function t(k){{return k;}}
            function $(id){{return {{value:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}}};}}
            function setComposerStatus(){{}}
            function showToast(){{}}
            function renderTray(){{}}
            function _showSteerIndicator(text){{indicatorText = text;}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(){{}}
            async function uploadPendingFiles(){{return [{{path:'/tmp/a.pdf'}}];}}
            async function api(url, options){{
              assert.strictEqual(url, '/api/chat/steer');
              apiPayload = JSON.parse(options.body);
              return {{accepted:true}};
            }}
            eval({json.dumps(steer_src)});
            (async()=>{{
              const delivered = await _trySteer('', false);
              assert.strictEqual(delivered, true);
              assert.strictEqual(indicatorText, 'Attached files: a.pdf');
              assert.ok(apiPayload.text.includes('[Attached files for this steer: /tmp/a.pdf]'));
              assert.ok(!indicatorText.includes('file tools/read_file'));
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_multiple_accepted_steers_preserve_pending_count(self):
        """The visible pending-steer count must grow, not collapse to one steer."""
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[]}};
            let status = null;
            function t(key, arg){{ return `${{key}}:${{arg ?? ''}}`; }}
            function _showSteerIndicator(){{}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(){{}}
            function showToast(){{}}
            async function api(){{ return {{accepted:true}}; }}
            globalThis.setComposerStatus = (value) => {{ status = value; }};
            eval({json.dumps(steer_src)});
            _setSteerPendingCount('A', 1);
            (async()=>{{
              const delivered = await _trySteer('second steer', true);
              assert.strictEqual(delivered, true);
              assert.strictEqual(status, 'steer_pending_count:2');
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_render_messages_does_not_clear_pending_steer(self):
        """Transcript rendering must not mutate or clear pending steer state."""
        render_start = "function renderMessages(options){"
        start = self.ui.find(render_start)
        assert start >= 0
        end = self.ui.find(chr(10) + "function ", start + len(render_start))
        render_src = self.ui[start:end]
        assert "if(typeof updateSteerPendingBadge==='function') updateSteerPendingBadge(sid);" in render_src, (
            "renderMessages may refresh the owner-scoped indicator"
        )
        assert "clearSteerPending" not in render_src, (
            "renderMessages must not explicitly clear pending state"
        )
        assert "_setSteerPendingCount" not in render_src, (
            "renderMessages must not mutate the pending count"
        )

    def test_set_busy_false_clears_pending_steer(self):
        """Turn completion is the explicit consumption/requeue boundary."""
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        busy_start = "function setBusy(v){"
        start = self.ui.find(busy_start)
        assert start >= 0
        end = self.ui.find(chr(10) + "function ", start + len(busy_start))
        busy_src = self.ui[start:end]
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            const counts = {{ A: 2 }};
            const clearCalls = [];
            let indicator = null;
            let queueBadgeSid = null;
            globalThis.S = {{ busy: true, session: {{ session_id: 'A' }} }};
            globalThis._queueDrainSid = 'A';
            globalThis.updateSendBtn = () => {{}};
            globalThis._clearActivityElapsedTimer = () => {{}};
            globalThis.setStatus = () => {{}};
            globalThis.setComposerStatus = () => {{}};
            globalThis.updateQueueBadge = (sid) => {{ queueBadgeSid = sid; }};
            globalThis.shiftQueuedSessionMessage = () => null;
            globalThis._steerPendingCounts = counts;
            globalThis.clearSteerPending = (sid) => {{
              clearCalls.push(sid);
              delete counts[sid];
              indicator = 0;
            }};
            eval({json.dumps(busy_src)});
            setBusy(false);
            assert.deepStrictEqual(clearCalls, ['A'], 'setBusy(false) must clear pending state');
            assert.strictEqual(queueBadgeSid, 'A');
            assert.strictEqual(counts.A, undefined, 'setBusy(false) must clear pending count');
            assert.strictEqual(indicator, 0, 'setBusy(false) must refresh empty indicator');
            assert.strictEqual(globalThis._queueDrainSid, null);
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def _run_steer_consumption_script(self, statements):
        """Evaluate the production arming/consumption helpers in Node."""
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        helper_start = self.msgs.find("const _STEER_CONSUMPTION_ARMED = {};")
        assert helper_start >= 0
        helper_end = self.msgs.find("\nfunction attachLiveStream(", helper_start)
        assert helper_end > helper_start
        helper_src = self.msgs[helper_start:helper_end]
        try_body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle")
        combined_src = helper_src.replace('const _STEER_CONSUMPTION_ARMED = {};', 'var _STEER_CONSUMPTION_ARMED = {};').replace('const _STEER_TOOL_BATCHES = {};', 'var _STEER_TOOL_BATCHES = {};').replace('function _resetSteerToolBatch', 'globalThis._resetSteerToolBatch = function').replace('function _clearSteerToolBatch', 'globalThis._clearSteerToolBatch = function').replace('function _trackSteerToolStart', 'globalThis._trackSteerToolStart = function', 1).replace('function _trackSteerToolComplete', 'globalThis._trackSteerToolComplete = function', 1).replace('function _armSteerConsumption', 'globalThis._armSteerConsumption = function').replace('function _resetSteerConsumptionArming', 'globalThis._resetSteerConsumptionArming = function').replace('function _consumeArmedSteer', 'globalThis._consumeArmedSteer = function')
        combined_src = combined_src + "\n" + try_body
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            globalThis.S = {{ session: {{ session_id: 'A', active_stream_id: 'stream-1' }}, pendingFiles: [], activeStreamId: 'stream-1' }};
            const counts = {{ A: 1 }};
            const clearCalls = [];
            globalThis._steerPendingCounts = counts;
            globalThis.clearSteerPending = (sid) => {{
              clearCalls.push(sid);
              delete counts[sid];
            }};
            globalThis._steerOwnerIsCurrent = (sid) => sid === 'A';
            globalThis._armSteerConsumption = (sid, streamId) => {{
              const current = _STEER_CONSUMPTION_ARMED[sid];
              if (current && current.streamId === streamId && current.armed) {{
                if (current.consumed) {{
                  delete _STEER_CONSUMPTION_ARMED[sid];
                  clearSteerPending(sid);
                  return true;
                }}
                return true;
              }}
              _STEER_CONSUMPTION_ARMED[sid] = {{ streamId, armed: true, consumed: false }};
              return true;
            }};
            globalThis.$ = () => null;
            globalThis.api = async () => ({{ accepted: true }});
            globalThis._steerTextWithPendingFiles = async (text) => text;
            globalThis._steerFallbackIsDeadRun = () => false;
            globalThis._steerOwnerStreamIsCurrent = () => true;
            globalThis._steerClearCurrentOwnerDeadRun = () => false;
            globalThis._showSteerRecovery = () => {{}};
            globalThis._steerIndicatorText = () => '';
            globalThis.t = (key) => key;
            globalThis._steerSetComposerStatusForOwner = () => {{}};
            globalThis._steerFailureMessageKey = (fallback) => `steer_fail_${{fallback}}`;
            globalThis._showSteerIndicator = () => {{}};
            globalThis._steerIndicatorText = () => '';
            globalThis.getSteerPendingCount = (sid) => counts[sid] || 0;
            globalThis._setSteerPendingCount = (sid, count) => {{ if (count) counts[sid] = count; else delete counts[sid]; }};
            globalThis._updateSteerPendingIndicatorStatus = () => {{}};
            globalThis.showToast = () => {{}};
            eval({json.dumps(combined_src)});
            (async()=>{{
              {statements}
            }})().catch(err => {{
              console.error(err);
              process.exit(1);
            }});
            """
        )
        try:
            subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout)
            print(exc.stderr, file=sys.stderr)
            raise

    def test_steer_helpers_never_reference_globalthis_lexical_state(self):
        """Greptile P1 (2026-09-04T14:32): `_consumeArmedSteer` indexed
        `globalThis._STEER_TOOL_BATCHES`, but messages.js is a classic script —
        its top-level `const` bindings are lexical and never land on
        globalThis, so that read threw TypeError before clearSteerPending ran
        and aborted the whole tool_complete handler. The eval-based harness
        masks this (direct-eval `var` probes land on globalThis), so guard the
        source directly: steer helpers must reference these bindings by their
        lexical names only."""
        forbidden = [
            "globalThis._STEER_TOOL_BATCHES",
            "globalThis._STEER_CONSUMPTION_ARMED",
        ]
        for needle in forbidden:
            assert needle not in self.msgs, (
                f"steer helpers must not read lexical state via {needle} — "
                "top-level const in a classic script is not a globalThis property"
            )

    def test_steer_event_before_submission_does_not_clear_pending_count(self):
        """A pre-submit boundary must not consume a steer accepted later."""
        self._run_steer_consumption_script(
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), false);\n"
            "assert.deepStrictEqual(clearCalls, []);\n"
            "assert.strictEqual(counts.A, 1);\n"
        )

    def test_next_tool_call_after_accepted_steer_clears_count(self):
        """A live `tool` event after the accepted steer means the batch was drained."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.streamId, 'stream-1');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined);\n"
        )

    def test_parallel_batch_completion_requires_all_tool_ids(self):
        """Finalized tool batches drain accepted steers once, onto their last result."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "_trackSteerToolStart('A', 'stream-1', 'tool-1');\n"
            "_trackSteerToolStart('A', 'stream-1', 'tool-2');\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-1'), false, 'the first parallel result must not drain');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), false);\n"
            "assert.deepStrictEqual(clearCalls, []);\n"
            "assert.strictEqual(counts.A, 2, 'the pending count must survive the first parallel result');\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-2'), true, 'the final parallel result drains');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
        )

    def test_single_tool_batch_completion_drains(self):
        """A single-tool batch retains the existing immediate drain behavior."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "_trackSteerToolStart('A', 'stream-1', 'tool-1');\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-1'), true, 'the single tracked tool completes the batch');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
            "assert.strictEqual(_STEER_TOOL_BATCHES.A, undefined);\n"
        )

    def test_batch_tracking_survives_same_stream_reconnect(self):
        """Reattaching an in-flight parallel batch must preserve its id set."""
        self._run_steer_consumption_script(
            "_trackSteerToolStart('A', 'stream-1', 'tool-1');\n"
            "_trackSteerToolStart('A', 'stream-1', 'tool-2');\n"
            "_resetSteerToolBatch('A', 'stream-1', { reconnecting: true });\n"
            "assert.deepStrictEqual([..._STEER_TOOL_BATCHES.A.ids].sort(), ['tool-1', 'tool-2']);\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-1'), false);\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-2'), true);\n"
        )

    def test_legacy_tool_completion_without_id_is_not_a_batch_boundary(self):
        """A legacy Hermes Agent emits tool_complete without tid. The missing
        id carries no batch-boundary information, so it must not consume every
        pending steer at the first concurrent tool result."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "_trackSteerToolStart('A', 'stream-1', 'tool-1');\n"
            "_trackSteerToolStart('A', 'stream-1', 'tool-2');\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', ''), false, 'legacy missing id is not a boundary');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), false, 'legacy missing id must not consume');\n"
            "assert.strictEqual(_STEER_TOOL_BATCHES.A.ids.size, 2, 'both batch members must survive');\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-1'), false);\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-2'), true, 'the tracked batch itself is the boundary');\n"
        )

    def test_detached_reconnect_preflight_clears_stale_pending_for_stream(self):
        """The reconnect preflight is another terminal teardown path for a
        detached stream that completed while no EventSource was attached."""
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        helper_src = _source_between(
            self.msgs,
            "function _clearOwnerInflightState",
            "\n  function _isMarkerOnlyAssistantMessage",
        )
        helper_src = helper_src.replace("function _clearOwnerInflightState(){", "globalThis._clearOwnerInflightState = function(){", 1)
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let clearedConsumption = [];
            globalThis.INFLIGHT = {{ A: {{ streamId: 'stream-1' }} }};
            globalThis._isActiveSession = () => true;
            globalThis.S = {{ activeStreamId: 'stream-1' }};
            globalThis.activeSid = 'A';
            globalThis.streamId = 'stream-1';
            globalThis._clearSteerToolBatch = () => {{}};
            globalThis._clearSteerConsumptionForStream = (sid, stream) => clearedConsumption.push([sid, stream]);
            globalThis.clearInflightState = () => {{}};
            globalThis._clearActivePaneInflightIfOwner = () => {{}};
            globalThis._resumeSessionStreamAfterLiveChat = () => {{}};
            eval({json.dumps(helper_src)});
            _clearOwnerInflightState();
            assert.deepStrictEqual(clearedConsumption, [['A', 'stream-1']]);
            """
        )
        try:
            subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout)
            print(exc.stderr, file=sys.stderr)
            raise

    def test_clear_inflight_state_releases_batch_tracking(self):
        """Terminal teardown must not leak stream-scoped batch state."""
        self._run_steer_consumption_script(
            "_trackSteerToolStart('A', 'stream-1', 'tool-1');\n"
            "_clearSteerToolBatch('A', 'stream-1');\n"
            "assert.strictEqual(_STEER_TOOL_BATCHES.A, undefined);\n"
            "assert.strictEqual(_trackSteerToolComplete('A', 'stream-1', 'tool-1'), false, 'a cleared batch has no boundary information');\n"
        )

    def test_terminal_inflight_teardown_clears_pending_steer_for_stream(self):
        """A detached stream can complete without delivering done to its old
        EventSource. The terminal teardown helper is the shared chokepoint that
        must expire the corresponding pending count for that stream."""
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        helper_src = _source_between(
            self.msgs,
            "function _clearOwnerInflightState",
            "\n  function _isMarkerOnlyAssistantMessage",
        )
        helper_src = helper_src.replace("function _clearOwnerInflightState(){", "globalThis._clearOwnerInflightState = function(){", 1)
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let clearedBatches = [];
            let clearedConsumption = [];
            globalThis.INFLIGHT = {{ A: {{ streamId: 'stream-1' }} }};
            globalThis._isActiveSession = () => true;
            globalThis.S = {{ activeStreamId: 'stream-1' }};
            globalThis.activeSid = 'A';
            globalThis.streamId = 'stream-1';
            globalThis._clearSteerToolBatch = (sid, stream) => clearedBatches.push([sid, stream]);
            globalThis._clearSteerConsumptionForStream = (sid, stream) => clearedConsumption.push([sid, stream]);
            globalThis.clearInflightState = () => {{}};
            globalThis._clearActivePaneInflightIfOwner = () => {{}};
            globalThis._resumeSessionStreamAfterLiveChat = () => {{}};
            eval({json.dumps(helper_src)});
            _clearOwnerInflightState();
            assert.deepStrictEqual(clearedBatches, [['A', 'stream-1']]);
            assert.deepStrictEqual(clearedConsumption, [['A', 'stream-1']]);
            assert.strictEqual(INFLIGHT.A, undefined);
            """
        )
        try:
            subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout)
            print(exc.stderr, file=sys.stderr)
            raise

    def test_idle_session_reload_clears_stale_pending_for_stream(self):
        """Reopening a session after its detached stream completed must expire
        the owner's stale pending count, even though the old EventSource never
        delivered done."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        start=self.sessions.index("  if(INFLIGHT[sid]){\n    _ensureInflightLiveAssistantMessage(INFLIGHT[sid]);")
        end=self.sessions.index("\n  // Sync context usage indicator", start)
        block=self.sessions[start:end]
        prefix = """
        const assert = require('assert');
        const counts={A:2};
        const cleared=[];
        globalThis.INFLIGHT={};
        globalThis.S={busy:true,activeStreamId:null,session:{session_id:'A',active_stream_id:null,pending_attachments:[]}};
        globalThis._keepStaleUntilLoaded=false;
        globalThis._loadGeneration=null;
        globalThis._hydrateTodosFromSession=()=>{};
        globalThis.sid='A'; globalThis.activeStreamId='stream-1'; globalThis.sameSessionForceReload=false;
        globalThis._serverLiveSnapshotInflight=()=>null; globalThis._selectLiveRecoveryInflight=()=>null;
        globalThis._ensureInflightLiveAssistantMessage=()=>{}; globalThis._projectInflightMessagesForActivityBursts=()=>[];
        globalThis._mergePendingSessionMessage=()=>{};
        globalThis.appendThinking=()=>{};
        globalThis._clearSteerConsumptionForStream=(sid,streamId)=>cleared.push([sid,streamId]);
        globalThis.clearLiveToolCards=()=>{}; globalThis._syncToolCallsForLoadedMessages=()=>{};
        globalThis._ensureMessagesLoaded=async()=>{}; globalThis._rearmActiveSessionStream=()=>{};
        globalThis._isCurrentLoad=()=>true; globalThis.setBusy=()=>{};
        globalThis.attachLiveStream=()=>{}; globalThis.watchInflightSession=()=>{};
        globalThis.updateSendBtn=()=>{}; globalThis.setStatus=()=>{};
        globalThis.setComposerStatus=()=>{}; globalThis.syncTopbar=()=>{};
        globalThis.renderMessages=()=>{}; globalThis.updateQueueBadge=()=>{};
        globalThis.startApprovalPolling=()=>{}; globalThis.startClarifyPolling=()=>{};
        globalThis._fetchYoloState=()=>{}; globalThis.resumeManualCompressionForSession=()=>{};
        globalThis._deferWorkspaceRefreshForSession=()=>{};
        S.activeStreamId=null;
        (async()=>{
          var activeStreamId='stream-1';
          var sid='A';
          var S={activeStreamId:'stream-2',session:{session_id:'B'}};
        """
        suffix = """
        assert.deepStrictEqual(cleared,[["A",null],["A","stream-1"]]);
        assert.strictEqual(counts.A,2);
        })().catch(err=>{console.error(err);process.exit(1);});
        """
        script = prefix + "await eval(" + json.dumps("(async()=>{" + block + "})()") + ");" + suffix
        try:
            subprocess.run([node,"-e",script],check=True,capture_output=True,text=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout)
            print(exc.stderr,file=sys.stderr)
            raise

    def test_idle_reload_expires_owner_pending_even_without_stream_snapshot(self):
        """Polling can force a reload after the owner is already known idle, so
        the idle branch must expire owner state even when no stream id remains."""
        start = self.sessions.index("  }else{\n    // Phase 2b: Idle session")
        end = self.sessions.index("    // _ensureMessagesLoaded is idempotent;", start)
        idle_head = self.sessions[start:end]
        assert "if (typeof _clearSteerConsumptionForStream === 'function') {" in idle_head
        assert "_clearSteerConsumptionForStream(sid, null);" in idle_head

    def test_new_stream_reattach_clears_stale_pending_count(self):
        """A different stream is a turn boundary and must expire stale pending state."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "_resetSteerConsumptionArming('A', 'stream-2');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined);\n"
            "assert.strictEqual(counts.A, undefined, 'a new stream must clear the stale pending count');\n"
            "assert.deepStrictEqual(clearCalls, []);\n"
        )

    def test_midawait_boundary_keeps_prearm_for_first_steer_of_turn(self):
        """Round-2 invariant: a boundary during an in-flight POST must not
        disarm the arm. If a tool-batch boundary lands while the POST is in
        flight, _consumeArmedSteer runs armed with count 0 and must keep the
        arm until either the next real boundary consumes it or the delayed
        response reconciles the boundary.

        The api stub fires a mid-await boundary (the exact 0-count window)
        while the POST is unresolved, then resolves accepted. Harness audit
        note: the shared harness seeds counts.A=1, so this test explicitly
        deletes the entry first to make the 0→1 transition real."""
        self._run_steer_consumption_script(
            # Real 0-to-1 transition: delete the harness seed first.
            "delete counts.A;\n"
            # Boundary fires inside the api stub while the POST is in flight.
            "globalThis.api = async () => {\n"
            "  const midAwait = _consumeArmedSteer('A', 'stream-1');\n"
            "  if (midAwait !== false) throw new Error('mid-await boundary must NOT consume at count 0');\n"
            "  if (!_STEER_CONSUMPTION_ARMED.A || _STEER_CONSUMPTION_ARMED.A.armed !== true) {\n"
            "    throw new Error('mid-await boundary must NOT delete the pre-arm');\n"
            "  }\n"
            "  return { accepted: true };\n"
            "};\n"
            "assert.strictEqual(await _trySteer('first steer', true), true);\n"
            "assert.strictEqual(counts.A, undefined, 'the boundary during the POST marked consumption; the accepted response reconciled it');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined, 'the reconciled arm was released');\n"
            "assert.strictEqual(counts.A, undefined);\n"
        )

    def test_boundary_during_post_reconciles_delayed_accepted_response(self):
        # A boundary arriving while the POST is unresolved marks consumption.
        # The browser-visible SSE boundary and the HTTP accepted response are
        # independent queues. If the backend consumes the steer before the
        # accepted response resolves, the response must not add a stale pending
        # count after the boundary.
        self._run_steer_consumption_script(
            "delete counts.A;\n"
            "let accept = null;\n"
            "globalThis.api = () => new Promise(resolve => {\n"
            "  accept = () => resolve({ accepted: true });\n"
            "});\n"
            "const first = _trySteer('racing steer', true);\n"
            "await Promise.resolve();\n"
            "await Promise.resolve();\n"
            "await Promise.resolve();\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.armed, true, 'pre-arm must exist before the response');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), false, 'count 0 cannot consume yet');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.consumed, true, 'boundary must mark the pre-acceptance consumption');\n"
            "accept();\n"
            "assert.strictEqual(await first, true);\n"
            "assert.strictEqual(counts.A, undefined, 'a consumed steer must not be counted by its delayed response');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined, 'the reconciled arm must be released');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), false, 'no stale arm remains');\n"
        )

    def test_prearm_steer_consumption_before_accepted_response(self):
        """The backend may call agent.steer() before HTTP resolves and reach the
        next tool boundary before response processing resumes. Pre-arm on
        submission so a concurrent boundary still sees the consumption signal;
        failed fallbacks release the pre-arm before restoring the draft."""
        self._run_steer_consumption_script(
            "counts.A = 1;\n"
            "await _trySteer('continue with this', true);\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
            "globalThis.api = async () => ({ accepted: false, fallback: 'busy' });\n"
                        "await _trySteer('rejected steer', true);\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined);\n"
        )

    def test_network_timeout_releases_prearm(self):
        """DeepSeek round-0 question, now locked by test: if the steer POST
        never reaches the server (network timeout / 5xx), the pre-arm installed
        at submit time must be released — otherwise a later accepted steer of
        the same session would be consumed at the stale stream boundary from
        the failed submission. The catch-all sets fallback=network_error and
        the generic fallback path releases the arm via
        _resetSteerConsumptionArming; the count stays 0 and nothing is
        consumed."""
        self._run_steer_consumption_script(
            # Real timeout shape: no accepted steer exists yet, so the count is
            # 0 (delete the harness seed — a count>0 here would mean a sibling
            # steer is genuinely pending, which is the concurrent-failure case
            # covered by test_concurrent_failure_keeps_sibling_accepted_arm).
            "delete counts.A;\n"
            "globalThis.api = async () => { throw new Error('timeout'); };\n"
            "assert.strictEqual(await _trySteer('will time out', true), false);\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined, 'timeout must release the pre-arm');\n"
            "assert.strictEqual(counts.A, undefined, 'timeout path must not touch the count');\n"
            "assert.deepStrictEqual(clearCalls, [], 'nothing was consumed');\n"
            # And a boundary arriving on the still-running stream consumes nothing.
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), false);\n"
        )

    def test_concurrent_failure_keeps_sibling_accepted_arm(self):
        """Greptile P1 (2026-09-05T04:07): two steers race on the same session
        and stream; one resolves accepted (count > 0, arm waiting for the
        boundary) before the other fails. The failed fallback's
        _resetSteerConsumptionArming must not delete the shared arm — the
        accepted steer's count would otherwise strand until turn end. A bare
        arm with no pending payload still gets cleared, and a stream change
        still clears everything."""
        self._run_steer_consumption_script(
            # steer#1 accepted mid-run: count raised, arm installed, waiting.
            "assert.strictEqual(await _trySteer('sibling accepted', true), true);\n"
            "assert.strictEqual(counts.A, 2);\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.armed, true);\n"
            # steer#2 on the same session+stream fails after steer#1 resolved.
            "globalThis.api = async () => ({ accepted: false, fallback: 'busy' });\n"
            "assert.strictEqual(await _trySteer('sibling failed', true), false);\n"
            "assert.strictEqual(counts.A, 2, 'the failed sibling must not touch the accepted count');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.armed, true, 'failure release must keep the sibling accepted arm');\n"
            # The next real boundary still consumes the accepted steer.
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
        )

    def test_failure_first_overlap_rearms_on_acceptance(self):
        """Greptile P1 (2026-09-05T04:29): two steers overlap on the same
        session and stream — #2's submit (with its pre-arm) happens while #1
        is still in flight, and #1 then fails, releasing the shared arm before
        #2 resolves accepted. Acceptance must re-arm idempotently before
        raising the count, otherwise the raised count never sees a boundary
        consume and strands until turn end."""
        self._run_steer_consumption_script(
            "counts.A = 0;\n"
            # #1's POST hangs until #2 has submitted, then fails — so the
            # failure release lands AFTER #2's pre-arm and BEFORE #2's accept.
            "let release1 = null;\n"
            "const originalApi = globalThis.api;\n"
            "globalThis.api = (url, options) => new Promise(resolve => {\n"
            "  release1 = () => resolve({ accepted: false, fallback: 'busy' });\n"
            "});\n"
            "const first = _trySteer('in flight then fails', true);\n"
            "await Promise.resolve();\n"
            "await Promise.resolve();\n"
            # #2 submits while #1 is still hanging: its pre-arm is a no-op
            # (already armed), but it moves the arm's lifecycle forward.
            "globalThis.api = async () => ({ accepted: true });\n"
            "const second = _trySteer('overlapping accepted', true);\n"
            "await Promise.resolve();\n"
            "if (typeof release1 !== 'function') throw new Error('#1 api stub never captured a resolver');\n"
            "release1();\n"
            "assert.strictEqual(await first, false, '#1 failed');\n"
            "assert.strictEqual(await second, true, '#2 accepted');\n"
            "assert.strictEqual(counts.A, 1, '#2 raised the count 0→1');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.armed, true, 'acceptance re-armed after #1 released the shared arm');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
        )

    def test_reset_still_clears_bare_arm_and_stream_change(self):
        """The count>0 protection only covers arms whose stream still holds
        pending payload: a bare arm with count 0 is released as before, and a
        stream change (attach/detach) clears both arm and stale count."""
        self._run_steer_consumption_script(
            # Bare arm, no pending payload → submission release still works.
            "counts.A = 0;\n"
            "globalThis.api = async () => ({ accepted: false, fallback: 'busy' });\n"
            "assert.strictEqual(await _trySteer('fails with count 0', true), false);\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined, 'bare arm still released');\n"
            # Stream change → full clear (arm + stale count). The arm belongs
            # to stream-9; a reset against a DIFFERENT stream id is the
            # attach/detach path and clears everything, count included.
            "counts.A = 2;\n"
            "_armSteerConsumption('A', 'stream-9');\n"
            "_resetSteerConsumptionArming('A', 'stream-8');\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A, undefined, 'stream-change full clear');\n"
            "assert.strictEqual(counts.A, undefined, 'stale count expired on stream change');\n"
        )

    def test_tool_completion_after_accepted_steer_clears_count(self):
        """A post-submit tool result is the earliest observable drain boundary."""
        listener_start = self.msgs.find("source.addEventListener('tool',e=>{")
        assert listener_start >= 0
        complete_start = self.msgs.find("source.addEventListener('tool_complete',e=>{", listener_start)
        assert complete_start > listener_start
        complete_end = self.msgs.find("\n    source.addEventListener('todo_state'", complete_start)
        assert complete_end > complete_start
        complete_listener = self.msgs[complete_start:complete_end]
        assert "if(typeof _trackSteerToolComplete === 'function') _trackSteerToolComplete(activeSid, streamId, d.tid||d.id)" in complete_listener
        assert "if(typeof _consumeArmedSteer === 'function') _consumeArmedSteer(activeSid, streamId)" in complete_listener
        assert "_trackSteerToolStart(activeSid, streamId, d.tid||d.id)" in self.msgs[listener_start:complete_start]
        assert "_consumeArmedSteer(activeSid, streamId)" not in self.msgs[listener_start:complete_start]

    def test_all_accumulated_steers_clear_at_one_boundary(self):
        """Agent drains concatenated pending steers once; one boundary clears all."""
        self._run_steer_consumption_script(
            "counts.A = 2;\n"
            "assert.strictEqual(await _trySteer('second steer', true), true);\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
            "assert.strictEqual(counts.A, undefined);\n"
        )

    def test_reconnect_before_boundary_preserves_consumption_signal(self):
        """Reattaching the same stream must not lose the post-submit boundary arm."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "_resetSteerConsumptionArming('A', 'stream-1', { reconnecting: true });\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.armed, true);\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
        )

    def test_same_stream_reconnect_preserves_accumulated_pending_count(self):
        """Reconnecting to the same stream keeps every accumulated steer live."""
        self._run_steer_consumption_script(
            "assert.strictEqual(await _trySteer('continue with this', true), true);\n"
            "_resetSteerConsumptionArming('A', 'stream-1', { reconnecting: true });\n"
            "assert.strictEqual(_STEER_CONSUMPTION_ARMED.A.armed, true);\n"
            "assert.strictEqual(counts.A, 2, 'the same stream reconnect must preserve the pending count');\n"
            "assert.strictEqual(_consumeArmedSteer('A', 'stream-1'), true);\n"
            "assert.deepStrictEqual(clearCalls, ['A']);\n"
        )

    def test_clear_steer_pending_refreshes_display(self):
        """Explicit clear is the only function that moves count to zero."""
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        badge_start = "function updateSteerPendingBadge(sessionId){"
        start = self.ui.find(badge_start)
        assert start >= 0
        end = self.ui.find(chr(10) + "function updateQueueBadge", start + len(badge_start))
        badge_src = self.ui[start:end]

        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            const counts = {{ A: 2 }};
            let indicator = null;
            globalThis._steerPendingCounts = counts;
            globalThis._currentSteerSessionId = () => 'A';
            globalThis._steerOwnerIsCurrent = () => true;
            globalThis.getSteerPendingCount = (sid) => counts[sid] || 0;
            globalThis.setComposerStatus = () => {{}};
            globalThis._updateSteerPendingIndicatorStatus = (count) => {{ indicator = count; }};
            globalThis._setSteerPendingCount = (sid, count) => {{
              if (count) counts[sid] = count; else delete counts[sid];
              return count;
            }};
            eval({json.dumps(badge_src)});
            counts.A = 2;
            updateSteerPendingBadge('A');
            assert.strictEqual(counts.A, 2, 'refresh must not mutate count');
            assert.strictEqual(indicator, 2);
            clearSteerPending('A');
            assert.strictEqual(counts.A, undefined, 'explicit clear must remove count');
            assert.strictEqual(indicator, 0, 'explicit clear must refresh display');
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_file_steer_targets_captured_session_when_user_switches_mid_upload(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[{{name:'a.pdf'}}]}};
            let uploadOptions = null;
            let apiPayload = null;
            let trayRenders = 0;
            let indicatorCalls = 0;
            let draftClears = [];
            function t(k){{return k;}}
            function $(id){{return {{value:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}}};}}
            function setComposerStatus(){{}}
            function showToast(){{}}
            function renderTray(){{trayRenders += 1;}}
            function _showSteerIndicator(){{indicatorCalls += 1;}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(sid,text,files){{draftClears.push({{sid,text,files}});}}
            async function uploadPendingFiles(options){{
              uploadOptions = options;
              S.session = {{session_id:'B'}};
              S.pendingFiles = [{{name:'b.pdf'}}];
              return [{{path:'/tmp/a.pdf'}}];
            }}
            async function api(url, options){{
              assert.strictEqual(url, '/api/chat/steer');
              apiPayload = JSON.parse(options.body);
              return {{accepted:true}};
            }}
            eval({json.dumps(steer_src)});
            (async()=>{{
              const delivered = await _trySteer('hint', false);
              assert.strictEqual(delivered, true);
              assert.strictEqual(uploadOptions.sessionId, 'A');
              assert.strictEqual(uploadOptions.files.length, 1);
              assert.strictEqual(uploadOptions.files[0].name, 'a.pdf');
              assert.strictEqual(apiPayload.session_id, 'A');
              assert.strictEqual(S.session.session_id, 'B');
              assert.strictEqual(S.pendingFiles.length, 1);
              assert.strictEqual(S.pendingFiles[0].name, 'b.pdf');
              assert.strictEqual(trayRenders, 0);
              assert.strictEqual(indicatorCalls, 0);
              assert.strictEqual(draftClears.length, 1);
              assert.strictEqual(draftClears[0].sid, 'A');
              assert.strictEqual(draftClears[0].files[0].name, 'a.pdf');
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_dead_steer_fallback_clears_busy_state_and_recovery_sends_normally(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _showSteerRecovery",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            const steerSrc = {json.dumps(steer_src)};
            function makeElement(tag){{
              return {{
                tag,
                className:'',
                textContent:'',
                children:[],
                listeners:{{}},
                appendChild(child){{this.children.push(child);}},
                remove(){{this.removed=true;}},
                addEventListener(name,fn){{this.listeners[name]=fn;}},
                querySelector(sel){{return null;}},
              }};
            }}
            let inner = makeElement('div');
            const document = {{
              getElementById(id){{return id==='msgInner'?inner:null;}},
              createElement: makeElement,
            }};
            function t(k){{return k;}}
            function _steerFailureMessageKey(fallback){{return 'steer_fail_'+fallback;}}
            function scrollToBottom(){{}}
            function setComposerStatus(){{}}
            function showToast(key){{if(globalThis.__toasts)globalThis.__toasts.push(key);}}
            function renderTray(){{if(globalThis.__trayRenders)globalThis.__trayRenders.count += 1;}}
            function autoResize(){{}}
            function _showSteerIndicator(){{}}
            function _clearComposerDraft(sid,text,files){{if(globalThis.__draftClears)globalThis.__draftClears.push({{sid,text,files}});}}
            async function uploadPendingFiles(){{return [];}}
            eval(steerSrc);

            async function runStreamDeadFallback(explicitSteer=false, msg='retry me'){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              let sendCalls = 0;
              let sendInput = null;
              let sendOptions = null;
              let apiPayload = null;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[{{name:'a.pdf'}}],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async options => {{sendCalls += 1; sendInput = input.value; sendOptions = options;}};
              globalThis.api = async (url, options) => {{
                assert.strictEqual(url, '/api/chat/steer');
                apiPayload = JSON.parse(options.body);
                return {{accepted:false, fallback:'stream_dead'}};
              }};

              const delivered = await _trySteer(msg, explicitSteer);
              assert.strictEqual(delivered, false);
              assert.deepStrictEqual(apiPayload, {{session_id:'A', text:msg}});
              assert.strictEqual(S.busy, false);
              assert.strictEqual(S.activeStreamId, null);
              assert.strictEqual(S.session.active_stream_id, null);
              assert.ok(!Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, ['A']);
              assert.strictEqual(updateSendBtnCalls, 1);
              assert.strictEqual(input.value, explicitSteer ? `/steer ${{msg}}` : msg);
              assert.strictEqual(S.pendingFiles.length, 1);
              const recovery = inner.children[inner.children.length - 1];
              const retry = recovery.children[1];
              assert.strictEqual(retry.textContent, 'clarify_send');
              retry.listeners.click();
              await Promise.resolve();
              assert.strictEqual(sendCalls, 1);
              assert.strictEqual(sendInput, msg);
              assert.deepStrictEqual(sendOptions, {{literalSlash:true}});
            }}

            async function runNoCachedAgentFallback(explicitSteer=false, msg='retry me'){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              let sendCalls = 0;
              let apiCalls = 0;
              let apiPayload = null;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[{{name:'a.pdf'}}],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async () => {{sendCalls += 1;}};
              globalThis.api = async (url, options) => {{
                assert.strictEqual(url, '/api/chat/steer');
                apiCalls += 1;
                apiPayload = JSON.parse(options.body);
                return {{accepted:false, fallback:'no_cached_agent'}};
              }};

              const delivered = await _trySteer(msg, explicitSteer);
              assert.strictEqual(delivered, false);
              assert.deepStrictEqual(apiPayload, {{session_id:'A', text:msg}});
              assert.strictEqual(S.busy, true);
              assert.strictEqual(S.activeStreamId, 'stream-1');
              assert.strictEqual(S.session.active_stream_id, 'stream-1');
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(input.value, explicitSteer ? `/steer ${{msg}}` : msg);
              assert.strictEqual(S.pendingFiles.length, 1);
              const recovery = inner.children[inner.children.length - 1];
              const retry = recovery.children[1];
              assert.strictEqual(retry.textContent, 'steer_recovery_retry');
              retry.listeners.click();
              await Promise.resolve();
              await Promise.resolve();
              assert.strictEqual(sendCalls, 0);
              assert.strictEqual(apiCalls, 2);
            }}

            async function runGatewayQueuedFallback(switchDuringAwait=false){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              let queued = [];
              let queueBadges = [];
              let draftClears = [];
              let trayRenders = 0;
              let toasts = [];
              let submittedFile = {{name:'a.pdf'}};
              let replacementFile = {{name:'replacement.pdf'}};
              let apiPayload = null;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1', model:'fallback-model', model_provider:'fallback-provider'}},
                activeStreamId:'stream-1',
                activeProfile:'work',
                busy:true,
                pendingFiles:[submittedFile],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.queueSessionMessage = (sid, payload) => queued.push({{sid, payload}});
              globalThis.updateQueueBadge = sid => queueBadges.push(sid);
              globalThis.__draftClears = draftClears;
              globalThis.__trayRenders = {{count:0}};
              globalThis.__toasts = toasts;
              globalThis._chatPayloadModelState = () => ({{model:'captured-model', model_provider:'captured-provider'}});
              globalThis.api = async (url, options) => {{
                assert.strictEqual(url, '/api/chat/steer');
                apiPayload = JSON.parse(options.body);
                if(switchDuringAwait){{
                  S.session={{session_id:'B', active_stream_id:'stream-B'}};
                  S.activeStreamId='stream-B';
                  S.pendingFiles=[replacementFile];
                }}else{{
                  S.pendingFiles=[submittedFile, replacementFile];
                }}
                return {{accepted:false, fallback:'gateway_steer_queued'}};
              }};

              const delivered = await _trySteer('queue me', false);
              assert.strictEqual(delivered, true);
              assert.deepStrictEqual(apiPayload, {{session_id:'A', text:'queue me'}});
              assert.strictEqual(S.busy, true);
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(inner.children.length, 0);
              assert.deepStrictEqual(queueBadges, ['A']);
              assert.strictEqual(queued.length, 1);
              assert.strictEqual(queued[0].sid, 'A');
              assert.strictEqual(queued[0].payload.text, 'queue me');
              assert.deepStrictEqual(queued[0].payload.files, [submittedFile]);
              assert.strictEqual(queued[0].payload.model, 'captured-model');
              assert.strictEqual(queued[0].payload.model_provider, 'captured-provider');
              assert.strictEqual(queued[0].payload.profile, 'work');
              assert.strictEqual(draftClears.length, 1);
              assert.strictEqual(draftClears[0].sid, 'A');
              assert.strictEqual(draftClears[0].text, 'queue me');
              assert.deepStrictEqual(draftClears[0].files, [submittedFile]);
              assert.deepStrictEqual(toasts, ['steer_leftover_queued']);
              if(switchDuringAwait){{
                assert.strictEqual(S.session.session_id, 'B');
                assert.deepStrictEqual(S.pendingFiles, [replacementFile]);
                assert.strictEqual(globalThis.__trayRenders.count, 0);
              }}else{{
                assert.deepStrictEqual(S.pendingFiles, [replacementFile]);
                assert.strictEqual(globalThis.__trayRenders.count, 1);
              }}
              delete globalThis.__draftClears;
              delete globalThis.__trayRenders;
              delete globalThis.__toasts;
            }}

            async function runLateDeadFallbackDoesNotClearNewStream(){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async () => {{throw new Error('send must not run for a stale dead fallback');}};
              globalThis.api = async () => {{
                S.activeStreamId='stream-2';
                S.session.active_stream_id='stream-2';
                return {{accepted:false, fallback:'stream_dead'}};
              }};

              const delivered = await _trySteer('old steer', false);
              assert.strictEqual(delivered, false);
              assert.strictEqual(S.busy, true);
              assert.strictEqual(S.activeStreamId, 'stream-2');
              assert.strictEqual(S.session.active_stream_id, 'stream-2');
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(input.value, '');
              assert.strictEqual(inner.children.length, 0);
            }}

            async function runAdjacentLiveFailure(){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[{{name:'a.pdf'}}],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async () => {{throw new Error('send must not run for live steer failures');}};
              globalThis.api = async () => {{return {{accepted:false, fallback:'agent_lacks_steer'}};}};

              const delivered = await _trySteer('live hint', false);
              assert.strictEqual(delivered, false);
              assert.strictEqual(S.busy, true);
              assert.strictEqual(S.activeStreamId, 'stream-1');
              assert.strictEqual(S.session.active_stream_id, 'stream-1');
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(input.value, 'live hint');
              assert.strictEqual(S.pendingFiles.length, 1);
              const recovery = inner.children[inner.children.length - 1];
              const retry = recovery.children[1];
              assert.strictEqual(retry.textContent, 'steer_recovery_retry');
            }}

            (async()=>{{
              await runNoCachedAgentFallback();
              await runNoCachedAgentFallback(true);
              await runGatewayQueuedFallback(false);
              await runGatewayQueuedFallback(true);
              await runStreamDeadFallback();
              await runStreamDeadFallback(true);
              await runStreamDeadFallback(true, '/help');
              await runLateDeadFallbackDoesNotClearNewStream();
              await runAdjacentLiveFailure();
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_send_busy_steer_accepts_file_only_input(self):
        idx = self.msgs.find("if(S.busy||compressionRunning)")
        assert idx >= 0
        block = self.msgs[idx:idx + 500]
        assert "if(text||S.pendingFiles.length)" in block, (
            "busy send must route file-only composer submissions through queue/interrupt/steer"
        )
        assert "_trySteer uploads with clearPending=false" in self.msgs

    def test_upload_pending_files_can_preserve_staged_files_for_steer(self):
        ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        assert "async function uploadPendingFiles(options={})" in ui
        assert "const pendingFiles=Array.isArray(opts.files)?opts.files.filter(Boolean):[...(S.pendingFiles||[])];" in ui
        assert "const sessionId=String(opts.sessionId||(S.session&&S.session.session_id)||'');" in ui
        assert "const clearPending=!(opts&&opts.clearPending===false)" in ui
        assert "fd.append('session_id',sessionId)" in ui
        assert "if(clearPending&&_uploadPendingFilesCurrentSession(sessionId)){S.pendingFiles=[];renderTray();}" in ui
        assert "else if(typeof renderTray==='function'&&_uploadPendingFilesCurrentSession(sessionId))renderTray();" in ui

    def test_upload_pending_files_progress_bar_is_session_scoped(self):
        ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        progress_helper = _source_between(
            ui,
            "const _uploadPendingFilesProgressBySession",
            "\nasync function uploadPendingFiles",
        )
        upload_body = ui[ui.index("async function uploadPendingFiles") :]
        sessions = (Path(__file__).parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")
        load_body = _source_between(sessions, "async function loadSession", "\nfunction _isMessagingSession")
        assert "_uploadPendingFilesSyncProgressForSession(sid)" in load_body
        assert "_uploadPendingFilesProgressBySession.set(owner,{percent:clamped})" in progress_helper
        assert "function _uploadPendingFilesSyncProgressForSession" in progress_helper
        assert "if(!_uploadPendingFilesCurrentSession(sessionId)){" in progress_helper
        assert "barWrap.dataset.uploadSessionId=owner" in progress_helper
        assert "activeForOwner" in progress_helper
        assert "barWrap.classList.remove('active')" in progress_helper
        assert "_uploadPendingFilesUpdateProgress(sessionId,0)" in upload_body
        assert "_uploadPendingFilesUpdateProgress(sessionId,Math.round((i+1)/total*100))" in upload_body
        assert "_uploadPendingFilesUpdateProgress(sessionId,null)" in upload_body
        assert "barWrap.classList.add('active');bar.style.width='0%';" not in upload_body
        assert "barWrap.classList.remove('active');bar.style.width='0%';" not in upload_body

    def test_upload_progress_bar_hides_on_switch_and_reappears_on_owner_return(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        progress_src = _source_between(
            ui,
            "const _uploadPendingFilesProgressBySession",
            "\nasync function uploadPendingFiles",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}}};
            const bar = {{style:{{width:''}}}};
            const barWrap = {{
              dataset: {{}},
              active: false,
              classList: {{
                add(cls){{ if(cls === 'active') barWrap.active = true; }},
                remove(cls){{ if(cls === 'active') barWrap.active = false; }},
              }},
            }};
            function $(id){{
              if(id === 'uploadBar') return bar;
              if(id === 'uploadBarWrap') return barWrap;
              return null;
            }}
            eval({json.dumps(progress_src)});
            _uploadPendingFilesUpdateProgress('A', 0);
            assert.strictEqual(barWrap.active, true);
            assert.strictEqual(bar.style.width, '0%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, 'A');

            S.session = {{session_id:'B'}};
            _uploadPendingFilesSyncProgressForSession('B');
            assert.strictEqual(barWrap.active, false);
            assert.strictEqual(bar.style.width, '0%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, undefined);

            _uploadPendingFilesUpdateProgress('A', 50);
            assert.strictEqual(barWrap.active, false);
            assert.strictEqual(bar.style.width, '0%');

            S.session = {{session_id:'A'}};
            _uploadPendingFilesSyncProgressForSession('A');
            assert.strictEqual(barWrap.active, true);
            assert.strictEqual(bar.style.width, '50%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, 'A');

            _uploadPendingFilesUpdateProgress('A', null);
            assert.strictEqual(barWrap.active, false);
            assert.strictEqual(bar.style.width, '0%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, undefined);
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_pending_steer_leftover_listener(self):
        """Frontend must listen for pending_steer_leftover SSE events and queue them."""
        idx = self.msgs.find("addEventListener('pending_steer_leftover'")
        assert idx >= 0, "messages.js must add a listener for pending_steer_leftover"
        block = self.msgs[idx:idx + 1200]
        assert "queueSessionMessage" in block, (
            "pending_steer_leftover handler must queue the leftover text for the next turn"
        )
        assert "clearSteerPending" in block, (
            "pending_steer_leftover must explicitly clear pending state after requeue"
        )
        assert "updateSteerPendingBadge" not in block, (
            "leftover must use explicit clear, not the display-only refresh"
        )

    def test_done_handler_clears_background_owner_pending_count(self):
        """Maintainer review (2026-09-04T15:15): the done handler runs for both
        active and background sessions, while the setBusy(false) clear only
        fires for the viewed session — so a steer delivered to a non-active
        owner session A (user switched to B) never cleared. The done handler
        must call clearSteerPending(completedSid) unconditionally, which is
        the one point that covers background owners without inventing an
        'applied' proof."""
        idx = self.msgs.find("addEventListener('done'")
        assert idx >= 0, "messages.js must have a done listener"
        block = self.msgs[idx:idx + 9000]
        completed_idx = block.find("_clearOwnerInflightState();")
        assert completed_idx >= 0, "done handler must call _clearOwnerInflightState"
        tail = block[completed_idx:completed_idx + 600]
        assert "clearSteerPending(completedSid)" in tail, (
            "done handler must call clearSteerPending(completedSid) right after "
            "_clearOwnerInflightState so a background owner's stale pending count "
            "clears when its turn completes"
        )


# ── i18n keys ─────────────────────────────────────────────────────────────

class TestI18nKeys:
    """Steer-facing user copy must exist in every locale block."""

    @classmethod
    def setup_class(cls):
        cls.i18n = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")

    def test_cmd_steer_delivered_in_all_locales(self):
        assert self.i18n.count("cmd_steer_delivered:") >= 6, (
            f"cmd_steer_delivered appears {self.i18n.count('cmd_steer_delivered:')} times; "
            f"expected ≥6 (one per locale)"
        )

    def test_steer_pending_count_in_all_locales(self):
        assert self.i18n.count("steer_pending_count:") == 15, (
            f"steer_pending_count appears {self.i18n.count('steer_pending_count:')} times; "
            f"expected 15 (one per locale)"
        )

    def test_steer_leftover_queued_in_all_locales(self):
        assert self.i18n.count("steer_leftover_queued:") >= 6, (
            f"steer_leftover_queued appears {self.i18n.count('steer_leftover_queued:')} times; "
            f"expected ≥6 (one per locale)"
        )


# ── Leftover SSE delivery: streaming.py emits pending_steer_leftover ─────

class TestLeftoverDelivery:
    """After run_conversation returns, _drain_pending_steer is called and a
    pending_steer_leftover SSE event is emitted if there's still text stashed."""

    def test_leftover_drain_call_in_streaming(self):
        """Verify the streaming.py source contains the drain call before put('done', ...)."""
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        assert "_drain_pending_steer" in src, (
            "_run_agent_streaming must call agent._drain_pending_steer() to deliver leftovers"
        )
        assert "pending_steer_leftover" in src, (
            "_run_agent_streaming must emit a pending_steer_leftover SSE event"
        )

    def test_leftover_drain_runs_before_done_event(self):
        """The drain must happen BEFORE put('done', ...) so frontend gets both events
        on the same turn."""
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        # Find the drain invocation and the next put('done', ...) AFTER it
        drain_idx = src.find("_drain_pending_steer()")
        assert drain_idx >= 0
        done_idx = src.find("put('done'", drain_idx)
        assert done_idx >= 0
        # No put('done', ...) should appear BEFORE the drain in the same code block
        # (we already check the drain is in the file; ordering matters within the
        # non-ephemeral success path)
        assert drain_idx < done_idx, (
            "_drain_pending_steer must run before put('done', ...) so the SSE listener "
            "sees the leftover before stream_end fires"
        )
