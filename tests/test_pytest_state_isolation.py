"""
Regression tests for pytest-process state isolation.

Some tests import api.config/api.models during collection and directly write
sessions from the pytest process. conftest must publish the test state env vars
before those imports, not only for the server subprocess.
"""

from pathlib import Path


def test_api_config_uses_pytest_state_dir():
    import api.config as config
    from tests.conftest import TEST_STATE_DIR

    test_state_dir = TEST_STATE_DIR.resolve()
    production_state_dir = (Path.home() / ".hermes" / "webui").resolve()

    assert config.STATE_DIR == test_state_dir
    assert config.SESSION_DIR == test_state_dir / "sessions"
    assert config.STATE_DIR != production_state_dir
    assert production_state_dir not in config.SESSION_DIR.resolve().parents
