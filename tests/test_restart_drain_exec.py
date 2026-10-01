"""The replacement image must admit work even though exec preserves its PID."""
import os
from pathlib import Path
import subprocess
import sys


def test_real_posix_exec_retires_previous_image_drain(tmp_path):
    if os.name != 'posix':
        import pytest
        pytest.skip('POSIX exec lifecycle')
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'),
               HERMES_WEBUI_STATE_DIR=str(tmp_path / 'state'),
               HERMES_WEBUI_RESTART_DRAIN_DIR=str(tmp_path / 'drain'))
    child = '''
import os, sys
from api import config
if len(sys.argv) == 1:
    config.enter_restart_drain('test-exec')
    assert config.restart_drain_active()
    os.execv(sys.executable, [sys.executable, '-c', sys.argv[0], str(os.getpid())])
assert os.getpid() == int(sys.argv[1])
config.register_active_run('replacement')
assert 'replacement' in config.ACTIVE_RUNS
config.unregister_active_run('replacement')
assert not config.restart_drain_active()
print('same-pid replacement admitted')
'''
    # Keep the source in argv[0] so the second image executes identical code.
    launch = 'import sys; source=sys.argv[1]; sys.argv=[source]; exec(source)'
    result = subprocess.run([sys.executable, '-c', launch, child],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'same-pid replacement admitted' in result.stdout
