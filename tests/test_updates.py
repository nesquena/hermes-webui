import api.updates as updates


def test_run_git_returns_stderr_on_failure(tmp_path):
    out, ok = updates._run_git(['pull', '--ff-only', 'origin/does-not-exist'], tmp_path)
    assert ok is False
    assert out
    assert 'not a git repository' in out.lower() or 'fatal' in out.lower()


def test_split_remote_ref_splits_tracking_ref():
    assert updates._split_remote_ref('origin/master') == ('origin', 'master')
    assert updates._split_remote_ref('origin/feature/foo') == ('origin', 'feature/foo')
    assert updates._split_remote_ref('master') == (None, 'master')
