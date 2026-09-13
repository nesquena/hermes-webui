"""Optional contract check against an official Hermes checkout.

Real groups handlers/driver/SQLite and WebSocket bridge; only Agent execution is
deterministic. No model calls, user profiles, or production gateway are used.
"""
import os
import time

import pytest

from api.bot_groups import call_group_method
from tests.test_bot_groups import gateway


class SyntheticAgentRPC:
    def __init__(self):
        self.sessions = {}

    def resolve_exact(self, *, profile, title, source):
        return self.sessions.get((profile, title))

    def create(self, *, profile, title, source):
        session = {"session_id": profile + "-fixture", "title": title}
        self.sessions[(profile, title)] = session
        return session

    def resume(self, *, profile, session_id, source):
        return {"session_id": session_id}

    def submit(self, *, profile, on_terminal, **_kwargs):
        on_terminal({"status": "settled", "text": "Synthetic reply from " + profile})
        return {"accepted": True}

    def history(self, **_kwargs):
        return []

    def info(self, **_kwargs):
        return {"active": False, "task_id": None}

    def interrupt(self, **_kwargs):
        return {"interrupted": True}


def test_official_groups_driver_and_persistent_log_through_bridge(monkeypatch, tmp_path):
    reference = os.getenv("HERMES_BOT_GROUPS_REFERENCE")
    if not reference:
        pytest.skip("Set HERMES_BOT_GROUPS_REFERENCE to an official groups protocol v2 checkout")
    monkeypatch.syspath_prepend(reference)
    for key in ("HERMES_HOME", "HERMES_BASE_HOME"):
        monkeypatch.setenv(key, str(tmp_path))
    import tui_gateway.server as server
    from tui_gateway import methods_groups

    methods_groups.stop_hosted_room_service(timeout=1)
    service = methods_groups.start_hosted_room_service()
    service.rpc = SyntheticAgentRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("research", "review")
    try:
        with gateway(lambda request: server._methods[request["method"]](request["id"], request["params"])) as (url, _calls):
            monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_URL", url)
            monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_TOKEN", "isolated-contract-token")
            assert call_group_method("capabilities", {})["driver"] is True
            members = [{"member_id": name, "profile": name, "handle": name, "display_name": name.title()} for name in ("research", "review")]
            room = call_group_method("create", {"room_id": "contract-room", "name": "Contract room", "members": members})["room"]
            assert room["members"][1]["profile"] == "review"
            params = {"room_id": room["room_id"], "event_id": "contract-send", "payload": {"text": "@review Check the plan", "thread_id": "main"}}
            assert call_group_method("send", params)["accepted"] is True
            assert call_group_method("send", params)["accepted"] is True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                page = call_group_method("log", {"room_id": room["room_id"], "since_seq": 0, "limit": 100})
                if any(event["kind"] == "message.member" for event in page["events"]):
                    break
                time.sleep(0.02)
            assert sum(event["kind"] == "message.user" for event in page["events"]) == 1
            assert any(event["payload"].get("text") == "Synthetic reply from review" for event in page["events"])
            assert page["authority"]["epoch"] == room["authority_epoch"]
            assert call_group_method("stop", {"room_id": room["room_id"], "cancel_id": "contract-stop"})["cancelled"] >= 0
            methods_groups.stop_hosted_room_service(timeout=2)
            replay = call_group_method("log", {"room_id": room["room_id"], "since_seq": 0, "limit": 100})
            assert replay["cursor"] >= page["cursor"]
            assert all(event["event_id"] in {e["event_id"] for e in replay["events"]} for event in page["events"])
    finally:
        methods_groups.stop_hosted_room_service(timeout=2)
