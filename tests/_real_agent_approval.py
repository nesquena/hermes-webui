"""Park a real Hermes Agent approval in-process, the way a WebUI turn does.

The Agent thread runs its own command gate and waits in ``_gateway_queues``.
Whatever answers it, the Agent applies the answer itself after its wait,
including saving a session/always grant, so tests read grants from the Agent's
own state instead of seeding a bare queue entry nobody waits on.
"""
from __future__ import annotations

import contextlib
import importlib
import threading
import types

PARKED_COMMAND = "rm -rf /tmp/webui-parked-approval-target"


@contextlib.contextmanager
def parked_agent_approval(monkeypatch, sid: str, command: str = PARKED_COMMAND):
    """Yield once the real Agent gate for ``command`` is waiting on a human answer.

    ``.card`` is the approval data the Agent notified, ``.keys`` its pattern keys, and
    ``.finish()`` joins the Agent thread and returns the gate result.
    """
    ta = importlib.import_module("tools.approval")
    approval_context = importlib.import_module("tools.approval_context")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    monkeypatch.setenv("HERMES_SESSION_KEY", sid)
    for name in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_CRON_SESSION", "HERMES_YOLO_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    if hasattr(ta, "_YOLO_MODE_FROZEN"):
        monkeypatch.setattr(ta, "_YOLO_MODE_FROZEN", False)

    cards: list[dict] = []
    notified = threading.Event()

    def notify(data):
        cards.append(dict(data))
        notified.set()

    gate: dict = {}

    def turn():
        approval_context.set_current_session_key(sid)
        gate["result"] = ta.check_all_command_guards(command, "local")

    ta.register_gateway_notify(sid, notify)
    thread = threading.Thread(target=turn, daemon=True)
    thread.start()

    def finish(timeout: float = 15.0) -> dict:
        thread.join(timeout)
        assert not thread.is_alive(), "the Agent thread is still waiting for an answer"
        return gate.get("result") or {}

    try:
        assert notified.wait(15), "the Agent gate never asked for approval"
        card = cards[0]
        keys = [k for k in (card.get("pattern_keys") or [card.get("pattern_key")]) if k]
        yield types.SimpleNamespace(card=card, keys=keys, finish=finish, thread=thread)
    finally:
        if thread.is_alive():
            ta.resolve_gateway_approval(sid, "deny", resolve_all=True)
            thread.join(10)
        ta.unregister_gateway_notify(sid)
        ta.clear_session(sid)
        with ta._lock:
            importlib.import_module("api.route_approvals")._pending.pop(sid, None)
