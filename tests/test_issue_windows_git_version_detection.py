"""Windows parallel to issue #5175 (macOS launchd git-on-PATH miss).

When the source WebUI server is launched from a Python environment whose PATH
does not include git (e.g. a venv on Windows, mirroring the macOS launchd case),
``shutil.which('git')`` returns None. Before this fix ``_resolve_git_executable``
had only a darwin fallback, so on Windows it returned None -> ``git describe``
never ran -> ``WEBUI_VERSION`` degraded to ``'unknown'`` -> the ``?v=<stamp>``
static-asset cache key froze, so browsers kept serving stale cached JS/CSS even
after the server restarted with fixed code.

The fix adds a Windows fallback that resolves git.exe from the Git-for-Windows
registry ``InstallPath`` (and common install dirs) without relying on PATH.

These tests mock the OS-specific surfaces (winreg, os.path.exists,
os.path.expandvars) so they exercise the Windows code path deterministically on
ANY host — the CI runners are Linux, where %VAR% expansion and native path
separators differ, so nothing here may depend on the running platform's real
environment or filesystem.
"""
import sys
from unittest.mock import MagicMock, patch

import api.updates as updates

# Deterministic install root used by the winreg stub. Kept as a forward-slash
# string so the expected git path is identical regardless of the host's
# os.path.join separator (CI is Linux; dev is Windows).
FAKE_GIT_ROOT = 'C:/Program Files/Git'
FAKE_GIT_EXE = FAKE_GIT_ROOT + '/cmd/git.exe'


def _fake_winreg(install_path=FAKE_GIT_ROOT):
    """Minimal winreg stub whose OpenKey/QueryValueEx yield a Git InstallPath."""
    mod = MagicMock()
    mod.HKEY_LOCAL_MACHINE = 0
    mod.HKEY_CURRENT_USER = 1
    mod.KEY_READ = 0x20019
    mod.KEY_WOW64_64KEY = 0x0100
    mod.KEY_WOW64_32KEY = 0x0200

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mod.OpenKey.return_value = _Key()
    mod.QueryValueEx.return_value = (install_path, 1)
    return mod


def _patch_join_forward_slash():
    """Make os.path.join deterministic (forward slash) regardless of host OS.

    The production code builds candidates with os.path.join; on Linux CI that
    uses '/', on Windows '\\'. Pin it so expected paths match on both.
    """
    def fake_join(*parts):
        return '/'.join(p.rstrip('/\\') for p in parts)
    return patch.object(updates.os.path, 'join', side_effect=fake_join)


def test_resolve_git_executable_uses_registry_on_windows():
    winreg = _fake_winreg()

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         _patch_join_forward_slash(), \
         patch.object(updates.os.path, 'exists', side_effect=lambda p: p == FAKE_GIT_EXE):
        resolved = updates._resolve_git_executable()

    assert resolved == FAKE_GIT_EXE


def test_resolve_git_executable_probes_known_dirs_when_registry_absent():
    # Registry has no value -> QueryValueEx raises -> fall through to dir probe.
    winreg = _fake_winreg()
    winreg.QueryValueEx.side_effect = OSError('no InstallPath')

    # The code probes os.path.expandvars('%ProgramFiles%\\Git\\cmd\\git.exe');
    # mock expandvars deterministically so the test does not depend on the
    # host's %VAR% expansion (Linux CI does not expand %ProgramFiles%).
    probed = 'C:/Program Files/Git/cmd/git.exe'

    def fake_expandvars(s):
        if 'ProgramFiles%' in s and '(x86)' not in s:
            return probed
        # Other candidates resolve to a sentinel that never "exists".
        return '/nonexistent/' + s

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'expandvars', side_effect=fake_expandvars), \
         patch.object(updates.os.path, 'exists', side_effect=lambda p: p == probed):
        resolved = updates._resolve_git_executable()

    assert resolved == probed


def test_resolve_git_executable_returns_none_on_windows_when_git_truly_absent():
    winreg = _fake_winreg()
    winreg.QueryValueEx.side_effect = OSError('no InstallPath')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'expandvars', side_effect=lambda s: s), \
         patch.object(updates.os.path, 'exists', return_value=False):
        resolved = updates._resolve_git_executable()

    assert resolved is None


def test_non_windows_never_touches_registry_fallback():
    """Falsifiable guard: the registry path must be Windows-only."""
    winreg = _fake_winreg()

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'linux'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'exists', return_value=True):
        resolved = updates._resolve_git_executable()

    assert resolved is None
    winreg.OpenKey.assert_not_called()


def test_detect_webui_version_recovers_via_windows_registry_fallback(tmp_path):
    winreg = _fake_winreg()

    def fake_exists(p):
        # git.exe resolves; no api/_version.py fallback file present.
        return p == FAKE_GIT_EXE

    def fake_run(cmd, **kwargs):
        assert cmd[0] == FAKE_GIT_EXE
        if cmd[1:] == ['describe', '--tags', '--always']:
            return MagicMock(returncode=0, stdout='v0.51.999\n', stderr='')
        if cmd[1:] == ['diff-index', '--quiet', 'HEAD', '--']:
            return MagicMock(returncode=0, stdout='', stderr='')
        raise AssertionError(f'unexpected git args: {cmd[1:]!r}')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         _patch_join_forward_slash(), \
         patch.object(updates.os.path, 'exists', side_effect=fake_exists), \
         patch.object(updates, 'REPO_ROOT', tmp_path), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        version = updates._detect_webui_version()

    assert version == 'v0.51.999'
