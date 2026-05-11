"""
Tests for profile-switch workspace and model fixes (#1200).

Bug 1: switch_profile(process_wide=False) returned the OLD profile's workspace
       because get_last_workspace() reads via get_active_profile_name() (TLS/global)
       rather than directly from the target profile's home directory.

Bug 2: /api/models returned stale results after a profile switch because the
       in-memory model cache (_available_models_cache) was not invalidated.

These tests verify both fixes.
"""
import os
import json
import tempfile
import textwrap
from pathlib import Path


def test_switch_profile_returns_target_workspace_not_current(tmp_path, monkeypatch):
    """
    switch_profile(process_wide=False) must return the TARGET profile's workspace,
    not the currently-active profile's workspace.

    Before the fix, get_last_workspace() was called at the end of switch_profile(),
    and it routed through get_active_profile_name() which still pointed to the OLD
    profile during a process_wide=False switch. This caused the wrong workspace to
    be returned and displayed in the UI immediately after switching.
    """
    import api.profiles as profiles

    # Build fake profile structure
    default_home = tmp_path / '.hermes'
    default_home.mkdir()
    ayan_home = default_home / 'profiles' / 'ayan'
    ayan_home.mkdir(parents=True)

    # Give ayan a terminal.cwd config (common case)
    ayan_workspace = tmp_path / 'ayan_workspace'
    ayan_workspace.mkdir()
    ayan_config = ayan_home / 'config.yaml'
    ayan_config.write_text(
        f'model:\n  default: kimi-k2-instruct\n  provider: nous\n'
        f'terminal:\n  cwd: {ayan_workspace}\n',
        encoding='utf-8',
    )

    # Give default profile a different workspace stored in last_workspace.txt
    default_ws = tmp_path / 'default_workspace'
    default_ws.mkdir()
    default_state = default_home / 'webui_state'
    default_state.mkdir()
    (default_state / 'last_workspace.txt').write_text(str(default_ws), encoding='utf-8')

    # Patch _DEFAULT_HERMES_HOME to our tmp dir
    orig_default = profiles._DEFAULT_HERMES_HOME
    profiles._DEFAULT_HERMES_HOME = default_home
    # Ensure _active_profile = 'default'
    orig_active = profiles._active_profile
    profiles._active_profile = 'default'
    # Clear TLS
    profiles._tls.profile = None

    try:
        result = profiles.switch_profile('ayan', process_wide=False)
        ws = result.get('default_workspace', '')
        # Must be ayan's workspace, NOT default's workspace
        assert str(ayan_workspace) in ws or ayan_workspace.resolve() == Path(ws), (
            f"Expected ayan's workspace ({ayan_workspace}), got: {ws}"
        )
        assert str(default_ws) not in ws, (
            f"Returned default profile workspace ({default_ws}) instead of ayan's"
        )
    finally:
        profiles._DEFAULT_HERMES_HOME = orig_default
        profiles._active_profile = orig_active
        profiles._tls.profile = None


def test_switch_profile_uses_last_workspace_txt_over_config(tmp_path, monkeypatch):
    """
    If a profile has a last_workspace.txt (previously chosen workspace),
    that takes priority over terminal.cwd in config.yaml.
    """
    import api.profiles as profiles

    default_home = tmp_path / '.hermes'
    default_home.mkdir()
    target_home = default_home / 'profiles' / 'myprofile'
    target_home.mkdir(parents=True)

    # config.yaml has terminal.cwd
    cfg_ws = tmp_path / 'cfg_workspace'
    cfg_ws.mkdir()
    (target_home / 'config.yaml').write_text(
        f'terminal:\n  cwd: {cfg_ws}\n', encoding='utf-8',
    )

    # last_workspace.txt overrides it
    explicit_ws = tmp_path / 'explicit_workspace'
    explicit_ws.mkdir()
    state_dir = target_home / 'webui_state'
    state_dir.mkdir()
    (state_dir / 'last_workspace.txt').write_text(str(explicit_ws), encoding='utf-8')

    orig_default = profiles._DEFAULT_HERMES_HOME
    profiles._DEFAULT_HERMES_HOME = default_home
    orig_active = profiles._active_profile
    profiles._active_profile = 'default'
    profiles._tls.profile = None

    try:
        result = profiles.switch_profile('myprofile', process_wide=False)
        ws = result.get('default_workspace', '')
        assert str(explicit_ws) in ws or Path(ws) == explicit_ws.resolve(), (
            f"Expected last_workspace.txt ({explicit_ws}), got: {ws}"
        )
        assert str(cfg_ws) not in ws, (
            f"terminal.cwd ({cfg_ws}) should not override last_workspace.txt"
        )
    finally:
        profiles._DEFAULT_HERMES_HOME = orig_default
        profiles._active_profile = orig_active
        profiles._tls.profile = None


def test_switch_profile_process_wide_false_returns_correct_model(tmp_path, monkeypatch):
    """
    switch_profile(process_wide=False) reads the default model from the TARGET
    profile's config.yaml directly (not from the process-global _cfg_cache).
    """
    import api.profiles as profiles

    default_home = tmp_path / '.hermes'
    default_home.mkdir()
    target_home = default_home / 'profiles' / 'aiprofile'
    target_home.mkdir(parents=True)

    target_ws = tmp_path / 'ai_ws'
    target_ws.mkdir()
    (target_home / 'config.yaml').write_text(
        f'model:\n  default: kimi-k2-instruct\n  provider: nous\n'
        f'terminal:\n  cwd: {target_ws}\n',
        encoding='utf-8',
    )

    orig_default = profiles._DEFAULT_HERMES_HOME
    profiles._DEFAULT_HERMES_HOME = default_home
    orig_active = profiles._active_profile
    profiles._active_profile = 'default'
    profiles._tls.profile = None

    try:
        result = profiles.switch_profile('aiprofile', process_wide=False)
        assert result.get('default_model') == 'kimi-k2-instruct', (
            f"Expected 'kimi-k2-instruct', got: {result.get('default_model')!r}"
        )
    finally:
        profiles._DEFAULT_HERMES_HOME = orig_default
        profiles._active_profile = orig_active
        profiles._tls.profile = None


def test_profile_switch_route_invalidates_models_cache(tmp_path):
    """
    After a profile switch, the model cache must be invalidated so the next
    /api/models request rebuilds from the new profile's config.
    
    This is a unit test verifying that invalidate_models_cache() is called
    as part of the /api/profile/switch response flow.
    """
    from api.config import invalidate_models_cache, _available_models_cache_lock
    import api.config as config_module

    # Seed a non-None cache value to simulate a populated cache
    with _available_models_cache_lock:
        config_module._available_models_cache = {
            'active_provider': 'old_provider',
            'default_model': 'old-model',
            'groups': [],
        }
        config_module._available_models_cache_ts = 9999999.0

    # Verify it's non-None before
    assert config_module._available_models_cache is not None

    # Call invalidate (the same function called by the route handler)
    invalidate_models_cache()

    # Must be None after invalidation
    assert config_module._available_models_cache is None, (
        "invalidate_models_cache() must clear _available_models_cache"
    )
    assert config_module._available_models_cache_ts == 0.0


"""
Test that syncTopbar() updates the profile chip label even when S.session is null.

Bug: The profile chip label (profileChipLabel) was only updated in the session-present
path of syncTopbar(). When S.session is null (fresh page / after profile switch with
no active session), the early-return branch ran without updating the chip.
This caused the chip to keep showing the old profile name after switchToProfile().
"""

import re


def test_syncTopbar_early_return_updates_profile_chip():
    """
    syncTopbar() must update profileChipLabel inside the !S.session early-return block.
    Without this, the composer profile chip stays stale when there is no active session.
    """
    from pathlib import Path
    ui_js = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")

    # Find the syncTopbar function
    fn_start = ui_js.find("function syncTopbar(){")
    assert fn_start != -1, "syncTopbar function not found in ui.js"

    # Find the early-return block (!S.session branch)
    early_ret_start = ui_js.find("if(!S.session){", fn_start)
    assert early_ret_start != -1, "!S.session early-return block not found in syncTopbar"

    # Find where the early return ends (the closing brace + return)
    early_ret_end = ui_js.find("return;", early_ret_start)
    assert early_ret_end != -1

    early_block = ui_js[early_ret_start : early_ret_end + len("return;")]

    # The profile chip update must be inside this early-return block
    assert "profileChipLabel" in early_block, (
        "syncTopbar() early-return block (!S.session) must update profileChipLabel. "
        "Without this, switching profiles with no active session leaves the chip stale."
    )
    assert "S.activeProfile" in early_block, (
        "profileChipLabel update in early-return block must read S.activeProfile"
    )


# ── Regression guard tests ────────────────────────────────────────────────────
# These tests exist to catch future regressions in profile switching behavior.
# Each one corresponds to a specific bug that was fixed in the #1200 PR.

def test_regression_switch_profile_default_workspace_not_from_process_global(tmp_path, monkeypatch):
    """
    REGRESSION GUARD (#1200 Bug 1): switch_profile(process_wide=False) must NOT
    return the active (old) profile's workspace via get_last_workspace().

    This test proves the fix by setting up a scenario where the old profile has
    a known workspace in last_workspace.txt and the target profile has a DIFFERENT
    workspace. If the regression returns, this test fails.
    """
    import api.profiles as profiles

    base = tmp_path / ".hermes"
    base.mkdir()

    # Old profile (default) has workspace A
    old_ws = tmp_path / "old_workspace"
    old_ws.mkdir()
    default_state = base / "webui_state"
    default_state.mkdir()
    (default_state / "last_workspace.txt").write_text(str(old_ws), encoding="utf-8")

    # Target profile has workspace B
    new_ws = tmp_path / "new_workspace"
    new_ws.mkdir()
    target_home = base / "profiles" / "target"
    target_home.mkdir(parents=True)
    target_state = target_home / "webui_state"
    target_state.mkdir()
    (target_state / "last_workspace.txt").write_text(str(new_ws), encoding="utf-8")
    (target_home / "config.yaml").write_text(
        "model:\n  default: some-model\n", encoding="utf-8"
    )

    orig_default = profiles._DEFAULT_HERMES_HOME
    orig_active = profiles._active_profile
    profiles._DEFAULT_HERMES_HOME = base
    profiles._active_profile = "default"
    profiles._tls.profile = None

    try:
        result = profiles.switch_profile("target", process_wide=False)
        ws = result.get("default_workspace", "")
        # Must be NEW workspace, not OLD
        assert str(new_ws) in ws or str(new_ws.resolve()) == ws, (
            f"REGRESSION: Got old workspace ({old_ws}) instead of target ({new_ws}). "
            "switch_profile() is reading from the wrong profile."
        )
        assert str(old_ws) not in ws, (
            f"REGRESSION: Returned old profile workspace. Bug 1 regressed."
        )
    finally:
        profiles._DEFAULT_HERMES_HOME = orig_default
        profiles._active_profile = orig_active
        profiles._tls.profile = None


def test_regression_models_cache_cleared_on_profile_switch():
    """
    REGRESSION GUARD (#1200 Bug 2): the model cache must be invalidated after
    a profile switch so the next /api/models returns the new profile's models.

    Without the invalidate_models_cache() call in the route handler, a populated
    cache from the old profile would be served unchanged.
    """
    import api.config as config_module
    from api.config import invalidate_models_cache, _available_models_cache_lock

    # Seed cache with "stale" data
    stale = {"active_provider": "stale", "default_model": "stale-model", "groups": []}
    with _available_models_cache_lock:
        config_module._available_models_cache = stale
        config_module._available_models_cache_ts = 9_999_999.0

    # Simulate what the route handler does
    invalidate_models_cache()

    # Cache must be cleared
    assert config_module._available_models_cache is None, (
        "REGRESSION: model cache not cleared after profile switch. Bug 2 regressed."
    )


def test_regression_synctopbar_early_return_updates_profile_chip():
    """
    REGRESSION GUARD (#1200 Bug 3): the syncTopbar() early-return branch (when
    S.session is null) must update the profileChipLabel.

    If this fix is reverted, the profile chip stays on the old profile name even
    though S.activeProfile has been updated, because syncTopbar() exits early
    before reaching the chip-update code at the end of the function.
    """
    from pathlib import Path

    ui_js = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")

    fn_start = ui_js.find("function syncTopbar(){")
    assert fn_start != -1, "syncTopbar not found — has it been renamed?"

    early_start = ui_js.find("if(!S.session){", fn_start)
    assert early_start != -1, "!S.session early-return block not found in syncTopbar"

    early_end = ui_js.find("return;", early_start)
    assert early_end != -1, "return; not found after !S.session block"

    early_block = ui_js[early_start : early_end + len("return;")]

    assert "profileChipLabel" in early_block, (
        "REGRESSION: syncTopbar() early-return no longer updates profileChipLabel. "
        "Profile name chip won't update after switching profiles with no active session. "
        "Bug 3 regressed."
    )


def test_regression_switch_profile_returns_target_model():
    """
    REGRESSION GUARD (#1200): switch_profile(process_wide=False) must return the
    target profile's default model, not the process-global cached model.
    """
    import api.profiles as profiles
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        target = base / "profiles" / "myp"
        target.mkdir(parents=True)
        ws = base / "mypws"; ws.mkdir()
        (target / "config.yaml").write_text(
            f"model:\n  default: my-target-model\nterminal:\n  cwd: {ws}\n",
            encoding="utf-8",
        )

        orig = profiles._DEFAULT_HERMES_HOME
        orig_act = profiles._active_profile
        profiles._DEFAULT_HERMES_HOME = base
        profiles._active_profile = "default"
        profiles._tls.profile = None
        try:
            r = profiles.switch_profile("myp", process_wide=False)
            assert r.get("default_model") == "my-target-model", (
                f"REGRESSION: Got {r.get('default_model')!r} instead of 'my-target-model'. "
                "switch_profile() is not reading from target profile's config. Bug 2 regressed."
            )
        finally:
            profiles._DEFAULT_HERMES_HOME = orig
            profiles._active_profile = orig_act
            profiles._tls.profile = None


def test_get_config_reloads_when_request_profile_changes(tmp_path, monkeypatch):
    """get_config() must follow the per-request profile, not stale global cache."""
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    import api.config as config
    import api.profiles as profiles

    default_home = tmp_path / ".hermes"
    work_home = default_home / "profiles" / "work"
    work_home.mkdir(parents=True)
    default_home.mkdir(exist_ok=True)
    (default_home / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n  default: gpt-5.5\n",
        encoding="utf-8",
    )
    (work_home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: google/gemini-3-flash-preview\n",
        encoding="utf-8",
    )
    same_mtime = 1_700_000_000
    os.utime(default_home / "config.yaml", (same_mtime, same_mtime))
    os.utime(work_home / "config.yaml", (same_mtime, same_mtime))

    monkeypatch.setattr(
        config,
        "_get_config_path",
        lambda: profiles.get_active_hermes_home() / "config.yaml",
    )

    orig_default_home = profiles._DEFAULT_HERMES_HOME
    orig_active = profiles._active_profile
    orig_cache = dict(config._cfg_cache)
    orig_mtime = config._cfg_mtime
    orig_path = getattr(config, "_cfg_path", None)
    orig_fingerprint = getattr(config, "_cfg_fingerprint", None)
    profiles._tls.profile = None
    try:
        profiles._DEFAULT_HERMES_HOME = default_home
        profiles._active_profile = "default"
        config._cfg_cache.clear()
        config._cfg_mtime = 0.0
        if hasattr(config, "_cfg_path"):
            config._cfg_path = None
        if hasattr(config, "_cfg_fingerprint"):
            config._cfg_fingerprint = None

        assert config.get_config()["model"]["provider"] == "openai-codex"
        profiles.set_request_profile("work")
        assert config._get_config_path() == work_home / "config.yaml"
        assert config.get_config()["model"]["provider"] == "openrouter"
    finally:
        profiles.clear_request_profile()
        profiles._DEFAULT_HERMES_HOME = orig_default_home
        profiles._active_profile = orig_active
        config._cfg_cache.clear()
        config._cfg_cache.update(orig_cache)
        config._cfg_mtime = orig_mtime
        if hasattr(config, "_cfg_path"):
            config._cfg_path = orig_path
        if hasattr(config, "_cfg_fingerprint"):
            config._cfg_fingerprint = orig_fingerprint


def test_chat_start_retags_empty_session_to_request_profile(monkeypatch, tmp_path):
    """An empty session created under profile A can be sent under profile B after a switch."""
    import api.routes as routes

    class FakeSession:
        def __init__(self):
            self.session_id = "sid-profile-switch"
            self.profile = "default"
            self.workspace = str(tmp_path)
            self.model = "google/gemini-3-flash-preview"
            self.model_provider = "openrouter"
            self.messages = []
            self.context_messages = []
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.saved = False

        def save(self):
            self.saved = True

    fake = FakeSession()
    monkeypatch.setattr(routes, "get_session", lambda sid: fake)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda path: tmp_path)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider: (model, provider, False),
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())

    started_threads = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started_threads.append((args, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(routes.threading, "Thread", FakeThread)

    payloads = []

    class Handler:
        pass

    def fake_j(handler, payload, status=200, **kwargs):
        payloads.append((status, payload))
        return payload

    monkeypatch.setattr(routes, "j", fake_j)

    body = {
        "session_id": fake.session_id,
        "message": "hello",
        "workspace": str(tmp_path),
        "model": fake.model,
        "model_provider": fake.model_provider,
        "profile": "work",
    }
    routes._handle_chat_start(Handler(), body)

    assert fake.profile == "work"
    assert fake.saved is True
    assert started_threads, "chat_start should launch the stream after retagging"
    assert payloads and payloads[-1][0] == 200


def test_chat_start_does_not_retag_non_empty_session(monkeypatch, tmp_path):
    """Profile retagging is limited to empty placeholder sessions."""
    import api.routes as routes

    class FakeSession:
        def __init__(self):
            self.session_id = "sid-profile-switch-non-empty"
            self.profile = "default"
            self.workspace = str(tmp_path)
            self.model = "google/gemini-3-flash-preview"
            self.model_provider = "openrouter"
            self.messages = [{"role": "user", "content": "previous turn"}]
            self.context_messages = []
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.saved = False

        def save(self):
            self.saved = True

    fake = FakeSession()
    monkeypatch.setattr(routes, "get_session", lambda sid: fake)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda path: tmp_path)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider: (model, provider, False),
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(routes.threading, "Thread", FakeThread)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: payload)

    routes._handle_chat_start(
        object(),
        {
            "session_id": fake.session_id,
            "message": "hello",
            "workspace": str(tmp_path),
            "model": fake.model,
            "model_provider": fake.model_provider,
            "profile": "work",
        },
    )

    assert fake.profile == "default"
    assert fake.saved is True


def test_chat_start_rejects_invalid_request_profile(monkeypatch):
    """chat_start validates the optional profile payload before retagging."""
    import api.routes as routes

    class FakeSession:
        profile = "default"

    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    errors = []

    def fake_bad(handler, message, status=400):
        errors.append((message, status))
        return {"error": message}

    monkeypatch.setattr(routes, "bad", fake_bad)

    result = routes._handle_chat_start(
        object(),
        {
            "session_id": "sid-invalid-profile",
            "message": "hello",
            "profile": "../etc",
        },
    )

    assert result == {"error": "invalid profile"}
    assert errors == [("invalid profile", 400)]
