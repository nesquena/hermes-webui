"""The disk gauge measures the filesystem holding the WebUI state, not "/" (#9167).

In a container the root is the image filesystem: with the state on its own volume the gauge
showed 0% (8 EB total under gVisor) while the state volume was full.
"""
import errno
import shutil
from collections import namedtuple

import pytest

from api import config, system_health

Usage = namedtuple("Usage", "total used free")
ROOT = Usage(total=8 * 2**60, used=2**30, free=8 * 2**60 - 2**30)   # what gVisor reports for "/"
STATE = Usage(total=1000, used=995, free=5)                         # a nearly full state volume


@pytest.fixture
def fake_mounts(monkeypatch, tmp_path):
    state_dir = tmp_path / "webui-state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    calls = []

    def disk_usage(path):
        calls.append(str(path))
        if str(path) == "/":
            return ROOT
        if str(path) == str(state_dir):
            return STATE
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    return state_dir, calls


def test_disk_gauge_measures_state_dir_on_another_filesystem(fake_mounts):
    state_dir, calls = fake_mounts
    disk = system_health.build_system_health_payload()["disk"]
    assert disk == {"used_bytes": 995, "total_bytes": 1000, "percent": 99.5}
    assert calls == [str(state_dir)]


def test_missing_state_dir_keeps_metric_unavailable_without_root_fallback(fake_mounts, monkeypatch, tmp_path):
    _, calls = fake_mounts
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "gone")
    payload = system_health.build_system_health_payload()
    assert payload["disk"] is None
    assert {"metric": "disk", "code": "FileNotFoundError"} in payload["errors"]
    assert "/" not in calls
    assert str(tmp_path) not in repr(payload)


def test_unreadable_state_dir_keeps_metric_unavailable_without_root_fallback(fake_mounts, monkeypatch):
    state_dir, calls = fake_mounts

    def denied(path):
        calls.append(str(path))
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "disk_usage", denied)
    payload = system_health.build_system_health_payload()
    assert payload["disk"] is None
    assert {"metric": "disk", "code": "PermissionError"} in payload["errors"]
    assert calls == [str(state_dir)]
    assert str(state_dir) not in repr(payload)


def test_real_state_dir_is_measured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    expected = shutil.disk_usage(tmp_path)
    disk = system_health._disk_usage()
    assert disk["total_bytes"] == expected.total
