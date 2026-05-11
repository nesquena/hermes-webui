"""
Shared pytest fixtures for webui-mvp tests.

TEST ISOLATION:
  Tests run against a SEPARATE server instance on port 8788 with a
  completely separate state directory. Production data is never touched.
  The test state dir is wiped before each full test run and again on teardown.

PATH DISCOVERY:
  No hardcoded paths. Discovery order:
    1. Environment variables (HERMES_WEBUI_AGENT_DIR, HERMES_WEBUI_PYTHON, etc.)
    2. Sibling checkout heuristics relative to this repo
    3. Common install paths (~/.hermes/hermes-agent)
    4. System python3 as a last resort
"""
import json
import os
import pathlib
import shutil
import subprocess
import time
import urllib.request
import urllib.error
import pytest

# ── Repo root discovery ────────────────────────────────────────────────────
# conftest.py lives at <repo>/tests/conftest.py
TESTS_DIR  = pathlib.Path(__file__).parent.resolve()
REPO_ROOT  = TESTS_DIR.parent.resolve()
HOME       = pathlib.Path.home()
HERMES_HOME = pathlib.Path(os.getenv('HERMES_HOME', str(HOME / '.hermes')))

# ── Test server config ────────────────────────────────────────────────────
# Port and state dir auto-derive from the repo path when no env var is set,
# giving every worktree its own isolated port (8800-8899) and state directory.
# Override with HERMES_WEBUI_TEST_PORT / HERMES_WEBUI_TEST_STATE_DIR to pin.

def _auto_test_port(repo_root) -> int:
    """Map repo path to a unique port in 20000-29999 (10k range = near-zero collisions).
    Far from system port ranges and Linux ephemeral ports (32768+).
    Override with HERMES_WEBUI_TEST_PORT to use a specific port."""
    import hashlib
    h = int(hashlib.md5(str(repo_root).encode()).hexdigest(), 16)
    return 20000 + (h % 10000)

def _auto_state_dir_name(repo_root) -> str:
    import hashlib
    h = hashlib.md5(str(repo_root).encode()).hexdigest()[:8]
    return f"webui-test-{h}"

TEST_PORT      = int(os.getenv('HERMES_WEBUI_TEST_PORT',
                               str(_auto_test_port(REPO_ROOT))))
TEST_BASE      = f"http://127.0.0.1:{TEST_PORT}"
TEST_STATE_DIR = pathlib.Path(os.getenv(
    'HERMES_WEBUI_TEST_STATE_DIR',
    str(HERMES_HOME / _auto_state_dir_name(REPO_ROOT))
))
TEST_WORKSPACE = TEST_STATE_DIR / 'test-workspace'

# Publish at module level so api.config, _pytest_port.py, and any test module
# importing stateful API code during collection see the isolated test paths.
#
# Direct assignment is intentional for production-risk paths: tests that import
# api.config/api.models in the pytest process must never inherit the real
# ~/.hermes state tree before the server subprocess fixture starts.
os.environ['HERMES_WEBUI_TEST_PORT'] = str(TEST_PORT)
os.environ['HERMES_WEBUI_TEST_STATE_DIR'] = str(TEST_STATE_DIR)
os.environ['HERMES_WEBUI_STATE_DIR'] = str(TEST_STATE_DIR)
os.environ['HERMES_WEBUI_DEFAULT_WORKSPACE'] = str(TEST_WORKSPACE)
os.environ['HERMES_HOME'] = str(TEST_STATE_DIR)
os.environ['HERMES_BASE_HOME'] = str(TEST_STATE_DIR)
# Hermes Agent sessions may inherit HERMES_CONFIG_PATH pointing at the live
# ~/.hermes/config.yaml.  Override it before any product modules are imported so
# tests that read/write config.yaml stay inside the isolated test home.
os.environ['HERMES_CONFIG_PATH'] = str(TEST_STATE_DIR / 'config.yaml')

# ── Server script: always relative to repo root ───────────────────────────
SERVER_SCRIPT = REPO_ROOT / 'server.py'
if not SERVER_SCRIPT.exists():
    raise RuntimeError(
        f"server.py not found at {SERVER_SCRIPT}. "
        "Is conftest.py in the tests/ subdirectory of the repo?"
    )

# ── Hermes agent discovery (mirrors api/config._discover_agent_dir) ───────
def _discover_agent_dir() -> pathlib.Path:
    candidates = [
        os.getenv('HERMES_WEBUI_AGENT_DIR', ''),
        str(HERMES_HOME / 'hermes-agent'),
        str(REPO_ROOT.parent / 'hermes-agent'),
        str(HOME / '.hermes' / 'hermes-agent'),
        str(HOME / 'hermes-agent'),
    ]
    for c in candidates:
        if not c:
            continue
        p = pathlib.Path(c).expanduser()
        if p.exists() and (p / 'run_agent.py').exists():
            return p.resolve()
    return None

# ── Python discovery (mirrors api/config._discover_python) ────────────────
def _discover_python(agent_dir) -> str:
    if os.getenv('HERMES_WEBUI_PYTHON'):
        return os.getenv('HERMES_WEBUI_PYTHON')
    if agent_dir:
        venv_py = agent_dir / 'venv' / 'bin' / 'python'
        if venv_py.exists():
            return str(venv_py)
    local_venv = REPO_ROOT / '.venv' / 'bin' / 'python'
    if local_venv.exists():
        return str(local_venv)
    return shutil.which('python3') or shutil.which('python') or 'python3'

HERMES_AGENT = _discover_agent_dir()
VENV_PYTHON  = _discover_python(HERMES_AGENT)

# Work dir: agent dir if found, else repo root
WORKDIR = str(HERMES_AGENT) if HERMES_AGENT else str(REPO_ROOT)

# ── Agent availability detection ─────────────────────────────────────────────
# Tests that require hermes-agent modules (cron, skills, approval, chat/stream)
# are skipped when the agent isn't installed, instead of failing with 500 errors.
AGENT_AVAILABLE = HERMES_AGENT is not None

def _check_agent_modules():
    """Verify hermes-agent Python modules are actually importable."""
    if not HERMES_AGENT:
        return False
    try:
        import importlib
        # These are the modules that cause 500 errors when missing
        for mod in ['cron.jobs', 'tools.skills_tool']:
            importlib.import_module(mod)
        return True
    except (ImportError, ModuleNotFoundError):
        return False

AGENT_MODULES_AVAILABLE = _check_agent_modules()

# pytest marker: skip tests that need hermes-agent when it's not present
requires_agent = pytest.mark.skipif(
    not AGENT_AVAILABLE,
    reason="hermes-agent not found (skipping agent-dependent test)"
)
requires_agent_modules = pytest.mark.skipif(
    not AGENT_MODULES_AVAILABLE,
    reason="hermes-agent Python modules not importable (cron, skills_tool)"
)

def pytest_configure(config):
    config.addinivalue_line("markers", "requires_agent: skip when hermes-agent dir is not found")
    config.addinivalue_line("markers", "requires_agent_modules: skip when hermes-agent Python modules are not importable")


# ── Environment isolation for tests ────────────────────────────────────────
# HERMES_WEBUI_SKIP_ONBOARDING is set by hosting providers (e.g. Agent37) and
# by some isolated test harnesses to short-circuit the onboarding wizard.
# When it leaks into the pytest environment, tests that exercise the wizard
# code paths (apply_onboarding_setup, etc.) fail because the function returns
# early without writing config files.
#
# This autouse fixture removes the variable for the test session. Tests that
# specifically need to validate the SKIP_ONBOARDING short-circuit can opt back
# in with `monkeypatch.setenv("HERMES_WEBUI_SKIP_ONBOARDING", "1")`.
@pytest.fixture(autouse=True, scope="session")
def _strip_skip_onboarding_env():
    prior = os.environ.pop("HERMES_WEBUI_SKIP_ONBOARDING", None)
    yield
    if prior is not None:
        os.environ["HERMES_WEBUI_SKIP_ONBOARDING"] = prior

def pytest_collection_modifyitems(config, items):
    """Auto-skip agent-dependent tests when hermes-agent is not available.

    Instead of requiring markers on every test function, we pattern-match
    test names to known categories that depend on hermes-agent modules.
    This keeps the test files clean and ensures new cron/skills tests
    get auto-skipped without manual annotation.
    """
    if AGENT_MODULES_AVAILABLE:
        return  # everything available, run all tests

    # Exact list of tests known to fail without hermes-agent.
    # These hit server endpoints that import cron.jobs, tools.skills_tool,
    # or require a running agent backend — returning 500 without the agent.
    _AGENT_DEPENDENT_TESTS = {
        # Cron endpoints (need cron.jobs module)
        'test_crons_list',
        'test_crons_list_has_required_fields',
        'test_crons_output_requires_job_id',
        'test_crons_output_real_job',
        'test_crons_run_nonexistent',
        'test_cron_create_success',
        'test_cron_update_unknown_job_404',
        'test_cron_delete_unknown_404',
        'test_crons_output_limit_param',
        # Skills endpoints (need tools.skills_tool module)
        'test_skills_list',
        'test_skills_list_has_required_fields',
        'test_skills_content_known',
        'test_skills_content_requires_name',
        'test_skills_search_returns_subset',
        'test_skill_save_delete_roundtrip',
        'test_skill_delete_unknown_404',
        # Agent backend (need running AIAgent)
        'test_chat_stream_opens_successfully',
        'test_approval_submit_and_respond',
        # Security redaction (flaky — session state varies across test ordering)
        'test_api_sessions_list_redacts_titles',
        # Workspace path (macOS /tmp -> /private/tmp symlink)
        'test_new_session_inherits_workspace',
        'test_workspace_add_valid',
        'test_workspace_rename',
        'test_last_workspace_updates_on_session_update',
        'test_new_session_inherits_last_workspace',
    }

    skip_marker = pytest.mark.skip(reason="requires hermes-agent (not installed)")
    skipped = 0

    for item in items:
        if item.name in _AGENT_DEPENDENT_TESTS:
            item.add_marker(skip_marker)
            skipped += 1

    if skipped:
        print(f"\nWARNING: hermes-agent not found; {skipped} agent-dependent tests will be skipped\n")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _post(base, path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


def _wait_for_server(base, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return True
        except Exception:
            time.sleep(0.3)
    return False


# ── Session-scoped test server ────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def test_server():
    """
    Start an isolated test server on TEST_PORT with a clean state directory.
    Paths are discovered dynamically -- no hardcoded absolute path assumptions.
    """
    # Kill any leftover process on the test port before starting.
    # Stale servers from QA harness runs or prior test sessions cause
    # conftest to think the server is already up, producing false failures.
    try:
        import subprocess as _sp
        _sp.run(['fuser', '-k', f'{TEST_PORT}/tcp'],
                capture_output=True, timeout=5)
    except Exception:
        pass
    import time as _time
    _time.sleep(0.5)  # brief pause to let the port release

    # Clean slate
    if TEST_STATE_DIR.exists():
        shutil.rmtree(TEST_STATE_DIR)
    TEST_STATE_DIR.mkdir(parents=True)
    TEST_WORKSPACE.mkdir(parents=True)

    # Symlink real skills into test home so skill-related tests work,
    # but all write-heavy state stays isolated.
    real_skills  = HERMES_HOME / 'skills'
    test_skills  = TEST_STATE_DIR / 'skills'
    if real_skills.exists() and not test_skills.exists():
        test_skills.symlink_to(real_skills)

    # Isolated cron state
    (TEST_STATE_DIR / 'cron').mkdir(parents=True, exist_ok=True)

    # Expose TEST_STATE_DIR to the test process itself so that tests which write
    # directly to state.db (e.g. test_gateway_sync.py) always use the same path
    # as the server.  Other test files (test_auth_sessions.py) may override
    # HERMES_WEBUI_STATE_DIR for their own purposes, but HERMES_WEBUI_TEST_STATE_DIR
    # is reserved for this mapping and is never overridden by individual test files.
    # Export both port and state-dir as env vars so individual test files
    # can read them without importing conftest (avoids circular imports).
    os.environ.setdefault('HERMES_WEBUI_TEST_PORT', str(TEST_PORT))
    # os.environ already set at module level above; no-op here.

    env = os.environ.copy()
    # Strip real provider keys so test subprocess never inherits production credentials.
    # The test server uses a mock/isolated config — no real API calls are made.
    for _k in list(env):
        if any(_k.startswith(p) for p in (
            'OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
            'GOOGLE_API_KEY', 'DEEPSEEK_API_KEY', 'XIAOMI_API_KEY',
        )):
            del env[_k]
    env.update({
        "HERMES_WEBUI_PORT":              str(TEST_PORT),
        "HERMES_WEBUI_HOST":              "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR":         str(TEST_STATE_DIR),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(TEST_WORKSPACE),
        "HERMES_WEBUI_DEFAULT_MODEL":     "openai/gpt-5.4-mini",
        "HERMES_HOME":                    str(TEST_STATE_DIR),
        "HERMES_CONFIG_PATH":             str(TEST_STATE_DIR / 'config.yaml'),
        # Belt-and-suspenders: HERMES_BASE_HOME hard-locks _DEFAULT_HERMES_HOME
        # in api/profiles.py to the test state dir regardless of profile switching
        # or any os.environ mutation that happens inside the server process.
        # Without this, a profile switch or active_profile file in the real
        # ~/.hermes can redirect _get_active_hermes_home() out of the sandbox,
        # causing onboarding writes (config.yaml, .env) to land in the production
        # ~/.hermes/profiles/webui/ and overwrite real API keys.
        "HERMES_BASE_HOME":               str(TEST_STATE_DIR),
        "HERMES_WEBUI_PASSWORD":          "",
    })

    # Pass agent dir if discovered so server.py doesn't have to re-discover
    if HERMES_AGENT:
        env["HERMES_WEBUI_AGENT_DIR"] = str(HERMES_AGENT)

    proc = subprocess.Popen(
        [VENV_PYTHON, str(SERVER_SCRIPT)],
        cwd=WORKDIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_for_server(TEST_BASE, timeout=20):
        proc.kill()
        pytest.fail(
            f"Test server on port {TEST_PORT} did not start within 20s.\n"
            f"  server.py : {SERVER_SCRIPT}\n"
            f"  python    : {VENV_PYTHON}\n"
            f"  agent dir : {HERMES_AGENT}\n"
            f"  workdir   : {WORKDIR}\n"
        )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    try:
        shutil.rmtree(TEST_STATE_DIR)
    except Exception:
        pass


# ── Test base URL ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def base_url():
    return TEST_BASE


# ── Per-test model cache invalidation ────────────────────────────────────────
# The TTL cache for get_available_models() persists across tests within the
# same process. Tests that modify cfg in-memory won't trigger the mtime path,
# so the cache must be explicitly invalidated after each test that exercises
# provider/model detection.

@pytest.fixture(autouse=True)
def _invalidate_models_cache_after_test():
    """Force the TTL cache to be cleared before and after every test.

    This prevents state bleed where a test that calls get_available_models()
    populates the cache with a particular config, and the next test sees stale
    results even though it has mutated _cfg_cache in-memory.
    """
    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


# ── Per-test session cleanup ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """
    Yields a list for tests to register created session IDs.
    Deletes all registered sessions after each test.
    Resets last_workspace to the test workspace to prevent state bleed.
    """
    created: list[str] = []
    yield created

    for sid in created:
        try:
            _post(TEST_BASE, "/api/session/delete", {"session_id": sid})
        except Exception:
            pass

    try:
        _post(TEST_BASE, "/api/sessions/cleanup_zero_message")
    except Exception:
        pass

    try:
        last_ws_file = TEST_STATE_DIR / "last_workspace.txt"
        last_ws_file.write_text(str(TEST_WORKSPACE), encoding='utf-8')
    except Exception:
        pass


# ── Convenience helpers ────────────────────────────────────────────────────────

def make_session_tracked(created_list, ws=None):
    """
    Create a session on the test server and register it for cleanup.

    Usage:
        def test_something(cleanup_test_sessions):
            sid, ws = make_session_tracked(cleanup_test_sessions)
    """
    body = {}
    if ws:
        body["workspace"] = str(ws)
    d = _post(TEST_BASE, "/api/session/new", body)
    sid = d["session"]["session_id"]
    ws_path = pathlib.Path(d["session"]["workspace"])
    created_list.append(sid)
    return sid, ws_path
