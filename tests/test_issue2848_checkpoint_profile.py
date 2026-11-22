"""Regression coverage for #2848 checkpoint saves in background threads."""
from __future__ import annotations

import os
from pathlib import Path
import threading


def test_checkpoint_save_uses_session_profile_env(monkeypatch, tmp_path):
    """Checkpoint saves run on their own thread, outside request TLS.

    They must route profile-scoped helpers through the session's profile instead
    of falling back to the process-global/default profile.
    """
    from api.models import Session
    from api.streaming import _save_streaming_checkpoint
    import api.config as config
    import api.profiles as profiles

    profile_home = tmp_path / "profiles" / "maiko"
    profile_home.mkdir(parents=True)
    captured = {}

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda profile: profile_home)
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda home: {"HERMES_CONFIG_PATH": str(Path(home) / "config.yaml")},
    )

    def fake_save(self, *args, **kwargs):
        captured["kwargs"] = kwargs
        captured["thread_env"] = dict(getattr(config._thread_ctx, "env", {}) or {})

    monkeypatch.setattr(Session, "save", fake_save)

    session = Session(session_id="issue2848", profile="maiko")

    _save_streaming_checkpoint(session)

    assert captured["kwargs"] == {"skip_index": True}
    assert captured["thread_env"]["HERMES_HOME"] == str(profile_home)
    assert captured["thread_env"]["HERMES_CONFIG_PATH"] == str(profile_home / "config.yaml")


def test_checkpoint_save_completes_without_skill_lock(monkeypatch, tmp_path):
    """Checkpoint saves must not block on the legacy skill module lock."""

    from api.models import Session
    from api.streaming import _save_streaming_checkpoint
    import api.config as config
    import api.profiles as profiles

    profile_home = tmp_path / "profiles" / "maiko"
    profile_home.mkdir(parents=True)
    captured = {}

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda profile: profile_home)
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda home: {"HERMES_CONFIG_PATH": str(Path(home) / "config.yaml")},
    )
    monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: None)

    patch_calls: list[dict] = []

    def fake_save(self, *args, **kwargs):
        captured["kwargs"] = kwargs
        captured["thread_env"] = dict(getattr(config._thread_ctx, "env", {}) or {})
        captured["env_hermes_home"] = os.environ.get("HERMES_HOME")

    def patch_skill_home_modules(*_):
        patch_calls.append({"patched": True})

    monkeypatch.setattr(Session, "save", fake_save)
    monkeypatch.setattr(profiles, "patch_skill_home_modules", patch_skill_home_modules)

    completion = threading.Event()

    session = Session(session_id="issue2848-lock", profile="maiko")

    acquired_lock = profiles._SKILL_HOME_MODULE_PATCH_LOCK.acquire(timeout=1)
    assert acquired_lock, "lock was unexpectedly unavailable before checkpoint test"

    try:
        def _worker() -> None:
            try:
                _save_streaming_checkpoint(session)
                completion.set()
            except Exception as exc:  # pragma: no cover - defensive
                captured["error"] = exc
                completion.set()

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        assert completion.wait(0.5), (
            "checkpoint worker should not block on skill module lock"
        )
    finally:
        profiles._SKILL_HOME_MODULE_PATCH_LOCK.release()
        worker.join(timeout=1)

    assert captured.get("kwargs") == {"skip_index": True}
    assert captured.get("thread_env", {}).get("HERMES_HOME") == str(profile_home)
    assert (
        captured.get("thread_env", {}).get("HERMES_CONFIG_PATH")
        == str(profile_home / "config.yaml")
    )
    assert captured.get("env_hermes_home") == str(profile_home)
    assert not patch_calls
    assert "error" not in captured
