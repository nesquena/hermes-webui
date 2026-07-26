"""Tests for #2316: Scripts panel — list and raw endpoint for ~/.hermes/scripts/."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

import pytest

from tests.conftest import TEST_STATE_DIR, TEST_BASE

pytestmark = pytest.mark.usefixtures("test_server")
REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")


def _clear_scripts_dir():
    """Clear the scripts directory before test."""
    scripts_dir = TEST_STATE_DIR / "scripts"
    if scripts_dir.is_symlink():
        scripts_dir.unlink(missing_ok=True)
    elif scripts_dir.exists():
        shutil.rmtree(scripts_dir)


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const scan = (i, closer) => {{
    let inSingle = false;
    let inDouble = false;
    let inBacktick = false;
    let inLineComment = false;
    let inBlockComment = false;
    let escape = false;
    let depth = 1;
    for (i += 1; i < src.length; i += 1) {{
      const ch = src[i];
      const next = src[i + 1];
      if (inLineComment) {{
        if (ch === '\\n') inLineComment = false;
        continue;
      }}
      if (inBlockComment) {{
        if (ch === '*' && next === '/') {{
          inBlockComment = false;
          i += 1;
        }}
        continue;
      }}
      if (escape) {{
        escape = false;
        continue;
      }}
      if (inSingle) {{
        if (ch === '\\\\') escape = true;
        else if (ch === "'") inSingle = false;
        continue;
      }}
      if (inDouble) {{
        if (ch === '\\\\') escape = true;
        else if (ch === '"') inDouble = false;
        continue;
      }}
      if (inBacktick) {{
        if (ch === '\\\\') escape = true;
        else if (ch === '`') inBacktick = false;
        continue;
      }}
      if (ch === '/' && next === '/') {{
        inLineComment = true;
        i += 1;
        continue;
      }}
      if (ch === '/' && next === '*') {{
        inBlockComment = true;
        i += 1;
        continue;
      }}
      if (ch === "'") {{
        inSingle = true;
        continue;
      }}
      if (ch === '"') {{
        inDouble = true;
        continue;
      }}
      if (ch === '`') {{
        inBacktick = true;
        continue;
      }}
      if (ch === closer[0]) depth += 1;
      else if (ch === closer[1]) {{
        depth -= 1;
        if (depth === 0) return i;
      }}
    }}
    throw new Error(name + ' scan failed');
  }};
  let paren = src.indexOf('(', start);
  if (paren < 0) throw new Error(name + ' signature not found');
  let bodyStart = scan(paren, '()');
  while (bodyStart < src.length && src[bodyStart] !== '{{') bodyStart += 1;
  if (bodyStart >= src.length) throw new Error(name + ' body not found');
  const i = scan(bodyStart, '{{}}') + 1;
  return src.slice(start, i);
}}
"""


def _run_playwright_probe(script: str, *, width: int = 1280, height: int = 720):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            page.goto(TEST_BASE, wait_until="domcontentloaded")
            page.wait_for_selector("#scriptsList", state="attached", timeout=10000)
            page.wait_for_timeout(500)
            return page.evaluate(script)
        finally:
            browser.close()


def test_scripts_list_empty():
    """GET /api/scripts/list should return empty array if directory doesn't exist."""
    _clear_scripts_dir()
    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())
    assert data["scripts"] == []


def test_scripts_list_iterdir_oserror_returns_empty(monkeypatch, tmp_path):
    """Direct list walk failures should degrade to an empty result, not a 500."""
    import api.routes as routes

    active_home = tmp_path / "active-home"
    scripts_dir = active_home / "scripts"
    scripts_dir.mkdir(parents=True)
    captured = {}

    monkeypatch.setattr(routes, "_hermes_active_home", lambda: active_home)
    monkeypatch.setattr(routes, "open_anchored_fd", lambda root, target, want_dir=False: 99)
    monkeypatch.setattr(routes.os, "fstat", lambda fd: scripts_dir.stat())
    monkeypatch.setattr(
        routes.os,
        "scandir",
        lambda fd: (_ for _ in ()).throw(PermissionError("scripts dir unreadable")),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {"scripts": []},
        "status": 200,
    }


def test_scripts_list_with_python_and_shell():
    """GET /api/scripts/list should return .py and .sh files with docstrings."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create a Python script with a docstring
    py_script = scripts_dir / "hello.py"
    py_script.write_text(
        '"""Say hello to the user."""\nprint("Hello world")\n',
        encoding="utf-8"
    )

    # Create a shell script with leading comments
    sh_script = scripts_dir / "backup.sh"
    sh_script.write_text(
        "#!/bin/bash\n# Backup the project\n# Run this daily\ntar -czf backup.tar.gz .\n",
        encoding="utf-8"
    )

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert len(data["scripts"]) == 2
    scripts_by_name = {s["name"]: s for s in data["scripts"]}

    assert "hello.py" in scripts_by_name
    assert scripts_by_name["hello.py"]["description"] == "Say hello to the user."

    assert "backup.sh" in scripts_by_name
    assert scripts_by_name["backup.sh"]["description"] == "Backup the project Run this daily"


def test_scripts_list_filters_non_script_files():
    """GET /api/scripts/list should ignore non-script file types."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create various files
    (scripts_dir / "script.py").write_text('"""A script."""\npass', encoding="utf-8")
    (scripts_dir / "readme.txt").write_text("Not a script", encoding="utf-8")
    (scripts_dir / "config.json").write_text("{}", encoding="utf-8")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert len(data["scripts"]) == 1
    assert data["scripts"][0]["name"] == "script.py"


def test_scripts_list_skips_symlink_escape():
    """GET /api/scripts/list must not follow a symlinked entry outside scripts/."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    outside = TEST_STATE_DIR / "outside-secret.py"
    outside.write_text('"""Outside."""\npass\n', encoding="utf-8")

    link = scripts_dir / "leak.py"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert data["scripts"] == []


def test_scripts_list_rejects_root_symlink_escape():
    _clear_scripts_dir()
    foreign_dir = TEST_STATE_DIR / "foreign-scripts-root-link"
    if foreign_dir.exists():
        shutil.rmtree(foreign_dir)
    foreign_dir.mkdir(parents=True, exist_ok=True)
    (foreign_dir / "foreign_secret.py").write_text(
        '"""PR3935_ROOT_SYMLINK_ESCAPE"""\nprint("PR3935_ROOT_SYMLINK_ESCAPE")\n',
        encoding="utf-8",
    )

    scripts_link = TEST_STATE_DIR / "scripts"
    try:
        os.symlink(str(foreign_dir), str(scripts_link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert data["scripts"] == []


def test_scripts_list_rejects_scripts_dir_swap_after_resolve(monkeypatch, tmp_path):
    import api.routes as routes

    if os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("anchored scripts-dir swap proof requires dir_fd support")

    active_home = tmp_path / "active-home"
    scripts_dir = active_home / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "visible.py").write_text(
        '"""Inside."""\nprint("inside")\n',
        encoding="utf-8",
    )
    foreign_dir = tmp_path / "foreign-scripts"
    foreign_dir.mkdir()
    (foreign_dir / "foreign_secret.py").write_text(
        '"""PR3935_SCRIPTS_DIR_SWAP"""\nprint("PR3935_SCRIPTS_DIR_SWAP")\n',
        encoding="utf-8",
    )

    original = routes.open_anchored_fd
    swapped = False
    captured = {}

    def swapping_open(root, target, want_dir=False):
        nonlocal swapped
        if not swapped:
            swapped = True
            shutil.rmtree(scripts_dir)
            os.symlink(str(foreign_dir), str(scripts_dir), target_is_directory=True)
        return original(root, target, want_dir=want_dir)

    monkeypatch.setattr(routes, "_hermes_active_home", lambda: active_home)
    monkeypatch.setattr(routes, "open_anchored_fd", swapping_open)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {"scripts": []},
        "status": 200,
    }


def test_scripts_list_skips_leaf_swap_after_resolve(monkeypatch):
    import api.routes as routes

    if os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("anchored leaf-swap proof requires dir_fd support")

    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    target = scripts_dir / "race.py"
    target.write_text('"""Inside."""\npass\n', encoding="utf-8")
    outside = TEST_STATE_DIR / "outside-list-secret.py"
    outside.write_text('"""Outside."""\npass\n', encoding="utf-8")

    try:
        os.symlink(str(outside), str(scripts_dir / "probe.py"))
        (scripts_dir / "probe.py").unlink()
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    original = routes._read_anchored_file_bytes
    swapped = False
    captured = {}

    def swapping_read(
        root, resolved_target, max_bytes=routes.MAX_FILE_BYTES, allow_prefix=False
    ):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.unlink()
            os.symlink(str(outside), str(target))
        return original(root, resolved_target, max_bytes, allow_prefix)

    monkeypatch.setattr(routes, "_read_anchored_file_bytes", swapping_read)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {"scripts": []},
        "status": 200,
    }


def test_scripts_list_allows_regular_active_home(monkeypatch, tmp_path):
    import api.routes as routes

    active_home = tmp_path / "profiles" / "worker"
    scripts_dir = active_home / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "worker.py").write_text(
        '"""Regular active home."""\nprint("ok")\n',
        encoding="utf-8",
    )
    captured = {}

    monkeypatch.setattr(routes, "_hermes_active_home", lambda: active_home)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {
            "scripts": [{"name": "worker.py", "description": "Regular active home."}]
        },
        "status": 200,
    }


def test_scripts_raw_returns_source():
    """GET /api/scripts/raw?path=<name> should return file source."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    content = "#!/bin/bash\necho 'test'\n"
    (scripts_dir / "test.sh").write_text(content, encoding="utf-8")

    url = TEST_BASE + "/api/scripts/raw?path=test.sh"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())

    assert data["name"] == "test.sh"
    assert data["source"] == content


def test_scripts_raw_rejects_nested_script_paths():
    _clear_scripts_dir()
    nested_dir = TEST_STATE_DIR / "scripts" / "private"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "credentials.py").write_text('print("secret")\n', encoding="utf-8")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            TEST_BASE + "/api/scripts/raw?path=private/credentials.py",
            timeout=5,
        )

    assert exc_info.value.code == 404


def test_scripts_raw_rejects_root_symlink_escape():
    _clear_scripts_dir()
    foreign_dir = TEST_STATE_DIR / "foreign-scripts-root-link"
    if foreign_dir.exists():
        shutil.rmtree(foreign_dir)
    foreign_dir.mkdir(parents=True, exist_ok=True)
    (foreign_dir / "foreign_secret.py").write_text(
        'print("PR3935_ROOT_SYMLINK_ESCAPE")\n',
        encoding="utf-8",
    )

    scripts_link = TEST_STATE_DIR / "scripts"
    try:
        os.symlink(str(foreign_dir), str(scripts_link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            TEST_BASE + "/api/scripts/raw?path=foreign_secret.py",
            timeout=5,
        )

    assert exc_info.value.code == 404


def test_scripts_raw_rejects_scripts_dir_swap_after_resolve(monkeypatch, tmp_path):
    import api.routes as routes

    if os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("anchored scripts-dir swap proof requires dir_fd support")

    active_home = tmp_path / "active-home"
    scripts_dir = active_home / "scripts"
    scripts_dir.mkdir(parents=True)
    target = scripts_dir / "visible.py"
    target.write_text("print('inside')\n", encoding="utf-8")
    foreign_dir = tmp_path / "foreign-scripts"
    foreign_dir.mkdir()
    (foreign_dir / "visible.py").write_text(
        "print('PR3935_SCRIPTS_DIR_SWAP')\n",
        encoding="utf-8",
    )

    original = routes._read_anchored_file_bytes
    swapped = False
    failures = []

    def swapping_read(
        root, resolved_target, max_bytes=routes.MAX_FILE_BYTES, allow_prefix=False
    ):
        nonlocal swapped
        if not swapped:
            swapped = True
            shutil.rmtree(scripts_dir)
            os.symlink(str(foreign_dir), str(scripts_dir), target_is_directory=True)
        return original(root, resolved_target, max_bytes, allow_prefix)

    monkeypatch.setattr(routes, "_hermes_active_home", lambda: active_home)
    monkeypatch.setattr(routes, "_read_anchored_file_bytes", swapping_read)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: failures.append((msg, status)),
    )

    routes._handle_scripts_raw(
        object(),
        type("Parsed", (), {"query": "path=visible.py"})(),
    )

    assert failures == [("script not found", 404)]


def test_scripts_raw_rejects_leaf_swap_after_resolve(monkeypatch):
    import api.routes as routes

    if os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("anchored leaf-swap proof requires dir_fd support")

    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    target = scripts_dir / "race.py"
    target.write_text("print('inside')\n", encoding="utf-8")
    outside = TEST_STATE_DIR / "outside-raw-secret.py"
    outside.write_text("print('outside')\n", encoding="utf-8")

    try:
        os.symlink(str(outside), str(scripts_dir / "probe.py"))
        (scripts_dir / "probe.py").unlink()
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    original = routes._read_anchored_file_bytes
    swapped = False
    failures = []

    def swapping_read(
        root, resolved_target, max_bytes=routes.MAX_FILE_BYTES, allow_prefix=False
    ):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.unlink()
            os.symlink(str(outside), str(target))
        return original(root, resolved_target, max_bytes, allow_prefix)

    monkeypatch.setattr(routes, "_read_anchored_file_bytes", swapping_read)
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: failures.append((msg, status)))

    routes._handle_scripts_raw(object(), type("Parsed", (), {"query": "path=race.py"})())

    assert failures == [("script not found", 404)]


def test_scripts_raw_rejects_unsupported_file_types():
    """GET /api/scripts/raw should 400 for files outside the script allowlist."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "config.json").write_text("{}", encoding="utf-8")

    url = TEST_BASE + "/api/scripts/raw?path=config.json"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_path_traversal_blocked():
    """GET /api/scripts/raw?path=../../../etc/passwd should return 400."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    url = TEST_BASE + "/api/scripts/raw?path=../../../etc/passwd"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_missing_path_param():
    """GET /api/scripts/raw without ?path should return 400."""
    _clear_scripts_dir()
    url = TEST_BASE + "/api/scripts/raw"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_nonexistent_file():
    """GET /api/scripts/raw?path=nonexistent should return 404."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    url = TEST_BASE + "/api/scripts/raw?path=nonexistent.py"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 404


def test_scripts_list_returns_sorted_order():
    """GET /api/scripts/list should return scripts in alphabetical order."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create scripts in non-alphabetical order
    for name in ["zebra.sh", "apple.py", "middle.bash"]:
        (scripts_dir / name).write_text("#!/bin/bash\n# Script\n", encoding="utf-8")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    names = [s["name"] for s in data["scripts"]]
    assert names == ["apple.py", "middle.bash", "zebra.sh"]


def test_scripts_resolver_failure_fails_closed(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    global_home = TEST_STATE_DIR / "global-home"
    (global_home / "scripts").mkdir(parents=True, exist_ok=True)
    (global_home / "scripts" / "secret.py").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(global_home))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: (_ for _ in ()).throw(RuntimeError("resolver secret")))
    results = []
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: results.append((msg, status)))

    routes._handle_scripts_list(object())
    routes._handle_scripts_raw(object(), type("Parsed", (), {"query": "path=secret.py"})())

    assert results == [("scripts unavailable", 503), ("scripts unavailable", 503)]


def test_scripts_list_description_is_bounded():
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "long.py").write_text(
        '"""' + "x" * 2000 + '"""\n' + ("# filler\n" * 10000),
        encoding="utf-8",
    )

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert [script["name"] for script in data["scripts"]] == ["long.py"]
    assert len(data["scripts"][0]["description"]) == 512


def test_scripts_list_skips_python_recursion_errors(monkeypatch, tmp_path):
    import api.routes as routes

    active_home = tmp_path / "active-home"
    scripts_dir = active_home / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "deep.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(routes, "_hermes_active_home", lambda: active_home)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )
    monkeypatch.setattr(
        routes,
        "_parse_script_docstring",
        lambda data, ext: (_ for _ in ()).throw(RecursionError("nesting exceeded")),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {"scripts": []},
        "status": 200,
    }


def test_scripts_raw_rejects_oversized_file():
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "large.py").write_bytes(b"x" * 400001)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(TEST_BASE + "/api/scripts/raw?path=large.py", timeout=5)

    assert exc_info.value.code == 413


def test_scripts_raw_skips_symlink_swap_escape():
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    outside = TEST_STATE_DIR / "outside-raw-secret.py"
    outside.write_text("outside", encoding="utf-8")
    link = scripts_dir / "race.py"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(TEST_BASE + "/api/scripts/raw?path=race.py", timeout=5)

    assert exc_info.value.code == 404


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_list_profile_generation_rejects_out_of_order_responses():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _scriptsData = null;
let _profileSwitchGeneration = 0;
let _scriptsRequestId = 0;
let _scriptsRawRequestId = 0;
const S = { activeProfile: 'a' };
const box = { innerHTML: '' };
const renders = [];
const pending = [];
function $(id){ return id === 'scriptsList' ? box : null; }
function esc(value){ return String(value); }
function t(key){ return key; }
function _renderScriptsList(scripts){ renders.push(scripts.map(s => s.name)); }
function api(){ return new Promise(resolve => pending.push(resolve)); }
eval(extractFunc('_invalidateScriptsRequests'));
eval(extractFunc('_tasksOwner'));
eval(extractFunc('_tasksOwns'));
eval(extractFunc('_scriptsOwner'));
eval(extractFunc('_scriptsOwns'));
eval(extractFunc('loadScripts'));
(async () => {
  const first = loadScripts();
  S.activeProfile = 'b';
  _profileSwitchGeneration += 1;
  _invalidateScriptsRequests();
  const second = loadScripts();
  pending[1]({ scripts: [{ name: 'b.py' }] });
  await second;
  pending[0]({ scripts: [{ name: 'a-secret.py' }] });
  await first;
  console.log(JSON.stringify({ data: _scriptsData.map(s => s.name), renders }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = json.loads(_run_node(source))
    assert result == {"data": ["b.py"], "renders": [["b.py"]]}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_raw_profile_generation_rejects_stale_record_commit():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]
  ));
}
function unescapeHtml(value) {
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '\"')
    .replace(/&#39;/g, \"'\")
    .replace(/&amp;/g, '&');
}
class FakeClassList {
  constructor() { this.items = new Set(); }
  add(name) { this.items.add(name); }
  remove(name) { this.items.delete(name); }
  toggle(name) {
    if (this.items.has(name)) { this.items.delete(name); return false; }
    this.items.add(name);
    return true;
  }
  contains(name) { return this.items.has(name); }
}
class FakeElement {
  constructor(kind='div') {
    this.kind = kind;
    this.children = [];
    this.style = {};
    this.listeners = {};
    this.classList = new FakeClassList();
    this._innerHTML = '';
    this._textContent = '';
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
}
let _scriptsData = null;
let _profileSwitchGeneration = 0;
let _scriptsRequestId = 0;
let _scriptsRawRequestId = 0;
const S = { activeProfile: 'a' };
const box = new FakeElement('box');
const document = { createElement(){ return new FakeElement(); } };
const window = { Prism: null };
function $(id){ return id === 'scriptsList' ? box : null; }
function esc(value){ return escapeHtml(value); }
function t(key){
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  if (key === 'loading') return 'Loading...';
  return key;
}
let resolver = null;
async function api(url) {
  if (url !== '/api/scripts/raw?path=a-secret.py') throw new Error('unexpected url: ' + url);
  return new Promise(resolve => {
    resolver = resolve;
  });
}
eval(extractFunc('_invalidateScriptsRequests'));
eval(extractFunc('_tasksOwner'));
eval(extractFunc('_tasksOwns'));
eval(extractFunc('_scriptsOwner'));
eval(extractFunc('_scriptsOwns'));
eval(extractFunc('_renderScriptsList'));
(async () => {
  const stale = { name: 'a-secret.py', description: '' };
  _scriptsData = [stale];
  _renderScriptsList(_scriptsData);
  const card = box.children[0];
  const clickPromise = card.querySelector('.script-header').listeners.click();
  S.activeProfile = 'b';
  _profileSwitchGeneration += 1;
  _invalidateScriptsRequests();
  _scriptsData = [{ name: 'b.py' }];
  resolver({ source: '#!/bin/bash\\necho stolen\\n' });
  await clickPromise;
  console.log(JSON.stringify({
    current: _scriptsData[0].name,
    staleSource: stale.source || null,
    staleLoaded: !!stale._loaded,
    staleText: card.querySelector('.script-source').querySelector('code').textContent,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "current": "b.py",
        "staleSource": None,
        "staleLoaded": False,
        "staleText": "Loading...",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_refresh_reenables_after_stale_raw_click():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]
  ));
}
function unescapeHtml(value) {
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '\"')
    .replace(/&#39;/g, \"'\")
    .replace(/&amp;/g, '&');
}
class FakeClassList {
  constructor() { this.items = new Set(); }
  add(name) { this.items.add(name); }
  remove(name) { this.items.delete(name); }
  toggle(name) {
    if (this.items.has(name)) { this.items.delete(name); return false; }
    this.items.add(name);
    return true;
  }
  contains(name) { return this.items.has(name); }
}
class FakeElement {
  constructor(kind='div') {
    this.kind = kind;
    this.children = [];
    this.style = {};
    this.listeners = {};
    this.classList = new FakeClassList();
    this._innerHTML = '';
    this._textContent = '';
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
}
let _scriptsData = [{ name: 'old.py', description: '' }];
let _profileSwitchGeneration = 0;
let _scriptsRequestId = 0;
let _scriptsRawRequestId = 0;
let _scriptsRefreshOwnerRequestId = 0;
const S = { activeProfile: 'a' };
const box = new FakeElement('box');
const refreshBtn = { style: {}, disabled: false };
const document = { createElement(){ return new FakeElement(); } };
const window = { Prism: null };
let pendingList = null;
let pendingRaw = null;
function $(id){
  return {
    scriptsList: box,
    scriptsRefreshBtn: refreshBtn,
  }[id] || null;
}
function esc(value){ return escapeHtml(value); }
function t(key){
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  if (key === 'loading') return 'Loading...';
  if (key === 'error_prefix') return 'Error: ';
  return key;
}
async function api(url) {
  if (url === '/api/scripts/list') {
    return new Promise(resolve => { pendingList = resolve; });
  }
  if (url === '/api/scripts/raw?path=old.py') {
    return new Promise(resolve => { pendingRaw = resolve; });
  }
  throw new Error('unexpected url: ' + url);
}
eval(extractFunc('_invalidateScriptsRequests'));
eval(extractFunc('_tasksOwner'));
eval(extractFunc('_tasksOwns'));
eval(extractFunc('_scriptsOwner'));
eval(extractFunc('_scriptsOwns'));
eval(extractFunc('_renderScriptsList'));
eval(extractFunc('loadScripts'));
(async () => {
  _renderScriptsList(_scriptsData);
  const staleCard = box.children[0];
  const reloadPromise = loadScripts(true);
  const clickPromise = staleCard.querySelector('.script-header').listeners.click();
  pendingList({ scripts: [{ name: 'fresh.py', description: '' }] });
  await reloadPromise;
  pendingRaw({ source: 'echo old\\n' });
  await clickPromise;
  console.log(JSON.stringify({
    data: _scriptsData.map(s => s.name),
    disabled: refreshBtn.disabled,
    opacity: refreshBtn.style.opacity || '',
    children: box.children.length,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "data": ["fresh.py"],
        "disabled": False,
        "opacity": "",
        "children": 1,
    }


def test_scripts_accessibility_contract_is_complete():
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    assert 'role="tablist"' in html
    assert 'role="tab"' in html
    assert 'handleTasksSubtabKeydown(event)' in html
    assert 'aria-selected="true"' in html
    assert 'role="tabpanel"' in html
    assert "function handleTasksSubtabKeydown" in js
    assert '<button type="button" class="script-header"' in js
    assert 'aria-expanded' in js
    assert 'script-expand' in js
    assert "t('loading')" in js


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tasks_subtab_keyboard_navigation_drives_real_tab_behavior():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _tasksSubtab = 'jobs';
let loadScriptsCalls = 0;
class FakeClassList {
  constructor(active) { this.items = new Set(active ? ['active'] : []); }
  toggle(name, enabled) {
    if (enabled) this.items.add(name);
    else this.items.delete(name);
  }
}
function makeTab(id, active) {
  return {
    id,
    attrs: { 'aria-selected': String(active) },
    classList: new FakeClassList(active),
    tabIndex: active ? 0 : -1,
    focus(){ globalThis.focused.push(this.id); },
    setAttribute(name, value){ this.attrs[name] = String(value); },
  };
}
globalThis.focused = [];
const jobsTab = makeTab('tasksSubtabJobs', true);
const scriptsTab = makeTab('tasksSubtabScripts', false);
const jobsPane = { style: {}, setAttribute(name, value){ this[name] = String(value); } };
const scriptsPane = { style: { display: 'none' }, setAttribute(name, value){ this[name] = String(value); } };
const jobsActions = { style: {} };
const scriptsActions = { style: { display: 'none' } };
const document = {
  querySelectorAll(selector){
    if (selector === '.tasks-subtab') return [jobsTab, scriptsTab];
    return [];
  }
};
function $(id){
  return {
    tasksJobsPane: jobsPane,
    tasksScriptsPane: scriptsPane,
    tasksJobActions: jobsActions,
    tasksScriptActions: scriptsActions,
  }[id] || null;
}
function _syncTaskDetailEmptyState(){}
async function loadScripts(){ loadScriptsCalls += 1; }
async function loadCrons(){}
eval(extractFunc('_ensureTasksSubtabLoaded'));
eval(extractFunc('switchTasksSubtab'));
eval(extractFunc('handleTasksSubtabKeydown'));
function press(currentTarget, key) {
  const event = {
    key,
    currentTarget,
    prevented: false,
    preventDefault(){ this.prevented = true; },
  };
  handleTasksSubtabKeydown(event);
  return event.prevented;
}
switchTasksSubtab('jobs');
const endPrevented = press(jobsTab, 'End');
const homePrevented = press(scriptsTab, 'Home');
const rightPrevented = press(jobsTab, 'ArrowRight');
console.log(JSON.stringify({
  endPrevented,
  homePrevented,
  rightPrevented,
  focused,
  loadScriptsCalls,
  jobsSelected: jobsTab.attrs['aria-selected'],
  scriptsSelected: scriptsTab.attrs['aria-selected'],
  jobsTabIndex: jobsTab.tabIndex,
  scriptsTabIndex: scriptsTab.tabIndex,
  jobsPaneDisplay: jobsPane.style.display || '',
  scriptsPaneDisplay: scriptsPane.style.display || '',
}));
"""
    assert json.loads(_run_node(source)) == {
        "endPrevented": True,
        "homePrevented": True,
        "rightPrevented": True,
        "focused": ["tasksSubtabScripts", "tasksSubtabJobs", "tasksSubtabScripts"],
        "loadScriptsCalls": 2,
        "jobsSelected": "false",
        "scriptsSelected": "true",
        "jobsTabIndex": -1,
        "scriptsTabIndex": 0,
        "jobsPaneDisplay": "none",
        "scriptsPaneDisplay": "",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_profile_switch_reloads_visible_scripts_owner():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _profileSwitchGeneration = 0;
let _scriptsData = [{ name: 'old.py' }];
let _scriptsRequestId = 0;
let _scriptsRawRequestId = 0;
let _cronsRequestId = 0;
let _cronList = [{ id: 'old-job' }];
let _currentPanel = 'tasks';
let _tasksSubtab = 'scripts';
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 4;
let _cronPreFormDetail = { id: 'old-job' };
let _editingCronId = 'old-job';
let _cronIsDuplicate = true;
let invalidations = 0;
let reloads = [];
const S = { activeProfile: 'old', session: null, messages: [] };
const window = {};
const cronList = { children: [{}], replaceChildren(){ this.children = []; } };
const cronRefreshBtn = { style: { opacity: '0.5' }, disabled: true };
const scriptsList = { children: [{}], replaceChildren(){ this.children = []; } };
const scriptsRefreshBtn = { style: { opacity: '0.5' }, disabled: true };
function $(id){
  return {
    cronList,
    cronRefreshBtn,
    scriptsList,
    scriptsRefreshBtn,
  }[id] || null;
}
function _clearCronDetail(){}
function _invalidateScriptsRequests(){
  invalidations += 1;
  _scriptsData = null;
  _scriptsRequestId += 1;
  _scriptsRawRequestId += 1;
}
async function api(){ throw new Error('switch rejected'); }
function showToast(){}
function t(key){ return key; }
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function bumpWorkspaceTreeGen(){}
function _refreshProfileSwitchBackground(){}
function renderSessionListFromCache(){}
async function loadScripts(force){ reloads.push(force); }
async function loadCrons(){}
eval(extractFunc('_ensureTasksSubtabLoaded'));
eval(extractFunc('_resetTasksForProfileTransition'));
eval(extractFunc('switchToProfile'));
(async () => {
  await switchToProfile('new');
  console.log(JSON.stringify({ profile: S.activeProfile, invalidations, reloads }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "profile": "old", "invalidations": 1, "reloads": [None]
    }


@pytest.mark.parametrize("width,height", [(1280, 720), (480, 320)])
def test_tasks_panes_scroll_in_chromium(width, height):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(TEST_BASE, wait_until="domcontentloaded")
        page.wait_for_selector("body", timeout=10000)
        result = page.evaluate("""
          () => {
            const panel = document.querySelector('#panelTasks');
            panel.style.cssText = 'display:flex;position:fixed;inset:0;height:' + window.innerHeight + 'px;width:100%;';
            const checks = [];
            for (const [paneId, listId] of [['tasksJobsPane', 'cronList'], ['tasksScriptsPane', 'scriptsList']]) {
              const pane = document.querySelector('#' + paneId);
              const list = document.querySelector('#' + listId);
              pane.style.display = 'flex';
              list.innerHTML = Array.from({length: 80}, (_, i) => '<div style="height:24px">row ' + i + '</div>').join('');
              const before = list.scrollTop;
              list.scrollTop = list.scrollHeight;
              const paneRect = pane.getBoundingClientRect();
              const listRect = list.getBoundingClientRect();
              checks.push({
                scrollable: list.scrollHeight > list.clientHeight,
                overflow: getComputedStyle(list).overflowY,
                moved: list.scrollTop > before,
                contained: listRect.bottom <= paneRect.bottom + 1,
              });
            }
            return checks;
          }
        """)
        browser.close()

    assert result == [
        {"scrollable": True, "overflow": "auto", "moved": True, "contained": True},
        {"scrollable": True, "overflow": "auto", "moved": True, "contained": True},
    ]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_switch_to_profile_clears_scripts_cache_before_panel_reload():
    """Profile switch must retire Tasks state before the panel reload hook runs."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _profileSwitchGeneration = 0;
let _scriptsData = ['stale'];
let _skillsData = ['old'];
let _workspaceList = ['old'];
let _showAllProfiles = true;
let _scriptsRequestId = 0;
let _scriptsRawRequestId = 0;
let _cronsRequestId = 0;
let _cronList = ['stale-job'];
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 3;
let _cronPreFormDetail = { id: 'stale-job' };
let _editingCronId = 'stale-job';
let _cronIsDuplicate = true;
const localStorage = { removed: [], removeItem(key){ this.removed.push(key); } };
const window = {};
const S = { activeProfile: 'default', session: null, messages: [] };
const panelLoads = [];
const cronList = { children: [{}], replaceChildren(){ this.children = []; } };
const cronRefreshBtn = { style: { opacity: '0.5' }, disabled: true };
const scriptsList = { children: [{}], replaceChildren(){ this.children = []; } };
const scriptsRefreshBtn = { style: { opacity: '0.5' }, disabled: true };
function $(id){
  return {
    cronList,
    cronRefreshBtn,
    scriptsList,
    scriptsRefreshBtn,
  }[id] || null;
}
async function api(url, opts){
  if (url !== '/api/profile/switch') throw new Error('unexpected api: ' + url);
  return { active: 'work', is_default: false };
}
async function renderSessionList(){}
function syncTopbar(){}
function loadDir(){ return Promise.resolve(); }
function showToast(){}
function t(key){ return key; }
function _clearCronDetail(){}
function _invalidateScriptsRequests(){
  _scriptsRequestId += 1;
  _scriptsRawRequestId += 1;
  _scriptsData = null;
}
async function _profileSwitchPanelLoad(){ panelLoads.push(_scriptsData); }
function _refreshProfileSwitchBackground(){}
function animateNextSessionListRefresh(){}
eval(extractFunc('_resetTasksForProfileTransition'));
eval(extractFunc('switchToProfile'));
(async () => {
  await switchToProfile('work');
  console.log(JSON.stringify({
    activeProfile: S.activeProfile,
    scriptsData: _scriptsData,
    switchGeneration: _profileSwitchGeneration,
    cronList: _cronList,
    cards: scriptsList.children.length,
    cronCards: cronList.children.length,
    refreshDisabled: scriptsRefreshBtn.disabled,
    refreshOpacity: scriptsRefreshBtn.style.opacity,
    panelLoads,
    removed: localStorage.removed,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = json.loads(_run_node(source))
    assert result["activeProfile"] == "work"
    assert result["scriptsData"] is None
    assert result["switchGeneration"] == 1
    assert result["cronList"] is None
    assert result["cards"] == 0
    assert result["cronCards"] == 0
    assert result["refreshDisabled"] is False
    assert result["refreshOpacity"] == ""
    assert result["panelLoads"] == [None]
    assert result["removed"] == ["hermes-webui-model"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_load_profile_switch_delegates_to_canonical_transaction():
    """Session-load profile changes must use the canonical switch transaction."""
    js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _profileSwitchOpeningExistingSession = false;
const S = { activeProfile: 'a' };
const calls = [];
async function switchToProfile(name){
  calls.push({ name, opening: _profileSwitchOpeningExistingSession });
  S.activeProfile = name;
  return true;
}
eval(extractFunc('_switchProfileForSessionLoad'));
(async () => {
  await _switchProfileForSessionLoad('b');
  console.log(JSON.stringify({ calls, guard: _profileSwitchOpeningExistingSession, profile: S.activeProfile }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "calls": [{"name": "b", "opening": True}],
        "guard": False,
        "profile": "b",
    }


def test_session_load_profile_switch_clears_scripts_dom_before_destination_render():
    """The real session-load ingress must retire prior-profile Scripts DOM before B renders."""
    result = _run_playwright_probe(
        """
        async () => {
          const tick = () => new Promise(resolve => setTimeout(resolve, 0));
          const settle = async (cycles = 4) => {
            for (let i = 0; i < cycles; i += 1) await tick();
          };
          const deferred = () => {
            let resolve;
            let reject;
            const promise = new Promise((res, rej) => {
              resolve = res;
              reject = rej;
            });
            return { promise, resolve, reject };
          };

          const rawA = deferred();
          const listB = deferred();
          const calls = [];
          const scriptsList = document.querySelector('#scriptsList');
          const refreshBtn = document.querySelector('#scriptsRefreshBtn');

          window.api = async (url, opts) => {
            calls.push({ url, profile: S.activeProfile });
            if (url === '/api/profile/switch') {
              const body = JSON.parse(opts.body);
              return { active: body.name, is_default: false };
            }
            if (url === '/api/scripts/list') {
              if (S.activeProfile === 'b') return listB.promise;
              throw new Error('unexpected list profile ' + S.activeProfile);
            }
            if (url === '/api/scripts/raw?path=a.py') return rawA.promise;
            throw new Error('unexpected api ' + url);
          };
          window.renderSessionList = async () => {};
          window.loadDir = async () => {};
          window.showToast = () => {};
          window.t = key => key;
          window.syncTopbar = () => {};
          window._refreshProfileSwitchBackground = () => {};
          window.animateNextSessionListRefresh = () => {};
          window.startGatewaySSE = () => {};
          window._resetCronUnreadForProfileSwitch = () => {};
          window._clearPersistedModelState = () => {};
          window.refreshProfileTransitionReasoningChip = () => {};
          window._setProfileSwitchListEmbargo = () => {};
          window.showSessionListSkeleton = () => {};
          window.bumpWorkspaceTreeGen = () => {};
          window.showWorkspaceTreeSkeleton = () => {};
          window.clearWorkspaceTreeSkeleton = () => {};
          window.renderSessionListFromCache = () => {};
          window._openProfileSwitchSessionBrowser = () => {};
          window.applyBotName = () => {};
          window.Prism = null;

          S.activeProfile = 'a';
          S.activeProfileIsDefault = false;
          S.session = null;
          S.messages = [];
          _workspacePanelMode = 'closed';
          _currentPanel = 'tasks';
          _tasksSubtab = 'scripts';
          _profileSwitchGeneration = 0;
          _showAllProfiles = true;
          _scriptsRequestId = 0;
          _scriptsRawRequestId = 0;
          _scriptsData = [{ name: 'a.py', description: 'Alpha script' }];

          scriptsList.replaceChildren();
          refreshBtn.style.opacity = '0.5';
          refreshBtn.disabled = true;
          _renderScriptsList(_scriptsData, _scriptsOwner());

          const firstHeader = scriptsList.querySelector('.script-header');
          firstHeader.click();
          await settle();
          rawA.resolve({ source: 'alpha source' });
          await settle();

          const beforeSwitch = {
            cards: scriptsList.children.length,
            expanded: firstHeader.getAttribute('aria-expanded'),
            source: scriptsList.querySelector('.script-source code').textContent,
          };

          const switchPromise = _switchProfileForSessionLoad('b');
          await settle();

          const mid = {
            profile: S.activeProfile,
            generation: _profileSwitchGeneration,
            scriptsDataCleared: _scriptsData === null,
            cards: scriptsList.children.length,
            hasAlphaSource: scriptsList.textContent.includes('alpha source'),
            expanded: scriptsList.querySelector('.script-header')?.getAttribute('aria-expanded') || null,
            refreshDisabled: refreshBtn.disabled,
            refreshOpacity: refreshBtn.style.opacity,
          };

          listB.resolve({ scripts: [{ name: 'b.py', description: 'Beta script' }] });
          await switchPromise;
          await settle();

          return {
            beforeSwitch,
            mid,
            final: {
              profile: S.activeProfile,
              generation: _profileSwitchGeneration,
              cards: scriptsList.children.length,
              name: scriptsList.querySelector('.script-name')?.textContent || null,
              text: scriptsList.textContent,
              refreshDisabled: refreshBtn.disabled,
              refreshOpacity: refreshBtn.style.opacity,
            },
            calls,
          };
        }
        """
    )

    assert result["beforeSwitch"] == {
        "cards": 1,
        "expanded": "true",
        "source": "alpha source",
    }
    assert result["mid"] == {
        "profile": "b",
        "generation": 1,
        "scriptsDataCleared": True,
        "cards": 0,
        "hasAlphaSource": False,
        "expanded": None,
        "refreshDisabled": False,
        "refreshOpacity": "",
    }
    assert result["final"]["profile"] == "b"
    assert result["final"]["generation"] == 1
    assert result["final"]["cards"] == 1
    assert result["final"]["name"] == "b"
    assert "Beta script" in result["final"]["text"]
    assert "alpha source" not in result["final"]["text"]
    assert result["final"]["refreshDisabled"] is False
    assert result["final"]["refreshOpacity"] == ""
    relevant_calls = [
        call["url"]
        for call in result["calls"]
        if call["url"] in {
            "/api/scripts/raw?path=a.py",
            "/api/profile/switch",
            "/api/scripts/list",
        }
    ]
    assert relevant_calls == [
        "/api/scripts/raw?path=a.py",
        "/api/profile/switch",
        "/api/scripts/list",
    ]


def test_session_load_profile_switch_retires_first_profile_owner_after_return():
    """A deferred first-A response cannot replace the later A ownership period."""
    result = _run_playwright_probe(
        """
        async () => {
          const tick = () => new Promise(resolve => setTimeout(resolve, 0));
          const settle = async (cycles = 4) => {
            for (let i = 0; i < cycles; i += 1) await tick();
          };
          const deferred = () => {
            let resolve;
            let reject;
            const promise = new Promise((res, rej) => {
              resolve = res;
              reject = rej;
            });
            return { promise, resolve, reject };
          };

          const rawFirstA = deferred();
          const rawSecondA = deferred();
          const listB = deferred();
          const listSecondA = deferred();
          let aRawCalls = 0;
          const scriptsList = document.querySelector('#scriptsList');
          const refreshBtn = document.querySelector('#scriptsRefreshBtn');

          window.api = async (url, opts) => {
            if (url === '/api/profile/switch') {
              const body = JSON.parse(opts.body);
              return { active: body.name, is_default: false };
            }
            if (url === '/api/scripts/list') {
              if (S.activeProfile === 'b') return listB.promise;
              if (S.activeProfile === 'a') return listSecondA.promise;
              throw new Error('unexpected list profile ' + S.activeProfile);
            }
            if (url === '/api/scripts/raw?path=a.py') {
              aRawCalls += 1;
              return aRawCalls === 1 ? rawFirstA.promise : rawSecondA.promise;
            }
            throw new Error('unexpected api ' + url);
          };
          window.renderSessionList = async () => {};
          window.loadDir = async () => {};
          window.showToast = () => {};
          window.t = key => key;
          window.syncTopbar = () => {};
          window._refreshProfileSwitchBackground = () => {};
          window.animateNextSessionListRefresh = () => {};
          window.startGatewaySSE = () => {};
          window._resetCronUnreadForProfileSwitch = () => {};
          window._clearPersistedModelState = () => {};
          window.refreshProfileTransitionReasoningChip = () => {};
          window._setProfileSwitchListEmbargo = () => {};
          window.showSessionListSkeleton = () => {};
          window.bumpWorkspaceTreeGen = () => {};
          window.showWorkspaceTreeSkeleton = () => {};
          window.clearWorkspaceTreeSkeleton = () => {};
          window.renderSessionListFromCache = () => {};
          window._openProfileSwitchSessionBrowser = () => {};
          window.applyBotName = () => {};
          window.Prism = null;

          S.activeProfile = 'a';
          S.activeProfileIsDefault = false;
          S.session = null;
          S.messages = [];
          _workspacePanelMode = 'closed';
          _currentPanel = 'tasks';
          _tasksSubtab = 'scripts';
          _profileSwitchGeneration = 0;
          _showAllProfiles = true;
          _scriptsRequestId = 0;
          _scriptsRawRequestId = 0;
          _scriptsData = [{ name: 'a.py', description: 'Alpha script' }];

          scriptsList.replaceChildren();
          refreshBtn.style.opacity = '';
          refreshBtn.disabled = false;
          _renderScriptsList(_scriptsData, _scriptsOwner());

          scriptsList.querySelector('.script-header').click();
          await settle();

          const switchToB = _switchProfileForSessionLoad('b');
          await settle();
          listB.resolve({ scripts: [{ name: 'b.py', description: 'Beta script' }] });
          await switchToB;
          await settle();

          const afterB = {
            profile: S.activeProfile,
            generation: _profileSwitchGeneration,
            text: scriptsList.textContent,
          };

          const switchBackToA = _switchProfileForSessionLoad('a');
          await settle();
          listSecondA.resolve({ scripts: [{ name: 'a.py', description: 'Alpha current' }] });
          await switchBackToA;
          await settle();

          const currentHeader = scriptsList.querySelector('.script-header');
          currentHeader.click();
          await settle();
          rawSecondA.resolve({ source: 'new A source' });
          await settle();

          const beforeStale = {
            profile: S.activeProfile,
            generation: _profileSwitchGeneration,
            cards: scriptsList.children.length,
            expanded: currentHeader.getAttribute('aria-expanded'),
            source: scriptsList.querySelector('.script-source code').textContent,
            cachedSource: _scriptsData[0].source,
          };

          rawFirstA.resolve({ source: 'stale first A source' });
          await settle();

          return {
            afterB,
            beforeStale,
            afterStale: {
              profile: S.activeProfile,
              generation: _profileSwitchGeneration,
              cards: scriptsList.children.length,
              expanded: scriptsList.querySelector('.script-header').getAttribute('aria-expanded'),
              source: scriptsList.querySelector('.script-source code').textContent,
              cachedSource: _scriptsData[0].source,
              name: scriptsList.querySelector('.script-name').textContent,
              hasNewSource: scriptsList.textContent.includes('new A source'),
              hasStaleSource: scriptsList.textContent.includes('stale first A source'),
            },
            aRawCalls,
          };
        }
        """
    )

    assert result["afterB"]["profile"] == "b"
    assert result["afterB"]["generation"] == 1
    assert "Beta script" in result["afterB"]["text"]
    assert result["beforeStale"] == {
        "profile": "a",
        "generation": 2,
        "cards": 1,
        "expanded": "true",
        "source": "new A source",
        "cachedSource": "new A source",
    }
    assert result["afterStale"] == {
        "profile": "a",
        "generation": 2,
        "cards": 1,
        "expanded": "true",
        "source": "new A source",
        "cachedSource": "new A source",
        "name": "a",
        "hasNewSource": True,
        "hasStaleSource": False,
    }
    assert result["aRawCalls"] == 2


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_panel_load_routes_tasks_reload_by_active_subtab():
    """Tasks panel reload should delegate to the active subtab consumer."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _currentPanel = 'tasks';
let _tasksSubtab = 'scripts';
const calls = [];
async function loadSkills(){ calls.push('skills'); }
async function loadMemory(){ calls.push('memory'); }
async function loadScripts(){ calls.push('scripts'); }
async function loadCrons(animate, options){ calls.push(['crons', animate, !!(options && options.allowCache)]); }
async function loadKanban(){ calls.push('kanban'); }
async function loadProfilesPanel(){ calls.push('profiles'); }
async function loadWorkspacesPanel(){ calls.push('workspaces'); }
eval(extractFunc('_ensureTasksSubtabLoaded'));
eval(extractFunc('_profileSwitchPanelLoad'));
(async () => {
  await _profileSwitchPanelLoad();
  _tasksSubtab = 'jobs';
  await _profileSwitchPanelLoad();
  console.log(JSON.stringify(calls));
})().catch(err => { console.error(err); process.exit(1); });
"""
    calls = json.loads(_run_node(source))
    assert calls == ["scripts", ["crons", False, False]]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_subtab_uses_scripts_aware_detail_empty_copy():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _tasksSubtab = 'jobs';
const title = { textContent: '', attrs: {}, setAttribute(name, value){ this.attrs[name] = String(value); } };
const sub = { textContent: '', attrs: {}, setAttribute(name, value){ this.attrs[name] = String(value); } };
const empty = {
  querySelector(selector){
    if (selector === '.main-view-empty-title') return title;
    if (selector === '.main-view-empty-sub') return sub;
    return null;
  }
};
function $(id){ return id === 'taskDetailEmpty' ? empty : null; }
function t(key){
  return {
    tasks_empty_title: 'Select a scheduled job',
    tasks_empty_sub: 'Pick a job from the sidebar to view its details and runs, or create a new one.',
    tasks_scripts_empty_title: 'Browse scripts in the sidebar',
    tasks_scripts_empty_sub: 'Expand a script in the sidebar to view its description and source.',
  }[key] || key;
}
eval(extractFunc('_syncTaskDetailEmptyState'));
_syncTaskDetailEmptyState();
const jobs = {
  title: title.textContent,
  sub: sub.textContent,
  titleKey: title.attrs['data-i18n'],
  subKey: sub.attrs['data-i18n'],
};
_tasksSubtab = 'scripts';
_syncTaskDetailEmptyState();
console.log(JSON.stringify({
  jobs,
  scripts: {
    title: title.textContent,
    sub: sub.textContent,
    titleKey: title.attrs['data-i18n'],
    subKey: sub.attrs['data-i18n'],
  }
}));
"""
    result = json.loads(_run_node(source))

    assert result["jobs"] == {
        "title": "Select a scheduled job",
        "sub": "Pick a job from the sidebar to view its details and runs, or create a new one.",
        "titleKey": "tasks_empty_title",
        "subKey": "tasks_empty_sub",
    }
    assert result["scripts"] == {
        "title": "Browse scripts in the sidebar",
        "sub": "Expand a script in the sidebar to view its description and source.",
        "titleKey": "tasks_scripts_empty_title",
        "subKey": "tasks_scripts_empty_sub",
    }


def test_tasks_tablist_aria_label_and_touch_target_rule_are_present():
    index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'data-i18n-aria-label="tasks_views_label"' in index_html
    assert "@media (pointer: coarse), (max-width: 640px)" in style_css
    assert ".tasks-subtab{min-height:44px" in style_css


def test_scripts_description_row_stays_inside_header_button():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    header_open = js.index('<button type="button" class="script-header"')
    desc = js.index("${s.description ? `<span class=\"script-desc\">${esc(s.description)}</span>` : ''}")
    header_close = js.index("</button>", header_open)
    source = js.index('<div class="script-source"', header_open)

    assert header_open < desc < header_close < source


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_panel_persists_loaded_source_across_rerender():
    """Loaded script source should be cached on the record and reused after rerender."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]
  ));
}
function unescapeHtml(value) {
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '\"')
    .replace(/&#39;/g, \"'\")
    .replace(/&amp;/g, '&');
}
class FakeClassList {
  constructor() { this.items = new Set(); }
  add(name) { this.items.add(name); }
  remove(name) { this.items.delete(name); }
  toggle(name) {
    if (this.items.has(name)) { this.items.delete(name); return false; }
    this.items.add(name);
    return true;
  }
  contains(name) { return this.items.has(name); }
}
class FakeElement {
  constructor(kind='div') {
    this.kind = kind;
    this.children = [];
    this.style = {};
    this.listeners = {};
    this.classList = new FakeClassList();
    this._innerHTML = '';
    this._textContent = '';
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
}
const box = new FakeElement('box');
const document = { createElement(){ return new FakeElement(); } };
const window = { Prism: null };
let _scriptsRawRequestId = 0;
function $(id){ return id === 'scriptsList' ? box : null; }
function esc(value){ return escapeHtml(value); }
function t(key){
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  return key;
}
let apiCalls = 0;
async function api(url) {
  apiCalls += 1;
  if (url !== '/api/scripts/raw?path=test.sh') throw new Error('unexpected url: ' + url);
  return { source: '#!/bin/bash\\necho test\\n' };
}
eval(extractFunc('_renderScriptsList'));
(async () => {
  const scripts = [{ name: 'test.sh', description: '' }];
  _renderScriptsList(scripts);
  const first = box.children[0];
  await first.querySelector('.script-header').listeners.click();
  _renderScriptsList(scripts);
  const second = box.children[0];
  await second.querySelector('.script-header').listeners.click();
  console.log(JSON.stringify({
    apiCalls,
    cachedSource: scripts[0].source,
    rerenderedSource: second.querySelector('.script-source').querySelector('code').textContent,
    loaded: scripts[0]._loaded,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = json.loads(_run_node(source))
    assert result["apiCalls"] == 1
    assert result["cachedSource"] == "#!/bin/bash\necho test\n"
    assert result["rerenderedSource"] == "#!/bin/bash\necho test\n"
    assert result["loaded"] is True

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_panel_keeps_source_hidden_if_card_collapses_before_fetch_settles():
    """Late async source loads must honor the card's current expansion state."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = f"""{_extract_func_script(js)}
function escapeHtml(value) {{
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => (
    {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[ch]
  ));
}}
function unescapeHtml(value) {{
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '\"')
    .replace(/&#39;/g, \"'\")
    .replace(/&amp;/g, '&');
}}
class FakeClassList {{
  constructor() {{ this.items = new Set(); }}
  add(name) {{ this.items.add(name); }}
  remove(name) {{ this.items.delete(name); }}
  toggle(name) {{
    if (this.items.has(name)) {{ this.items.delete(name); return false; }}
    this.items.add(name);
    return true;
  }}
  contains(name) {{ return this.items.has(name); }}
}}
class FakeElement {{
  constructor(kind='div') {{
    this.kind = kind;
    this.children = [];
    this.style = {{}};
    this.listeners = {{}};
    this.classList = new FakeClassList();
    this._innerHTML = '';
    this._textContent = '';
  }}
  appendChild(child) {{
    this.children.push(child);
    return child;
  }}
  addEventListener(type, handler) {{
    this.listeners[type] = handler;
  }}
  setAttribute(name, value) {{
    this[name] = String(value);
  }}
  querySelector(selector) {{
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }}
  set innerHTML(html) {{
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {{
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }}
  }}
  get innerHTML() {{ return this._innerHTML; }}
  set textContent(value) {{ this._textContent = String(value); }}
  get textContent() {{ return this._textContent; }}
}}
const box = new FakeElement('box');
const document = {{ createElement(){{ return new FakeElement(); }} }};
const window = {{ Prism: null }};
let _scriptsRawRequestId = 0;
function $(id){{ return id === 'scriptsList' ? box : null; }}
function esc(value){{ return escapeHtml(value); }}
function t(key){{
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  return key;
}}
let resolver = null;
async function api(url) {{
  if (url !== '/api/scripts/raw?path=test.sh') throw new Error('unexpected url: ' + url);
  return new Promise(resolve => {{
    resolver = resolve;
  }});
}}
eval(extractFunc('_renderScriptsList'));
(async () => {{
  const scripts = [{{ name: 'test.sh', description: '' }}];
  _renderScriptsList(scripts);
  const card = box.children[0];
  const clickPromise = card.querySelector('.script-header').listeners.click();
  card.querySelector('.script-header').listeners.click();
  resolver({{ source: '#!/bin/bash\\necho test\\n' }});
  await clickPromise;
  console.log(JSON.stringify({{
    display: card.querySelector('.script-source').style.display,
    cachedSource: scripts[0].source,
    loaded: scripts[0]._loaded,
  }}));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    result = json.loads(_run_node(source))
    assert result["display"] == "none"
    assert result["cachedSource"] == "#!/bin/bash\necho test\n"
    assert result["loaded"] is True
