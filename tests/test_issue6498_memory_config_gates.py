"""
Regression tests for PR #6498: memory_enabled and user_profile_enabled config gates.

Checks that _handle_memory_read and _handle_memory_write respect the per-profile
config flags, and that get_config_snapshot() (not get_config()) prevents the
process-global cache race across profiles.
"""
import io
import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_profile_home(tmp_path):
    """Create a temp profile home with memory files and a config."""
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("This is my memory content", encoding="utf-8")
    (mem_dir / "USER.md").write_text("This is my user profile", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("This is my soul", encoding="utf-8")
    return tmp_path


@pytest.fixture
def mock_handler():
    """Create a mock HTTP handler that captures response status and body."""
    h = MagicMock()
    h.wfile = io.BytesIO()

    def send_response(status):
        h.status = status

    def send_header(k, v):
        pass

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = MagicMock()
    return h


def _body_from_handler(handler):
    """Deserialize JSON from the handler's wfile buffer."""
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


@pytest.fixture(autouse=True)
def _patch_get_active_hermes_home(monkeypatch, fake_profile_home):
    """Patch get_active_hermes_home at its source (api.profiles) so all
    internal imports within handler functions see the mock."""
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: fake_profile_home)


class TestMemoryReadConfigGates:
    """_handle_memory_read respects memory_enabled and user_profile_enabled."""

    def test_read_memory_disabled(self, mock_handler, fake_profile_home, monkeypatch):
        import api.routes as routes

        monkeypatch.setattr(routes, "get_config_snapshot", lambda: {"memory": {"memory_enabled": False}})

        routes._handle_memory_read(mock_handler)

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        assert body["memory"] == ""
        assert body["memory_path"] == ""
        assert body["memory_mtime"] is None
        # user should still be readable (not disabled)
        assert body["user"] == "This is my user profile"
        assert body["user_path"] != ""
        assert body["user_mtime"] is not None
        # soul always readable
        assert body["soul"] == "This is my soul"

    def test_read_user_profile_disabled(self, mock_handler, fake_profile_home, monkeypatch):
        import api.routes as routes

        monkeypatch.setattr(routes, "get_config_snapshot", lambda: {"memory": {"user_profile_enabled": False}})

        routes._handle_memory_read(mock_handler)

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        assert body["user"] == ""
        assert body["user_path"] == ""
        assert body["user_mtime"] is None
        # memory should still be readable
        assert body["memory"] == "This is my memory content"
        assert body["memory_path"] != ""
        assert body["memory_mtime"] is not None

    def test_read_both_disabled(self, mock_handler, fake_profile_home, monkeypatch):
        import api.routes as routes

        monkeypatch.setattr(
            routes,
            "get_config_snapshot",
            lambda: {"memory": {"memory_enabled": False, "user_profile_enabled": False}},
        )

        routes._handle_memory_read(mock_handler)

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        assert body["memory"] == ""
        assert body["memory_path"] == ""
        assert body["memory_mtime"] is None
        assert body["user"] == ""
        assert body["user_path"] == ""
        assert body["user_mtime"] is None
        # soul always readable
        assert body["soul"] == "This is my soul"

    def test_read_default_true_when_key_missing(self, mock_handler, fake_profile_home, monkeypatch):
        """When config keys are absent, both default to True (backward compat)."""
        import api.routes as routes

        monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})

        routes._handle_memory_read(mock_handler)

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        assert body["memory"] == "This is my memory content"
        assert body["user"] == "This is my user profile"
        assert body["soul"] == "This is my soul"

    def test_read_default_true_when_memory_mapping_malformed(self, mock_handler, fake_profile_home, monkeypatch):
        """A non-dict `memory` mapping (e.g. a string) must also default to True."""
        import api.routes as routes

        monkeypatch.setattr(routes, "get_config_snapshot", lambda: {"memory": "not-a-dict"})

        routes._handle_memory_read(mock_handler)

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        assert body["memory"] == "This is my memory content"
        assert body["user"] == "This is my user profile"
        assert body["soul"] == "This is my soul"

    def test_read_soul_unaffected_by_disabled_flags(self, mock_handler, fake_profile_home, monkeypatch):
        """Soul section is unaffected by both flags."""
        import api.routes as routes

        monkeypatch.setattr(
            routes,
            "get_config_snapshot",
            lambda: {"memory": {"memory_enabled": False, "user_profile_enabled": False}},
        )

        routes._handle_memory_read(mock_handler)

        body = _body_from_handler(mock_handler)
        assert body["soul"] == "This is my soul"
        assert body["soul_path"] != ""
        assert body["soul_mtime"] is not None


class TestMemoryWriteConfigGates:
    """_handle_memory_write returns 403 for disabled sections."""

    def test_write_memory_disabled(self, mock_handler, fake_profile_home, monkeypatch):
        import api.routes as routes

        monkeypatch.setattr(routes, "get_config_snapshot", lambda: {"memory": {"memory_enabled": False}})

        routes._handle_memory_write(mock_handler, {"section": "memory", "content": "new content"})

        assert mock_handler.status == 403
        body = _body_from_handler(mock_handler)
        assert "disabled" in body.get("error", "").lower()

    def test_write_user_profile_disabled(self, mock_handler, fake_profile_home, monkeypatch):
        import api.routes as routes

        monkeypatch.setattr(routes, "get_config_snapshot", lambda: {"memory": {"user_profile_enabled": False}})

        routes._handle_memory_write(mock_handler, {"section": "user", "content": "new profile"})

        assert mock_handler.status == 403
        body = _body_from_handler(mock_handler)
        assert "disabled" in body.get("error", "").lower()

    def test_write_soul_unaffected(self, mock_handler, fake_profile_home, monkeypatch):
        """Soul section writes are unaffected by both flags."""
        import api.routes as routes

        monkeypatch.setattr(
            routes,
            "get_config_snapshot",
            lambda: {"memory": {"memory_enabled": False, "user_profile_enabled": False}},
        )

        soul_path = fake_profile_home / "SOUL.md"

        routes._handle_memory_write(mock_handler, {"section": "soul", "content": "updated soul"})

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        assert body["ok"] is True
        # Verify the file was actually written
        assert soul_path.read_text(encoding="utf-8") == "updated soul"

    def test_write_allowed_when_enabled(self, mock_handler, fake_profile_home, monkeypatch):
        """Happy path: writes go through when the relevant flag is enabled."""
        import api.routes as routes

        monkeypatch.setattr(
            routes,
            "get_config_snapshot",
            lambda: {"memory": {"memory_enabled": True, "user_profile_enabled": True}},
        )

        # Write memory
        mem_file = fake_profile_home / "memories" / "MEMORY.md"
        routes._handle_memory_write(mock_handler, {"section": "memory", "content": "updated memory"})
        assert mock_handler.status == 200
        assert mem_file.read_text(encoding="utf-8") == "updated memory"

        # Write user
        user_file = fake_profile_home / "memories" / "USER.md"
        routes._handle_memory_write(mock_handler, {"section": "user", "content": "updated user"})
        assert mock_handler.status == 200
        assert user_file.read_text(encoding="utf-8") == "updated user"


class TestProfileIsolation:
    """The config snapshot must be request-owned, not the process-global _cfg_cache."""

    def test_snapshot_is_independent_after_reload(self, monkeypatch, tmp_path):
        """
        get_config_snapshot() returns a deep copy under the cache lock, so
        mutating the original cache after the snapshot is taken doesn't affect
        the snapshot. This proves profile-isolation: profile A's snapshot stays
        frozen even if profile B's config reload changes the shared cache.
        """
        import api.routes as routes

        mock_snapshot = {"memory": {"memory_enabled": False, "user_profile_enabled": True}}
        call_count = {"n": 0}

        def counting_snapshot():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return dict(mock_snapshot)  # profile A
            else:
                return {"memory": {"memory_enabled": True, "user_profile_enabled": True}}  # profile B

        monkeypatch.setattr(routes, "get_config_snapshot", counting_snapshot)

        mock_h = MagicMock()
        mock_h.wfile = io.BytesIO()
        mock_h.send_response = lambda s: setattr(mock_h, "status", s)
        mock_h.send_header = lambda k, v: None
        mock_h.end_headers = MagicMock()

        assert call_count["n"] == 0
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
        (tmp_path / "memories").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memories" / "MEMORY.md").write_text("test", encoding="utf-8")
        (tmp_path / "memories" / "USER.md").write_text("test", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("test", encoding="utf-8")

        routes._handle_memory_read(mock_h)

        assert call_count["n"] == 1, "get_config_snapshot should be called exactly once per read request"

    def test_disabled_profile_write_cannot_be_allowed_by_other_profile_reload(self, monkeypatch, tmp_path):
        """
        Profile A has memory_enabled: false. Profile B has memory_enabled: true.
        A concurrent reload for profile B must not allow A's write to go through.
        """
        import api.routes as routes

        profile_a_home = tmp_path / "profile_a"
        profile_a_home.mkdir()
        (profile_a_home / "memories").mkdir()
        mem_file = profile_a_home / "memories" / "MEMORY.md"
        mem_file.write_text("original", encoding="utf-8")

        snapshot_call = {"count": 0}

        def per_profile_snapshot():
            """Return profile A config first, profile B config second (simulating reload)."""
            snapshot_call["count"] += 1
            if snapshot_call["count"] == 1:
                return {"memory": {"memory_enabled": False, "user_profile_enabled": True}}
            else:
                return {"memory": {"memory_enabled": True, "user_profile_enabled": True}}

        monkeypatch.setattr(routes, "get_config_snapshot", per_profile_snapshot)
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: profile_a_home)

        mock_h = MagicMock()
        mock_h.wfile = io.BytesIO()
        mock_h.send_response = lambda s: setattr(mock_h, "status", s)
        mock_h.send_header = lambda k, v: None
        mock_h.end_headers = MagicMock()

        routes._handle_memory_write(mock_h, {"section": "memory", "content": "should be blocked"})

        # Profile A has memory_enabled: false -> write should be 403
        assert mock_h.status == 403, (
            f"Expected 403 for disabled profile A, got {mock_h.status}"
        )
        # File must NOT have been modified
        assert mem_file.read_text(encoding="utf-8") == "original", (
            "Profile A's MEMORY.md must remain unchanged when memory_enabled: false"
        )
        # The second call (profile B's config) must NOT be used for profile A's check
        assert snapshot_call["count"] == 1, (
            "get_config_snapshot should be called exactly once; "
            "the profile B reload must not affect profile A's check"
        )


class TestCrossProfileForcedReload:
    """The race the reviewer described: profile A captures a request-owned
    snapshot, then a concurrent profile B request reloads the process-global
    mutable cache in place. The handler must keep using A's captured flags —
    it must never re-read the ambient get_config() afterwards.

    Simulated faithfully: get_config_snapshot() returns a deep copy of A's
    config, then the shared ambient dict (what a buggy get_config() re-read
    would observe) is mutated in place to B's config BEFORE the handler uses
    the capture — exactly the "reload between authorization capture and use"
    window from the review.
    """

    def _install_race(self, monkeypatch, routes, a_cfg, b_cfg):
        """Install get_config_snapshot/get_config under the cross-profile race.

        get_config_snapshot() deep-copies A's config and then mutates the
        shared ambient dict to B's config before returning — simulating a
        concurrent profile-B reload landing between capture and use. A buggy
        handler that re-read get_config() afterwards would observe B's flags.
        """
        shared = dict(a_cfg)
        calls = {"snapshot": 0}

        def snapshot():
            calls["snapshot"] += 1
            captured = dict(shared)  # deep copy under lock (get_config_snapshot)
            shared.clear()
            shared.update(b_cfg)  # profile B reload mutates the shared cache in place
            return captured

        monkeypatch.setattr(routes, "get_config_snapshot", snapshot)
        # Buggy-path simulation: any later get_config() re-read sees profile B.
        monkeypatch.setattr(routes, "get_config", lambda: shared)
        return calls

    def _handler(self):
        h = MagicMock()
        h.wfile = io.BytesIO()
        h.send_response = lambda s: setattr(h, "status", s)
        h.send_header = lambda k, v: None
        h.end_headers = MagicMock()
        return h

    def test_read_memory_disabled_a_not_bypassed_by_b_enabled_reload(self, monkeypatch, tmp_path):
        """A memory_enabled=false, B memory_enabled=true (reload between
        capture and use): GET must still return no content/path/mtime for A's
        existing MEMORY.md, while A's enabled user/soul stay readable."""
        import api.routes as routes

        home = tmp_path / "profile_a"
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "MEMORY.md").write_text("A's private memory", encoding="utf-8")
        (home / "memories" / "USER.md").write_text("A's user profile", encoding="utf-8")
        (home / "SOUL.md").write_text("A's soul", encoding="utf-8")
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        calls = self._install_race(
            monkeypatch,
            routes,
            a_cfg={"memory": {"memory_enabled": False, "user_profile_enabled": True}},
            b_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": True}},
        )

        h = self._handler()
        routes._handle_memory_read(h)

        body = _body_from_handler(h)
        # A's disabled memory stays hidden even though B's reload enabled it globally
        assert body["memory"] == ""
        assert body["memory_path"] == ""
        assert body["memory_mtime"] is None
        # A's enabled user profile and soul remain readable
        assert body["user"] == "A's user profile"
        assert body["user_path"] != ""
        assert body["user_mtime"] is not None
        assert body["soul"] == "A's soul"
        # Snapshot captured exactly once — no re-read of the ambient config
        assert calls["snapshot"] == 1

    def test_read_user_disabled_a_not_bypassed_by_b_enabled_reload(self, monkeypatch, tmp_path):
        """A user_profile_enabled=false, B user_profile_enabled=true: GET must
        still return no content/path/mtime for A's existing USER.md."""
        import api.routes as routes

        home = tmp_path / "profile_a"
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "MEMORY.md").write_text("A's memory", encoding="utf-8")
        (home / "memories" / "USER.md").write_text("A's private profile", encoding="utf-8")
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        calls = self._install_race(
            monkeypatch,
            routes,
            a_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": False}},
            b_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": True}},
        )

        h = self._handler()
        routes._handle_memory_read(h)

        body = _body_from_handler(h)
        assert body["user"] == ""
        assert body["user_path"] == ""
        assert body["user_mtime"] is None
        # A's enabled memory remains readable
        assert body["memory"] == "A's memory"
        assert calls["snapshot"] == 1

    def test_read_inverse_enabled_a_not_blocked_by_b_disabled_reload(self, monkeypatch, tmp_path):
        """Inverse: A memory_enabled=true, B memory_enabled=false. B's disabled
        config must NOT leak into A's request — A's memory stays readable."""
        import api.routes as routes

        home = tmp_path / "profile_a"
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "MEMORY.md").write_text("A's memory", encoding="utf-8")
        (home / "memories" / "USER.md").write_text("A's user", encoding="utf-8")
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        calls = self._install_race(
            monkeypatch,
            routes,
            a_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": True}},
            b_cfg={"memory": {"memory_enabled": False, "user_profile_enabled": False}},
        )

        h = self._handler()
        routes._handle_memory_read(h)

        body = _body_from_handler(h)
        assert body["memory"] == "A's memory"
        assert body["memory_path"] != ""
        assert body["memory_mtime"] is not None
        assert body["user"] == "A's user"
        assert calls["snapshot"] == 1

    def test_write_memory_disabled_a_not_allowed_by_b_enabled_reload(self, monkeypatch, tmp_path):
        """A memory_enabled=false, B memory_enabled=true: POST memory must 403
        and A's MEMORY.md must remain byte-identical."""
        import api.routes as routes

        home = tmp_path / "profile_a"
        (home / "memories").mkdir(parents=True)
        mem_file = home / "memories" / "MEMORY.md"
        mem_file.write_text("original bytes", encoding="utf-8")
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        calls = self._install_race(
            monkeypatch,
            routes,
            a_cfg={"memory": {"memory_enabled": False, "user_profile_enabled": True}},
            b_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": True}},
        )

        h = self._handler()
        routes._handle_memory_write(h, {"section": "memory", "content": "should be blocked"})

        assert h.status == 403
        assert mem_file.read_bytes() == b"original bytes"
        assert calls["snapshot"] == 1

    def test_write_user_disabled_a_not_allowed_by_b_enabled_reload(self, monkeypatch, tmp_path):
        """A user_profile_enabled=false, B user_profile_enabled=true: POST user
        must 403 and A's USER.md must remain byte-identical."""
        import api.routes as routes

        home = tmp_path / "profile_a"
        (home / "memories").mkdir(parents=True)
        user_file = home / "memories" / "USER.md"
        user_file.write_text("original user", encoding="utf-8")
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        calls = self._install_race(
            monkeypatch,
            routes,
            a_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": False}},
            b_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": True}},
        )

        h = self._handler()
        routes._handle_memory_write(h, {"section": "user", "content": "should be blocked"})

        assert h.status == 403
        assert user_file.read_bytes() == b"original user"
        assert calls["snapshot"] == 1

    def test_write_inverse_enabled_a_not_blocked_by_b_disabled_reload(self, monkeypatch, tmp_path):
        """Inverse: A memory_enabled=true, B memory_enabled=false. B's disabled
        config must NOT leak into A — A's write goes through (200, persisted)."""
        import api.routes as routes

        home = tmp_path / "profile_a"
        (home / "memories").mkdir(parents=True)
        mem_file = home / "memories" / "MEMORY.md"
        mem_file.write_text("old", encoding="utf-8")
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        calls = self._install_race(
            monkeypatch,
            routes,
            a_cfg={"memory": {"memory_enabled": True, "user_profile_enabled": True}},
            b_cfg={"memory": {"memory_enabled": False, "user_profile_enabled": True}},
        )

        h = self._handler()
        routes._handle_memory_write(h, {"section": "memory", "content": "new content"})

        assert h.status == 200
        assert mem_file.read_text(encoding="utf-8") == "new content"
        assert calls["snapshot"] == 1


class TestRealLoaderNestedConfig:
    """The gates must read the REAL nested cfg['memory'] flags via the real
    config loader — a config.yaml with ``memory: {memory_enabled: false}`` must
    blank the read and deny the write (403) without creating ``memories/``.
    """

    _DISABLED_YAML = (
        "memory:\n"
        "  memory_enabled: false\n"
        "  user_profile_enabled: false\n"
    )

    def test_real_loader_disabled_read_blank_and_write_403_no_mkdir(
        self, monkeypatch, tmp_path, mock_handler
    ):
        import api.config as config
        import api.routes as routes

        # Real profile home with private memory files and a real config.yaml
        # using the nested ``memory`` section (Hermes Agent's schema).
        home = tmp_path / "profile_home"
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "MEMORY.md").write_text("private memory", encoding="utf-8")
        (home / "memories" / "USER.md").write_text("private user", encoding="utf-8")
        (home / "SOUL.md").write_text("soul", encoding="utf-8")
        config_path = home / "config.yaml"
        config_path.write_text(self._DISABLED_YAML, encoding="utf-8")

        # Load through the real loader: point _get_config_path at the temp
        # config.yaml and force a reload so the process cache is parsed from it.
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        config.reload_config()
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)

        routes._handle_memory_read(mock_handler)

        assert mock_handler.status == 200
        body = _body_from_handler(mock_handler)
        # Disabled read: existing private files must come back blank.
        assert body["memory"] == ""
        assert body["memory_path"] == ""
        assert body["memory_mtime"] is None
        assert body["user"] == ""
        assert body["user_path"] == ""
        assert body["user_mtime"] is None
        # SOUL is unaffected.
        assert body["soul"] == "soul"

        # Disabled write: 403 and memories/ must NOT be created.
        write_home = tmp_path / "write_home"
        write_home.mkdir()
        write_config = write_home / "config.yaml"
        write_config.write_text(self._DISABLED_YAML, encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: write_config)
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: write_home)
        config.reload_config()

        h2 = MagicMock()
        h2.wfile = io.BytesIO()
        h2.send_response = lambda s: setattr(h2, "status", s)
        h2.send_header = lambda k, v: None
        h2.end_headers = MagicMock()

        routes._handle_memory_write(h2, {"section": "memory", "content": "should be blocked"})

        assert h2.status == 403
        body = _body_from_handler(h2)
        assert "disabled" in body.get("error", "").lower()
        assert not (write_home / "memories").exists(), (
            "memories/ must not be created when memory is disabled"
        )
