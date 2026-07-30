"""Tests for STATE_DIR divergence warning (issue #3915).

When a user switches launch methods (bootstrap.py / ctl.sh / systemd), the
HERMES_WEBUI_STATE_DIR env var may differ from the previous run, leaving
the current state directory empty while session data exists in a sibling.

This test suite verifies that _warn_state_dir_divergence() detects this
condition and prints a diagnostic warning, helping users recover their sessions.
"""

import tempfile
from pathlib import Path
from unittest import mock
import pytest


def test_warn_when_session_dir_empty_and_sibling_has_data(capsys):
    """Warning should print when SESSION_DIR is empty but a sibling has sessions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current (empty) state directory
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", current_state / "sessions" / "_index.json"):
                    # Import after patching to get the patched values
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" in captured.out
        assert str(current_state) in captured.out
        assert str(sibling_state) in captured.out
        assert "HERMES_WEBUI_STATE_DIR" in captured.out


def test_no_warn_when_session_dir_has_files(capsys):
    """No warning should print when SESSION_DIR has session files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with session data
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        (current_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session2.json").write_text('{"id": "s2"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", current_state / "sessions" / "_index.json"):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" not in captured.out


def test_no_warn_when_index_file_has_valid_content(capsys):
    """No warning should print when SESSION_INDEX_FILE has valid JSON content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with valid index file but no session files
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"
        index_file.write_text('{"sessions": ["s1", "s2"]}')

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" not in captured.out


def test_no_warn_when_no_sibling_with_data(capsys):
    """No warning should print when no sibling has session data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current (empty) state directory
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()

        # Create sibling without sessions directory
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", current_state / "sessions" / "_index.json"):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" not in captured.out


def test_warn_with_empty_index_file(capsys):
    """Warning should print when SESSION_INDEX_FILE is empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with empty index file
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"
        index_file.write_text("")

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" in captured.out


def test_warn_with_index_file_containing_null(capsys):
    """Warning should print when SESSION_INDEX_FILE contains only 'null'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with null index file
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"
        index_file.write_text("null")

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" in captured.out


def test_warn_with_index_file_containing_empty_object(capsys):
    """Warning should print when SESSION_INDEX_FILE contains only '{}'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with {} index file
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"
        index_file.write_text("{}")

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" in captured.out


def test_warn_with_index_file_containing_empty_array(capsys):
    """Warning should print when SESSION_INDEX_FILE contains only '[]'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with [] index file
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"
        index_file.write_text("[]")

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        assert "STATE_DIR is empty but a sibling state directory has session data" in captured.out


def test_exception_handling_missing_session_dir(capsys):
    """Function should not crash if SESSION_DIR doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory (without sessions subdir)
        current_state = tmpdir / "current"
        current_state.mkdir()

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "nonexistent"):
                with mock.patch("api.config.SESSION_INDEX_FILE", current_state / "nonexistent" / "_index.json"):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"
                    # Should not raise
                    config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        # No output expected since SESSION_DIR doesn't exist and parent iteration is safe
        assert "STATE_DIR is empty but a sibling state directory has session data" not in captured.out


def test_exception_handling_permission_error(capsys):
    """Function should not crash on permission errors when reading files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory with unreadable index file
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"
        index_file.write_text("")

        # Create sibling state directory with session data
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()
        (sibling_state / "sessions" / "session1.json").write_text('{"id": "s1"}')

        # Mock the global config variables and patch open to raise PermissionError
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    with mock.patch("builtins.open", side_effect=PermissionError("Access denied")):
                        import api.config as config

                        warn_prefix = "\033[33m[!!]\033[0m"
                        # Should not raise
                        config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        # Warning should print since index_file_empty stays True when open fails
        assert "STATE_DIR is empty but a sibling state directory has session data" in captured.out


def test_exception_handling_glob_error(capsys):
    """Function should not crash on exception during glob operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create current state directory
        current_state = tmpdir / "current"
        current_state.mkdir()
        (current_state / "sessions").mkdir()
        index_file = current_state / "sessions" / "_index.json"

        # Create sibling with sessions
        sibling_state = tmpdir / "sibling"
        sibling_state.mkdir()
        (sibling_state / "sessions").mkdir()

        # Mock the global config variables
        with mock.patch("api.config.STATE_DIR", current_state):
            with mock.patch("api.config.SESSION_DIR", current_state / "sessions"):
                with mock.patch("api.config.SESSION_INDEX_FILE", index_file):
                    import api.config as config

                    warn_prefix = "\033[33m[!!]\033[0m"

                    # Patch glob to raise an exception
                    original_glob = Path.glob
                    call_count = [0]

                    def mock_glob(self, pattern):
                        call_count[0] += 1
                        # First glob is in _warn_state_dir_divergence for SESSION_DIR
                        # Let it succeed for the first, fail on sibling scan
                        if call_count[0] > 1:
                            raise OSError("Simulated glob error")
                        return original_glob(self, pattern)

                    with mock.patch.object(Path, "glob", mock_glob):
                        # Should not raise due to outer try/except
                        config._warn_state_dir_divergence(warn_prefix)

        captured = capsys.readouterr()
        # No output expected since exception is caught in the try/except
        assert "STATE_DIR is empty but a sibling state directory has session data" not in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
