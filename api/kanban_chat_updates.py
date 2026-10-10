"""Durable Kanban -> origin-chat receipts, serviced by the existing drain loop.

No worker, model wake, transport fallback, or task mutation. Raw worker prose
is never forwarded. Native events, SessionDB leases and WebUI SessionChannel
remain the authorities for task state, writer admission and browser delivery.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger(__name__)
_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out",
          "review_requested", "changes_requested", "block_loop_detected",
          "continuation_required", "commented", "attached")
_LIMIT = 128


def _native_boards():
    from hermes_cli import kanban_db as kb
    return [(b["slug"], Path(b["db_path"])) for b in kb.list_boards(include_archived=False)]


def _readonly(path):
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    return conn


def _notice(conn, event, task, language):
    """Return safe content and a semantic milestone identity, or silence."""
    try:
        payload = json.loads(event["payload"] or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = event["kind"]
    egyptian = language == "ar-EG"
    milestone = ""
    if kind == "continuation_required":
        checkpoint = payload.get("checkpoint") or {}
        if not isinstance(checkpoint, dict):
            return None
        files = checkpoint.get("changed_files") or []
        if not isinstance(files, list) or not files:
            return None
        # A saved checkpoint with changed files is evidence of work, not proof
        # of completed checks. Repeated budget exits on that same evidence stay quiet.
        milestone = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        count = len(files)
        text = (f"فيه تعديلات محفوظة في {count} ملفات، والعامل هيكمّل من آخر نقطة. الشغل لسه ما اكتملش."
                if egyptian else f"Changes saved in {count} files. The worker will resume its checkpoint; work is still in progress.")
    elif kind == "commented":
        comment = conn.execute("SELECT author,body FROM task_comments WHERE id=? AND task_id=?",
                               (payload.get("comment_id"), event["task_id"])).fetchone()
        if not comment or comment["author"] != task["assignee"]:
            return None
        try:
            update = json.loads(comment["body"]).get("user_update", {})
        except (ValueError, AttributeError):
            return None
        if not isinstance(update, dict):
            return None
        stage, count = update.get("stage"), update.get("count")
        if stage not in {"checks_passed", "files_written", "items_verified"} or type(count) is not int or not 0 < count <= 10000:
            return None
        units = {"checks_passed": ("اختبارات عدّت بنجاح", "checks passed"),
                 "files_written": ("ملفات نتيجة اتكتبت", "output files written"),
                 "items_verified": ("عناصر اتراجعت", "items verified")}
        text = f"تقدم ملموس: {count} {units[stage][0]}. العامل لسه بيكمّل." if egyptian else f"Progress: {count} {units[stage][1]}. Work is continuing."
        milestone = f"{stage}:{count}"
    elif kind == "attached":
        # Only worker-authored nonempty artifacts; uploaded inputs are not progress.
        if payload.get("by") != task["assignee"] or not isinstance(payload.get("size"), int) or payload["size"] <= 0:
            return None
        text = "العامل أضاف ملف نتيجة للمراجعة. ده تقدم في الشغل، مش إعلان اكتمال." if egyptian else "The worker added an output file for review; work is still in progress."
    else:
        texts = {
            "completed": ("العامل سلّم نتيجة المهمة. النتيجة جاهزة للمراجعة المعتادة.", "The worker submitted its result, ready for normal review."),
            "blocked": ("فيه عائق موقف المهمة؛ العامل محتاج توضيح منك عشان يكمل." if payload.get("kind") == "needs_input" else "المهمة واقفة عند عائق. محتاجة يتراجع سبب الوقفة قبل ما الشغل يكمل.", "The task is blocked and needs attention before continuing."),
            "gave_up": ("محاولات المهمة وقفت بعد تكرار الفشل. محتاجة تدخل قبل المحاولة الجاية.", "Task retries stopped after repeated failure; intervention is needed."),
            "crashed": ("العامل توقف بشكل غير متوقع. المهمة ما اكتملتش.", "The worker stopped unexpectedly; the task did not complete."),
            "timed_out": ("المهمة عدّت وقت التنفيذ المسموح واتوقفت. ما اكتملتش.", "The task exceeded its execution time and stopped without completing."),
            "review_requested": ("العامل وصل لمرحلة المراجعة. محتاج مراجعة النتيجة قبل اعتمادها.", "The worker requested review before approval."),
            "changes_requested": ("المراجعة طلبت تعديلات. المهمة محتاجة تستكمل التعديلات دي.", "Review requested changes; the task needs further work."),
            "block_loop_detected": ("المهمة بتكرر نفس العائق. محتاجة تدخل عشان تخرج من الوقفة دي.", "The task is repeating the same blocker and needs intervention."),
        }
        if kind not in texts:
            return None
        text = texts[kind][0 if egyptian else 1]
    ordinal = conn.execute("SELECT COUNT(*) FROM tasks WHERE session_id=? AND rowid <= "
                           "(SELECT rowid FROM tasks WHERE id=?)",
                           (task["session_id"], event["task_id"])).fetchone()[0]
    label = "اختبار وصول تحديثات المهام" if task["title"] == "[webui-notification-canary]" else f"تحديث المهمة {ordinal} من الشات ده"
    if not egyptian:
        label = f"Task {ordinal} update"
    return f"{label}: {text}", milestone


class KanbanChatUpdates:
    def __init__(self, state_dir, home, profile, language, *, boards=None):
        self.path = Path(state_dir) / "kanban-chat-updates.json"
        self.home, self.profile, self.language = Path(home).resolve(), profile, language
        self.boards = boards or _native_boards
        self.lock = threading.Lock()

    def _load(self):
        if not self.path.exists():
            return {"profile": self.profile, "home": str(self.home), "cursors": {}, "pending": []}
        state = json.loads(self.path.read_text())
        if state["profile"] != self.profile or state["home"] != str(self.home):
            raise ValueError("Kanban notification consumer identity mismatch")
        return state

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _owned(self, sid):
        from api.models import Session
        if not sid:
            return False
        sidecar = Session.load_metadata_only(sid)
        if not sidecar or sidecar.profile != self.profile or getattr(sidecar, "is_cli_session", False):
            return False
        with closing(_readonly(self.home / "state.db")) as conn:
            row = conn.execute("SELECT source,profile_name,ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
        return bool(row and row["source"] == "webui" and row["profile_name"] == self.profile and not row["ended_at"])

    def _resolve_target(self, sid):
        from hermes_state import SessionDB
        if not sid:
            return None
        with closing(SessionDB(self.home / "state.db", read_only=True)) as db:
            origin = db.get_session(sid)
            if not origin or origin.get("source") != "webui" or origin.get("profile_name") != self.profile:
                return None
            target = db.resolve_resume_session_id(sid)
            if target != sid and origin.get("end_reason") != "compression":
                return None
        return target if self._owned(target) else None

    def _deliver(self, pending):
        from api import config as cfg
        from api.background_process import _session_has_active_turn, get_session_channel
        from api.models import Session, get_session
        from hermes_state import SessionDB
        sid = self._resolve_target(pending["session_id"])
        if not sid:
            return True  # no inferred routing or cross-platform fallback
        lock = cfg._get_session_agent_lock(sid)
        if not lock.acquire(blocking=False):
            return False
        db = None
        holder = f"webui-kanban:{os.getpid()}:{uuid.uuid4().hex}"
        try:
            if _session_has_active_turn(sid):
                return False
            session = get_session(sid)
            if session.profile != self.profile or session.active_stream_id or session.pending_user_message:
                return False
            db = SessionDB(self.home / "state.db")
            if not db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=30, patience_s=0.5):
                return False
            # Revalidate under both native writer admission boundaries.
            if not self._owned(sid) or _session_has_active_turn(sid):
                return False
            key = pending["event_key"]
            persisted = Session.load(sid)
            milestone_key = pending.get("milestone_key")
            persisted_messages = persisted.messages if persisted else []
            previous_milestone = next((m.get("kanban_milestone_key") for m in reversed(persisted_messages)
                                       if m.get("kanban_task_key") == pending["task_key"]
                                       and m.get("kanban_milestone_key")), None)
            if milestone_key and previous_milestone == milestone_key:
                return True
            if persisted and any(m.get("kanban_event_key") == key for m in persisted.messages):
                channel = get_session_channel(sid)
                if channel:
                    channel.emit("session-updated", {"session_id": sid, "message_count": len(persisted.messages),
                                                     "source": "kanban"})
                return True  # Only the committed sidecar proves delivery.
            metadata = {"event_key": key, "task_key": pending["task_key"], "milestone_key": milestone_key}
            def commit(conn):
                row = conn.execute("WITH RECURSIVE lineage(id) AS (SELECT ? UNION "
                                   "SELECT s.parent_session_id FROM sessions s JOIN lineage l ON s.id=l.id "
                                   "JOIN sessions p ON p.id=s.parent_session_id WHERE p.end_reason='compression') "
                                   "SELECT m.id,m.content,m.timestamp FROM messages m JOIN lineage l ON m.session_id=l.id "
                                   "WHERE m.display_kind='kanban_update' AND json_extract(m.display_metadata,'$.event_key')=?",
                                   (sid, key)).fetchone()
                if row:
                    return tuple(row)
                db._check_transcript_write_guards(conn, sid, None, turn_lease_holder=holder)
                stamp = time.time()
                mid = conn.execute("INSERT INTO messages(session_id,role,content,timestamp,display_kind,display_metadata) "
                                   "VALUES(?,'assistant',?,?,'kanban_update',?)",
                                   (sid, pending["content"], stamp, json.dumps(metadata))).lastrowid
                db._bump_session_counters(conn, sid, 1, 0, unit=True)
                return mid, pending["content"], stamp
            mid, content, stamp = db._execute_write(commit, patience_s=0.5)
            # Native row + sidecar carry the same identity and timestamp. A crash
            # between these stores reuses the row instead of generating a reply.
            if not any(m.get("kanban_event_key") == key or m.get("_state_db_row_id") == mid for m in session.messages):
                message = {"role": "assistant", "content": content, "timestamp": stamp,
                           "_state_db_row_id": mid, "kanban_event_key": key,
                           "kanban_task_key": pending["task_key"], "kanban_milestone_key": milestone_key}
                session.messages.append(message)
                if isinstance(session.context_messages, list):
                    session.context_messages.append(dict(message))
            session.save()
            session._metadata_message_count = len(session.messages)
            channel = get_session_channel(sid)
            if channel:
                channel.emit("session-updated", {"session_id": sid, "message_count": len(session.messages),
                                                 "source": "kanban"})
            return True
        finally:
            try:
                if db:
                    try:
                        db.release_session_turn_lease(sid, holder)
                    finally:
                        db.close()
            finally:
                lock.release()

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            state = self._load()
            for _board, path in self.boards():
                path = Path(path).resolve()
                if not path.exists():
                    continue
                board_key = str(path)
                with closing(_readonly(path)) as conn:
                    if board_key not in state["cursors"]:
                        # Activation watermark prevents replay of old user work.
                        state["cursors"][board_key] = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events").fetchone()[0]
                        self._save(state)
                        continue
                    if len(state["pending"]) >= _LIMIT:
                        continue  # backpressure: events remain in the authoritative board
                    rows = conn.execute("SELECT e.*,t.session_id,t.assignee,t.title FROM task_events e "
                                        "JOIN tasks t ON t.id=e.task_id WHERE e.id>? AND e.kind IN (" +
                                        ",".join("?" for _ in _KINDS) + ") ORDER BY e.id LIMIT ?",
                                        (state["cursors"][board_key], *_KINDS, _LIMIT - len(state["pending"]))).fetchall()
                    for event in rows:
                        state["cursors"][board_key] = event["id"]
                        if not self._resolve_target(event["session_id"]):
                            continue
                        notice = _notice(conn, event, event, self.language)
                        if not notice:
                            continue
                        content, milestone = notice
                        identity = hashlib.sha256(board_key.encode()).hexdigest()[:24]
                        state["pending"].append({"event_key": f"{identity}:{event['id']}",
                                                 "task_key": f"{identity}:{event['task_id']}",
                                                 "task_id": event["task_id"], "board": board_key,
                                                 "session_id": event["session_id"], "content": content,
                                                 "milestone_key": milestone})
                    self._save(state)
            remaining = []
            for pending in state["pending"]:
                # Task origin is rechecked at delivery, including a deferred event.
                try:
                    with closing(_readonly(pending["board"])) as conn:
                        row = conn.execute("SELECT session_id FROM tasks WHERE id=?", (pending["task_id"],)).fetchone()
                except (OSError, sqlite3.Error):
                    remaining.append(pending)
                    continue
                if not row or row["session_id"] != pending["session_id"]:
                    continue
                try:
                    delivered = self._deliver(pending)
                except Exception:
                    logger.warning("Kanban chat receipt deferred (persistence/admission unavailable)", exc_info=True)
                    delivered = False
                if not delivered:
                    remaining.append(pending)
            state["pending"] = remaining
            self._save(state)
        finally:
            self.lock.release()


_consumer = None


def drain_kanban_chat_updates():
    """Called on the existing process-completion drain tick; no new thread."""
    global _consumer
    profile = os.environ.get("HERMES_WEBUI_TASK_UPDATES_PROFILE", "").strip()
    if not profile:
        return
    if _consumer is None:
        from api import config as cfg
        home = os.environ.get("HERMES_HOME", "")
        if not home:
            raise ValueError("Task notifications require an explicit Hermes home")
        _consumer = KanbanChatUpdates(cfg.STATE_DIR, home, profile,
                                      os.environ.get("HERMES_WEBUI_TASK_UPDATES_LANGUAGE", "en"))
    _consumer.tick()
