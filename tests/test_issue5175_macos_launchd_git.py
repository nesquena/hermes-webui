import sys
from unittest.mock import MagicMock, patch

import api.updates as updates


def test_run_git_uses_which_result_when_available(tmp_path):
    with patch.object(updates.shutil, 'which', return_value='C:/Tools/git.exe'), \
         patch.object(updates.subprocess, 'run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='v0.51.999\n', stderr='')

        out, ok = updates._run_git(['describe', '--tags'], tmp_path)

    assert ok is True
    assert out == 'v0.51.999'
    assert mock_run.call_args.args[0][0] == 'C:/Tools/git.exe'


def test_run_git_falls_back_to_usr_bin_git_on_darwin(tmp_path):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == '/usr/bin/git'
        return MagicMock(returncode=0, stdout='v0.51.999\n', stderr='')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'darwin'), \
         patch.object(updates.os.path, 'exists', return_value=True), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        out, ok = updates._run_git(['describe', '--tags'], tmp_path)

    assert ok is True
    assert out == 'v0.51.999'


def test_run_git_returns_not_found_when_no_executable(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == '/usr/bin/git':
            raise AssertionError('non-macOS path miss must not use /usr/bin/git')
        raise FileNotFoundError

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'linux'), \
         patch.object(updates.os.path, 'exists', return_value=True), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        out, ok = updates._run_git(['status'], tmp_path)

    assert ok is False
    assert out == 'git executable not found'
    assert '/usr/bin/git' not in calls


def test_run_git_returns_not_found_when_usr_bin_git_absent_on_darwin(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        raise FileNotFoundError

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'darwin'), \
         patch.object(updates.os.path, 'exists', return_value=False), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        out, ok = updates._run_git(['status'], tmp_path)

    assert ok is False
    assert out == 'git executable not found'
    assert '/usr/bin/git' not in calls


def test_detect_webui_version_recovers_via_launchd_fallback(tmp_path):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == '/usr/bin/git'
        if cmd[1:] == ['describe', '--tags', '--always']:
            return MagicMock(returncode=0, stdout='v0.51.999\n', stderr='')
        if cmd[1:] == ['diff-index', '--quiet', 'HEAD', '--']:
            return MagicMock(returncode=0, stdout='', stderr='')
        raise AssertionError(f'unexpected git args: {cmd[1:]!r}')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'darwin'), \
         patch.object(updates.os.path, 'exists', return_value=True), \
         patch.object(updates, 'REPO_ROOT', tmp_path), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        version = updates._detect_webui_version()

    assert version == 'v0.51.999'


def test_check_repo_does_not_report_git_not_found_via_launchd_fallback(tmp_path):
    (tmp_path / '.git').mkdir()

    def fake_run(cmd, **kwargs):
        assert cmd[0] == '/usr/bin/git'
        git_args = cmd[1:]
        if git_args == ['fetch', 'origin', '--tags', '--force']:
            return MagicMock(returncode=0, stdout='', stderr='')
        if git_args == ['tag', '--list', 'v*', '--sort=-v:refname']:
            return MagicMock(returncode=0, stdout='', stderr='')
        if git_args == ['rev-parse', '--abbrev-ref', '@{upstream}']:
            return MagicMock(returncode=0, stdout='origin/master\n', stderr='')
        if git_args == ['rev-list', '--count', 'HEAD..origin/master']:
            return MagicMock(returncode=0, stdout='0\n', stderr='')
        if git_args == ['merge-base', 'HEAD', 'origin/master']:
            return MagicMock(returncode=0, stdout='abcdef1234567890\n', stderr='')
        if git_args == ['rev-parse', '--short', 'abcdef1234567890']:
            return MagicMock(returncode=0, stdout='abcdef1\n', stderr='')
        if git_args == ['rev-parse', '--short', 'origin/master']:
            return MagicMock(returncode=0, stdout='fedcba9\n', stderr='')
        if git_args == ['remote', 'get-url', 'origin']:
            return MagicMock(
                returncode=0,
                stdout='https://github.com/nesquena/hermes-webui.git\n',
                stderr='',
            )
        if git_args == ['diff-index', '--quiet', 'HEAD', '--']:
            return MagicMock(returncode=0, stdout='', stderr='')
        raise AssertionError(f'unexpected git args: {git_args!r}')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'darwin'), \
         patch.object(updates.os.path, 'exists', return_value=True), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        info = updates._check_repo(tmp_path, 'webui')

    assert info['behind'] == 0
    assert info['dirty'] is False
    assert 'error' not in info
