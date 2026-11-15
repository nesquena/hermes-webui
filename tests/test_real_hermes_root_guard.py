"""The suite-level real-Hermes-root guard (tests/_real_hermes_root_guard.py) and HOME sandbox."""
import os
import pathlib
import shutil
import sqlite3
import sys
import urllib.parse
from contextlib import contextmanager

import pytest

from tests import _real_hermes_root_guard as guard


@contextmanager
def _simulated_real_root(tmp_path):
    """Point the live guard at a fake 'real' root for the duration of the block."""
    root = tmp_path / "real home" / ".hermes"  # the space must survive sqlite URI encoding
    (root / "profiles" / "default").mkdir(parents=True)
    (root / "config.yaml").write_text("original")
    saved = list(guard.PROTECTED), list(guard.ALLOWED), len(guard.VIOLATIONS)
    guard.PROTECTED[:] = [os.path.realpath(root)]
    guard.ALLOWED[:] = [os.path.realpath(root / "allowed-scratch")]
    try:
        yield root
    finally:
        guard.PROTECTED[:], guard.ALLOWED[:] = saved[0], saved[1]
        del guard.VIOLATIONS[saved[2]:]  # expected refusals must not fail this test


def _in_dir(path, op):
    """Run *op* with a descriptor for *path*: the descriptor-relative (``dir_fd``) calls."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return op(fd)
    finally:
        os.close(fd)


def _anchored_create(root):
    from api.workspace import open_anchored_create_fd  # the real writer: mkdir/os.open with dir_fd
    os.close(open_anchored_create_fd(root, root.resolve() / "profiles" / "testprof1" / "config.yaml"))


def _dotdot_through_link(root):
    link = root.parent.parent / "outside-link"  # outside the root, pointing into it
    link.symlink_to(root / "profiles", target_is_directory=True)
    pathlib.Path(f"{link}/../config.yaml").write_text("x")  # the kernel follows the link before '..'


_needs_dir_fd = pytest.mark.skipif(os.mkdir not in os.supports_dir_fd, reason="no dir_fd support")


@pytest.mark.parametrize("write", [
    lambda r: (r / "profiles" / "testprof1").mkdir(),
    lambda r: (r / "config.yaml").write_text("x"),
    lambda r: os.close(os.open(r / "new.db", os.O_CREAT | os.O_WRONLY)),
    lambda r: sqlite3.connect(r / "state.db"),
    lambda r: os.replace(r / "profiles" / "default", r / "profiles" / "moved"),
    lambda r: (r / "profiles" / "default").rmdir(),
    pytest.param(lambda r: _in_dir(r / "profiles", lambda fd: os.mkdir("testprof1", dir_fd=fd)), marks=_needs_dir_fd),
    pytest.param(lambda r: _in_dir(r, lambda fd: os.close(os.open("new.db", os.O_CREAT | os.O_WRONLY, dir_fd=fd))),
                 marks=_needs_dir_fd),
    pytest.param(lambda r: _in_dir(r / "profiles", lambda fd: os.rename("default", "moved", src_dir_fd=fd, dst_dir_fd=fd)),
                 marks=_needs_dir_fd),
    pytest.param(lambda r: _in_dir(r / "profiles", lambda fd: os.rmdir("default", dir_fd=fd)), marks=_needs_dir_fd),
    _anchored_create,
    lambda r: sqlite3.connect((r / "state.db").as_uri() + "?mode=rwc", uri=True),
    lambda r: sqlite3.connect("file://localhost" + urllib.parse.quote(str(r / "state.db")) + "?cache=private", uri=True),
    lambda r: sqlite3.connect((r / "state.db").as_uri() + "%00ignored?mode=rwc", uri=True),  # SQLite stops at NUL
    _dotdot_through_link,
    lambda r: os.link(r / "config.yaml", r.parent.parent / "alias"),  # an outside alias would write the real file
    lambda r: (r / "profiles" / "default" / "__pycache__").mkdir(),  # only Agent bytecode is exempt
], ids=["mkdir", "write_text", "os.open-create", "sqlite-connect", "replace", "rmdir",
        "mkdir-dir_fd", "os.open-dir_fd", "rename-dir_fd", "rmdir-dir_fd", "workspace-anchored-create",
        "sqlite-uri-encoded", "sqlite-uri-localhost", "sqlite-uri-NUL", "dotdot-through-link", "hardlink-source",
        "profile-pycache"])
def test_write_into_real_root_is_refused_and_recorded(tmp_path, write):
    with _simulated_real_root(tmp_path) as root:
        before = len(guard.VIOLATIONS)
        with pytest.raises(guard.RealHermesRootWriteError, match="REFUSED"):
            write(root)
        assert guard.VIOLATIONS[before:], "a refused write must be recorded for the per-test/session failure"
        assert sorted(p.name for p in (root / "profiles").iterdir()) == ["default"]


def test_write_through_symlink_into_real_root_is_refused(tmp_path):
    with _simulated_real_root(tmp_path) as root:
        link = tmp_path / "innocent-looking"
        link.symlink_to(root / "profiles", target_is_directory=True)
        with pytest.raises(guard.RealHermesRootWriteError):
            (link / "testprof1").mkdir()
        assert not (root / "profiles" / "testprof1").exists()


def test_removing_a_link_into_the_real_root_does_not_write_into_it(tmp_path):
    """rmtree unlinks entries with dir_fd: a link that points into the root is removed, never followed."""
    with _simulated_real_root(tmp_path) as root:
        tree = tmp_path / "scratch-tree"
        tree.mkdir()
        (tree / "agent").symlink_to(root / "profiles", target_is_directory=True)
        shutil.rmtree(tree)
        assert not tree.exists() and (root / "profiles" / "default").is_dir()


@pytest.mark.skipif(os.link not in os.supports_follow_symlinks, reason="os.link cannot keep a symlink source")
def test_hardlinking_a_scratch_symlink_that_points_into_the_root_is_not_a_write(tmp_path):
    """Linking the symlink itself (Linux link() semantics) leaves the root untouched."""
    with _simulated_real_root(tmp_path) as root:
        (tmp_path / "src").symlink_to(root / "config.yaml")
        os.link(tmp_path / "src", tmp_path / "dst", follow_symlinks=False)
        assert (tmp_path / "dst").is_symlink() and (root / "config.yaml").read_text() == "original"


def test_readonly_and_memory_sqlite_uris_are_not_writes(tmp_path):
    db = tmp_path / "real home" / "state.db"
    assert guard._sqlite_target(db.as_uri() + "?mode=ro") == ()
    assert guard._sqlite_target("file:memdb?mode=memory&cache=shared") == ()
    assert guard._sqlite_target(db.as_uri() + "?mode=rw#frag") == (str(db),)


def test_reads_allowed_subtrees_and_bytecode_are_not_refused(tmp_path):
    with _simulated_real_root(tmp_path) as root:
        before = len(guard.VIOLATIONS)
        sorted((root / "profiles").iterdir())
        (root / "allowed-scratch" / "tmp").mkdir(parents=True)
        (root / "allowed-scratch" / "tmp" / "f").write_text("ok")
        assert (root / "allowed-scratch" / "tmp" / "f").read_text() == "ok"
        assert guard.violation(root / "hermes-agent" / "pkg" / "__pycache__" / "m.pyc") is None
        assert guard.violation(root / "hermes-agent" / "pkg" / "__pycache__") is None
        assert guard.violation(root / "profiles" / "x") == os.path.realpath(root / "profiles" / "x")
        assert len(guard.VIOLATIONS) == before


def test_installing_again_keeps_paths_allowed_earlier(tmp_path, monkeypatch):
    """conftest.py can run again mid-session (a test loads it by path), which calls install() again. That must
    not drop a path allowed earlier, such as the --basetemp that pytest_configure adds under a real root."""
    with _simulated_real_root(tmp_path) as root:
        basetemp = root / "scratch" / "bt"
        guard.allow(basetemp)
        monkeypatch.setattr(guard, "real_roots", lambda: [os.path.realpath(root)])
        monkeypatch.setitem(guard._STATE, "profiles", dict(guard._STATE["profiles"]))
        guard.install([tmp_path / "repo"])
        assert guard.violation(basetemp / "pytest-0" / "test_x0") is None
        assert guard.violation(root / "profiles" / "new") is not None


def test_bytecode_under_a_discovered_agent_checkout_is_not_refused(tmp_path):
    """Discovery prefers <HERMES_HOME>/hermes-agent, so a profile home's checkout sits at
    <root>/profiles/<name>/hermes-agent; importing it writes bytecode there."""
    with _simulated_real_root(tmp_path) as root:
        checkout = root / "profiles" / "coder" / "hermes-agent"
        saved = list(guard.AGENT_CHECKOUTS)
        try:
            assert guard.violation(checkout / "pkg" / "__pycache__" / "m.pyc") is not None
            guard.register_agent_checkout(checkout)
            assert guard.violation(checkout / "pkg" / "__pycache__" / "m.pyc") is None
            assert guard.violation(checkout / "pkg" / "__pycache__") is None
            assert guard.violation(checkout / "pkg" / "m.py") is not None  # only bytecode is exempt
            assert guard.violation(root / "profiles" / "coder" / "config.yaml") is not None
        finally:
            guard.AGENT_CHECKOUTS[:] = saved


def _conftest_module():
    return next((m for m in list(sys.modules.values()) if os.path.realpath(getattr(m, "__file__", "") or "")
                 == os.path.realpath(pathlib.Path(__file__).with_name("conftest.py"))), None)


def test_session_registers_the_discovered_agent_checkout():
    agent = getattr(_conftest_module(), "HERMES_AGENT", None)
    if agent is None:
        pytest.skip("no Agent checkout discovered")
    assert os.path.realpath(os.fspath(agent)) in guard.AGENT_CHECKOUTS


def test_a_registered_checkout_never_exempts_a_whole_real_root(tmp_path):
    """A 'checkout' equal to, or above, a protected root must not open bytecode writes across it."""
    with _simulated_real_root(tmp_path) as root:
        saved = list(guard.AGENT_CHECKOUTS)
        try:
            for too_broad in (root, root.parent):
                guard.AGENT_CHECKOUTS[:] = saved
                guard.register_agent_checkout(too_broad)
                assert guard.violation(root / "profiles" / "other" / "__pycache__" / "m.pyc") is not None, too_broad
        finally:
            guard.AGENT_CHECKOUTS[:] = saved


def test_link_real_dirs_mirrors_existing_dirs_into_the_sandbox(tmp_path):
    real, sandbox = tmp_path / "real", tmp_path / "sandbox"
    (real / "cache" / "ms-playwright").mkdir(parents=True)
    (sandbox / "kept").mkdir(parents=True)
    failed = guard.link_real_dirs(sandbox, (
        (real / "cache" / "ms-playwright", ".cache/ms-playwright"),  # parents are created
        (real / "missing", "Library/Caches/ms-playwright"),           # a missing source is skipped
        (real / "cache", "kept"),                                      # an existing entry is kept
    ))
    assert failed == []
    assert os.path.realpath(sandbox / ".cache" / "ms-playwright") == os.path.realpath(real / "cache" / "ms-playwright")
    assert not os.path.lexists(sandbox / "Library")
    assert (sandbox / "kept").is_dir() and not (sandbox / "kept").is_symlink()


def test_session_keeps_playwrights_default_browser_cache():
    """HOME (LOCALAPPDATA on Windows) moves into the sandbox; Playwright's default cache there must be the real one,
    so its own lookup (PLAYWRIGHT_BROWSERS_PATH, npm config, XDG_CACHE_HOME) needs no help."""
    conftest = _conftest_module()
    pairs = getattr(conftest, "_PLAYWRIGHT_CACHES", None)
    assert pairs, "conftest must link Playwright's default caches into the sandbox"
    if getattr(conftest, "_PLAYWRIGHT_FALLBACK", False):  # Windows without symlink privilege
        assert os.path.realpath(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == os.path.realpath(pairs[2][0])
    failed = set(getattr(conftest, "_LINK_FAILED", ()))
    linked = [(src, rel) for src, rel in pairs if src.is_dir() and rel not in failed]
    if not linked:
        pytest.skip("no linkable Playwright browser cache on this machine")
    for src, rel in linked:
        assert os.path.realpath(conftest.SANDBOX_HOME / rel) == os.path.realpath(src), rel


@pytest.mark.parametrize("env, uses_default", [
    ({}, True),
    ({"PLAYWRIGHT_BROWSERS_PATH": ""}, True),
    ({"PLAYWRIGHT_BROWSERS_PATH": "", "npm_config_playwright_browsers_path": "/x"}, True),  # first defined wins
    ({"npm_config_playwright_browsers_path": ""}, True),
    ({"npm_package_config_playwright_browsers_path": "/x"}, False),
    ({"npm_config_playwright_browsers_path": "/x", "npm_package_config_playwright_browsers_path": ""}, False),
    ({"PLAYWRIGHT_BROWSERS_PATH": "0"}, False),  # package-local browsers, not the default cache
])
def test_playwright_default_cache_detection_follows_playwrights_order(env, uses_default):
    assert guard.playwright_uses_default_cache(env) is uses_default


def test_root_of_mirrors_the_agent_layouts():
    base = pathlib.PurePosixPath("/srv/hermes-guard-test")  # synthetic: tmp_path may itself sit under a .hermes dir
    assert guard._root_of(base / ".hermes" / "profiles" / "webui") == base / ".hermes"
    assert guard._root_of(base / "data" / "profiles" / "coder") == base / "data"
    assert guard._root_of(base / "data") == base / "data"


def test_session_protects_the_account_hermes_root():
    roots = os.environ[guard.ENV].split(os.pathsep)
    assert guard.PROTECTED == roots
    pwd = pytest.importorskip("pwd")  # without pwd the account home is the (now sandboxed) HOME
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    assert os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".hermes" + suffix)) in roots


def test_session_does_not_resolve_the_real_root_for_a_temp_home_under_it(monkeypatch):
    """Regression: a test HERMES_HOME in a scratch temp dir under the real ~/.hermes resolved to it."""
    hermes_constants = pytest.importorskip("hermes_constants")
    if not hasattr(hermes_constants, "get_default_hermes_root"):
        pytest.skip("Agent without get_default_hermes_root")
    for real_root in guard.PROTECTED:
        temp_home = os.path.join(real_root, "profiles", "webui", "cache", "scratch", "tmpabc", ".hermes")
        monkeypatch.setenv("HERMES_HOME", temp_home)
        resolved = os.path.realpath(hermes_constants.get_default_hermes_root())
        assert resolved not in guard.PROTECTED, f"HERMES_HOME={temp_home} resolved the real root {resolved}"


def test_windows_native_root_is_protected(tmp_path, monkeypatch):
    """On Windows the Agent's native root is %LOCALAPPDATA%\\hermes; it must be protected like ~/.hermes."""
    appdata = tmp_path / "original localappdata"
    monkeypatch.delenv(guard.ENV)  # restored after the test: the session keeps its recorded roots
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setattr(sys, "platform", "win32")
    roots = guard.real_roots()
    monkeypatch.undo()
    assert os.path.realpath(appdata / "hermes") in roots
    assert guard.violation(appdata / "hermes" / "config.yaml", protected=roots, allowed=[]) is not None


def test_session_sandboxes_the_windows_native_root(monkeypatch):
    """LOCALAPPDATA points into the sandbox home, so the Agent's Windows default cannot be the real root."""
    hermes_constants = pytest.importorskip("hermes_constants")
    sandbox = os.path.realpath(os.environ["HOME"])
    assert os.path.realpath(os.environ.get("LOCALAPPDATA", "")).startswith(sandbox + os.sep)
    platform_default = getattr(hermes_constants, "_platform_default_hermes_home", None)
    if platform_default is None:
        pytest.skip("Agent without _platform_default_hermes_home")
    monkeypatch.setattr(sys, "platform", "win32")
    native = os.path.realpath(platform_default(os.environ, pathlib.Path.home))
    monkeypatch.undo()
    assert native.startswith(sandbox + os.sep) and guard.violation(native) is None


def test_test_server_child_inherits_the_sandbox(test_server):
    """The audit hook is in-process only: children are protected by inheriting the sandboxed homes."""
    environ = pathlib.Path(f"/proc/{test_server.pid}/environ")
    if not environ.exists():
        pytest.skip("reading a child's environment needs /proc")
    child = dict(e.split("=", 1) for e in environ.read_bytes().decode(errors="replace").split("\0") if "=" in e)
    sandbox = os.environ["HOME"]
    assert child.get("HOME") == child.get("HERMES_REAL_HOME") == sandbox
    assert child.get("LOCALAPPDATA") == os.path.join(sandbox, "AppData", "Local")
    assert child.get("HERMES_HOME") == os.environ["HERMES_WEBUI_TEST_STATE_DIR"]
    assert child.get(guard.ENV) == os.environ[guard.ENV]
    for key in ("HOME", "HERMES_REAL_HOME", "LOCALAPPDATA", "HERMES_HOME"):
        assert guard.violation(os.path.join(child[key], "x")) is None, f"child {key} is inside a real Hermes root"
