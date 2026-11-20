"""#9077: Memory and Skills panel writes refuse when the panel's data belongs to
another profile than the one the write would land in.

The active profile is a shared cookie, so a late GET reply, another tab or a
session-load switch can leave a panel showing profile A while its write lands
in profile B. GET /api/memory and /api/skills report the ``profile`` they were
served for; the Memory editor save and the skill enable/disable toggle echo it
as ``expected_profile``; the server refuses a mismatch with 409 before touching
anything. Without the field every route behaves exactly as before (native/mobile
clients, scripts). The skill save/delete routes accept the same optional field,
but the skill EDITOR client does not send it yet (separate follow-up).
"""
import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._pytest_port import BASE

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

SKILL_MD = "---\nname: race-skill\ndescription: BETA dummy skill\n---\nDummy only.\n"
CONFIG = "model:\n  default: test/no-provider\nskills:\n  disabled: []\n"


# ── server: direct route handlers, isolated dummy "beta" profile ─────────────

@pytest.fixture
def beta(tmp_path, monkeypatch):
    """Requests resolve to profile 'beta' whose home is a temp dir.

    'default' and the renamed root 'kinni' are the root aliases.
    """
    import api.profiles as profiles
    import api.routes as routes

    home = tmp_path / "beta"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("BETA MEMORY ORIGINAL\n", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("BETA USER ORIGINAL\n", encoding="utf-8")
    (home / "SOUL.md").write_text("BETA SOUL ORIGINAL\n", encoding="utf-8")
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    skill_dir = home / "skills" / "race-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "beta")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in {"default", "kinni"})
    monkeypatch.setattr(routes, "_is_root_profile", lambda name: name in {"default", "kinni"})
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: home / "skills")
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: home / "config.yaml")
    monkeypatch.setattr(
        routes, "_find_skill_in_dirs",
        lambda name, dirs: (skill_dir, skill_dir / "SKILL.md") if name == "race-skill" else (None, None),
    )
    monkeypatch.setattr(routes, "reload_config", lambda: None)
    return home


def _snapshot(home: Path) -> dict:
    return {
        str(p.relative_to(home)): p.read_bytes()
        for p in sorted(home.rglob("*"))
        if p.is_file()
    }


def _call(fn, body):
    handler = MagicMock()
    handler.headers = {}
    fn(handler, body)
    status = handler.send_response.call_args[0][0]
    payload = json.loads(handler.wfile.write.call_args[0][0])
    return status, payload


def _route(name):
    import api.routes as routes

    return getattr(routes, name)


WRITES = [
    ("memory", "_handle_memory_write", {"section": "memory", "content": "ALPHA MEMORY ORIGINAL\n"}),
    ("user", "_handle_memory_write", {"section": "user", "content": "ALPHA USER ORIGINAL\n"}),
    ("soul", "_handle_memory_write", {"section": "soul", "content": "ALPHA SOUL ORIGINAL\n"}),
    ("toggle", "_handle_skill_toggle", {"name": "race-skill", "enabled": False}),
    ("save-edit", "_handle_skill_save", {"name": "race-skill", "content": "ALPHA skill body\n"}),
    ("save-create", "_handle_skill_save", {"name": "alpha-only", "content": "ALPHA new skill\n"}),
    ("delete", "_handle_skill_delete", {"name": "race-skill"}),
]


REFUSALS = {
    "memory": "This editor was opened for profile 'default', but the active profile is now 'beta'. "
              "Nothing was saved. Your text is still here: copy it before you close or reload this page.",
    "toggle": "This list was loaded for profile 'default', but the active profile is now 'beta'. "
              "Nothing was changed; the list now shows 'beta'.",
    "save": "This request was made for profile 'default', but the active profile is now 'beta'. Nothing was saved.",
    "delete": "This request was made for profile 'default', but the active profile is now 'beta'. Nothing was deleted.",
}
SURFACE = {"memory": "memory", "user": "memory", "soul": "memory", "toggle": "toggle",
           "save-edit": "save", "save-create": "save", "delete": "delete"}


@pytest.mark.parametrize("label,route,body", WRITES, ids=[w[0] for w in WRITES])
def test_mismatched_expected_profile_is_refused_and_writes_nothing(beta, label, route, body):
    before = _snapshot(beta)
    status, payload = _call(_route(route), {**body, "expected_profile": "default"})
    assert status == 409, payload
    assert payload == {"error": REFUSALS[SURFACE[label]], "code": "profile_mismatch"}
    assert _snapshot(beta) == before, f"{label}: beta files changed despite the refusal"


def test_refusal_names_a_renamed_root_as_default(beta, monkeypatch):
    """The message shows normalised names: the renamed root 'kinni' reads 'default'."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "kinni")
    status, payload = _call(routes._handle_skill_toggle,
                            {"name": "race-skill", "enabled": False, "expected_profile": "beta"})
    assert status == 409
    assert payload["error"] == ("This list was loaded for profile 'beta', but the active profile is now 'default'. "
                                "Nothing was changed; the list now shows 'default'.")
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "beta")
    status, payload = _call(routes._handle_skill_toggle,
                            {"name": "race-skill", "enabled": False, "expected_profile": "kinni"})
    assert payload["error"].startswith(
        "This list was loaded for profile 'default', but the active profile is now 'beta'.")


@pytest.mark.parametrize("expected", ["beta", None], ids=["matching", "absent"])
@pytest.mark.parametrize("label,route,body", WRITES, ids=[w[0] for w in WRITES])
def test_matching_or_absent_expected_profile_writes_as_before(beta, label, route, body, expected):
    before = _snapshot(beta)
    request = dict(body)
    if expected is not None:
        request["expected_profile"] = expected
    status, payload = _call(_route(route), request)
    assert status == 200 and payload.get("ok") is True, payload
    assert _snapshot(beta) != before, f"{label}: the write did not land"


MALFORMED = ["", "   ", False, 0, [], {}, 123, True, ["beta"]]


@pytest.mark.parametrize("value", MALFORMED, ids=[repr(v) for v in MALFORMED])
@pytest.mark.parametrize("label,route,body", WRITES, ids=[w[0] for w in WRITES])
def test_malformed_expected_profile_is_refused_and_writes_nothing(beta, label, route, body, value):
    """Only an omitted field or JSON null means 'no owner'; anything else must be a
    non-blank string. Blank or non-string values are refused, never read as
    absent (which would skip the check) or coerced with str()."""
    before = _snapshot(beta)
    status, payload = _call(_route(route), {**body, "expected_profile": value})
    assert status == 400, payload
    assert payload["error"] == "expected_profile must be a non-empty string"
    assert _snapshot(beta) == before, f"{label}: beta files changed despite the refusal"


@pytest.mark.parametrize("label,route,body", WRITES, ids=[w[0] for w in WRITES])
def test_json_null_expected_profile_counts_as_absent(beta, label, route, body):
    before = _snapshot(beta)
    status, payload = _call(_route(route), {**body, "expected_profile": None})
    assert status == 200 and payload.get("ok") is True, payload
    assert _snapshot(beta) != before


def test_root_alias_counts_as_the_same_profile(beta, monkeypatch):
    """A renamed root ('kinni') and its 'default' alias are one owner."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    status, payload = _call(routes._handle_memory_write,
                            {"section": "memory", "content": "ROOT\n", "expected_profile": "kinni"})
    assert status == 200, payload
    assert (beta / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "ROOT\n"


def test_memory_read_reports_the_profile_it_was_served_for(beta):
    import api.routes as routes

    handler = MagicMock()
    handler.headers = {}
    routes._handle_memory_read(handler, None)
    payload = json.loads(handler.wfile.write.call_args[0][0])
    assert payload["profile"] == "beta"
    assert payload["memory"] == "BETA MEMORY ORIGINAL\n"


def test_skills_list_reports_the_profile_it_was_served_for(beta, monkeypatch):
    """Agent-free pin of the GET /api/skills owner (the live check below needs the Agent's skill tools)."""
    import api.routes as routes
    from urllib.parse import urlparse

    monkeypatch.setattr(routes, "_skills_list_from_dir", lambda *a, **k: {"skills": [{"name": "race-skill"}]})
    handler = MagicMock()
    handler.headers = {}
    routes.handle_get(handler, urlparse("/api/skills"))
    payload = json.loads(handler.wfile.write.call_args[0][0])
    assert payload == {"skills": [{"name": "race-skill"}], "profile": "beta"}


# ── server: the real dispatcher on the test server ───────────────────────────

def _http(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def test_live_get_replies_name_the_profile_like_the_active_profile_api():
    active, _ = _http("/api/profile/active")
    memory, _ = _http("/api/memory")
    assert memory["profile"] == active["name"]
    skills, status = _http("/api/skills")
    if status != 200:
        # Without the Agent the skills listing itself fails before any reply is
        # built; the Agent-free unit test above pins the owner field instead.
        pytest.skip(f"GET /api/skills is unavailable on this server ({status})")
    assert skills["profile"] == active["name"]


def test_live_memory_write_refuses_a_foreign_owner_and_keeps_the_file():
    """Real dispatcher + real profile resolution (matching/absent: unit tests above)."""
    before, _ = _http("/api/memory")
    payload, status = _http("/api/memory/write", {
        "section": "memory", "content": "FOREIGN BYTES\n", "expected_profile": "issue9077-other"})
    assert status == 409, payload
    assert payload["code"] == "profile_mismatch"
    assert "Nothing was saved. Your text is still here" in payload["error"]
    after, _ = _http("/api/memory")
    assert after["memory"] == before["memory"]
    assert after["memory_mtime"] == before["memory_mtime"]


# ── client: the real panels.js functions under node ──────────────────────────

def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    open_brace = src.find("{", src.find(")", start))
    depth = 0
    for idx in range(open_brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start: idx + 1]
    raise AssertionError(f"{name} closing brace not found")


_HARNESS = r"""
'use strict';
function makeEl(){
  const el = { textContent:'', value:'', style:{ display:'' }, dataset:{},
    children:[], listeners:{}, className:'', _html:'',
    addEventListener(type, fn){ this.listeners[type] = fn; },
    append(...kids){ this.children.push(...kids); }, appendChild(kid){ this.children.push(kid); },
    focus(){}, scrollIntoView(o){ this.scrolled = o; },
    classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } } };
  // Like the DOM: assigning innerHTML replaces the element's children.
  Object.defineProperty(el, 'innerHTML', { get(){ return this._html; }, set(v){ this._html = v; this.children = []; } });
  return el;
}
const document = { createElement(){ return makeEl(); } };
function li(){ return ''; }
const els = { skillsList: makeEl(), skillsSearch: makeEl(), memoryDetailTitle: makeEl(), memoryDetailBody: makeEl(),
  memoryDetailEmpty: makeEl(), memoryPanel: null };
// The toggle buttons renderSkills has mounted in #skillsList right now.
function mountedToggles(){
  const out = [];
  (function walk(el){ for (const kid of (el.children || [])) {
    if (String(kid.className).startsWith('skill-toggle')) out.push(kid); walk(kid); } })(els.skillsList);
  return out;
}
function clickToggle(el){ el.listeners.click({ stopPropagation(){} }); }
// The click handler doesn't return toggleSkill's promise; let its awaits (and any reload) run.
async function settle(){ for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); }
let editor = null;
function $(id){
  if (id === 'memEditContent') {
    // Minimal DOM: build the textarea from the HTML _renderMemoryEdit wrote.
    if (!editor) {
      const m = /<textarea id="memEditContent"([^>]*)>([\s\S]*?)<\/textarea>/.exec(els.memoryDetailBody.innerHTML);
      if (!m) return null;
      editor = makeEl();
      const owner = /data-profile="([^"]*)"/.exec(m[1]);
      if (owner) editor.dataset.profile = owner[1];
      editor.value = m[2];
    }
    return editor;
  }
  if (id === 'memEditError') return els.memEditError || (els.memEditError = makeEl());
  return els[id] || null;
}
const window = {};
const S = { activeProfile:'beta', session:null };
const calls = { api:[], status:[], toasts:[], loadMemory:[] };
let replies = {};
async function api(path, opts){
  calls.api.push({ path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const r = typeof replies[path] === 'function' ? replies[path]() : replies[path];
  if (r instanceof Error) throw r;
  if (r && typeof r.then === 'function') return r;  // a reply still in flight
  return JSON.parse(JSON.stringify(r));
}
function conflict(){
  const e = new Error("This panel was loaded for profile 'default', but the active profile is now 'beta'.");
  e.status = 409; return e;
}
function t(k){ return k === 'error_prefix' ? 'Error: ' : k; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }
function setStatus(m){ calls.status.push(m); }
function showToast(...args){ calls.toasts.push(args); }
function _setMemoryHeaderButtons(){}
function _memorySectionMeta(k){ return { key:k, readOnly:false }; }
function _memorySectionLabel(){ return 'label'; }
function _memorySectionContent(k){ return (_memoryData && _memoryData[k]) || ''; }
function _renderMemoryDetail(){ _memoryMode = 'read'; }
async function loadNotesSources(){}
let _skillsData = null;
let _collapsedCats = new Set();
let _memoryData = null;
let _memoryMode = 'read';
let _currentMemorySection = null;
__FUNCS__
(async () => {
__BODY__
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

_CLIENT_FUNCS = ("loadSkills", "renderSkills", "toggleSkill", "_renderMemoryEdit", "submitMemorySave", "loadMemory")


def _run_client(body: str, *, stub_load_memory: bool = False) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    funcs = [_extract_function(PANELS_JS, n) for n in _CLIENT_FUNCS
             if not (stub_load_memory and n == "loadMemory")]
    if stub_load_memory:
        funcs.append("async function loadMemory(force){ calls.loadMemory.push(!!force); "
                     "_memoryData = { memory:'BETA MEMORY ORIGINAL\\n', profile:'beta' }; }")
    script = _HARNESS.replace("__FUNCS__", "\n".join(funcs)).replace("__BODY__", body)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        result = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=20, cwd=str(REPO_ROOT))
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}\nstdout:\n{result.stdout}")
    return json.loads(result.stdout)


def test_toggle_sends_the_server_reported_owner_and_reloads_on_409():
    out = _run_client(r"""
// The list was served for 'default' although this tab thinks 'beta' is active.
replies['/api/skills'] = { skills:[{ name:'race-skill', disabled:false }], profile:'default' };
await loadSkills();
const staleRow = mountedToggles()[0];
replies['/api/skills/toggle'] = conflict;
replies['/api/skills'] = { skills:[{ name:'race-skill', disabled:false }], profile:'beta' };
clickToggle(staleRow); await settle();
const toggle = calls.api.find(c => c.path === '/api/skills/toggle');
clickToggle(mountedToggles()[0]); await settle();
const second = calls.api.filter(c => c.path === '/api/skills/toggle')[1];
console.log(JSON.stringify({ sent: toggle.body, status: calls.status, toasts: calls.toasts,
  skillsGets: calls.api.filter(c => c.path === '/api/skills').length,
  owner: _skillsData && _skillsData.profile, second: second.body }));
""")
    assert out["sent"] == {"name": "race-skill", "enabled": False, "expected_profile": "default"}
    # The 409 is an error toast (type 'error': Copy/Dismiss, the long error lifetime), never setStatus(),
    # whose 4 s toast guesses the type from English keywords.
    assert out["status"] == []
    assert out["toasts"][0] == ["skill_toggle_failed" + "This panel was loaded for profile 'default', "
                                "but the active profile is now 'beta'.", "error"]
    assert out["skillsGets"] >= 2, "the stale list must be reloaded after the 409"
    assert out["owner"] == "beta"
    assert out["second"]["expected_profile"] == "beta"


def test_rows_still_shown_after_the_cached_list_is_cleared_keep_their_owner():
    """A profile switch clears the cached list before the replacement loads, and
    the reload after a refusal does the same. A click on the rows still on screen
    in that window must carry the owner those rows were rendered for."""
    out = _run_client(r"""
replies['/api/skills'] = { skills:[{ name:'race-skill', disabled:false }], profile:'default' };
await loadSkills();
const row = mountedToggles()[0];
_skillsData = null;  // what switchToProfile does before the new list arrives
replies['/api/skills/toggle'] = conflict;
replies['/api/skills'] = () => new Promise(() => {});  // the replacement list never lands
clickToggle(row); await settle();   // first click: refused, its reload stays pending
clickToggle(row); await settle();   // second click on the same stale row while the reload is pending
console.log(JSON.stringify({ sent: calls.api.filter(c => c.path === '/api/skills/toggle').map(c => c.body) }));
""")
    assert out["sent"] == [{"name": "race-skill", "enabled": False, "expected_profile": "default"}] * 2


def test_absent_profile_in_the_skills_reply_omits_expected_profile():
    """An older server (no ``profile`` field): the write carries no owner, so the
    route keeps today's behaviour instead of refusing on a guessed owner."""
    out = _run_client(r"""
replies['/api/skills'] = { skills:[{ name:'race-skill' }] };
await loadSkills();
replies['/api/skills/toggle'] = { ok:true };
clickToggle(mountedToggles()[0]); await settle();
console.log(JSON.stringify({ sent: calls.api.find(c => c.path === '/api/skills/toggle').body }));
""")
    assert out["sent"] == {"name": "race-skill", "enabled": False}


def test_memory_save_sends_the_editor_owner_and_keeps_typed_text_on_409():
    out = _run_client(r"""
_memoryData = { memory:'ALPHA MEMORY ORIGINAL\n', profile:'default' };
_currentMemorySection = 'memory';
_renderMemoryEdit('memory');
const ta = $('memEditContent');
ta.value = 'ALPHA MEMORY ORIGINAL\nplus my typing\n';
replies['/api/memory/write'] = conflict;
await submitMemorySave();
const first = calls.api.find(c => c.path === '/api/memory/write').body;
const err = $('memEditError');
// A second Save after the refresh still carries the editor's owner, not the refreshed one.
await submitMemorySave();
const second = calls.api.filter(c => c.path === '/api/memory/write')[1].body;
console.log(JSON.stringify({ first, second, mode: _memoryMode, text: $('memEditContent').value,
  error: err.textContent, errorShown: err.style.display !== 'none', refreshed: calls.loadMemory,
  dataOwner: _memoryData.profile, scrolled: err.scrolled || null,
  alertRole: /<div id="memEditError"[^>]*role="alert"/.test(els.memoryDetailBody.innerHTML) }));
""", stub_load_memory=True)
    assert out["first"] == {"section": "memory", "content": "ALPHA MEMORY ORIGINAL\nplus my typing\n",
                            "expected_profile": "default"}
    assert out["mode"] == "edit", "the editor must stay open on a 409"
    assert out["text"] == "ALPHA MEMORY ORIGINAL\nplus my typing\n"
    assert out["errorShown"] and "active profile is now 'beta'" in out["error"]
    assert out["refreshed"] and out["refreshed"][0] is True
    assert out["alertRole"] is True, "the inline error must be announced (role=alert)"
    assert out["scrolled"] == {"block": "nearest"}, "the error must be scrolled into view above a soft keyboard"
    assert out["dataOwner"] == "beta"
    assert out["second"]["expected_profile"] == "default"


def test_memory_owner_flows_from_the_real_loader_to_the_save():
    """Real loadMemory -> _renderMemoryEdit -> submitMemorySave: the owner is the
    profile the server reported, not the tab's S.activeProfile; a reply without
    ``profile`` makes the save omit expected_profile."""
    out = _run_client(r"""
S.activeProfile = 'beta';   // the tab believes beta; the reply was served for 'default'
_currentMemorySection = 'memory';
replies['/api/memory'] = { memory:'ALPHA MEMORY ORIGINAL\n', profile:'default' };
replies['/api/memory/write'] = { ok:true };
await loadMemory();
_renderMemoryEdit('memory');
await submitMemorySave();
editor = null;
replies['/api/memory'] = { memory:'OLD SERVER\n' };
await loadMemory();
_renderMemoryEdit('memory');
await submitMemorySave();
const writes = calls.api.filter(c => c.path === '/api/memory/write').map(c => c.body);
console.log(JSON.stringify({ writes }));
""")
    assert out["writes"] == [
        {"section": "memory", "content": "ALPHA MEMORY ORIGINAL\n", "expected_profile": "default"},
        {"section": "memory", "content": "OLD SERVER\n"},
    ]


def test_show_toast_treats_a_type_in_the_duration_slot_as_the_type():
    """toggleSkill calls showToast(msg, 'error'); ui.js must keep reading that as the type."""
    ui = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    show_toast = _extract_function(ui, "showToast")
    assert "typeof ms==='string'&&/^(error|success|warning|info)$/.test(ms)" in show_toast
    assert "showToast(t('skill_toggle_failed') + e.message, 'error');" in _extract_function(PANELS_JS, "toggleSkill")


# The skill EDITOR (create / edit / delete) is out of scope for this change: its
# owner model (fresh detail owner + category, New while the list is pending,
# draft preservation) is a separate follow-up. Pin that its client still sends
# no expected_profile, so this PR can't leave it half-guarded.
_SKILL_EDITOR_FUNCS = ("openSkill", "openSkillCreate", "editCurrentSkill", "_renderSkillForm",
                       "saveSkillForm", "deleteCurrentSkill")


@pytest.mark.parametrize("name", _SKILL_EDITOR_FUNCS)
def test_skill_editor_client_is_not_half_guarded(name):
    src = _extract_function(PANELS_JS, name)
    assert "expected_profile" not in src
    assert "data-profile" not in src
    assert not src.startswith("async function openSkillCreate")
