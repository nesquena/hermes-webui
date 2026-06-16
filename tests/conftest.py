"""
Shared pytest fixtures for webui-mvp tests.

TEST ISOLATION:
  Tests run against a SEPARATE server instance on an auto-derived test port
  with a completely separate state directory. Production data is never touched.
  The test state dir is wiped before each full test run and again on teardown.

PATH DISCOVERY:
  No hardcoded paths. Discovery order:
    1. Environment variables (HERMES_WEBUI_AGENT_DIR, HERMES_WEBUI_PYTHON, etc.)
    2. Sibling checkout heuristics relative to this repo
    3. Common install paths (~/.hermes/hermes-agent)
    4. System python3 as a last resort
"""
import json
import inspect
import multiprocessing
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import pytest

if not (3, 11) <= sys.version_info[:2] <= (3, 13):
    pytest.exit(
        "Hermes WebUI tests require Python 3.11, 3.12, or 3.13. "
        "Run ./scripts/test.sh so the repo-local supported .venv is used "
        "instead of an unsupported system python.",
        returncode=3,
    )

WINDOWS = sys.platform == "win32"
requires_fcntl = pytest.mark.skipif(
    WINDOWS,
    reason="requires fcntl-backed nonblocking pipe reads",
)
requires_fork = pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires multiprocessing fork",
)

# ── Repo root discovery ────────────────────────────────────────────────────
# conftest.py lives at <repo>/tests/conftest.py
TESTS_DIR  = pathlib.Path(__file__).parent.resolve()
REPO_ROOT  = TESTS_DIR.parent.resolve()
HOME       = pathlib.Path.home()
HERMES_HOME = pathlib.Path(os.getenv('HERMES_HOME', str(HOME / '.hermes')))

# ── Test server config ────────────────────────────────────────────────────
# Port and state dir auto-derive from the repo path when no env var is set,
# giving every worktree its own isolated port (20000-29999) and state directory.
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

# ── Test state dir: HARD-ISOLATED from production ──────────────────────────
# Test state must NEVER live inside the real Hermes home (~/.hermes or any
# HERMES_HOME), or anywhere near production profiles/files/server. Earlier this
# defaulted to ``HERMES_HOME / webui-test-<hash>`` which wrote test state INTO
# ~/.hermes/profiles/<...>/ (observed: 144 leaked webui-test-* dirs in the real
# profile home). We now anchor the default under the OS temp dir, in a dedicated
# `hermes-webui-tests/` namespace, fully outside any production tree.
import tempfile as _tempfile
_TEST_STATE_ROOT = pathlib.Path(
    os.getenv('HERMES_WEBUI_TEST_STATE_ROOT', _tempfile.gettempdir())
) / 'hermes-webui-tests'
TEST_STATE_DIR = pathlib.Path(os.getenv(
    'HERMES_WEBUI_TEST_STATE_DIR',
    str(_TEST_STATE_ROOT / _auto_state_dir_name(REPO_ROOT))
)).resolve()

# Production-proximity guard: refuse to run if the resolved test state dir lands
# inside the REAL Hermes home tree — a misconfigured HERMES_WEBUI_TEST_STATE_DIR
# pointing at ~/.hermes would let tests wipe/clobber production profiles,
# sessions, and credentials on teardown. We anchor on the literal user-home
# `~/.hermes` (NOT $HERMES_HOME): HERMES_HOME is frequently overridden to a
# profile dir or, during a test run, to TEST_STATE_DIR itself — comparing
# against it would either miss a real production path or false-trip when the
# test dir legitimately lives in /tmp. A test dir under the OS temp dir is always
# allowed even if it nominally sits below a temp-rooted HERMES_HOME.
_PROD_HERMES_HOME = (HOME / '.hermes').resolve()
_TEMP_ROOT = pathlib.Path(_tempfile.gettempdir()).resolve()
# The temp-root exception only holds when the temp root is itself OUTSIDE the
# production home. If TMPDIR is (mis)configured under ~/.hermes, a "temp" path is
# still a production path — don't let it suppress the guard.
_temp_root_is_safe = not (
    _TEMP_ROOT == _PROD_HERMES_HOME or _PROD_HERMES_HOME in _TEMP_ROOT.parents
)
_under_temp = _temp_root_is_safe and (
    TEST_STATE_DIR == _TEMP_ROOT or _TEMP_ROOT in TEST_STATE_DIR.parents
)
_under_prod = TEST_STATE_DIR == _PROD_HERMES_HOME or _PROD_HERMES_HOME in TEST_STATE_DIR.parents
if _under_prod and not _under_temp:
    raise RuntimeError(
        f"REFUSING TO RUN: test state dir {TEST_STATE_DIR} is inside the production "
        f"Hermes home {_PROD_HERMES_HOME}. Tests must never touch production files. "
        f"Unset HERMES_WEBUI_TEST_STATE_DIR (defaults to a temp dir) or point it "
        f"outside ~/.hermes."
    )

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


@pytest.fixture(autouse=True)
def _isolate_hermes_config_path():
    """Keep profile/.env side effects from leaking the live config path across tests."""
    isolated_config_path = str(TEST_STATE_DIR / 'config.yaml')
    os.environ['HERMES_CONFIG_PATH'] = isolated_config_path
    yield
    os.environ['HERMES_CONFIG_PATH'] = isolated_config_path


_MISSING = object()  # sentinel: api.profiles module not loaded pre-test


@pytest.fixture(autouse=True)
def _restore_profile_home_globals():
    """Restore HERMES_HOME / HERMES_BASE_HOME after every test.

    Several tests call ``api.profiles.switch_profile()`` (or set HERMES_HOME
    directly) which mutates ``os.environ['HERMES_HOME']`` IN PLACE — not via
    monkeypatch — so the change is not auto-reverted at test teardown. In the
    normal sequential run the next test usually re-establishes its own profile so
    the leak is masked, but under pytest-shard (or pytest-randomly) the leaked
    HERMES_HOME points at a deleted tmpdir and breaks any later test whose
    config/profile resolution reads it (e.g. test_title_aux_routing's
    background-worker profile routing, which then falls back to DEFAULT_CONFIG
    where ``model`` is an empty string). Snapshotting at the conftest level fixes
    the whole class at once, regardless of which test does the leaking.
    """
    saved_home = os.environ.get('HERMES_HOME')
    saved_base = os.environ.get('HERMES_BASE_HOME')
    # Snapshot the process-global active-profile name too. Several tests call
    # switch_profile() (process_wide=True), which mutates api.profiles._active_profile
    # in place and never restores it. In a sequential run the next test usually
    # re-establishes its own profile so the leak is masked, but a test that only
    # patches a profile-scoped *path* (e.g. config._models_cache_path) without
    # setting an active profile then resolves the LEAKED profile — e.g.
    # _get_models_cache_path() returns models_cache.<leaked>.json instead of the
    # patched default path. Restoring the name here fixes the whole class.
    prof_mod_pre = sys.modules.get('api.profiles')
    # Use a sentinel so we restore whenever the module was importable pre-test,
    # independent of the value (covers a hypothetical None, though _active_profile
    # defaults to 'default'). _MISSING means "module wasn't loaded" → skip restore.
    saved_active_profile = getattr(prof_mod_pre, '_active_profile', _MISSING) if prof_mod_pre else _MISSING
    # Re-derive the cached base-home global BEFORE the test runs too: a prior
    # test's teardown ordering (monkeypatch restoring sys.modules['api.profiles']
    # after this fixture's teardown) can leave the live module's
    # _DEFAULT_HERMES_HOME stale. Fixing it at setup time guarantees each test
    # starts from a base root that matches the current (restored) env.
    _rederive_default_hermes_home()
    yield
    for key, val in (('HERMES_HOME', saved_home), ('HERMES_BASE_HOME', saved_base)):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    prof_mod_post = sys.modules.get('api.profiles')
    if prof_mod_post is not None and saved_active_profile is not _MISSING:
        prof_mod_post._active_profile = saved_active_profile
        # Also clear any leaked per-request thread-local profile (issue #798).
        try:
            prof_mod_post.clear_request_profile()
        except Exception:
            pass
    _rederive_default_hermes_home()


def _rederive_default_hermes_home():
    """Recompute api.profiles._DEFAULT_HERMES_HOME from the current env.

    api.profiles caches the base home at import time. A test that re-imports
    api.profiles under a temporary HERMES_BASE_HOME (e.g. test_profile_env_isolation)
    corrupts that global to a now-deleted tmpdir, making get_hermes_home_for_profile
    resolve later tests' profiles under the dead path. Re-deriving keeps it honest.
    """
    prof_mod = sys.modules.get('api.profiles')
    if prof_mod is not None and hasattr(prof_mod, '_resolve_base_hermes_home'):
        try:
            prof_mod._DEFAULT_HERMES_HOME = prof_mod._resolve_base_hermes_home()
        except Exception:
            pass

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
        for venv_dir in ('venv', '.venv'):
            for subdir, binary in (('bin', 'python'), ('Scripts', 'python.exe')):
                venv_py = agent_dir / venv_dir / subdir / binary
                if venv_py.exists():
                    return str(venv_py)
    for subdir, binary in (('bin', 'python'), ('Scripts', 'python.exe')):
        local_venv = REPO_ROOT / '.venv' / subdir / binary
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
    config.addinivalue_line("markers", "requires_fcntl: skip when fcntl-backed file-descriptor operations are unavailable")
    config.addinivalue_line("markers", "requires_fork: skip when the platform lacks multiprocessing fork support")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_report_collectionfinish(config, items):
    """Avoid pytest-shard's giant nodeid dump on sharded `-v` CI runs."""
    verbose = getattr(config.option, "verbose", 0)
    shard_total = getattr(config.option, "num_shards", 1)
    if shard_total > 1 and verbose > 0:
        config.option.verbose = 0
    try:
        yield
    finally:
        config.option.verbose = verbose


# ── Disable AWS IMDS probing for the pytest session ────────────────────────
# Background: when hermes-agent's bedrock_adapter / botocore credential chain
# runs during test execution (e.g. provider catalog enumeration triggered by
# api/config.py imports), botocore probes the EC2 Instance Metadata Service at
# 169.254.169.254 looking for an instance role. On VPS hosts where IMDS is
# reachable but rate-limited (HTTP 429) or non-responsive, this dominates wall
# time and turns a 161s test run into 600+s.
#
# Tests have no legitimate reason to call IMDS — the bedrock-related tests use
# explicit mocks or env-var creds. Setting AWS_EC2_METADATA_DISABLED before
# anything imports botocore is the supported way to silence the probe (matches
# the guard the hermes_cli/doctor.py command already uses in its parallel-probe
# block).
#
# Setting this here instead of in a fixture so it lands BEFORE any test-file
# imports trigger botocore initialisation.
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

# ── Permanent os.execv guard for the pytest session ────────────────────────
# Several tests in tests/test_update_banner_fixes.py exercise
# api.updates._schedule_restart(), which spawns a DAEMON thread that sleeps
# for a short delay and then calls ``os.execv(sys.executable, sys.argv)``.
# Those tests monkeypatch ``os.execv`` to a no-op for the test scope, but
# monkeypatch teardown happens at test exit — if the daemon thread has not
# yet woken up by then (system load, GC pause, _apply_lock contention), the
# real ``os.execv`` is restored before the thread fires it. The daemon then
# REPLACES the pytest process image with a fresh ``pytest tests/ -q ...``
# invocation, looking from the outside like pytest "hangs at 99%" and then
# restarts the entire suite from 0% — a self-perpetuating loop.
#
# Daemon threads cannot be reliably joined from a test fixture (they live in
# ``api.updates`` module scope), so the only safe answer is to render
# ``os.execv`` permanently inert for the pytest session. Production code is
# unaffected because production never imports this conftest.
#
# Tests that need to verify execv WAS called still monkeypatch it themselves
# — their patched version takes precedence over this no-op wrapper for the
# test's lifetime, and the no-op only kicks in after teardown for daemon
# threads that wake up late.
_real_execv = os.execv

def _pytest_session_safe_execv(_exe, _args):  # pragma: no cover — never called in prod
    # Drop the call on the floor. A late-firing daemon thread from
    # _schedule_restart() must not be able to re-exec the pytest process.
    return None

os.execv = _pytest_session_safe_execv

# ── Hermetic network isolation ─────────────────────────────────────────────
# Tests must not reach the public internet. Outbound to Anthropic / OpenAI /
# Amazon / OpenRouter / etc. is forbidden by default. The test suite already
# mocks every legitimate outbound (probe_provider_endpoint, get_available_models,
# urlopen calls inside api/config.py), so a real outbound socket is either a
# missing mock, a leaked credential triggering an SDK init, or an unintended
# regression like the one PR #1970 introduced where a new code path bypassed
# an existing mock and tried to hit the real LM Studio host.
#
# This module-level monkey-patch wraps socket.create_connection so any
# non-loopback / non-RFC1918 / non-link-local / non-TEST-NET destination
# raises OSError("hermes test network isolation").  Tests that deliberately
# attempt outbound (only test_dns_resolution_failure today) opt back in
# explicitly via the `allow_outbound_network` fixture below.
#
# Allowed destinations (silent pass-through):
#   - 127.0.0.0/8     loopback
#   - ::1             IPv6 loopback
#   - 192.168.0.0/16  RFC1918 private
#   - 10.0.0.0/8      RFC1918 private
#   - 172.16.0.0/12   RFC1918 private (16-31)
#   - 169.254.0.0/16  link-local (covers IMDS — already separately blocked
#                     by AWS_EC2_METADATA_DISABLED, but allowed at the socket
#                     layer because IMDS-using tests mock the response)
#   - 203.0.113.0/24  RFC5737 TEST-NET-3 (used as documentation IPs in tests)
#   - hostnames `localhost`, `*.local`, `*.test`, `*.example`, `*.example.com`
#     `*.example.net`, `*.example.org`, `*.invalid` (RFC2606/6761 reserved)
#
# A test that opts in via the `allow_outbound_network` fixture sees the real
# socket.create_connection.
import socket as _hermes_test_socket
_REAL_CREATE_CONNECTION = _hermes_test_socket.create_connection
_REAL_SOCKET_CONNECT = _hermes_test_socket.socket.connect


def _hermes_addr_is_local(host: str) -> bool:
    """Return True for loopback / RFC1918 / link-local / reserved-TLD hosts."""
    if not isinstance(host, str):
        return False
    h = host.strip().lower()
    if not h:
        return False
    # IPv6 loopback / link-local
    # IPv6 unique-local: fc00::/7 — any address starting with fc?? or fd?? (?? = hex pair).
    # Loose "startswith('fc')" / "startswith('fd')" would also match the hostnames
    # "food.example.com" or "fdsa.test", so require the second char to be a hex
    # digit followed by either a colon or another hex digit (canonical IPv6 syntax).
    import re as _re
    if h in ('::1', '0:0:0:0:0:0:0:1') or h.startswith('fe80:') or _re.match(r'^f[cd][0-9a-f]{0,2}:', h):
        return True
    # Hostname allow-list (RFC2606/6761 reserved TLDs + localhost)
    if h == 'localhost' or h.endswith('.localhost'):
        return True
    if h.endswith('.local') or h.endswith('.test') or h.endswith('.invalid'):
        return True
    if h == 'example.com' or h.endswith('.example.com'):
        return True
    if h == 'example.net' or h.endswith('.example.net'):
        return True
    if h == 'example.org' or h.endswith('.example.org'):
        return True
    if h.endswith('.example'):
        return True
    # IPv4 — parse octets if it looks like a dotted quad
    if h[0].isdigit() and h.count('.') == 3:
        try:
            o1, o2, o3, o4 = [int(p) for p in h.split('.')]
        except ValueError:
            return False
        if o1 == 127:                          # loopback
            return True
        if o1 == 10:                           # RFC1918 10.0.0.0/8
            return True
        if o1 == 192 and o2 == 168:            # RFC1918 192.168.0.0/16
            return True
        if o1 == 172 and 16 <= o2 <= 31:       # RFC1918 172.16.0.0/12
            return True
        if o1 == 169 and o2 == 254:            # link-local 169.254.0.0/16
            return True
        if o1 == 203 and o2 == 0 and o3 == 113:  # RFC5737 TEST-NET-3
            return True
    return False


def _hermes_blocked_create_connection(address, *a, **kw):
    try:
        host = address[0]
    except (TypeError, IndexError):
        host = ""
    if _hermes_addr_is_local(host):
        return _REAL_CREATE_CONNECTION(address, *a, **kw)
    raise OSError(
        f"hermes test network isolation: outbound socket to {address!r} is blocked. "
        f"Tests should mock urllib.request.urlopen / requests / socket.create_connection. "
        f"If a test genuinely needs real outbound, request the allow_outbound_network fixture."
    )


def _hermes_blocked_socket_connect(self, address):
    try:
        host = address[0]
    except (TypeError, IndexError):
        host = ""
    if _hermes_addr_is_local(host):
        return _REAL_SOCKET_CONNECT(self, address)
    raise OSError(
        f"hermes test network isolation: socket.connect to {address!r} is blocked."
    )


_hermes_test_socket.create_connection = _hermes_blocked_create_connection
_hermes_test_socket.socket.connect = _hermes_blocked_socket_connect


@pytest.fixture
def allow_outbound_network(monkeypatch):
    """Opt-in to real outbound network for the duration of one test.

    Swaps `socket.create_connection` and `socket.socket.connect` back to the
    real (unwrapped) implementations for this test only, then monkeypatch
    teardown restores the wrapped versions. Direct swap is more reliable
    than a module-global toggle on CI runners where wrapper-closure
    lookup semantics can surprise.

    Use sparingly. Today zero tests in the repo call this — the previous
    test_dns_resolution_failure case was rewritten to mock socket.getaddrinfo
    instead, which is fully hermetic.
    """
    monkeypatch.setattr(_hermes_test_socket, "create_connection", _REAL_CREATE_CONNECTION)
    monkeypatch.setattr(_hermes_test_socket.socket, "connect", _REAL_SOCKET_CONNECT)
    yield




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
        'test_delivery_options_returns_200',
        'test_delivery_options_has_platforms',
        'test_delivery_options_structure',
        'test_delivery_options_includes_common_platforms',
        'test_delivery_options_local_label',
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


def _wait_for_server(base, timeout=45, proc=None, log_path=None):
    """Poll ``base/health`` until the server reports ready, or fail fast.

    Returns ``(True, "")`` once ``/health`` returns ``status == "ok"``.

    Returns ``(False, reason)`` when the server does not come up. ``reason`` is a
    human-readable diagnostic that, crucially, includes WHY when we can tell:

    * If ``proc`` is supplied and the subprocess has already exited, we stop
      polling immediately (no point waiting out the full timeout for a process
      that is already dead) and surface its exit code.
    * If ``log_path`` is supplied, the tail of the captured server stdout/stderr
      is included so an import error / bind failure / traceback is visible
      instead of a bare "did not start" message.

    The previous implementation polled for the whole timeout regardless of
    whether the process had died, and the server's output was discarded to
    ``DEVNULL`` — so a boot failure produced a generic timeout with no clue as
    to the cause, and every HTTP-dependent test then cascaded with
    ConnectionRefused. Capturing output + early-exit detection turns that opaque
    cascade into a single actionable failure.
    """
    deadline = time.time() + timeout
    last_err = "no successful /health response"
    while time.time() < deadline:
        # Fail fast if the server subprocess has already died — don't wait out
        # the full timeout polling a port nothing is listening on.
        if proc is not None and proc.poll() is not None:
            return False, _server_boot_diagnostic(
                f"server process exited early with code {proc.returncode}",
                log_path,
            )
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return True, ""
                last_err = "/health responded but status != 'ok'"
        except Exception as e:  # noqa: BLE001 — diagnostic capture, re-raised as text
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.3)
    return False, _server_boot_diagnostic(
        f"timed out after {timeout}s waiting for /health (last: {last_err})",
        log_path,
    )


def _server_boot_diagnostic(headline, log_path):
    """Build a boot-failure message, appending the captured server log tail."""
    parts = [headline]
    if log_path is not None:
        try:
            text = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
            tail = "".join(text.splitlines(keepends=True)[-40:]).strip()
            if tail:
                parts.append("---- server output (last 40 lines) ----\n" + tail)
            else:
                parts.append("(server produced no output)")
        except Exception as e:  # noqa: BLE001
            parts.append(f"(could not read server log {log_path}: {e})")
    return "\n".join(parts)


def _kill_process_tree(pid):
    """Best-effort kill for a known fixture-owned or port-owning PID."""
    if not pid or pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                **({"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
            return
        import signal

        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _kill_port_owner(port):
    """Best-effort free of TEST_PORT, using a Windows-native owner lookup."""
    try:
        if sys.platform != "win32":
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)
            return

        creationflags = {"creationflags": subprocess.CREATE_NO_WINDOW}
        ps_cmd = (
            "$port = " + str(port) + "; "
            "$conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | "
            "Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1', '0.0.0.0', '::', '::ffff:127.0.0.1') } | "
            "Select-Object -First 1 -ExpandProperty OwningProcess; "
            "if ($conn) { $conn }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=5,
            **creationflags,
        )
        pid_text = proc.stdout.strip()
        if not pid_text:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=5,
                **creationflags,
            )
            port_suffixes = (f":{port}", f"[::]:{port}", f"127.0.0.1:{port}", f"[::1]:{port}")
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                local_addr = parts[1]
                state = parts[3].upper()
                owner_pid = parts[4]
                if state == "LISTENING" and any(local_addr.endswith(suffix) for suffix in port_suffixes):
                    pid_text = owner_pid
                    break
        if pid_text:
            _kill_process_tree(int(pid_text))
    except Exception:
        pass


def _rmtree_retry(path):
    """Remove a tree, retrying transient races (Linux + Windows).

    The retry loop covers two real failure modes seen in CI/local:
      * Windows: read-only bits / lingering handles (cleared via onexc/onerror).
      * Linux: `OSError [Errno 39] Directory not empty` — a background thread or
        daemon (model-metadata fetcher, session-events watcher, profile writer)
        creates a file inside the tree WHILE shutil.rmtree is walking it, so the
        parent rmdir fails even though we just emptied it. A short retry lets the
        racing writer settle; a final ignore_errors sweep guarantees teardown
        never fails the test over a pure cleanup race (the dir is abandoned test
        state, not an assertion target).
    """
    target = pathlib.Path(path)
    if not target.exists():
        return

    def _clear_readonly(_func, entry, _exc):
        try:
            os.chmod(entry, 0o666)
            _func(entry)
        except Exception:
            raise

    rmtree_kwargs = (
        {"onexc": _clear_readonly}
        if "onexc" in inspect.signature(shutil.rmtree).parameters
        else {"onerror": _clear_readonly}
    ) if sys.platform == "win32" else {}

    attempts = 5
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(target, **rmtree_kwargs)
            return
        except FileNotFoundError:
            return
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            time.sleep(0.3)

    # Final fallback: a concurrent-writer race (Errno 39) shouldn't fail teardown.
    # Best-effort ignore_errors sweep; if anything remains it's abandoned test
    # state under HERMES_HOME, not something a test asserts on.
    shutil.rmtree(target, ignore_errors=True)
    if not target.exists():
        return
    leftovers = []
    try:
        leftovers = [child.name for child in list(target.iterdir())[:5]]
    except Exception:
        pass
    # Don't raise — log-and-continue. Raising here turns a benign cleanup race
    # into a spurious test ERROR (the behaviour #4283-area tests hit when a
    # model-metadata background fetch repopulated the profile dir mid-teardown).
    import warnings as _warnings
    leftover_note = f" leftovers={leftovers}" if leftovers else ""
    _warnings.warn(
        f"_rmtree_retry: could not fully remove {target} after {attempts} attempts"
        f"{leftover_note} (last_exc={last_exc!r}); left for OS temp cleanup.",
        stacklevel=2,
    )


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
    _kill_port_owner(TEST_PORT)
    import time as _time
    _time.sleep(0.5)  # brief pause to let the port release

    # Clean slate
    if TEST_STATE_DIR.exists():
        _rmtree_retry(TEST_STATE_DIR)
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
    # Strip ANY real credential env var so the test subprocess never inherits
    # production creds. The test server uses a mock/isolated config — no real
    # API calls are made, no real OAuth flow runs, no real cloud SDK should
    # ever be initialised with usable credentials.
    #
    # Without this strip, a stray credential left in the runner's env was
    # observed making outbound TLS to a real provider during test runs.
    # See investigation notes in pytest-pitfalls SKILL §B.3.
    _CRED_ENV_PREFIXES = (
        # LLM providers
        'OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL',
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
        'GOOGLE_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
        'DEEPSEEK_API_KEY', 'XIAOMI_API_KEY',
        'XAI_API_KEY', 'MISTRAL_API_KEY', 'OLLAMA_API_KEY',
        'GROQ_API_KEY', 'TOGETHER_API_KEY', 'PERPLEXITY_API_KEY',
        'CEREBRAS_API_KEY', 'COHERE_API_KEY', 'FIREWORKS_API_KEY',
        'NOUS_API_KEY', 'NOVITA_API_KEY', 'TENCENT_API_KEY',
        'BIGMODEL_API_KEY', 'GLM_API_KEY', 'STEPFUN_API_KEY',
        'MINIMAX_API_KEY', 'LM_API_KEY', 'LMSTUDIO_API_KEY',
        'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT',
        # AWS — must be stripped or botocore probes IMDS / picks up real creds
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
        'AWS_PROFILE', 'AWS_BEARER_TOKEN_BEDROCK',
        # Memory providers, telemetry, dashboards
        'MEM0_API_KEY', 'HONCHO_API_KEY', 'SUPERMEMORY_API_KEY',
        # Messaging / gateway
        'TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN', 'SLACK_BOT_TOKEN',
        'SIGNAL_API_TOKEN', 'WHATSAPP_API_TOKEN',
        # Browser / image-gen / search
        'FIRECRAWL_API_KEY', 'FAL_KEY', 'TAVILY_API_KEY',
        'SERPER_API_KEY', 'BRAVE_API_KEY',
        # Github tokens (PR/issue tools shouldn't be exercised in tests)
        'GH_TOKEN', 'GITHUB_TOKEN',
    )
    for _k in list(env):
        if any(_k.startswith(p) for p in _CRED_ENV_PREFIXES):
            del env[_k]
    # Belt-and-suspenders: keep IMDS disabled in the spawn env too (we set it
    # at module level above for the pytest process, but make it explicit here
    # so it's never accidentally cleared by an env.update later).
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    # Activate the same network-isolation block in the test_server subprocess
    # that conftest.py installs in the pytest process. server.py reads this
    # env var at import time and installs an identical socket-block guard.
    # Without this, the subprocess can make outbound requests that the
    # pytest-side block can't see.
    env["HERMES_WEBUI_TEST_NETWORK_BLOCK"] = "1"
    env.update({
        "HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE": "1",
        # Small archive-extraction cap so the zip-bomb guard is exercisable
        # against the out-of-process test server (the real 10x-upload default is
        # ~200MB — impractical to exceed in a test). 5MB is far above any other
        # test's archive payload, so only the bomb test trips it.
        "HERMES_WEBUI_MAX_EXTRACTED_MB":  "5",
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

    # Capture server stdout/stderr to a temp log instead of DEVNULL so a boot
    # failure (import error, port-bind race, traceback) is diagnosable. Without
    # this, _wait_for_server could only report a bare "did not start" timeout
    # and every HTTP-dependent test then cascaded with ConnectionRefused —
    # hundreds of opaque failures from a single root cause.
    import tempfile as _tempfile
    _server_log = pathlib.Path(_tempfile.gettempdir()) / f"hermes-webui-test-server-{TEST_PORT}.log"

    # Boot the server, retrying once if it dies early or fails to bind. Boot
    # failures here are most often transient (a port not yet released by a prior
    # session, a momentary import hiccup under load), so one clean retry with a
    # fresh port-kill turns an intermittent cascade into a reliable start.
    proc = None
    boot_attempts = 2
    last_reason = ""
    for _attempt in range(1, boot_attempts + 1):
        with open(_server_log, "w", encoding="utf-8") as _logf:
            proc = subprocess.Popen(
                [VENV_PYTHON, str(SERVER_SCRIPT)],
                cwd=WORKDIR,
                env=env,
                stdout=_logf,
                stderr=subprocess.STDOUT,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
            )
        # 45s (up from 20s): server.py imports the full hermes-agent, which is
        # import-heavy and can exceed 20s on a loaded runner — the old timeout
        # turned a slow-but-fine boot into a whole-suite failure.
        ok, reason = _wait_for_server(TEST_BASE, timeout=45, proc=proc, log_path=_server_log)
        if ok:
            break
        last_reason = reason
        # Tear down the failed attempt and free the port before retrying.
        try:
            _kill_process_tree(proc.pid)
            proc.wait(timeout=5)
        except Exception:
            pass
        if _attempt < boot_attempts:
            _kill_port_owner(TEST_PORT)
            time.sleep(1.0)
    else:
        pytest.fail(
            f"Test server on port {TEST_PORT} did not start after {boot_attempts} attempts.\n"
            f"  reason    : {last_reason}\n"
            f"  server.py : {SERVER_SCRIPT}\n"
            f"  python    : {VENV_PYTHON}\n"
            f"  agent dir : {HERMES_AGENT}\n"
            f"  workdir   : {WORKDIR}\n"
            f"  log       : {_server_log}\n"
        )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)

    _rmtree_retry(TEST_STATE_DIR)


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
