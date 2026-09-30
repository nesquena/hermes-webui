import time
from pathlib import Path
import api.config as config


def test_touch_models_cache_mtime(tmp_path):
    cache_file = tmp_path / "models_cache.json"
    cache_file.write_text("{}", encoding="utf-8")

    # Set mtime back by 100 seconds
    past_time = time.time() - 100
    import os
    os.utime(cache_file, (past_time, past_time))
    old_mtime = cache_file.stat().st_mtime
    assert abs(old_mtime - past_time) < 2

    # Touch cache mtime
    res = config._touch_models_cache_mtime(cache_file)
    assert res is True

    new_mtime = cache_file.stat().st_mtime
    assert new_mtime > old_mtime
    assert abs(new_mtime - time.time()) < 2


def test_touch_models_cache_mtime_nonexistent():
    res = config._touch_models_cache_mtime(Path("/nonexistent/models_cache.json"))
    assert res is False
