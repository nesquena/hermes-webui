# Session Sidebar Projects Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the active-profile-scoped Chat sidebar list with a global Projects/Chats session index that supports avatar-based agent identity, collapsible workspace projects, and lazy age-based Archive sections.

**Architecture:** Preserve `/api/sessions` as the historical active-profile endpoint and add a dedicated `/api/session-index` read model for the sidebar. Add a small persisted `workspace_group` intent to distinguish runtime cwd from sidebar grouping, then keep grouping, age-Archive classification, and pagination in a pure Python module that routes and tests can exercise without loading messages. The frontend keeps the existing vanilla JS row renderer, action menus, virtual scroll helpers, avatar helpers, and date-section collapse styling while replacing project chips/date buckets with Projects, Chats, and one Archive subsection per group.

**Tech Stack:** Python standard library HTTP routes, existing Hermes WebUI session model/index, vanilla JavaScript/CSS, pytest/source-level tests, isolated local browser validation.

---

## File Structure

- Create `api/session_sidebar_index.py`: pure sidebar grouping, archive classification, display-name resolution, and cursor pagination helpers.
- Modify `api/models.py`: persist and compact `workspace_group` on sessions; make `new_session()` accept this grouping intent.
- Modify `api/routes.py`: keep `/api/sessions` compatible, add `/api/session-index` and `/api/session-index/archive`, pass `workspace_group` through `/api/session/new`, and make messaging-session dedupe profile-aware for global sidebar rows.
- Modify `api/config.py`: add `session_archive_after_days` default and allowed integer values.
- Modify `static/index.html`: add the Preferences select for Archive cutoff near existing sidebar/session controls.
- Modify `static/boot.js`: hydrate `window._sessionArchiveAfterDays`.
- Modify `static/panels.js`: save, autosave, hydrate, and apply `session_archive_after_days`.
- Modify `static/i18n.js`: add locale keys for the Archive cutoff label, helper copy, and option labels.
- Modify `static/sessions.js`: fetch the new session index, render Projects/Chats/Archive groups, lazy-load archive rows, add row avatars, preserve row actions and virtual scrolling.
- Modify `static/style.css`: add narrow sidebar styles for workspace group headers, row avatars, and archive load/error rows using existing colors and date-header primitives.
- Modify `CHANGELOG.md`: note the user-visible sidebar and setting changes.
- Add `tests/test_session_sidebar_index.py`: pure backend grouping/archive/pagination tests.
- Add `tests/test_session_workspace_group_metadata.py`: session model and new-session grouping metadata tests.
- Add `tests/test_session_archive_after_setting.py`: settings API and UI wiring tests for Archive cutoff.
- Add `tests/test_session_sidebar_index_routes.py`: route-level and source-level checks for the new sidebar endpoint and route boundaries.
- Add `tests/test_session_sidebar_projects_archive_source.py`: source-level frontend contract tests for the new sidebar render path.
- Update `tests/test_issue673.py`: sidebar density detailed mode must not render visible profile names.
- Update `tests/test_issue500_session_list_virtualization.py`: keep virtualization contract aligned with the new `flatSessionRows` entries.
- Review and update if failing: `tests/test_issue1611_session_profile_filtering.py`, `tests/test_issue1614_project_profile_filtering.py`, `tests/test_sprint14.py`, `tests/test_sprint15.py`, `tests/test_metadata_save_wipe_1558.py`, `tests/test_issue2157_sessions_list_stale_stream_state.py`, `tests/test_sidebar_unassigned_filter.py`, `tests/test_issue2551_project_picker_cache_refresh.py`, `tests/test_firefox_sidebar_scroll_stability.py`, `tests/test_issue856_pinned_indicator_layout.py`, `tests/test_workspace_panel_session_list.py`, `tests/test_1045_bfcache_layout_restore.py`, and `tests/test_session_search_bfcache_822.py`.

## Design Decisions Locked By This Plan

- `/api/sessions` remains profile-scoped by default; the new sidebar uses `/api/session-index`.
- The new persisted `workspace_group` values are `workspace` and `chats`.
- Existing sessions without `workspace_group` infer `workspace` when they have a workspace path, preserving historical discoverability.
- New general chats still keep a runtime workspace fallback for agent execution, but store `workspace_group: "chats"` so they appear under Chats.
- Age Archive is virtual and never writes `session.archived`.
- Manual archived rows stay out of the normal current/age-Archive read model.
- Pinned, unread, streaming, pending, and currently open sessions remain in the current list even when older than the cutoff.
- Detailed sidebar rows may show metadata such as model and message count, but not visible profile-name badges.

### Task 1: Backend Pure Sidebar Index

**Files:**
- Create: `api/session_sidebar_index.py`
- Test: `tests/test_session_sidebar_index.py`

- [ ] **Step 1: Write the failing pure helper tests**

Create `tests/test_session_sidebar_index.py`:

```python
from __future__ import annotations

from api.session_sidebar_index import (
    build_archive_page,
    build_session_sidebar_index,
    normalize_archive_after_days,
    normalize_workspace_group,
    session_activity_ts,
)


NOW = 1_779_560_000.0
DAY = 86_400


def row(
    sid,
    *,
    title=None,
    profile="default",
    workspace=None,
    workspace_group=None,
    last_message_at=None,
    updated_at=None,
    created_at=None,
    pinned=False,
    archived=False,
    unread=False,
    streaming=False,
):
    return {
        "session_id": sid,
        "title": title or sid,
        "profile": profile,
        "workspace": workspace,
        "workspace_group": workspace_group,
        "last_message_at": last_message_at,
        "updated_at": updated_at,
        "created_at": created_at or (NOW - DAY),
        "message_count": 2,
        "pinned": pinned,
        "archived": archived,
        "unread": unread,
        "is_streaming": streaming,
    }


def test_normalize_archive_after_days_accepts_only_supported_values():
    assert normalize_archive_after_days(None) == 7
    assert normalize_archive_after_days("14") == 14
    assert normalize_archive_after_days(30) == 30
    assert normalize_archive_after_days("bad") == 7
    assert normalize_archive_after_days(8) == 7


def test_normalize_workspace_group_keeps_general_chats_distinct_from_runtime_workspace():
    assert normalize_workspace_group("chats", workspace="/tmp/runtime") == "chats"
    assert normalize_workspace_group("workspace", workspace="/tmp/runtime") == "workspace"
    assert normalize_workspace_group(None, workspace="/tmp/runtime") == "workspace"
    assert normalize_workspace_group(None, workspace=None) == "chats"


def test_activity_time_prefers_last_message_then_updated_then_created():
    assert session_activity_ts(row("a", last_message_at=10, updated_at=20, created_at=30)) == 10
    assert session_activity_ts(row("b", updated_at=20, created_at=30)) == 20
    assert session_activity_ts(row("c", created_at=30)) == 30


def test_index_groups_workspace_projects_across_profiles_and_general_chats():
    payload = build_session_sidebar_index(
        [
            row("alpha", profile="default", workspace="/repo/hermes", last_message_at=NOW - DAY),
            row("beta", profile="research", workspace="/repo/hermes", last_message_at=NOW - 2 * DAY),
            row("gamma", profile="default", workspace="/repo/runtime", workspace_group="chats", last_message_at=NOW - DAY),
        ],
        settings={"session_archive_after_days": 7},
        now=NOW,
        workspace_names={"/repo/hermes": "Hermes WebUI"},
    )

    assert [g["group_id"] for g in payload["groups"]] == ["workspace:/repo/hermes", "chats"]
    project = payload["groups"][0]
    assert project["kind"] == "project"
    assert project["name"] == "Hermes WebUI"
    assert [s["session_id"] for s in project["sessions"]] == ["alpha", "beta"]
    assert {s["profile"] for s in project["sessions"]} == {"default", "research"}
    chats = payload["groups"][1]
    assert chats["kind"] == "chats"
    assert [s["session_id"] for s in chats["sessions"]] == ["gamma"]


def test_age_archive_counts_without_sending_old_rows():
    payload = build_session_sidebar_index(
        [
            row("new", workspace="/repo/hermes", last_message_at=NOW - DAY),
            row("old", workspace="/repo/hermes", last_message_at=NOW - 8 * DAY),
        ],
        settings={"session_archive_after_days": 7},
        now=NOW,
    )

    group = payload["groups"][0]
    assert group["current_count"] == 1
    assert group["archive_count"] == 1
    assert [s["session_id"] for s in group["sessions"]] == ["new"]


def test_current_exceptions_stay_visible_even_when_old():
    payload = build_session_sidebar_index(
        [
            row("pinned", workspace="/repo/hermes", last_message_at=NOW - 30 * DAY, pinned=True),
            row("unread", workspace="/repo/hermes", last_message_at=NOW - 30 * DAY, unread=True),
            row("streaming", workspace="/repo/hermes", last_message_at=NOW - 30 * DAY, streaming=True),
            row("open", workspace="/repo/hermes", last_message_at=NOW - 30 * DAY),
        ],
        settings={"session_archive_after_days": 7},
        now=NOW,
        current_session_id="open",
    )

    group = payload["groups"][0]
    assert group["archive_count"] == 0
    assert [s["session_id"] for s in group["sessions"]] == ["pinned", "unread", "streaming", "open"]


def test_manual_archived_rows_are_not_age_archive_rows():
    payload = build_session_sidebar_index(
        [
            row("manual", workspace="/repo/hermes", last_message_at=NOW - 30 * DAY, archived=True),
            row("old", workspace="/repo/hermes", last_message_at=NOW - 30 * DAY),
        ],
        settings={"session_archive_after_days": 7},
        now=NOW,
    )

    group = payload["groups"][0]
    assert group["archive_count"] == 1
    assert group["manual_archived_count"] == 1


def test_archive_page_is_group_scoped_and_cursor_stable():
    rows = [
        row("a", workspace="/repo/hermes", last_message_at=NOW - 8 * DAY),
        row("b", workspace="/repo/hermes", last_message_at=NOW - 9 * DAY),
        row("c", workspace="/repo/hermes", last_message_at=NOW - 10 * DAY),
        row("other", workspace="/repo/other", last_message_at=NOW - 8 * DAY),
    ]

    first = build_archive_page(
        rows,
        group_id="workspace:/repo/hermes",
        settings={"session_archive_after_days": 7},
        now=NOW,
        limit=2,
    )
    assert [s["session_id"] for s in first["sessions"]] == ["a", "b"]
    assert first["next_cursor"]
    assert first["remaining_count"] == 1

    second = build_archive_page(
        rows,
        group_id="workspace:/repo/hermes",
        settings={"session_archive_after_days": 7},
        now=NOW,
        limit=2,
        cursor=first["next_cursor"],
    )
    assert [s["session_id"] for s in second["sessions"]] == ["c"]
    assert second["next_cursor"] is None
```

- [ ] **Step 2: Run the tests and confirm the intended failure**

Run:

```bash
python -m pytest tests/test_session_sidebar_index.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'api.session_sidebar_index'
```

- [ ] **Step 3: Implement the pure helper module**

Create `api/session_sidebar_index.py`:

```python
from __future__ import annotations

import time
from pathlib import Path
from typing import Any


ARCHIVE_AFTER_DAY_CHOICES = (7, 14, 30, 90)
SECONDS_PER_DAY = 86_400
WORKSPACE_GROUP_WORKSPACE = "workspace"
WORKSPACE_GROUP_CHATS = "chats"


def normalize_archive_after_days(value: Any, default: int = 7) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return parsed if parsed in ARCHIVE_AFTER_DAY_CHOICES else default


def normalize_workspace_group(value: Any, *, workspace: Any = None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {WORKSPACE_GROUP_CHATS, "chat", "general", "none"}:
        return WORKSPACE_GROUP_CHATS
    if raw in {WORKSPACE_GROUP_WORKSPACE, "project"}:
        return WORKSPACE_GROUP_WORKSPACE
    return WORKSPACE_GROUP_WORKSPACE if _normalize_workspace_path(workspace) else WORKSPACE_GROUP_CHATS


def session_activity_ts(session: dict[str, Any]) -> float:
    for key in ("last_message_at", "updated_at", "created_at"):
        value = session.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def build_session_sidebar_index(
    sessions: list[dict[str, Any]],
    *,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
    current_session_id: str | None = None,
    workspace_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    cutoff_days = normalize_archive_after_days((settings or {}).get("session_archive_after_days"))
    workspace_names = workspace_names or {}
    groups: dict[str, dict[str, Any]] = {}

    for session in sessions:
        group = _group_for_session(session, workspace_names)
        entry = groups.setdefault(group["group_id"], group)
        entry["latest_activity_at"] = max(entry.get("latest_activity_at") or 0.0, session_activity_ts(session))

        if session.get("archived"):
            entry["manual_archived_count"] += 1
            continue

        if _is_current_session_row(session, now=now, cutoff_days=cutoff_days, current_session_id=current_session_id):
            entry["sessions"].append(_compact_sidebar_session(session, now=now, cutoff_days=cutoff_days, group_id=entry["group_id"]))
        else:
            entry["archive_count"] += 1

    for group in groups.values():
        group["sessions"].sort(key=lambda item: (item["activity_ts"], item["session_id"]), reverse=True)
        group["current_count"] = len(group["sessions"])

    projects = [group for group in groups.values() if group["kind"] == "project"]
    projects.sort(key=lambda item: (item.get("latest_activity_at") or 0.0, item["name"].lower()), reverse=True)
    chats = [group for group in groups.values() if group["kind"] == "chats"]
    ordered = projects + chats

    return {
        "groups": ordered,
        "server_time": now,
        "server_tz": time.strftime("%z"),
        "session_archive_after_days": cutoff_days,
    }


def build_archive_page(
    sessions: list[dict[str, Any]],
    *,
    group_id: str,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
    limit: int = 50,
    cursor: str | None = None,
    workspace_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    cutoff_days = normalize_archive_after_days((settings or {}).get("session_archive_after_days"))
    workspace_names = workspace_names or {}
    limit = max(1, min(int(limit or 50), 200))
    cursor_tuple = _decode_cursor(cursor)

    archive_rows = []
    for session in sessions:
        if session.get("archived"):
            continue
        group = _group_for_session(session, workspace_names)
        if group["group_id"] != group_id:
            continue
        if _is_current_session_row(session, now=now, cutoff_days=cutoff_days, current_session_id=None):
            continue
        item = _compact_sidebar_session(session, now=now, cutoff_days=cutoff_days, group_id=group_id)
        archive_rows.append(item)

    archive_rows.sort(key=lambda item: (item["activity_ts"], item["session_id"]), reverse=True)
    if cursor_tuple is not None:
        archive_rows = [
            item
            for item in archive_rows
            if (item["activity_ts"], item["session_id"]) < cursor_tuple
        ]

    page = archive_rows[:limit]
    remaining = max(0, len(archive_rows) - len(page))
    next_cursor = _encode_cursor(page[-1]) if remaining and page else None
    return {
        "group_id": group_id,
        "sessions": page,
        "next_cursor": next_cursor,
        "remaining_count": remaining,
        "server_time": now,
        "server_tz": time.strftime("%z"),
        "session_archive_after_days": cutoff_days,
    }


def _group_for_session(session: dict[str, Any], workspace_names: dict[str, str]) -> dict[str, Any]:
    workspace = _normalize_workspace_path(session.get("workspace"))
    group_kind = normalize_workspace_group(session.get("workspace_group"), workspace=workspace)
    if group_kind == WORKSPACE_GROUP_WORKSPACE and workspace:
        return {
            "group_id": f"workspace:{workspace}",
            "kind": "project",
            "name": workspace_names.get(workspace) or _workspace_display_name(workspace),
            "workspace": workspace,
            "current_count": 0,
            "archive_count": 0,
            "manual_archived_count": 0,
            "sessions": [],
        }
    return {
        "group_id": "chats",
        "kind": "chats",
        "name": "Chats",
        "workspace": None,
        "current_count": 0,
        "archive_count": 0,
        "manual_archived_count": 0,
        "sessions": [],
    }


def _normalize_workspace_path(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return raw


def _workspace_display_name(path: str) -> str:
    try:
        name = Path(path).name
    except (OSError, RuntimeError, ValueError):
        name = ""
    return name or path


def _is_current_session_row(
    session: dict[str, Any],
    *,
    now: float,
    cutoff_days: int,
    current_session_id: str | None,
) -> bool:
    sid = str(session.get("session_id") or "")
    if current_session_id and sid == str(current_session_id):
        return True
    if session.get("pinned") or session.get("unread"):
        return True
    if session.get("is_streaming") or session.get("active_stream_id") or session.get("pending_user_message"):
        return True
    cutoff_seconds = cutoff_days * SECONDS_PER_DAY
    return (now - session_activity_ts(session)) < cutoff_seconds


def _compact_sidebar_session(
    session: dict[str, Any],
    *,
    now: float,
    cutoff_days: int,
    group_id: str,
) -> dict[str, Any]:
    keep_keys = (
        "session_id",
        "title",
        "profile",
        "workspace",
        "workspace_group",
        "model",
        "model_provider",
        "message_count",
        "created_at",
        "updated_at",
        "last_message_at",
        "pinned",
        "archived",
        "project_id",
        "is_cli_session",
        "source_tag",
        "raw_source",
        "session_source",
        "source_label",
        "read_only",
        "parent_session_id",
        "worktree_path",
        "worktree_branch",
        "worktree_repo_root",
        "active_stream_id",
        "pending_user_message",
        "unread",
    )
    activity = session_activity_ts(session)
    item = {key: session.get(key) for key in keep_keys if key in session}
    item["activity_ts"] = activity
    item["group_id"] = group_id
    item["age_archived"] = (now - activity) >= cutoff_days * SECONDS_PER_DAY
    return item


def _encode_cursor(item: dict[str, Any]) -> str:
    return f"{float(item['activity_ts']):.6f}:{item.get('session_id') or ''}"


def _decode_cursor(cursor: str | None) -> tuple[float, str] | None:
    if not cursor:
        return None
    raw = str(cursor)
    ts_text, sep, sid = raw.partition(":")
    if not sep:
        return None
    try:
        return (float(ts_text), sid)
    except ValueError:
        return None
```

- [ ] **Step 4: Run the pure helper tests**

Run:

```bash
python -m pytest tests/test_session_sidebar_index.py -q
```

Expected:

```text
8 passed
```

- [ ] **Step 5: Commit the pure helper**

Run:

```bash
git add api/session_sidebar_index.py tests/test_session_sidebar_index.py
git commit -m "Add session sidebar index helpers"
```

Expected:

```text
[codex/... <sha>] Add session sidebar index helpers
```

### Task 2: Session Grouping Metadata And Settings Contract

**Files:**
- Modify: `api/models.py`
- Modify: `api/config.py`
- Test: `tests/test_session_workspace_group_metadata.py`
- Test: `tests/test_session_archive_after_setting.py`
- Update: `tests/test_configurable_pinned_sessions_limit.py`

- [ ] **Step 1: Write failing tests for `workspace_group` metadata**

Create `tests/test_session_workspace_group_metadata.py`:

```python
from api.models import Session, new_session


def test_session_compact_includes_workspace_group():
    session = Session(workspace="/tmp/hermes-runtime", workspace_group="chats")
    compact = session.compact()

    assert compact["workspace"] == "/tmp/hermes-runtime"
    assert compact["workspace_group"] == "chats"


def test_legacy_session_with_workspace_infers_workspace_group():
    session = Session(workspace="/tmp/hermes-project")
    assert session.compact()["workspace_group"] == "workspace"


def test_new_session_accepts_general_chat_grouping(monkeypatch):
    monkeypatch.setattr("api.models.get_last_workspace", lambda: "/tmp/hermes-runtime")

    session = new_session(workspace=None, workspace_group="chats", profile="default")

    assert session.workspace == "/tmp/hermes-runtime"
    assert session.workspace_group == "chats"
```

- [ ] **Step 2: Write failing tests for Archive cutoff setting**

Create `tests/test_session_archive_after_setting.py`:

```python
import json
import pathlib
import urllib.error
import urllib.request

from tests._pytest_port import BASE


ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def test_archive_cutoff_setting_is_registered_and_wired_through_ui():
    assert '"session_archive_after_days": 7' in CONFIG_PY
    assert '"session_archive_after_days": {7, 14, 30, 90}' in CONFIG_PY
    assert 'id="settingsArchiveAfterDays"' in INDEX_HTML
    assert 'data-i18n="settings_label_archive_after_days"' in INDEX_HTML
    assert 'data-i18n="settings_desc_archive_after_days"' in INDEX_HTML
    assert "payload.session_archive_after_days=parseInt(archiveAfterSel.value,10)" in PANELS_JS
    assert "settings.session_archive_after_days" in PANELS_JS
    assert "window._sessionArchiveAfterDays=parseInt(s.session_archive_after_days||7,10)||7" in BOOT_JS


def test_settings_api_persists_archive_cutoff_and_rejects_invalid_values():
    try:
        data, status = post("/api/settings", {"session_archive_after_days": 14})
        assert status == 200
        assert data["session_archive_after_days"] == 14

        data, status = post("/api/settings", {"session_archive_after_days": "30"})
        assert status == 200
        assert data["session_archive_after_days"] == 30

        data, status = post("/api/settings", {"session_archive_after_days": 8})
        assert status == 200
        assert data["session_archive_after_days"] == 30
    finally:
        post("/api/settings", {"session_archive_after_days": 7})
```

- [ ] **Step 3: Run tests and confirm failures**

Run:

```bash
python -m pytest tests/test_session_workspace_group_metadata.py tests/test_session_archive_after_setting.py -q
```

Expected failures include:

```text
TypeError: Session.__init__() got an unexpected keyword argument 'workspace_group'
AssertionError: assert '"session_archive_after_days": 7' in CONFIG_PY
```

- [ ] **Step 4: Add `workspace_group` to the session model**

Modify `api/models.py`:

```python
from api.session_sidebar_index import normalize_workspace_group
```

Add the argument to `Session.__init__` near `project_id`:

```python
project_id: str=None, profile=None, workspace_group=None,
```

Set the attribute after `self.project_id`:

```python
self.workspace_group = normalize_workspace_group(workspace_group, workspace=self.workspace)
```

Add it to `compact()` after `workspace`:

```python
'workspace_group': self.workspace_group,
```

Update `new_session()` signature:

```python
def new_session(workspace=None, model=None, profile=None, model_provider=None, project_id=None, workspace_group=None, worktree_info=None):
```

Pass it into `Session(...)`:

```python
workspace_group=workspace_group,
```

- [ ] **Step 5: Add Archive cutoff setting validation**

Modify `api/config.py`:

```python
"session_archive_after_days": 7,  # inactive sessions move into virtual sidebar Archive after this many days
```

Add an integer-choice validation map after `_SETTINGS_INT_RANGES`:

```python
_SETTINGS_INT_CHOICES = {
    "session_archive_after_days": {7, 14, 30, 90},
}
```

Add this block in `save_settings()` after the `_SETTINGS_INT_RANGES` block:

```python
if k in _SETTINGS_INT_CHOICES:
    try:
        v = int(v)
    except (TypeError, ValueError):
        continue
    if v not in _SETTINGS_INT_CHOICES[k]:
        continue
```

Normalize loaded values after appearance normalization in `load_settings()`:

```python
try:
    archive_after = int(settings.get("session_archive_after_days", 7))
except (TypeError, ValueError):
    archive_after = 7
settings["session_archive_after_days"] = (
    archive_after if archive_after in {7, 14, 30, 90} else 7
)
```

- [ ] **Step 6: Keep existing pin-limit source test aligned**

Modify `tests/test_configurable_pinned_sessions_limit.py` so its config assertion still checks the pin range and does not confuse the new integer-choice map:

```python
assert '"pinned_sessions_limit": (1, 99)' in CONFIG_PY
assert '"session_archive_after_days": {7, 14, 30, 90}' in CONFIG_PY
```

- [ ] **Step 7: Run metadata and setting tests**

Run:

```bash
python -m pytest tests/test_session_workspace_group_metadata.py tests/test_session_archive_after_setting.py tests/test_configurable_pinned_sessions_limit.py -q
```

Expected:

```text
passed
```

- [ ] **Step 8: Commit metadata and settings**

Run:

```bash
git add api/models.py api/config.py tests/test_session_workspace_group_metadata.py tests/test_session_archive_after_setting.py tests/test_configurable_pinned_sessions_limit.py
git commit -m "Add session grouping and archive cutoff settings"
```

Expected:

```text
[codex/... <sha>] Add session grouping and archive cutoff settings
```

### Task 3: Sidebar Index Routes

**Files:**
- Modify: `api/routes.py`
- Test: `tests/test_gateway_sync.py`
- Test: `tests/test_session_sidebar_index_routes.py`

- [ ] **Step 1: Write source and route tests**

Create `tests/test_session_sidebar_index_routes.py`:

```python
import json
import pathlib
import urllib.request

from tests._pytest_port import BASE


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read()), response.status


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read()), response.status


def test_routes_define_sidebar_index_endpoints_and_preserve_sessions_endpoint():
    assert 'parsed.path == "/api/session-index"' in ROUTES_PY
    assert 'parsed.path == "/api/session-index/archive"' in ROUTES_PY
    assert 'parsed.path == "/api/sessions"' in ROUTES_PY
    assert "build_session_sidebar_index" in ROUTES_PY
    assert "build_archive_page" in ROUTES_PY


def test_messaging_dedupe_can_be_profile_aware_for_global_index():
    assert "profile_aware: bool = False" in ROUTES_PY
    assert 'dedupe_key = f"{profile_key}\\x1f{key}" if profile_aware else key' in ROUTES_PY
    assert "profile_aware=True" in ROUTES_PY


def test_new_session_accepts_workspace_group_for_general_chat():
    data, status = post("/api/session/new", {"workspace_group": "chats", "profile": "default"})
    assert status == 200
    assert data["session"]["workspace_group"] == "chats"
    assert data["session"]["workspace"]


def test_session_index_returns_groups_without_messages_payload():
    created = []
    try:
        for title in ("Sidebar route project", "Sidebar route chat"):
            data, status = post("/api/session/import", {
                "title": title,
                "messages": [{"role": "user", "content": "hello"}],
                "model": "test/sidebar-index",
            })
            assert status == 200
            created.append(data["session"]["session_id"])

        payload, status = get("/api/session-index")
        assert status == 200
        assert isinstance(payload["groups"], list)
        assert "server_time" in payload
        for group in payload["groups"]:
            for session in group["sessions"]:
                assert "messages" not in session
                assert "session_id" in session
                assert "profile" in session
        assert isinstance(payload.get("projects"), list)
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})
```

- [ ] **Step 2: Run tests and confirm route failures**

Run:

```bash
python -m pytest tests/test_session_sidebar_index_routes.py -q
```

Expected failures include:

```text
AssertionError: assert 'parsed.path == "/api/session-index"' in ROUTES_PY
```

- [ ] **Step 3: Make messaging dedupe profile-aware without changing existing callers**

Modify `_keep_latest_messaging_session_per_source()` in `api/routes.py`:

```python
def _keep_latest_messaging_session_per_source(
    sessions: list[dict],
    *,
    show_previous_messaging_sessions: bool = False,
    profile_aware: bool = False,
) -> list[dict]:
```

Inside the loop, immediately after `if not key`:

```python
profile_key = _safe_first(session.get("profile"))
dedupe_key = f"{profile_key}\x1f{key}" if profile_aware else key
```

Replace all uses of `key` for `kept_sources` and `best_by_source` in that loop with `dedupe_key`, while leaving `_should_hide_stale_messaging_session(...)` unchanged.

- [ ] **Step 4: Extract shared sidebar row collection for route reuse**

In `api/routes.py`, extract the existing `/api/sessions` source-loading block into a helper near `_keep_latest_messaging_session_per_source()`:

```python
def _collect_sidebar_session_rows(parsed, settings: dict, diag, *, all_profiles: bool, profile_aware_dedupe: bool = False) -> dict:
    show_cli_sessions = bool(settings.get("show_cli_sessions"))
    webui_sessions = all_sessions(diag=diag)
    cli = []
    deduped_cli = []
    if show_cli_sessions:
        cli = _load_cli_sessions_for_sidebar()
        cli_by_id = {s["session_id"]: s for s in cli}
        for session in webui_sessions:
            meta = cli_by_id.get(session.get("session_id"))
            if not meta:
                continue
            if _is_messaging_session_record(meta):
                session.update(_merge_cli_sidebar_metadata(session, meta))
                if session.get("session_id") != meta.get("session_id"):
                    session["session_id"] = meta.get("session_id")
            else:
                for key in ("source_tag", "raw_source", "session_source", "source_label"):
                    if not session.get(key) and meta.get(key):
                        session[key] = meta[key]
        webui_sessions = [s for s in webui_sessions if is_cli_session_row_visible(s)]
        webui_ids = {s["session_id"] for s in webui_sessions}
        from api.models import _hide_from_default_sidebar as _cron_hide
        deduped_cli = [
            s for s in cli
            if s["session_id"] not in webui_ids
            and is_cli_session_row_visible(s)
            and not _cron_hide(s)
        ]
    else:
        webui_sessions = [s for s in webui_sessions if not _is_cli_session_for_settings(s)]

    merged = webui_sessions + deduped_cli
    merged.sort(
        key=lambda s: s.get("last_message_at") or s.get("updated_at", 0) or 0,
        reverse=True,
    )

    from api.profiles import get_active_profile_name
    active_profile = get_active_profile_name()
    if all_profiles:
        scoped = merged
        other_profile_count = 0
    else:
        scoped = [s for s in merged if _profiles_match(s.get("profile"), active_profile)]
        other_profile_count = len(merged) - len(scoped)

    scoped = _keep_latest_messaging_session_per_source(
        scoped,
        show_previous_messaging_sessions=bool(settings.get("show_previous_messaging_sessions")),
        profile_aware=profile_aware_dedupe,
    )
    if show_cli_sessions:
        scoped = _cap_recent_cli_sessions(scoped, cli_cap=CLI_VISIBLE_SESSION_CAP)

    return {
        "rows": scoped,
        "cli_count": len(deduped_cli),
        "all_profiles": all_profiles,
        "active_profile": active_profile,
        "other_profile_count": other_profile_count,
    }
```

Then update `/api/sessions` to call this helper with:

```python
collected = _collect_sidebar_session_rows(
    parsed,
    settings,
    diag,
    all_profiles=_all_profiles_query_flag(parsed),
    profile_aware_dedupe=False,
)
```

Build its response from `collected["rows"]` and preserve the existing `cli_count`, `all_profiles`, `active_profile`, and `other_profile_count` fields.

- [ ] **Step 5: Add workspace display-name collection**

Add this helper in `api/routes.py`:

```python
def _workspace_name_map_for_sidebar(rows: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        from api.workspace import load_workspaces
        for workspace in load_workspaces():
            if not isinstance(workspace, dict):
                continue
            path = workspace.get("path")
            name = workspace.get("name")
            if path and name:
                try:
                    path_key = str(Path(str(path)).expanduser().resolve())
                except Exception:
                    path_key = str(path)
                names[path_key] = str(name)
    except Exception:
        logger.debug("failed to load workspace names for sidebar index", exc_info=True)
    return names
```

- [ ] **Step 6: Add `/api/session-index`**

Import the helper:

```python
from api.session_sidebar_index import build_archive_page, build_session_sidebar_index, normalize_workspace_group
```

Add this GET route before `/api/sessions` or directly after it:

```python
if parsed.path == "/api/session-index":
    diag = _SessionListDiagnostics("session-index")
    try:
        settings = load_settings()
        current_session_id = parse_qs(parsed.query).get("current_session_id", [""])[0] or None
        collected = _collect_sidebar_session_rows(
            parsed,
            settings,
            diag,
            all_profiles=True,
            profile_aware_dedupe=True,
        )
        payload = build_session_sidebar_index(
            collected["rows"],
            settings=settings,
            current_session_id=current_session_id,
            workspace_names=_workspace_name_map_for_sidebar(collected["rows"]),
        )
        try:
            payload["projects"] = load_projects()
        except Exception:
            logger.debug("failed to load manual projects for session index", exc_info=True)
            payload["projects"] = []
        return j(handler, payload)
    finally:
        diag.finish()
```

- [ ] **Step 7: Add `/api/session-index/archive`**

Add this GET route:

```python
if parsed.path == "/api/session-index/archive":
    diag = _SessionListDiagnostics("session-index-archive")
    try:
        settings = load_settings()
        qs = parse_qs(parsed.query)
        group_id = (qs.get("group_id", [""])[0] or "").strip()
        if not group_id:
            return bad(handler, "group_id is required", status=400)
        limit_raw = qs.get("limit", ["50"])[0]
        cursor = qs.get("cursor", [""])[0] or None
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 50
        collected = _collect_sidebar_session_rows(
            parsed,
            settings,
            diag,
            all_profiles=True,
            profile_aware_dedupe=True,
        )
        payload = build_archive_page(
            collected["rows"],
            group_id=group_id,
            settings=settings,
            limit=limit,
            cursor=cursor,
            workspace_names=_workspace_name_map_for_sidebar(collected["rows"]),
        )
        return j(handler, payload)
    finally:
        diag.finish()
```

- [ ] **Step 8: Pass `workspace_group` through `/api/session/new`**

In `/api/session/new`, before `new_session(...)`:

```python
workspace_group = normalize_workspace_group(body.get("workspace_group"), workspace=workspace)
if worktree_info:
    workspace_group = "workspace"
```

Pass it:

```python
workspace_group=workspace_group,
```

- [ ] **Step 9: Run route-focused tests**

Run:

```bash
python -m pytest \
  tests/test_session_sidebar_index_routes.py \
  tests/test_gateway_sync.py \
  tests/test_issue1611_session_profile_filtering.py \
  tests/test_issue1614_project_profile_filtering.py \
  tests/test_sprint14.py \
  tests/test_sprint15.py \
  tests/test_metadata_save_wipe_1558.py \
  tests/test_issue2157_sessions_list_stale_stream_state.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 10: Commit route changes**

Run:

```bash
git add api/routes.py tests/test_session_sidebar_index_routes.py
git commit -m "Add global session sidebar index routes"
```

Expected:

```text
[codex/... <sha>] Add global session sidebar index routes
```

### Task 4: Preferences UI For Archive Cutoff

**Files:**
- Modify: `static/index.html`
- Modify: `static/boot.js`
- Modify: `static/panels.js`
- Modify: `static/i18n.js`
- Test: `tests/test_session_archive_after_setting.py`

- [ ] **Step 1: Run the UI wiring test and confirm frontend failures remain**

Run:

```bash
python -m pytest tests/test_session_archive_after_setting.py::test_archive_cutoff_setting_is_registered_and_wired_through_ui -q
```

Expected:

```text
AssertionError: assert 'id="settingsArchiveAfterDays"' in INDEX_HTML
```

- [ ] **Step 2: Add the Preferences select**

In `static/index.html`, place this field immediately after `settingsPinnedSessionsLimit`:

```html
<div class="settings-field">
  <label for="settingsArchiveAfterDays" data-i18n="settings_label_archive_after_days">Archive inactive sessions after</label>
  <select id="settingsArchiveAfterDays" style="width:100%;padding:8px;background:var(--code-bg);color:var(--text);border:1px solid var(--border2);border-radius:6px">
    <option value="7" data-i18n="settings_archive_after_days_7">7 days</option>
    <option value="14" data-i18n="settings_archive_after_days_14">14 days</option>
    <option value="30" data-i18n="settings_archive_after_days_30">30 days</option>
    <option value="90" data-i18n="settings_archive_after_days_90">90 days</option>
  </select>
  <div style="font-size:11px;color:var(--muted);margin-top:4px" data-i18n="settings_desc_archive_after_days">Older sessions move into the collapsed Archive section in the sidebar. This does not manually archive or delete conversations.</div>
</div>
```

- [ ] **Step 3: Hydrate the boot global**

In `static/boot.js`, after `_pinnedSessionsLimit`:

```javascript
window._sessionArchiveAfterDays=parseInt(s.session_archive_after_days||7,10)||7;
```

In the boot catch fallback near `_pinnedSessionsLimit`:

```javascript
window._sessionArchiveAfterDays=7;
```

- [ ] **Step 4: Wire Preferences payload, saved state, and hydration**

In `static/panels.js` `_preferencesPayloadFromUi()` after `pinnedLimitField`:

```javascript
const archiveAfterSel=$('settingsArchiveAfterDays');
if(archiveAfterSel) payload.session_archive_after_days=parseInt(archiveAfterSel.value,10);
```

In `_rememberPreferencesSaved(payload)`:

```javascript
if(payload.session_archive_after_days!==undefined) localStorage.setItem('hermes-pref-session_archive_after_days',String(payload.session_archive_after_days));
```

In the settings-panel hydration block where `settingsPinnedSessionsLimit` is hydrated:

```javascript
const archiveAfterSel=$('settingsArchiveAfterDays');
if(archiveAfterSel){
  const archiveAfter=parseInt(settings.session_archive_after_days||7,10)||7;
  archiveAfterSel.value=String([7,14,30,90].includes(archiveAfter)?archiveAfter:7);
}
```

In `saveSettings()`, after `pinnedSessionsLimit`:

```javascript
const archiveAfterDays=parseInt(($('settingsArchiveAfterDays')||{}).value,10)||7;
```

Add to the body:

```javascript
body.session_archive_after_days=archiveAfterDays;
```

After settings save, update the global:

```javascript
window._sessionArchiveAfterDays=[7,14,30,90].includes(archiveAfterDays)?archiveAfterDays:7;
```

When calling `_applySavedSettingsUi`, include `archiveAfterDays` in the context object so the helper can preserve the saved value:

```javascript
{sendKey,showTokenUsage,showQuotaChip,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize,avatarPresenceLayout,archiveAfterDays}
```

- [ ] **Step 5: Add locale keys**

In each locale object in `static/i18n.js`, add English fallback strings if that locale has no translated session-sidebar settings terms:

```javascript
settings_label_archive_after_days: 'Archive inactive sessions after',
settings_desc_archive_after_days: 'Older sessions move into the collapsed Archive section in the sidebar. This does not manually archive or delete conversations.',
settings_archive_after_days_7: '7 days',
settings_archive_after_days_14: '14 days',
settings_archive_after_days_30: '30 days',
settings_archive_after_days_90: '90 days',
```

- [ ] **Step 6: Run settings tests**

Run:

```bash
python -m pytest tests/test_session_archive_after_setting.py tests/test_1003_preferences_autosave.py tests/test_issue673.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit settings UI**

Run:

```bash
git add static/index.html static/boot.js static/panels.js static/i18n.js tests/test_session_archive_after_setting.py
git commit -m "Expose archive cutoff preference"
```

Expected:

```text
[codex/... <sha>] Expose archive cutoff preference
```

### Task 5: Frontend Session Index Rendering

**Files:**
- Modify: `static/sessions.js`
- Test: `tests/test_session_sidebar_projects_archive_source.py`
- Update: `tests/test_issue500_session_list_virtualization.py`
- Update if needed: `tests/test_sidebar_unassigned_filter.py`, `tests/test_issue2551_project_picker_cache_refresh.py`

- [ ] **Step 1: Write source-level tests for the new sidebar contract**

Create `tests/test_session_sidebar_projects_archive_source.py`:

```python
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _render_body():
    start = SESSIONS_JS.index("function renderSessionListFromCache()")
    end = SESSIONS_JS.index("async function _handleActiveSessionStorageEvent", start)
    return SESSIONS_JS[start:end]


def _render_list_body():
    start = SESSIONS_JS.index("async function renderSessionList")
    end = SESSIONS_JS.index("// ── Gateway session SSE", start)
    return SESSIONS_JS[start:end]


def test_render_fetches_sidebar_index_without_active_profile_filter():
    render_list = _render_list_body()
    assert "/api/session-index" in render_list
    assert "/api/projects" not in render_list
    assert "all_profiles" not in render_list


def test_projects_chats_and_archive_state_keys_are_present():
    assert "hermes-sidebar-projects-collapsed" in SESSIONS_JS
    assert "hermes-sidebar-archive-collapsed" in SESSIONS_JS
    assert "function _loadSessionIndexArchive" in SESSIONS_JS
    assert "workspace:" in SESSIONS_JS
    assert "Archive" in SESSIONS_JS


def test_archive_uses_existing_date_header_primitives():
    body = _render_body()
    assert "session-date-group" in body
    assert "session-date-header" in body
    assert "session-date-caret" in body
    assert "Archive" in body


def test_sidebar_no_longer_renders_profile_name_badges():
    assert "metaBits.push(s.profile)" not in SESSIONS_JS
    assert "session-agent-avatar" in SESSIONS_JS
    assert "_profileAvatar" in SESSIONS_JS


def test_virtual_scroll_still_uses_flat_session_rows():
    body = _render_body()
    assert "const flatSessionRows=[]" in body
    assert "flatSessionRows.push({group,session:s})" in body
    assert "_sessionVirtualWindow" in body
    assert "_sessionVirtualSpacer" in body
```

- [ ] **Step 2: Run source-level tests and confirm failures**

Run:

```bash
python -m pytest tests/test_session_sidebar_projects_archive_source.py -q
```

Expected:

```text
AssertionError: assert '/api/session-index' in render_list
```

- [ ] **Step 3: Replace the fetch path and payload state**

In `static/sessions.js`, add globals near `_allSessions`:

```javascript
let _sessionIndexGroups=[];
let _sessionIndexArchiveRows={};
let _sessionIndexArchiveNextCursor={};
let _sessionIndexArchiveLoading={};
let _sessionIndexArchiveErrors={};
```

Replace `_applySessionListPayload(sessData, projData)` with:

```javascript
function _applySessionIndexPayload(indexData){
  _sessionIndexGroups=Array.isArray(indexData&&indexData.groups)?indexData.groups:[];
  window._sessionArchiveAfterDays=parseInt((indexData&&indexData.session_archive_after_days)||window._sessionArchiveAfterDays||7,10)||7;
  if (typeof indexData.server_time === 'number' && indexData.server_time > 0) {
    _serverTimeDelta = Date.now() - (indexData.server_time * 1000);
  }
  if (typeof indexData.server_tz === 'string') {
    _serverTz = indexData.server_tz;
  }
  const flattened=[];
  for(const group of _sessionIndexGroups){
    const rows=Array.isArray(group.sessions)?group.sessions:[];
    for(const session of rows) flattened.push(session);
  }
  for(const rows of Object.values(_sessionIndexArchiveRows||{})){
    if(Array.isArray(rows)) for(const session of rows) flattened.push(session);
  }
  _reconcileActiveSessionIdleStateFromList(flattened);
  _allSessions=_mergeOptimisticFirstTurnSessions(flattened);
  _clearLineageReportCache();
  _allProjects=Array.isArray(indexData&&indexData.projects)?indexData.projects:[];
  _otherProfileCount=0;
  _markPollingCompletionUnreadTransitions(_allSessions);
  const isStreaming=_allSessions.some(s=>Boolean(s&&s.is_streaming));
  if(isStreaming) startStreamingPoll();
  else stopStreamingPoll();
  ensureSessionTimeRefreshPoll();
  ensureActiveSessionExternalRefreshPoll();
  if(!_sessionListFirstRenderAnimated&&Array.isArray(_allSessions)&&_allSessions.length){
    animateNextSessionListRefresh({enterAll:true});
    _sessionListFirstRenderAnimated=true;
  }
  ensureSessionEventsSSE();
  renderSessionListFromCache();
}
```

Change `renderSessionList()` to fetch the new endpoint:

```javascript
const params=new URLSearchParams();
const currentSid=S.session&&S.session.session_id;
if(currentSid) params.set('current_session_id',currentSid);
const suffix=params.toString()?('?'+params.toString()):'';
const indexData=await api('/api/session-index'+suffix);
```

Update `_schedulePendingSessionListApply()` to apply the new payload shape:

```javascript
_applySessionIndexPayload(payload.indexData);
```

When deferring while interacting, store:

```javascript
_pendingSessionListPayload={gen:_gen,indexData};
```

Apply immediately with:

```javascript
_applySessionIndexPayload(indexData);
```

- [ ] **Step 4: Add collapse state helpers**

In `static/sessions.js` near session-list helpers:

```javascript
function _readJsonLocalStorage(key, fallback){
  try{
    const parsed=JSON.parse(localStorage.getItem(key)||'');
    return parsed&&typeof parsed==='object'?parsed:fallback;
  }catch(_){return fallback;}
}

function _writeJsonLocalStorage(key, value){
  try{localStorage.setItem(key,JSON.stringify(value||{}));}catch(_){}
}

function _sidebarProjectCollapsedState(){
  return _readJsonLocalStorage('hermes-sidebar-projects-collapsed',{});
}

function _saveSidebarProjectCollapsedState(value){
  _writeJsonLocalStorage('hermes-sidebar-projects-collapsed',value);
}

function _sidebarArchiveCollapsedState(){
  return _readJsonLocalStorage('hermes-sidebar-archive-collapsed',{});
}

function _saveSidebarArchiveCollapsedState(value){
  _writeJsonLocalStorage('hermes-sidebar-archive-collapsed',value);
}
```

- [ ] **Step 5: Add lazy Archive loader**

Add below `renderSessionList()`:

```javascript
async function _loadSessionIndexArchive(groupId, opts={}){
  if(!groupId||_sessionIndexArchiveLoading[groupId]) return;
  _sessionIndexArchiveLoading[groupId]=true;
  _sessionIndexArchiveErrors[groupId]=null;
  renderSessionListFromCache();
  try{
    const params=new URLSearchParams();
    params.set('group_id',groupId);
    params.set('limit',String(opts.limit||50));
    const cursor=opts.cursor||_sessionIndexArchiveNextCursor[groupId];
    if(cursor) params.set('cursor',cursor);
    const payload=await api('/api/session-index/archive?'+params.toString());
    const rows=Array.isArray(payload&&payload.sessions)?payload.sessions:[];
    const current=Array.isArray(_sessionIndexArchiveRows[groupId])?_sessionIndexArchiveRows[groupId]:[];
    const seen=new Set(current.map(s=>s&&s.session_id).filter(Boolean));
    for(const row of rows){
      if(row&&row.session_id&&!seen.has(row.session_id)){
        current.push(row);
        seen.add(row.session_id);
      }
    }
    _sessionIndexArchiveRows[groupId]=current;
    _sessionIndexArchiveNextCursor[groupId]=(payload&&payload.next_cursor)||null;
  }catch(e){
    console.warn('load session archive',e);
    _sessionIndexArchiveErrors[groupId]=e&&e.message?e.message:String(e||'Archive load failed');
  }finally{
    _sessionIndexArchiveLoading[groupId]=false;
    renderSessionListFromCache();
  }
}
```

- [ ] **Step 6: Replace chip/date rendering with Projects and Chats**

Inside `renderSessionListFromCache()`, keep the existing interaction guards, batch select bar, virtual scrolling helpers, `_renderOneSession()`, and FLIP restore logic. Replace the project chip filter, profile toggle, manual archived toggle, and date-bucket grouping block with this structure:

```javascript
const groups=Array.isArray(_sessionIndexGroups)?_sessionIndexGroups:[];
const projectGroups=groups.filter(g=>g&&g.kind==='project');
const chatsGroup=groups.find(g=>g&&g.kind==='chats')||{group_id:'chats',kind:'chats',name:'Chats',sessions:[],archive_count:0,current_count:0};
const projectCollapsed=_sidebarProjectCollapsedState();
const archiveCollapsed=_sidebarArchiveCollapsedState();
const visibleGroups=[...projectGroups,chatsGroup];
const flatSessionRows=[];
for(const group of visibleGroups){
  if(group.kind==='project'&&projectCollapsed[group.group_id]) continue;
  const currentRows=Array.isArray(group.sessions)?group.sessions:[];
  for(const s of currentRows) flatSessionRows.push({group,session:s});
  const archiveOpen=archiveCollapsed[group.group_id]===false;
  if(archiveOpen){
    const archiveRows=Array.isArray(_sessionIndexArchiveRows[group.group_id])?_sessionIndexArchiveRows[group.group_id]:[];
    for(const s of archiveRows) flatSessionRows.push({group,session:s,archive:true});
  }
}
```

Render top-level labels:

```javascript
function _appendSidebarSectionLabel(text){
  const label=document.createElement('div');
  label.className='session-sidebar-section-label';
  label.textContent=text;
  list.appendChild(label);
}
```

Render project headers:

```javascript
function _appendProjectHeader(group){
  const header=document.createElement('button');
  header.type='button';
  header.className='session-project-group-header';
  header.dataset.groupId=group.group_id;
  header.setAttribute('aria-expanded',projectCollapsed[group.group_id]?'false':'true');
  const caret=document.createElement('span');
  caret.className='session-date-caret';
  if(projectCollapsed[group.group_id]) caret.classList.add('collapsed');
  caret.textContent='▾';
  const folder=document.createElement('span');
  folder.className='session-project-folder';
  folder.textContent='▱';
  const name=document.createElement('span');
  name.className='session-project-group-name';
  name.textContent=group.name||'Project';
  const count=document.createElement('span');
  count.className='session-project-group-count';
  count.textContent=String((group.current_count||0)+(group.archive_count||0));
  const add=document.createElement('span');
  add.className='session-project-new';
  add.textContent='+';
  add.title='New chat in '+(group.name||'project');
  add.onclick=(event)=>{
    event.stopPropagation();
    newSession(true,{workspace:group.workspace,workspace_group:'workspace'});
  };
  header.append(caret,folder,name,count,add);
  header.onclick=()=>{
    projectCollapsed[group.group_id]=!projectCollapsed[group.group_id];
    _saveSidebarProjectCollapsedState(projectCollapsed);
    renderSessionListFromCache();
  };
  list.appendChild(header);
}
```

Render Archive headers with existing date-group classes:

```javascript
function _appendArchiveSection(group, renderRows){
  if(!(group.archive_count>0)) return;
  const wrapper=document.createElement('div');
  wrapper.className='session-date-group session-archive-group';
  const hdr=document.createElement('div');
  hdr.className='session-date-header';
  const caret=document.createElement('span');
  caret.className='session-date-caret';
  caret.textContent='▾';
  const isCollapsed=archiveCollapsed[group.group_id]!==false;
  if(isCollapsed) caret.classList.add('collapsed');
  const label=document.createElement('span');
  label.textContent='Archive';
  const count=document.createElement('span');
  count.className='session-archive-count';
  count.textContent=String(group.archive_count||0);
  hdr.append(caret,label,count);
  const body=document.createElement('div');
  body.className='session-date-body';
  body.style.display=isCollapsed?'none':'';
  hdr.onclick=()=>{
    const opening=archiveCollapsed[group.group_id]!==false;
    archiveCollapsed[group.group_id]=!opening;
    _saveSidebarArchiveCollapsedState(archiveCollapsed);
    if(opening&&!_sessionIndexArchiveRows[group.group_id]){
      void _loadSessionIndexArchive(group.group_id);
    }
    renderSessionListFromCache();
  };
  wrapper.append(hdr,body);
  if(!isCollapsed){
    renderRows(body);
    _appendArchiveLoadState(body,group);
  }
  list.appendChild(wrapper);
}
```

Render row windows using the existing `globalSessionRowIndex` logic, but feed it `flatSessionRows` from Projects/Chats/Archive instead of date buckets.

- [ ] **Step 7: Add archive load/error rows**

Add:

```javascript
function _appendArchiveLoadState(body, group){
  const groupId=group&&group.group_id;
  if(!groupId) return;
  if(_sessionIndexArchiveLoading[groupId]){
    const row=document.createElement('div');
    row.className='session-archive-load-row';
    row.textContent='Loading Archive...';
    body.appendChild(row);
    return;
  }
  if(_sessionIndexArchiveErrors[groupId]){
    const retry=document.createElement('button');
    retry.type='button';
    retry.className='session-archive-load-row error';
    retry.textContent='Retry Archive';
    retry.onclick=()=>_loadSessionIndexArchive(groupId,{cursor:null});
    body.appendChild(retry);
    return;
  }
  if(_sessionIndexArchiveNextCursor[groupId]){
    const more=document.createElement('button');
    more.type='button';
    more.className='session-archive-load-row';
    more.textContent='Load more';
    more.onclick=()=>_loadSessionIndexArchive(groupId);
    body.appendChild(more);
  }
}
```

- [ ] **Step 8: Update new-session default grouping**

In `newSession(flash, options={})`, set the request grouping:

```javascript
const explicitWorkspace=hasOption('workspace');
const explicitWorkspaceGroup=hasOption('workspace_group')?String(options.workspace_group||'').trim().toLowerCase():null;
const workspaceGroup=explicitWorkspaceGroup||(explicitWorkspace?'workspace':'chats');
```

Add to `reqBody`:

```javascript
workspace_group: workspaceGroup==='workspace'?'workspace':'chats',
```

When `options.worktree` is true, force:

```javascript
reqBody.workspace_group='workspace';
```

- [ ] **Step 9: Remove visible profile-name metadata**

In `_renderOneSession`, delete this line:

```javascript
if(_showAllProfiles&&s.profile) metaBits.push(s.profile);
```

Keep profile names in row `title`/`aria-label` only:

```javascript
if(s.profile) el.title=(el.title?el.title+'\n':'')+'Agent: '+s.profile;
```

- [ ] **Step 10: Update virtualization source test**

In `tests/test_issue500_session_list_virtualization.py`, change:

```python
assert "flatSessionRows.push({group:g,session:s})" in render_body
```

to:

```python
assert "flatSessionRows.push({group,session:s})" in render_body
```

- [ ] **Step 11: Run frontend source tests**

Run:

```bash
python -m pytest tests/test_session_sidebar_projects_archive_source.py tests/test_issue500_session_list_virtualization.py tests/test_issue673.py tests/test_sidebar_unassigned_filter.py tests/test_issue2551_project_picker_cache_refresh.py -q
```

Expected:

```text
passed
```

- [ ] **Step 12: Commit session renderer changes**

Run:

```bash
git add static/sessions.js tests/test_session_sidebar_projects_archive_source.py tests/test_issue500_session_list_virtualization.py tests/test_issue673.py tests/test_sidebar_unassigned_filter.py tests/test_issue2551_project_picker_cache_refresh.py
git commit -m "Render Projects and Chats session sidebar"
```

Expected:

```text
[codex/... <sha>] Render Projects and Chats session sidebar
```

### Task 6: Avatar Rows And Sidebar Styling

**Files:**
- Modify: `static/sessions.js`
- Modify: `static/style.css`
- Test: `tests/test_session_sidebar_projects_archive_source.py`
- Test: `tests/test_workspace_panel_session_list.py`
- Test: `tests/test_issue856_pinned_indicator_layout.py`

- [ ] **Step 1: Add row avatar assertions**

Extend `tests/test_session_sidebar_projects_archive_source.py`:

```python
def test_session_rows_use_existing_profile_avatar_helpers():
    assert "session-agent-avatar" in SESSIONS_JS
    assert "_profileAvatarForUi" in SESSIONS_JS or "_profileAvatarMarkup" in SESSIONS_JS
    assert "profile-avatar--session-row" in SESSIONS_JS
```

- [ ] **Step 2: Run avatar source test and confirm failure**

Run:

```bash
python -m pytest tests/test_session_sidebar_projects_archive_source.py::test_session_rows_use_existing_profile_avatar_helpers -q
```

Expected:

```text
AssertionError
```

- [ ] **Step 3: Add row avatar markup through existing helpers**

In `_renderOneSession(s, isPinnedGroup=false)`, before the title wrapper is appended:

```javascript
const avatarWrap=document.createElement('span');
avatarWrap.className='session-agent-avatar';
avatarWrap.setAttribute('aria-hidden','true');
if(typeof _profileForAvatarSurfaceRefresh==='function'&&typeof _profileAvatarForUi==='function'){
  const profile=_profileForAvatarSurfaceRefresh(s.profile||'default');
  avatarWrap.innerHTML=_profileAvatarForUi(profile,'profile-avatar--session-row');
}else if(typeof _conversationProfileAvatarMarkupForState==='function'){
  avatarWrap.innerHTML=_conversationProfileAvatarMarkupForState('idle',{classes:'profile-avatar--session-row',profileName:s.profile||'default'});
}else{
  avatarWrap.textContent=String(s.profile||'?').trim().slice(0,1).toUpperCase()||'?';
}
el.appendChild(avatarWrap);
```

Insert this after the batch-select checkbox block and before:

```javascript
const sessionText=document.createElement('div');
```

Keep profile names in `title`/ARIA:

```javascript
if(s.profile) avatarWrap.title='Agent: '+s.profile;
```

- [ ] **Step 4: Add CSS using existing theme tokens**

Add to `static/style.css` near session row styles:

```css
.session-sidebar-section-label{font-size:11px;color:var(--muted);padding:10px 10px 4px;font-weight:600;}
.session-project-group-header{width:100%;display:flex;align-items:center;gap:7px;padding:6px 10px;border:0;background:transparent;color:var(--text);cursor:pointer;text-align:left;font:inherit;min-height:30px;}
.session-project-group-header:hover{background:var(--hover);}
.session-project-folder{width:16px;flex:0 0 16px;color:var(--muted);font-size:14px;line-height:1;}
.session-project-group-name{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.session-project-group-count,.session-archive-count{margin-left:auto;color:var(--muted);font-size:11px;}
.session-project-new{width:20px;height:20px;border-radius:6px;display:inline-flex;align-items:center;justify-content:center;color:var(--muted);opacity:0;}
.session-project-group-header:hover .session-project-new,.session-project-new:focus{opacity:1;background:var(--hover2);}
.session-agent-avatar{width:24px;height:24px;flex:0 0 24px;display:inline-flex;align-items:center;justify-content:center;margin-right:7px;overflow:hidden;}
.session-agent-avatar .profile-avatar,.profile-avatar--session-row{width:22px;height:22px;font-size:12px;}
.session-archive-group{margin-top:2px;}
.session-archive-load-row{display:block;width:100%;border:0;background:transparent;color:var(--muted);font-size:12px;text-align:left;padding:7px 10px 7px 34px;cursor:pointer;}
.session-archive-load-row:hover{color:var(--text);background:var(--hover);}
.session-archive-load-row.error{color:var(--error);}
```

- [ ] **Step 5: Run styling-sensitive tests**

Run:

```bash
python -m pytest tests/test_session_sidebar_projects_archive_source.py tests/test_workspace_panel_session_list.py tests/test_issue856_pinned_indicator_layout.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit avatar and CSS changes**

Run:

```bash
git add static/sessions.js static/style.css tests/test_session_sidebar_projects_archive_source.py
git commit -m "Add avatar identity to session rows"
```

Expected:

```text
[codex/... <sha>] Add avatar identity to session rows
```

### Task 7: Search, Current Session Visibility, And Archive Access

**Files:**
- Modify: `static/sessions.js`
- Test: `tests/test_session_search_bfcache_822.py`
- Test: `tests/test_1045_bfcache_layout_restore.py`

- [ ] **Step 1: Add source checks for archive search affordance**

Extend `tests/test_session_sidebar_projects_archive_source.py`:

```python
def test_search_can_request_archive_without_preloading_every_old_row():
    assert "Search Archive" in SESSIONS_JS
    assert "_loadSessionIndexArchive" in SESSIONS_JS
    assert "_contentSearchResults" in SESSIONS_JS
```

- [ ] **Step 2: Implement current-loaded search first**

In `renderSessionListFromCache()`, keep:

```javascript
const q=($('sessionSearch').value||'').toLowerCase();
```

Filter current and loaded archive rows before pushing into `flatSessionRows`:

```javascript
const rowMatches=(s)=>!q||_sessionDisplayTitle(s).toLowerCase().includes(q);
for(const s of currentRows){
  if(rowMatches(s)) flatSessionRows.push({group,session:s});
}
```

For loaded archive rows:

```javascript
for(const s of archiveRows){
  if(rowMatches(s)) flatSessionRows.push({group,session:s,archive:true});
}
```

- [ ] **Step 3: Add a scoped Search Archive affordance**

After rendering current groups, if `q` is non-empty and at least one group has archive rows not loaded:

```javascript
function _appendSearchArchiveAffordance(groups, query){
  if(!query) return;
  const candidates=groups.filter(g=>g&&g.archive_count>0&&!_sessionIndexArchiveRows[g.group_id]);
  if(!candidates.length) return;
  const btn=document.createElement('button');
  btn.type='button';
  btn.className='session-archive-load-row';
  btn.textContent='Search Archive';
  btn.onclick=()=>{
    for(const group of candidates){
      const archiveState=_sidebarArchiveCollapsedState();
      archiveState[group.group_id]=false;
      _saveSidebarArchiveCollapsedState(archiveState);
      void _loadSessionIndexArchive(group.group_id);
    }
  };
  list.appendChild(btn);
}
```

Call it after groups render:

```javascript
_appendSearchArchiveAffordance(visibleGroups,q);
```

- [ ] **Step 4: Ensure active archived session stays visible**

When `renderSessionList()` builds query params, keep sending `current_session_id`. Backend Task 1 already treats this as a current exception. Add a source assertion:

```python
def test_sidebar_index_sends_current_session_id():
    assert "current_session_id" in SESSIONS_JS
```

- [ ] **Step 5: Run search and restore tests**

Run:

```bash
python -m pytest tests/test_session_sidebar_projects_archive_source.py tests/test_session_search_bfcache_822.py tests/test_1045_bfcache_layout_restore.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit search/access refinements**

Run:

```bash
git add static/sessions.js tests/test_session_sidebar_projects_archive_source.py
git commit -m "Keep archived sessions searchable on demand"
```

Expected:

```text
[codex/... <sha>] Keep archived sessions searchable on demand
```

### Task 8: Documentation And Release Note

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-05-23-session-sidebar-projects-archive-design.md` if implementation naming differs from the spec

- [ ] **Step 1: Add changelog entry**

In `CHANGELOG.md`, add this bullet under the active unreleased section:

```markdown
- Add a global Chat sidebar index organized into workspace Projects and general Chats, with avatar-based agent identity, lazy virtual Archive sections for inactive sessions, and a configurable Archive cutoff in Preferences.
```

- [ ] **Step 2: Keep the spec naming aligned**

If the implementation uses `workspace_group` as planned, add this sentence under the spec's "State Layers And Invariants" section:

```markdown
The persisted `workspace_group` field records sidebar grouping intent (`workspace` or `chats`) separately from the runtime `workspace` path used to launch agents.
```

- [ ] **Step 3: Run docs/source checks**

Run:

```bash
python -m pytest tests/test_session_sidebar_projects_archive_source.py tests/test_session_archive_after_setting.py -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Commit docs**

Run:

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-05-23-session-sidebar-projects-archive-design.md
git commit -m "Document session sidebar redesign"
```

Expected:

```text
[codex/... <sha>] Document session sidebar redesign
```

### Task 9: Focused Regression And Manual Validation

**Files:**
- No code files unless a verification failure identifies a concrete fix.

- [ ] **Step 1: Run focused automated regression**

Run:

```bash
python -m pytest \
  tests/test_session_sidebar_index.py \
  tests/test_session_workspace_group_metadata.py \
  tests/test_session_archive_after_setting.py \
  tests/test_session_sidebar_index_routes.py \
  tests/test_session_sidebar_projects_archive_source.py \
  tests/test_issue673.py \
  tests/test_configurable_pinned_sessions_limit.py \
  tests/test_gateway_sync.py \
  tests/test_issue500_session_list_virtualization.py \
  tests/test_sidebar_unassigned_filter.py \
  tests/test_issue2551_project_picker_cache_refresh.py \
  tests/test_firefox_sidebar_scroll_stability.py \
  tests/test_issue856_pinned_indicator_layout.py \
  tests/test_workspace_panel_session_list.py \
  tests/test_1045_bfcache_layout_restore.py \
  tests/test_session_search_bfcache_822.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 2: Run a safe isolated local server**

Run from WSL:

```bash
HERMES_HOME=/tmp/hermes-webui-session-sidebar-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-session-sidebar-state \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```

Expected:

```text
Hermes WebUI running
```

- [ ] **Step 3: Validate desktop sidebar manually**

Open:

```text
http://127.0.0.1:8789
```

Check:

- Projects section is visible.
- Workspace project rows collapse and expand.
- Chats section is visible.
- Session rows show avatar, title, and relative age.
- No visible profile-name badges appear in compact or detailed density.
- Archive is the only nested subsection under a group.
- Archive is collapsed by default and first expansion fetches rows for that group only.
- `Load more` appears when archive pagination returns `next_cursor`.
- Opening a session from another profile keeps that session's profile in the conversation state.
- Global `+` creates a `workspace_group: "chats"` session.
- Project header `+` creates a `workspace_group: "workspace"` session for that workspace.

- [ ] **Step 4: Validate narrow/mobile sidebar manually**

Resize the browser to narrow width and check:

- Project names truncate without overlapping counts or plus buttons.
- Session row title and age do not overlap the avatar.
- Archive header caret/count stays readable.
- Row action menus still open and do not shift the list width.

- [ ] **Step 5: Validate settings behavior manually**

In Preferences:

- Change `Archive inactive sessions after` to 14 days.
- Confirm the setting saves without reopening the panel.
- Refresh the page and confirm 14 days remains selected.
- Change it back to 7 days.
- Confirm `session_jump_buttons` and `session_endless_scroll` still affect transcript navigation only.

- [ ] **Step 6: Stop the isolated server**

Stop the process with `Ctrl+C`.

Remove isolated state if desired:

```bash
rm -rf /tmp/hermes-webui-session-sidebar-home /tmp/hermes-webui-session-sidebar-state
```

- [ ] **Step 7: Final status check**

Run:

```bash
git status --short --untracked-files=all
```

Expected:

```text
 M .gitignore
```

The `.gitignore` entry is pre-existing user work and must remain untouched.

## Self-Review

- Spec coverage: The plan covers global cross-profile browsing, avatar row identity, collapsible Projects, Chats, virtual Archive, lazy archive loading, cutoff Preferences, general-chat creation, existing manual project label preservation by avoiding chip deletion semantics in backend storage, and the active-profile/new-session boundary.
- State invariants: The age Archive lives only in `api/session_sidebar_index.py` classification and local frontend state; it never writes `session.archived`. Opening a cross-profile session continues through the existing `loadSession(s.session_id)` path. Full messages remain excluded from `/api/session-index`.
- Settings scope: Only one new setting is introduced. Existing Preferences and Appearance settings retain their current domains.
- Performance: Initial `/api/session-index` returns group summaries and current rows only. Archive rows load by group and page.
- Risk areas to watch during execution: extracting `/api/sessions` source loading without changing gateway/CLI behavior, preserving virtual scroll row counts after replacing date buckets, and ensuring `workspace_group: "chats"` does not clear the runtime workspace required by agents.
