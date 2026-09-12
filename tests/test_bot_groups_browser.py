"""Browser + real WebUI HTTP bridge + loopback WS fixture (no model calls).

Run explicitly with Playwright/Chromium installed. Screenshots contain synthetic data.
"""
import os
from pathlib import Path

import pytest

from tests.test_bot_groups import gateway
from tests.test_bot_groups_http import bot_http  # noqa: F401 — shared isolated HTTP fixture


class RoomsFixture:
    def __init__(self):
        self.rooms = []
        self.events = {}
        self.actions = []
        self.working = False
        self.lose_send_ack = False

    def respond(self, request):
        op = request["method"].split(".")[-1]
        params = request["params"]
        if request["method"] == "profiles.list":
            return {"result": {"profiles": [{"name": p, "display_name": p.title()} for p in ["research", "review", "writer"]]}}
        if op == "capabilities":
            return {"result": {"protocol_version": 2, "driver": True, "methods": ["groups." + m for m in ["list", "create", "state", "log", "send", "stop", "retry", "approve"]]}}
        if op == "list":
            return {"result": {"rooms": self.rooms, "next_offset": None}}
        if op == "create":
            existing = next((r for r in self.rooms if r["room_id"] == params["room_id"]), None)
            room = existing or {**params, "authority_gateway_id": "fixture", "authority_epoch": 1}
            if not existing:
                self.rooms.append(room)
                self.events[room["room_id"]] = []
            return {"result": {"room": room}}
        if op == "state":
            room = next(r for r in self.rooms if r["room_id"] == params["room_id"])
            return {"result": {"room": room, "driver_status": {"running": True, "working": self.working, "blocked": bool(self.actions), "pending_actions": self.actions}}}
        if op == "log":
            events = self.events[params["room_id"]]
            return {"result": {"events": [e for e in events if e["seq"] > params["since_seq"]], "cursor": len(events), "has_more": False, "authority": {"gateway_id": "fixture", "epoch": 1}}}
        if op == "send":
            events = self.events[params["room_id"]]
            if not any(e["event_id"] == params["event_id"] for e in events):
                events.append({"room_id": params["room_id"], "event_id": params["event_id"], "seq": len(events) + 1, "kind": "message.user", "payload": params["payload"]})
            if self.lose_send_ack:
                self.lose_send_ack = False
                return {"error": {"code": 5112, "message": "Synthetic lost acknowledgement"}}
            return {"result": {"accepted": True}}
        if op in {"approve", "retry", "stop"}:
            self.actions = []
            self.working = False
            return {"result": {"approved": True, "retried": True, "cancelled": 1}}
        raise AssertionError(op)


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_create_send_replay_approval_and_stop(bot_http, monkeypatch, width):  # noqa: F811 — imported pytest fixture
    pw = pytest.importorskip("playwright.sync_api")
    fixture = RoomsFixture()
    with gateway(fixture.respond) as (url, calls), pw.sync_playwright() as playwright:
        monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_URL", url)
        monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_TOKEN", "fixture-token-never-in-browser")
        browser = playwright.chromium.launch(channel=os.getenv("BOT_GROUPS_BROWSER_CHANNEL") or None)
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.add_init_script("if(window===top) localStorage.setItem('hermes-lang','en')")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        screenshots = os.getenv("BOT_GROUPS_SCREENSHOTS")
        if screenshots:
            target = Path(screenshots)
            target.mkdir(parents=True, exist_ok=True)
            monkeypatch.setenv("HERMES_WEBUI_BOT_GROUPS", "")
            page.goto(bot_http)
            page.wait_for_function("typeof switchPanel === 'function' && !!window.BotGroups")
            pw.expect(page.locator('.rail [data-panel="botGroups"]')).to_be_hidden()
            page.screenshot(path=str(target / f"webui-before-{width}.png"))
            monkeypatch.setenv("HERMES_WEBUI_BOT_GROUPS", "1")
        page.goto(bot_http)
        # Use the real navigation; on a phone the normal menu must open first.
        page.wait_for_function("typeof switchPanel === 'function' && !!window.BotGroups")
        page.evaluate("switchPanel('botGroups')")
        pw.expect(page.locator("#btnTitlebarNewChat")).to_be_hidden()
        page.locator("#botGroupsCreate").wait_for(state="attached")
        pw.expect(page.locator("#botGroupsCreate")).to_be_enabled()
        # On mobile the secondary panel lives in the existing drawer.
        if width < 768:
            page.evaluate("document.querySelector('.sidebar').classList.add('mobile-open','mobile-panel-drawer')")
        page.locator("#botGroupsCreate").click()
        page.locator('#botGroupsCreateForm input[name="name"]').fill("Research team")
        page.locator('#botGroupsProfileChoices input').nth(0).check()
        page.locator('#botGroupsProfileChoices input').nth(1).check()
        page.locator("#botGroupsSave").click()
        pw.expect(page.locator("#botGroupsTitle")).to_have_text("Research team")
        page.locator("#botGroupsMembers button").first.click()
        pw.expect(page.locator("#botGroupsText")).to_have_value("@bot-1 ")
        page.locator("#botGroupsText").fill("<img src=x onerror=alert(1)> Review this plan")
        fixture.lose_send_ack = True
        page.locator("#botGroupsSend").click()
        pw.expect(page.locator("#botGroupsError")).to_contain_text("Delivery was not confirmed")
        page.locator("#botGroupsSend").click()
        pw.expect(page.locator("#botGroupsText")).to_have_value("")
        pw.expect(page.locator("#botGroupsMessages article")).to_have_count(1)
        assert page.locator("#botGroupsMessages img").count() == 0
        sends = [c for c in calls if c["method"] == "groups.send"]
        assert len(sends) == 2 and sends[0]["params"] == sends[1]["params"]
        room_id = fixture.rooms[0]["room_id"]
        fixture.events[room_id].append({"room_id": room_id, "event_id": "reply-1", "seq": 2,
            "kind": "message.member", "actor": {"kind": "member", "id": "member-2"},
            "payload": {"text": "I will review the assumptions and report the risks. (Synthetic Agent reply)"}})
        pw.expect(page.locator("#botGroupsMessages article")).to_have_count(2)
        pw.expect(page.locator("#botGroupsMessages article").last.locator("strong")).to_have_text("Review")
        fixture.actions = [{"kind": "approval", "member_id": "member-1", "task_id": "task-1", "execution_generation": 2, "request_id": "request-1", "approval": {"command": "echo test", "choices": ["once", "deny"]}}]
        pw.expect(page.locator("#botGroupsActions")).to_contain_text("echo test", timeout=5000)
        page.locator("#botGroupsActions").get_by_role("button", name="Deny", exact=True).click()
        pw.expect(page.locator("#botGroupsActions")).to_be_empty()
        approval = next(c["params"] for c in calls if c["method"] == "groups.approve")
        assert approval["execution_generation"] == 2 and approval["request_id"] == "request-1"
        fixture.working = True
        pw.expect(page.locator("#botGroupsStop")).to_be_enabled(timeout=5000)
        page.locator("#botGroupsStop").click()
        pw.expect(page.locator("#botGroupsStatus")).to_have_text("Ready")
        # A fresh page can replay committed history without re-sending anything.
        page.reload()
        page.wait_for_function("typeof switchPanel === 'function' && !!window.BotGroups")
        page.evaluate("switchPanel('botGroups')")
        pw.expect(page.locator("#botGroupsMessages article")).to_have_count(2)
        assert len([c for c in calls if c["method"] == "groups.send"]) == 2
        assert "fixture-token-never-in-browser" not in page.content()
        if screenshots:
            page.screenshot(path=str(target / f"webui-after-{width}.png"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right>innerWidth+1 && getComputedStyle(e).position!=='fixed').slice(0,12).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().width])")
        assert errors == []
        browser.close()
