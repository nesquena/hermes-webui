"""Real-browser regression: a shared app dialog acts only on the conversation it
was opened for.

* Share and Stop sharing in the conversation panel are bound to the conversation
  on screen when they were clicked;
* a conversation change through browser history dismisses an open dialog (as
  Escape would) before switching, so its action never runs against another
  conversation (Share, Stop sharing, Clear conversation, a workspace file delete);
* the session shortcuts (J/K, Ctrl+K) do nothing while a dialog is open.

* a dismissed share dialog (close button, Escape, backdrop, a conversation change)
  writes nothing, from the panel and from the sidebar menu, including when the
  dialog was reopened before the sidebar refreshed or the switch is vetoed;
* copying a share link still works when the clipboard permission is denied;
* a dialog opened while a switch to another conversation is still in flight
  never acts on that next conversation.

Starts a real ``server.py`` with its own state, seeds conversations through
``/api/session/import`` (A shared through the real ``/api/share/create``, B and C
never shared; the later tests seed their own), then drives headless Chromium.
Request bodies of the writing endpoints are recorded, and state is read back from
the server and the disk.
"""
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parent.parent
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
WAIT_MS = 15000
SETTLE_MS = 1500

# Record clipboard writes and opened share tabs instead of performing them.
_INIT = """
window.__copied = [];
window.__opened = [];
try {
  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
    writeText: async (text) => { window.__copied.push(String(text)); },
    readText: async () => '',
  }});
} catch (_) {}
window.open = function (url) { window.__opened.push(String(url)); return null; };
"""

_STATE_JS = """() => ({
  sid: (typeof S !== 'undefined' && S.session) ? S.session.session_id : null,
  dialogOpen: typeof _isAppDialogOpen === 'function' ? _isAppDialogOpen() : null,
  copied: window.__copied.slice(),
  opened: window.__opened.slice(),
})"""


def _free_port():
    start = random.randint(18000, 19999)
    for offset in range(2000):
        port = 18000 + (start - 18000 + offset) % 2000
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 18000-19999")


def _request(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json", "Origin": base},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _share_token(base, sid):
    status, reply = _request(base, f"/api/session?session_id={sid}")
    assert status == 200, reply
    return reply["session"].get("share_token")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    state = tmp_path_factory.mktemp("share-bind-state")
    workspace = state / "workspace"
    workspace.mkdir()
    (state / "tmp").mkdir()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # A minimal explicit environment: nothing from the runner's credentials or
    # Hermes home reaches this server.
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        # Workspaces under HOME are trusted, so tests can give conversations their own.
        "HOME": str(state),
        "LANG": "C.UTF-8",
        "TMPDIR": str(state / "tmp"),
        "PYTHONUNBUFFERED": "1",
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state),
        "HERMES_HOME": str(state),
        "HERMES_BASE_HOME": str(state),
        "HERMES_CONFIG_PATH": str(state / "config.yaml"),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(state / "no-agent"),
        "HERMES_WEBUI_PASSWORD": "",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        "BROWSER": "echo",
    }
    for key in ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA"):
        if key in os.environ:
            env[key] = os.environ[key]
    log_path = state / "server.log"
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "server.py")], cwd=REPO, env=env,
            stdout=log, stderr=subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
        )
    try:
        deadline = time.time() + 45
        while True:
            if proc.poll() is not None or time.time() > deadline:
                pytest.fail("share test server did not start:\n" + log_path.read_text(errors="replace")[-3000:])
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        sids = {}
        for name in ("C", "A", "B"):
            status, reply = _request(base, "/api/session/import", {
                "title": f"Conversation {name}",
                "workspace": str(workspace),
                "messages": [
                    {"role": "user", "content": f"{name} question"},
                    {"role": "assistant", "content": f"{name} answer"},
                ],
            })
            assert status == 200, reply
            sids[name] = reply["session"]["session_id"]
        status, reply = _request(base, "/api/share/create", {"session_id": sids["A"]})
        assert status == 200, reply
        yield {"base": base, **sids, "a_token": reply["share"]["token"], "state": state, "workspace": workspace}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        except Exception as exc:  # no browser binary in this environment
            pytest.skip(f"chromium unavailable: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def shares(server):
    """Every test starts from: A shared with its original link, B and C not shared."""
    base = server["base"]
    for name in ("B", "C"):
        if _share_token(base, server[name]):
            status, reply = _request(base, "/api/share/revoke", {"session_id": server[name]})
            assert status == 200, reply
    assert _share_token(base, server["A"]) == server["a_token"]
    return server


@pytest.fixture
def page(browser, shares):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, base_url=shares["base"])
    ctx.add_init_script(_INIT)
    pg = ctx.new_page()
    pg.share_posts = []
    pg.share_statuses = []
    pg.writes = []  # (path, body) of the other writing endpoints under test

    def on_request(req):
        if req.method == "POST" and req.url.endswith("/api/share/create"):
            pg.share_posts.append(json.loads(req.post_data or "{}").get("session_id"))
        for path in ("/api/session/clear", "/api/share/revoke", "/api/file/delete"):
            if req.method == "POST" and req.url.endswith(path):
                pg.writes.append((path, json.loads(req.post_data or "{}")))

    def on_response(resp):
        if resp.request.method == "POST" and resp.url.endswith("/api/share/create"):
            pg.share_statuses.append(resp.status)

    pg.on("request", on_request)
    pg.on("response", on_response)
    yield pg
    ctx.close()


def _wait_until(page, expression, arg=None, timeout_ms=WAIT_MS):
    """Poll with page.evaluate: the app's CSP has no 'unsafe-eval'."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression, arg):
            return True
        page.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {expression!r} ({arg!r})")


def _active(page, sid):
    _wait_until(page, "(sid) => typeof S !== 'undefined' && !!S.session && S.session.session_id === sid", sid)


def _open_after(page, first, second):
    """Browser history first -> second, second opened from its sidebar row."""
    page.goto(f"/session/{first}", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=30000)
    _active(page, first)
    row = f".session-item[data-sid='{second}'] .session-title"
    page.wait_for_selector(row, timeout=WAIT_MS)
    page.click(row)
    _active(page, second)
    page.wait_for_timeout(800)


def _open_a_after_b(page, server):
    _open_after(page, server["B"], server["A"])


def _open_panel_dialog(page, button):
    """Settings -> Conversation, then the panel button that opens a confirm dialog."""
    page.click(".rail-btn[data-panel='settings']")
    page.wait_for_selector(button, state="visible", timeout=WAIT_MS)
    _wait_until(page, "(sel) => !document.querySelector(sel).disabled", button)
    page.click(button)
    _wait_until(page, "() => _isAppDialogOpen()")
    page.wait_for_timeout(300)


def _open_panel_share_dialog(page):
    _open_panel_dialog(page, "#btnShareSession")


def _import(server, title, workspace=None):
    status, reply = _request(server["base"], "/api/session/import", {
        "title": title,
        "workspace": str(workspace or server["workspace"]),
        "messages": [
            {"role": "user", "content": f"{title} question"},
            {"role": "assistant", "content": f"{title} answer"},
        ],
    })
    assert status == 200, reply
    return reply["session"]["session_id"]


def _message_count(server, sid):
    status, reply = _request(server["base"], f"/api/session?session_id={sid}")
    assert status == 200, reply
    return len(reply["session"].get("messages") or [])


def _back_then_confirm_if_still_open(page, sid):
    """Browser Back to ``sid``; if the dialog survived the change, press its confirm
    button (the user's next click). Returns whether it survived."""
    _browser_back_to(page, sid)
    survived = page.evaluate(_STATE_JS)["dialogOpen"]
    if survived:
        page.click("#appDialogConfirm")
    page.wait_for_timeout(SETTLE_MS)
    return survived


def _browser_back_to(page, sid, presses=3):
    """The browser Back button's code path (CDP history navigation)."""
    cdp = page.context.new_cdp_session(page)
    for _ in range(presses):
        history = cdp.send("Page.getNavigationHistory")
        index = history["currentIndex"] - 1
        assert index >= 0, history
        cdp.send("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]})
        page.wait_for_timeout(SETTLE_MS)
        if page.evaluate(_STATE_JS)["sid"] == sid:
            return
    raise AssertionError(f"browser Back never reached {sid}")


def _assert_only_a_shared(server):
    base = server["base"]
    assert _share_token(base, server["B"]) is None
    assert _share_token(base, server["C"]) is None
    assert _share_token(base, server["A"]) == server["a_token"]


def _a_url(page, server):
    return page.evaluate("(token) => new URL('/share/' + token, location.origin).href", server["a_token"])


# ── a conversation change while the dialog is open ───────────────────────────

@pytest.mark.parametrize("answer", ["#appDialogCancel", "#appDialogConfirm", "Escape"])
def test_conversation_change_while_the_dialog_is_open_writes_nothing(page, shares, answer):
    _open_a_after_b(page, shares)
    _open_panel_share_dialog(page)
    _browser_back_to(page, shares["B"])
    survived = page.evaluate(_STATE_JS)["dialogOpen"]
    if survived:  # answer the dialog that is still on screen over B
        if answer == "Escape":
            page.keyboard.press("Escape")
        else:
            page.click(answer)
    page.wait_for_timeout(SETTLE_MS)
    state = page.evaluate(_STATE_JS)
    assert state["sid"] == shares["B"]
    assert state["dialogOpen"] is False
    # Fail closed: the dialog belonged to A, and A is no longer on screen, so
    # nothing is written, copied or opened for either conversation.
    assert page.share_posts == []
    assert state["copied"] == [] and state["opened"] == []
    assert page.evaluate("() => S.session.share_token || null") is None
    _assert_only_a_shared(shares)
    assert survived is False, "the conversation change should dismiss the dialog"


def test_session_keys_are_ignored_while_the_dialog_is_open(page, shares):
    _open_a_after_b(page, shares)
    _open_panel_share_dialog(page)
    page.click("#appDialogDesc")  # focus leaves the dialog buttons
    rows = page.evaluate("() => [...document.querySelectorAll('.session-item[data-sid]')].map(r => r.dataset.sid)")
    index = rows.index(shares["A"])
    # Press every key that would move away from A (j = next row, k = previous row).
    keys = (["j"] if index < len(rows) - 1 else []) + (["k"] if index > 0 else [])
    assert keys, rows
    for key in keys:
        page.keyboard.press(key)
        page.wait_for_timeout(SETTLE_MS)
        state = page.evaluate(_STATE_JS)
        assert state["sid"] == shares["A"], f"{key!r} switched the conversation under the dialog"
        assert state["dialogOpen"] is True
    page.click("#appDialogCancel")  # Refresh snapshot, still on A
    _wait_until(page, "() => window.__opened.length > 0")
    assert page.share_posts == [shares["A"]]
    assert page.share_statuses == [200]
    _assert_only_a_shared(shares)


def test_new_chat_shortcut_is_ignored_while_the_dialog_is_open(page, shares):
    _open_a_after_b(page, shares)
    _open_panel_share_dialog(page)
    status, before = _request(shares["base"], "/api/sessions")
    assert status == 200, before
    page.keyboard.press("Control+k")
    page.wait_for_timeout(SETTLE_MS)
    state = page.evaluate(_STATE_JS)
    assert state["sid"] == shares["A"], "Ctrl+K opened a new chat under the dialog"
    assert state["dialogOpen"] is True
    status, after = _request(shares["base"], "/api/sessions")
    assert status == 200, after
    assert len(after["sessions"]) == len(before["sessions"])
    page.click("#appDialogConfirm")  # Copy existing
    page.wait_for_timeout(SETTLE_MS)
    assert page.share_posts == []
    _assert_only_a_shared(shares)


# ── controls: no conversation change ─────────────────────────────────────────

def test_refresh_snapshot_without_a_switch_refreshes_the_open_conversation(page, shares):
    _open_a_after_b(page, shares)
    _open_panel_share_dialog(page)
    page.click("#appDialogCancel")
    _wait_until(page, "() => window.__opened.length > 0")
    state = page.evaluate(_STATE_JS)
    assert page.share_posts == [shares["A"]]
    assert page.share_statuses == [200]
    assert state["sid"] == shares["A"]
    assert state["opened"] == [_a_url(page, shares)]
    assert state["copied"] == [_a_url(page, shares)]
    assert page.evaluate("() => S.session.share_token") == shares["a_token"]
    _assert_only_a_shared(shares)


def test_copy_existing_without_a_switch_copies_the_link_and_writes_nothing(page, shares):
    _open_a_after_b(page, shares)
    _open_panel_share_dialog(page)
    page.click("#appDialogConfirm")
    _wait_until(page, "() => window.__opened.length > 0")
    page.wait_for_timeout(500)
    state = page.evaluate(_STATE_JS)
    assert page.share_posts == []
    assert state["copied"] == [_a_url(page, shares)]
    assert state["opened"] == [_a_url(page, shares)]
    _assert_only_a_shared(shares)


def test_sidebar_menu_share_still_shares_its_own_row(page, shares):
    _open_a_after_b(page, shares)
    target = shares["C"]
    page.hover(f".session-item[data-sid='{target}']")
    page.click(f".session-item[data-sid='{target}'] .session-actions-trigger")
    page.wait_for_selector(".session-action-opt", timeout=WAIT_MS)
    clicked = page.evaluate("""() => {
      const item = [...document.querySelectorAll('.session-action-opt')]
        .find(el => (el.querySelector('.ws-opt-name') || {}).textContent === t('share_session'));
      if (item) item.click();
      return !!item;
    }""")
    assert clicked
    _wait_until(page, "() => window.__opened.length > 0")
    assert page.share_posts == [target]
    assert page.share_statuses == [200]
    assert page.evaluate(_STATE_JS)["sid"] == shares["A"]
    assert page.evaluate("() => S.session.share_token") == shares["a_token"]
    assert _share_token(shares["base"], target)
    assert _share_token(shares["base"], shares["B"]) is None


# ── other dialogs: a conversation change dismisses them ──────────────────────

def test_clear_conversation_confirm_never_clears_another_conversation(page, server):
    first, second = _import(server, "Clear first"), _import(server, "Clear second")
    _open_after(page, first, second)
    _open_panel_dialog(page, "#btnClearConvModal")
    survived = _back_then_confirm_if_still_open(page, first)
    assert page.evaluate(_STATE_JS)["sid"] == first
    outcome = {"writes": page.writes, "first_messages": _message_count(server, first),
               "second_messages": _message_count(server, second)}
    assert outcome == {"writes": [], "first_messages": 2, "second_messages": 2}, outcome
    assert survived is False, "the conversation change should dismiss the dialog"


def test_stop_sharing_confirm_never_revokes_another_conversation(page, server):
    first, second = _import(server, "Stop first"), _import(server, "Stop second")
    tokens = {}
    for sid in (first, second):
        status, reply = _request(server["base"], "/api/share/create", {"session_id": sid})
        assert status == 200, reply
        tokens[sid] = reply["share"]["token"]
    _open_after(page, first, second)
    _open_panel_dialog(page, "#btnStopSharingSession")
    survived = _back_then_confirm_if_still_open(page, first)
    assert page.evaluate(_STATE_JS)["sid"] == first
    outcome = {"writes": page.writes, "first_token": _share_token(server["base"], first),
               "second_token": _share_token(server["base"], second)}
    assert outcome == {"writes": [], "first_token": tokens[first], "second_token": tokens[second]}, outcome
    assert survived is False, "the conversation change should dismiss the dialog"


def test_stop_sharing_without_a_switch_still_revokes_the_open_conversation(page, server):
    sid = _import(server, "Stop control")
    status, reply = _request(server["base"], "/api/share/create", {"session_id": sid})
    assert status == 200, reply
    _open_after(page, server["B"], sid)
    _open_panel_dialog(page, "#btnStopSharingSession")
    page.click("#appDialogConfirm")
    _wait_until(page, "() => !S.session.share_token")
    assert page.writes == [("/api/share/revoke", {"session_id": sid})]
    assert _share_token(server["base"], sid) is None


def test_workspace_file_delete_confirm_never_deletes_in_another_workspace(page, server):
    files = {}
    sids = []
    for name in ("one", "two"):
        workspace = server["state"] / f"files-{name}"
        workspace.mkdir()
        files[name] = workspace / "notes.txt"
        files[name].write_text(f"{name} notes\n", encoding="utf-8")
        sids.append(_import(server, f"Files {name}", workspace))
    first, second = sids
    _open_after(page, first, second)
    # The file tree's Delete action calls this handler for the open conversation.
    page.evaluate("() => { deleteWorkspaceFile('notes.txt', 'notes.txt'); return true; }")
    _wait_until(page, "() => _isAppDialogOpen()")
    survived = _back_then_confirm_if_still_open(page, first)
    assert page.evaluate(_STATE_JS)["sid"] == first
    outcome = {"writes": page.writes, "one": files["one"].exists(), "two": files["two"].exists()}
    assert outcome == {"writes": [], "one": True, "two": True}, outcome
    assert survived is False, "the conversation change should dismiss the dialog"


# ── a dismissed share dialog writes nothing ──────────────────────────────────

def _history_go(page, sid):
    """Browser Back to the latest earlier history entry for ``sid`` (one navigation,
    no waiting for the switch to finish)."""
    cdp = page.context.new_cdp_session(page)
    history = cdp.send("Page.getNavigationHistory")
    earlier = history["entries"][:history["currentIndex"]]
    entry = next((e for e in reversed(earlier) if e["url"].endswith(f"/session/{sid}")), None)
    assert entry, history
    cdp.send("Page.navigateToHistoryEntry", {"entryId": entry["id"]})


def _open_sidebar_share(page, sid):
    page.hover(f".session-item[data-sid='{sid}']")
    page.click(f".session-item[data-sid='{sid}'] .session-actions-trigger")
    page.wait_for_selector(".session-action-opt", timeout=WAIT_MS)
    assert page.evaluate("""() => {
      const item = [...document.querySelectorAll('.session-action-opt')]
        .find(el => (el.querySelector('.ws-opt-name') || {}).textContent === t('share_session'));
      if (item) item.click();
      return !!item;
    }""")


def _slow_network(page, latency_ms=1000):
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions",
             {"offline": False, "latency": latency_ms, "downloadThroughput": -1, "uploadThroughput": -1})


def _share_dialog_dismissed_by_history_writes_nothing(page, shares):
    state = page.evaluate(_STATE_JS)
    assert page.share_posts == [], "a dismissed share dialog published a snapshot"
    assert state["copied"] == [] and state["opened"] == []
    _assert_only_a_shared(shares)
    assert state["dialogOpen"] is False, "the conversation change should dismiss the dialog"


def test_reopened_sidebar_share_dialog_dismissed_by_history_writes_nothing(page, shares):
    _open_a_after_b(page, shares)
    _slow_network(page)
    # Share A from its menu (answering Refresh if it already asks), then reopen the
    # menu Share before the sidebar refresh: the row now knows A is shared and asks.
    _open_sidebar_share(page, shares["A"])
    _wait_until(page, "() => window.__opened.length > 0 || _isAppDialogOpen()")
    if page.evaluate("() => _isAppDialogOpen()"):
        page.click("#appDialogCancel")
    _wait_until(page, "() => window.__opened.length > 0")
    page.share_posts.clear()
    page.evaluate("() => { window.__opened = []; window.__copied = []; return true; }")
    _open_sidebar_share(page, shares["A"])
    _wait_until(page, "() => _isAppDialogOpen()")
    _history_go(page, shares["B"])
    page.wait_for_timeout(2500)
    _share_dialog_dismissed_by_history_writes_nothing(page, shares)


def test_panel_share_dialog_dismissed_by_a_vetoed_history_change_writes_nothing(page, shares):
    _open_a_after_b(page, shares)
    # An extension cancels the switch before it starts: A stays on screen.
    page.evaluate("() => { registerHermesSessionOpenHandler((sid, data, opts) => opts.preload ? {cancel: true} : {}); return true; }")
    _open_panel_share_dialog(page)
    _history_go(page, shares["B"])
    page.wait_for_timeout(SETTLE_MS)
    assert page.evaluate(_STATE_JS)["sid"] == shares["A"]
    _share_dialog_dismissed_by_history_writes_nothing(page, shares)


# ── clipboard permission denied ──────────────────────────────────────────────

@pytest.mark.parametrize("entry", ["panel", "menu"])
def test_share_link_is_copied_when_the_clipboard_permission_is_denied(browser, shares, entry):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, base_url=shares["base"])
    ctx.add_init_script("window.__opened = []; window.open = (u) => { window.__opened.push(String(u)); return null; };")
    try:
        page = ctx.new_page()
        _open_a_after_b(page, shares)
        ctx.grant_permissions(["clipboard-write"])
        page.evaluate("() => navigator.clipboard.writeText('CLIPBOARD-SENTINEL')")
        ctx.clear_permissions()
        cdp = page.context.new_cdp_session(page)
        context_id = cdp.send("Target.getTargetInfo")["targetInfo"]["browserContextId"]
        cdp.send("Browser.setPermission", {
            "permission": {"name": "clipboard-write", "allowWithoutSanitization": False},
            "setting": "denied", "origin": shares["base"], "browserContextId": context_id,
        })
        assert page.evaluate("async () => (await navigator.permissions.query({name: 'clipboard-write'})).state") == "denied"
        if entry == "panel":
            _open_panel_share_dialog(page)
        else:
            _open_sidebar_share(page, shares["A"])
            _wait_until(page, "() => window.__opened.length > 0 || _isAppDialogOpen()")
        if page.evaluate("() => _isAppDialogOpen()"):
            page.click("#appDialogConfirm")  # Copy existing
        _wait_until(page, "() => window.__opened.length > 0")
        page.wait_for_timeout(300)
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        assert page.evaluate("() => navigator.clipboard.readText()") == _a_url(page, shares)
    finally:
        ctx.close()


def test_clipboard_copy_rejects_when_the_fallback_copy_also_fails(browser, shares):
    """Clipboard permission denied and execCommand('copy') returning false: the copy must reject, so
    callers show the link instead of claiming it was copied."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, base_url=shares["base"])
    try:
        page = ctx.new_page()
        _open_a_after_b(page, shares)
        ctx.clear_permissions()
        cdp = page.context.new_cdp_session(page)
        context_id = cdp.send("Target.getTargetInfo")["targetInfo"]["browserContextId"]
        cdp.send("Browser.setPermission", {
            "permission": {"name": "clipboard-write", "allowWithoutSanitization": False},
            "setting": "denied", "origin": shares["base"], "browserContextId": context_id,
        })
        outcome = page.evaluate("""async () => {
            document.execCommand = () => false;
            try { await _copyTextToClipboard('x'); return 'resolved'; } catch (_) { return 'rejected'; }
        }""")
        assert outcome == "rejected"
        assert page.evaluate("() => document.querySelectorAll('textarea[style*=\"-9999px\"]').length") == 0
    finally:
        ctx.close()


# ── a dialog opened while a switch is still in flight ────────────────────────

@pytest.mark.parametrize("action", ["clear", "file-delete"])
def test_dialog_opened_during_a_pending_switch_never_acts_on_the_next_conversation(page, server, action):
    tag = f"{action}-{int(time.time() * 1000)}"
    files = {}
    sids = []
    for name in ("first", "second"):
        workspace = server["state"] / f"pending-{tag}-{name}"
        workspace.mkdir()
        files[name] = workspace / "notes.txt"
        files[name].write_text(f"{name} notes\n", encoding="utf-8")
        sids.append(_import(server, f"Pending {tag} {name}", workspace))
    first, second = sids
    _open_after(page, first, second)
    page.fill("#msg", "an unsent draft")
    page.click(".rail-btn[data-panel='settings']")
    page.wait_for_selector("#btnClearConvModal", state="visible", timeout=WAIT_MS)
    held = []
    page.route("**/api/session/draft", lambda route: held.append(route))
    # Back to the first conversation: the switch waits for the draft save of the second.
    _history_go(page, first)
    deadline = time.time() + 10
    while not held and time.time() < deadline:
        page.wait_for_timeout(50)
    assert held, "the draft save was not held"
    assert page.evaluate(_STATE_JS)["sid"] == second
    if action == "clear":
        page.click("#btnClearConvModal")
    else:
        page.evaluate("() => { deleteWorkspaceFile('notes.txt', 'notes.txt'); return true; }")
    _wait_until(page, "() => _isAppDialogOpen()")
    held.pop().continue_()
    page.unroute("**/api/session/draft")
    _active(page, first)
    page.wait_for_timeout(300)
    if page.evaluate("() => _isAppDialogOpen()"):
        page.click("#appDialogConfirm")
    page.wait_for_timeout(SETTLE_MS)
    outcome = {
        "writes": page.writes,
        "messages": [_message_count(server, first), _message_count(server, second)],
        "files": [files["first"].exists(), files["second"].exists()],
    }
    assert outcome == {"writes": [], "messages": [2, 2], "files": [True, True]}, outcome
