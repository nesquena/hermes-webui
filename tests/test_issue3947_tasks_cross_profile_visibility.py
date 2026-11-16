"""Focused coverage for issue #3947, cross-profile cron visibility in Tasks."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler: _JSONHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found in panels.js"
    open_brace = src.find("{", start)
    assert open_brace >= 0, f"{name} opening brace not found"
    depth = 0
    for idx in range(open_brace, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} closing brace not found")


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(REPO_ROOT),
        )
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}\nscript:\n{script}")
    return json.loads(result.stdout)


def _install_cron_jobs(monkeypatch, jobs_by_home, current_home):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def _list_jobs(include_disabled=True):
        return [dict(job) for job in jobs_by_home[current_home["value"]]]

    cron_jobs.list_jobs = _list_jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)


def test_crons_route_hides_other_profiles_by_default_but_reports_count(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    current_home = {"value": None}
    jobs_by_home = {
        "alpha-home": [{"id": "job-shared", "name": "Alpha", "profile": "worker"}],
        "default-home": [],
        "beta-home": [{"id": "job-shared", "name": "Beta", "profile": None}],
    }
    _install_cron_jobs(monkeypatch, jobs_by_home, current_home)

    class _Ctx:
        def __init__(self, home):
            self.home = str(home)
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = self.home
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [
        {"name": "alpha", "visible": True},
        {"name": "beta", "visible": True},
    ])
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: Path({"alpha": "alpha-home", "beta": "beta-home", "default": "default-home"}[name]),
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    handler = _JSONHandler()
    assert routes.handle_get(handler, SimpleNamespace(path="/api/crons", query="")) is not False
    body = _payload(handler)

    assert handler.status == 200
    assert body["all_profiles"] is False
    assert body["active_profile"] == "alpha"
    assert body["other_profile_count"] == 1
    assert [job["name"] for job in body["jobs"]] == ["Alpha"]
    assert body["jobs"][0]["owner_profile"] == "alpha"
    assert body["jobs"][0]["read_only"] is False
    assert body["jobs"][0]["profile"] == "worker"

    handler = _JSONHandler()
    assert routes.handle_get(handler, SimpleNamespace(path="/api/crons", query="all_profiles=1")) is not False
    body = _payload(handler)

    assert handler.status == 200
    assert body["all_profiles"] is True
    assert body["other_profile_count"] == 0
    assert [job["owner_profile"] for job in body["jobs"]] == ["alpha", "beta"]
    assert body["jobs"][0]["read_only"] is False
    assert body["jobs"][1]["read_only"] is True
    assert body["jobs"][1]["profile"] is None


def test_crons_route_dedupes_root_aliases_by_resolved_home(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    current_home = {"value": None}
    jobs_by_home = {
        "base-home": [{"id": "root-job", "name": "Root job", "profile": None}],
        "beta-home": [{"id": "beta-job", "name": "Beta job", "profile": "beta"}],
    }
    calls = {"base-home": 0, "beta-home": 0}

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def _list_jobs(include_disabled=True):
        calls[current_home["value"]] += 1
        return [dict(job) for job in jobs_by_home[current_home["value"]]]

    cron_jobs.list_jobs = _list_jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    class _Ctx:
        def __init__(self, home):
            self.home = str(home)
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = self.home
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "rootalias")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [
        {"name": "default", "visible": True},
        {"name": "beta", "visible": True},
    ])
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: Path({
            "rootalias": "base-home",
            "default": "base-home",
            "beta": "beta-home",
        }[name]),
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    handler = _JSONHandler()
    assert routes.handle_get(handler, SimpleNamespace(path="/api/crons", query="all_profiles=1")) is not False
    body = _payload(handler)

    assert calls["base-home"] == 1, "root alias + default must not double-read the same home"
    assert calls["beta-home"] == 1
    assert [job["owner_profile"] for job in body["jobs"]] == ["rootalias", "beta"]


def test_crons_route_skips_hidden_default_profile_when_inactive(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    current_home = {"value": None}
    jobs_by_home = {
        "alpha-home": [{"id": "alpha-job", "name": "Alpha", "profile": None}],
        "default-home": [{"id": "root-job", "name": "Root", "profile": None}],
    }
    _install_cron_jobs(monkeypatch, jobs_by_home, current_home)

    class _Ctx:
        def __init__(self, home):
            self.home = str(home)
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = self.home
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [
        {"name": "default", "visible": False},
        {"name": "alpha", "visible": True},
    ])
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: Path({"alpha": "alpha-home", "default": "default-home"}[name]),
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    handler = _JSONHandler()
    assert routes.handle_get(handler, SimpleNamespace(path="/api/crons", query="all_profiles=1")) is not False
    body = _payload(handler)

    assert handler.status == 200
    assert body["all_profiles"] is True
    assert body["other_profile_count"] == 0
    assert [job["owner_profile"] for job in body["jobs"]] == ["alpha"]


def test_crons_route_ignores_all_profiles_toggle_in_isolated_mode(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    current_home = {"value": None}
    jobs_by_home = {
        "alpha-home": [{"id": "alpha-job", "name": "Alpha", "profile": None}],
    }
    _install_cron_jobs(monkeypatch, jobs_by_home, current_home)
    lookups = []

    class _Ctx:
        def __init__(self, home):
            self.home = str(home)
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = self.home
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "alpha", "visible": True}])
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: lookups.append(name) or Path("alpha-home"),
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    handler = _JSONHandler()
    assert routes.handle_get(handler, SimpleNamespace(path="/api/crons", query="all_profiles=1")) is not False
    body = _payload(handler)

    assert handler.status == 200
    assert body["all_profiles"] is False
    assert body["other_profile_count"] == 0
    assert [job["owner_profile"] for job in body["jobs"]] == ["alpha"]
    assert lookups == ["alpha"]


def test_cron_jobs_cross_profile_skips_foreign_failures_but_reraises_active_failure(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    current_home = {"value": None}
    jobs_by_home = {
        "alpha-home": [{"id": "alpha-job", "name": "Alpha", "profile": None}],
        "beta-home": [{"id": "beta-job", "name": "Beta", "profile": None}],
        "gamma-home": [{"id": "gamma-job", "name": "Gamma", "profile": None}],
    }
    failing_homes = {"beta-home"}

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def _list_jobs(include_disabled=True):
        home = current_home["value"]
        if home in failing_homes:
            raise RuntimeError(f"boom-{home}")
        return [dict(job) for job in jobs_by_home[home]]

    cron_jobs.list_jobs = _list_jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    class _Ctx:
        def __init__(self, home):
            self.home = str(home)
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = self.home
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [
        {"name": "alpha", "visible": True},
        {"name": "beta", "visible": True},
        {"name": "gamma", "visible": True},
    ])
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: Path({
            "alpha": "alpha-home",
            "beta": "beta-home",
            "gamma": "gamma-home",
        }[name]),
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    active_jobs, other_jobs = routes._cron_jobs_cross_profile("alpha")

    assert [job["owner_profile"] for job in active_jobs] == ["alpha"]
    assert [job["owner_profile"] for job in other_jobs] == ["gamma"]
    assert active_jobs[0]["read_only"] is False
    assert other_jobs[0]["read_only"] is True

    failing_homes.clear()
    failing_homes.add("alpha-home")

    with pytest.raises(RuntimeError, match="boom-alpha-home"):
        routes._cron_jobs_cross_profile("alpha")


def test_panels_toggle_button_flips_state_and_refetches():
    append_toggle = _extract_function(PANELS_JS, "_appendCronProfileToggle")
    script = f"""
const results = {{}};
let _showAllCronProfiles = false;
let _cronOtherProfileCount = 3;
let loadCalls = 0;
async function loadCrons() {{ loadCalls += 1; }}
const document = {{
  createElement(tag) {{
    return {{
      tag,
      type: '',
      className: '',
      textContent: '',
      onclick: null,
      style: {{}},
      children: [],
      appendChild(child) {{ this.children.push(child); }},
    }};
  }},
}};
const parent = {{
  children: [],
  appendChild(child) {{ this.children.push(child); }},
}};
{append_toggle}
(async () => {{
  _appendCronProfileToggle(parent);
  const wrap = parent.children[0];
  const btn = wrap.children[0];
  results.initialText = btn.textContent;
  await btn.onclick();
  results.toggled = _showAllCronProfiles;
  results.loadCalls = loadCalls;
  _cronOtherProfileCount = 0;
  const parent2 = {{ children: [], appendChild(child) {{ this.children.push(child); }} }};
  _appendCronProfileToggle(parent2);
  results.showActiveOnlyText = parent2.children[0].children[0].textContent;
  results.sourceUsesQuery = {json.dumps("?all_profiles=1" in PANELS_JS)};
  process.stdout.write(JSON.stringify(results));
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
    result = _run_node(script)

    assert result["initialText"] == "Show 3 from other profiles"
    assert result["toggled"] is True
    assert result["loadCalls"] == 1
    assert result["showActiveOnlyText"] == "Show active profile only"
    assert result["sourceUsesQuery"] is True


def test_profile_switch_resets_cross_profile_tasks_toggle():
    profile_switch_panel_load = _extract_function(PANELS_JS, "_profileSwitchPanelLoad").replace(
        "function _profileSwitchPanelLoad",
        "async function _profileSwitchPanelLoad",
        1,
    )
    script = f"""
const results = {{}};
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 9;
let _currentPanel = 'chat';
let skillLoads = 0;
let memoryLoads = 0;
let taskLoads = 0;
let kanbanLoads = 0;
let profileLoads = 0;
let workspaceLoads = 0;
let clearCronDetailCalls = 0;
let _editingCronId = 'old-job';
let _cronPreFormDetail = {{ id: 'old-job' }};
let _cronIsDuplicate = true;
async function loadSkills() {{ skillLoads += 1; }}
async function loadMemory() {{ memoryLoads += 1; }}
async function loadCrons() {{ taskLoads += 1; }}
async function loadKanban() {{ kanbanLoads += 1; }}
async function loadProfilesPanel() {{ profileLoads += 1; }}
async function loadWorkspacesPanel() {{ workspaceLoads += 1; }}
function _clearCronDetail() {{ clearCronDetailCalls += 1; }}
{profile_switch_panel_load}
(async () => {{
  await _profileSwitchPanelLoad();
  results.chatPanelToggle = _showAllCronProfiles;
  results.chatPanelCount = _cronOtherProfileCount;
  results.chatPanelTaskLoads = taskLoads;
  results.chatPanelClears = clearCronDetailCalls;
  results.chatPanelEditing = _editingCronId;
  results.chatPanelPreForm = _cronPreFormDetail;
  results.chatPanelDuplicate = _cronIsDuplicate;
  _showAllCronProfiles = true;
  _cronOtherProfileCount = 4;
  _currentPanel = 'tasks';
  _editingCronId = 'second-job';
  _cronPreFormDetail = {{ id: 'second-job' }};
  _cronIsDuplicate = true;
  await _profileSwitchPanelLoad();
  results.tasksPanelToggle = _showAllCronProfiles;
  results.tasksPanelCount = _cronOtherProfileCount;
  results.tasksPanelTaskLoads = taskLoads;
  results.tasksPanelClears = clearCronDetailCalls;
  results.tasksPanelEditing = _editingCronId;
  results.tasksPanelPreForm = _cronPreFormDetail;
  results.tasksPanelDuplicate = _cronIsDuplicate;
  process.stdout.write(JSON.stringify(results));
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
    result = _run_node(script)

    assert result["chatPanelToggle"] is False
    assert result["chatPanelCount"] == 0
    assert result["chatPanelTaskLoads"] == 0
    assert result["chatPanelClears"] == 1
    assert result["chatPanelEditing"] is None
    assert result["chatPanelPreForm"] is None
    assert result["chatPanelDuplicate"] is False
    assert result["tasksPanelToggle"] is False
    assert result["tasksPanelCount"] == 0
    assert result["tasksPanelTaskLoads"] == 1
    assert result["tasksPanelClears"] == 2
    assert result["tasksPanelEditing"] is None
    assert result["tasksPanelPreForm"] is None
    assert result["tasksPanelDuplicate"] is False


def test_panels_js_uses_composite_cron_row_identity():
    assert "function _cronJobKey(job)" in PANELS_JS
    assert "_currentCronDetailKey" in PANELS_JS
    assert "function _cronDetailMatches(jobId, detailKey)" in PANELS_JS
    assert "openCronDetail(job, item)" in PANELS_JS


def test_panels_read_only_rows_hide_actions_and_skip_unread_side_effects():
    profile_name = _extract_function(PANELS_JS, "_cronProfileName")
    owner_name = _extract_function(PANELS_JS, "_cronOwnerProfileName")
    job_key = _extract_function(PANELS_JS, "_cronJobKey")
    set_buttons = _extract_function(PANELS_JS, "_setCronHeaderButtons")
    open_detail = _extract_function(PANELS_JS, "openCronDetail")
    script = f"""
const results = {{}};
const buttons = Object.create(null);
const header = {{ style: {{ display: 'initial' }} }};
function $(id) {{
  if (id === 'mainTasks') return {{ querySelector() {{ return header; }} }};
  if (!buttons[id]) buttons[id] = {{ style: {{ display: 'initial' }} }};
  return buttons[id];
}}
let unreadClears = 0;
let watchCalls = 0;
let stopCalls = 0;
let rendered = null;
let _cronPreFormDetail = 'before';
let _editingCronId = 'existing';
function _findCronJob() {{ return {{ id: 'job-1', read_only: true, owner_profile: 'beta' }}; }}
function _cronItemId() {{ return 'cron-beta'; }}
function _clearCronUnreadForJob() {{ unreadClears += 1; }}
function _stopCronWatch() {{ stopCalls += 1; }}
function _renderCronDetail(job) {{ rendered = job; }}
function _checkCronWatchOnDetail() {{ watchCalls += 1; }}
function _closeMobileSidebarAfterPanelSelection() {{}}
const dot = {{ removed: false, remove() {{ this.removed = true; }} }};
const activeEl = {{
  classList: {{ add() {{}}, remove() {{}} }},
  querySelector() {{ return dot; }},
}};
const document = {{
  querySelectorAll() {{ return [activeEl]; }},
}};
{profile_name}
{owner_name}
{job_key}
{set_buttons}
{open_detail}
_setCronHeaderButtons('read', {{ read_only: true }});
openCronDetail('job-1', activeEl);
results.hidden = Object.fromEntries(Object.entries(buttons).map(([key, value]) => [key, value.style.display]));
results.unreadClears = unreadClears;
results.watchCalls = watchCalls;
results.stopCalls = stopCalls;
results.dotRemoved = dot.removed;
results.renderedReadOnly = !!(rendered && rendered.read_only);
process.stdout.write(JSON.stringify(results));
"""
    result = _run_node(script)

    expected_hidden = {
        "btnRunTaskDetail",
        "btnPauseTaskDetail",
        "btnResumeTaskDetail",
        "btnEditTaskDetail",
        "btnDuplicateTaskDetail",
        "btnDeleteTaskDetail",
        "btnCancelTaskDetail",
        "btnSaveTaskDetail",
    }
    assert expected_hidden.issubset(result["hidden"].keys())
    assert all(result["hidden"][button] == "none" for button in expected_hidden)
    assert result["unreadClears"] == 0
    assert result["watchCalls"] == 0
    assert result["stopCalls"] == 1
    assert result["dotRemoved"] is False
    assert result["renderedReadOnly"] is True
    assert "if (!isReadOnly) _loadCronDetailRuns(job.id, _currentCronDetailKey);" in PANELS_JS
    assert "Read-only from another profile" in PANELS_JS


def test_history_race_ignores_stale_active_response_after_foreign_row_switch():
    profile_name = _extract_function(PANELS_JS, "_cronProfileName")
    owner_name = _extract_function(PANELS_JS, "_cronOwnerProfileName")
    job_key = _extract_function(PANELS_JS, "_cronJobKey")
    detail_matches = _extract_function(PANELS_JS, "_cronDetailMatches")
    load_runs = _extract_function(PANELS_JS, "_loadCronDetailRuns").replace(
        "function _loadCronDetailRuns",
        "async function _loadCronDetailRuns",
        1,
    )
    script = f"""
const results = {{}};
let resolveHistory;
let _currentCronDetail = {{ id: 'job-shared', owner_profile: 'alpha', read_only: false }};
let _currentCronDetailKey = 'alpha\\u0000job-shared';
const card = {{ innerHTML: 'read-only placeholder' }};
function $(id) {{ return id === 'cronDetailRuns' ? card : null; }}
function api() {{
  return new Promise((resolve) => {{ resolveHistory = resolve; }});
}}
function _cronOutputTitle() {{ return 'Output'; }}
function _isCronScriptJob() {{ return false; }}
function t(key) {{ return key; }}
function esc(value) {{ return String(value); }}
{profile_name}
{owner_name}
{job_key}
{detail_matches}
{load_runs}
(async () => {{
  const pending = _loadCronDetailRuns('job-shared', _currentCronDetailKey);
  _currentCronDetail = {{ id: 'job-shared', owner_profile: 'beta', read_only: true }};
  _currentCronDetailKey = 'beta\\u0000job-shared';
  resolveHistory({{ runs: [], total: 0 }});
  await pending;
  results.cardHtml = card.innerHTML;
  process.stdout.write(JSON.stringify(results));
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
    result = _run_node(script)

    assert result["cardHtml"] == "read-only placeholder"


def test_status_probe_race_does_not_start_watch_for_foreign_row_with_same_job_id():
    profile_name = _extract_function(PANELS_JS, "_cronProfileName")
    owner_name = _extract_function(PANELS_JS, "_cronOwnerProfileName")
    job_key = _extract_function(PANELS_JS, "_cronJobKey")
    detail_matches = _extract_function(PANELS_JS, "_cronDetailMatches")
    check_watch = _extract_function(PANELS_JS, "_checkCronWatchOnDetail")
    script = f"""
const results = {{}};
let resolveStatus;
let _currentCronDetail = {{ id: 'job-shared', owner_profile: 'alpha', read_only: false }};
let _currentCronDetailKey = 'alpha\\u0000job-shared';
let watchStarts = 0;
function api() {{
  return new Promise((resolve) => {{ resolveStatus = resolve; }});
}}
function _startCronWatch() {{ watchStarts += 1; }}
{profile_name}
{owner_name}
{job_key}
{detail_matches}
{check_watch}
(async () => {{
  _checkCronWatchOnDetail('job-shared', _currentCronDetailKey);
  _currentCronDetail = {{ id: 'job-shared', owner_profile: 'beta', read_only: true }};
  _currentCronDetailKey = 'beta\\u0000job-shared';
  resolveStatus({{ running: true }});
  await Promise.resolve();
  await Promise.resolve();
  results.watchStarts = watchStarts;
  process.stdout.write(JSON.stringify(results));
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
    result = _run_node(script)

    assert result["watchStarts"] == 0
