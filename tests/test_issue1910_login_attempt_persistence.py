import json
import stat
import time

from api import auth


def test_login_attempts_persist_failed_attempts(tmp_path, monkeypatch):
    attempts_file = tmp_path / ".login_attempts.json"
    monkeypatch.setattr(auth, "_LOGIN_ATTEMPTS_FILE", attempts_file)
    monkeypatch.setattr(auth, "_login_attempts", {})

    auth._record_login_attempt("203.0.113.10")

    data = json.loads(attempts_file.read_text(encoding="utf-8"))
    assert "203.0.113.10" in data
    assert len(data["203.0.113.10"]) == 1
    assert stat.S_IMODE(attempts_file.stat().st_mode) == 0o600


def test_login_attempts_load_prunes_expired_entries(tmp_path, monkeypatch):
    attempts_file = tmp_path / ".login_attempts.json"
    now = time.time()
    attempts_file.write_text(
        json.dumps(
            {
                "203.0.113.10": [now],
                "203.0.113.11": [now - auth._LOGIN_WINDOW - 5],
                "bad": "not-a-list",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(auth, "_LOGIN_ATTEMPTS_FILE", attempts_file)

    loaded = auth._load_login_attempts()

    assert list(loaded) == ["203.0.113.10"]
    assert len(loaded["203.0.113.10"]) == 1


def test_login_rate_limit_survives_reload(tmp_path, monkeypatch):
    attempts_file = tmp_path / ".login_attempts.json"
    monkeypatch.setattr(auth, "_LOGIN_ATTEMPTS_FILE", attempts_file)
    monkeypatch.setattr(auth, "_login_attempts", {})

    for _ in range(auth._LOGIN_MAX_ATTEMPTS):
        auth._record_login_attempt("203.0.113.12")

    monkeypatch.setattr(auth, "_login_attempts", auth._load_login_attempts())

    assert not auth._check_login_rate("203.0.113.12")
