"""Regression coverage for #2848 checkpoint saves in background threads."""
from __future__ import annotations

from pathlib import Path


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
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda home: {"HERMES_CONFIG_PATH": str(Path(home) / "config.yaml")})

    def fake_save(self, *args, **kwargs):
        captured["kwargs"] = kwargs
        captured["thread_env"] = dict(getattr(config._thread_ctx, "env", {}) or {})

    monkeypatch.setattr(Session, "save", fake_save)

    session = Session(session_id="issue2848", profile="maiko")

    _save_streaming_checkpoint(session)

    assert captured["kwargs"] == {"skip_index": True}
    assert captured["thread_env"]["HERMES_HOME"] == str(profile_home)
    assert captured["thread_env"]["HERMES_CONFIG_PATH"] == str(profile_home / "config.yaml")
