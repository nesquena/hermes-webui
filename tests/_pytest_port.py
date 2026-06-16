"""
Shared test server constants for use in individual test files.

Instead of hardcoding ``BASE = "http://127.0.0.1:8788"`` in every test file,
import from here so the port and state dir are always consistent with
what conftest.py computed for this worktree.

Usage::

    from tests._pytest_port import BASE

conftest.py publishes ``HERMES_WEBUI_TEST_PORT`` and
``HERMES_WEBUI_TEST_STATE_DIR`` to ``os.environ`` at module level
(before any test file is imported), so this module always reads the
correct values.  The auto-derivation fallback matches conftest's logic
exactly, so standalone imports also work correctly.
"""
import hashlib
import os
import pathlib

def _auto_test_port(repo_root: pathlib.Path) -> int:
    h = int(hashlib.md5(str(repo_root).encode()).hexdigest(), 16)
    return 20000 + (h % 10000)

def _auto_state_dir_name(repo_root: pathlib.Path) -> str:
    h = hashlib.md5(str(repo_root).encode()).hexdigest()[:8]
    return f"webui-test-{h}"

_TESTS_DIR   = pathlib.Path(__file__).parent.resolve()
_REPO_ROOT   = _TESTS_DIR.parent.resolve()

TEST_PORT = int(os.environ.get('HERMES_WEBUI_TEST_PORT',
                               str(_auto_test_port(_REPO_ROOT))))
BASE = f"http://127.0.0.1:{TEST_PORT}"

# Test state dir: prefer the value conftest.py published to the environment.
# The standalone fallback anchors under the OS temp dir (NOT ~/.hermes) so test
# state is never created inside the production Hermes home — matching conftest's
# hard-isolation default. (See conftest.py TEST_STATE_DIR.)
import tempfile as _tempfile
_TEST_STATE_ROOT = pathlib.Path(
    os.environ.get('HERMES_WEBUI_TEST_STATE_ROOT', _tempfile.gettempdir())
) / 'hermes-webui-tests'
TEST_STATE_DIR = pathlib.Path(os.environ.get(
    'HERMES_WEBUI_TEST_STATE_DIR',
    str(_TEST_STATE_ROOT / _auto_state_dir_name(_REPO_ROOT))
))

# Default model injected by conftest — tests that mutate the default model
# must restore to this value so later tests see a consistent baseline.
TEST_DEFAULT_MODEL = os.environ.get('HERMES_WEBUI_DEFAULT_MODEL', 'openai/gpt-5.4-mini')
