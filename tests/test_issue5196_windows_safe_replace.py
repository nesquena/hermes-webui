# tests/test_issue5196_windows_safe_replace.py
from unittest import mock
from api import models

def test_safe_replace_passthrough_off_windows():
    calls = []
    with mock.patch.object(models.os, "name", "posix"), \
         mock.patch.object(models.os, "replace", lambda s, d: calls.append((s, d))), \
         mock.patch.object(models.time, "sleep") as slept:
        models._safe_replace("a", "b")
    assert len(calls) == 1 and not slept.called

def test_safe_replace_retries_then_succeeds_on_windows():
    n = {"c": 0}
    def flaky(s, d):
        n["c"] += 1
        if n["c"] <= 2:
            raise PermissionError("WinError 5")
    with mock.patch.object(models.os, "name", "nt"), \
         mock.patch.object(models.os, "replace", flaky), \
         mock.patch.object(models.time, "sleep", lambda x: None):
        models._safe_replace("a", "b")
    assert n["c"] == 3

def test_safe_replace_reraises_after_exhausting_retries():
    n = {"c": 0}
    def always(s, d):
        n["c"] += 1
        raise PermissionError("WinError 5")
    with mock.patch.object(models.os, "name", "nt"), \
         mock.patch.object(models.os, "replace", always), \
         mock.patch.object(models.time, "sleep", lambda x: None):
        import pytest
        with pytest.raises(PermissionError):
            models._safe_replace("a", "b")
    assert n["c"] == 5  # no silent success on a save path

def test_safe_replace_does_not_retry_non_permission_errors():
    n = {"c": 0}
    def oserr(s, d):
        n["c"] += 1
        raise OSError("disk full")
    with mock.patch.object(models.os, "name", "nt"), \
         mock.patch.object(models.os, "replace", oserr), \
         mock.patch.object(models.time, "sleep", lambda x: None):
        import pytest
        with pytest.raises(OSError):
            models._safe_replace("a", "b")
    assert n["c"] == 1

def test_safe_replace_backoff_sequence():
    delays = []
    def always(s, d):
        raise PermissionError("x")
    with mock.patch.object(models.os, "name", "nt"), \
         mock.patch.object(models.os, "replace", always), \
         mock.patch.object(models.time, "sleep", lambda x: delays.append(round(x, 3))):
        import pytest
        with pytest.raises(PermissionError):
            models._safe_replace("a", "b")
    assert delays == [0.05, 0.1, 0.2, 0.4]
