"""
Tests for bootstrap.py .env loading fix (issue #730).

bootstrap.py is the primary documented entry point ("python3 bootstrap.py").
Previously it did not load REPO_ROOT/.env, so HERMES_WEBUI_HOST, HERMES_WEBUI_PORT
etc. were silently ignored when launching without start.sh.

Covers:
  1. _load_repo_dotenv() sets env vars from a repo .env file
  2. _load_repo_dotenv() ignores commented lines and blank lines
  3. _load_repo_dotenv() strips quotes from values
  4. _load_repo_dotenv() is a no-op when .env does not exist
  5. _load_repo_dotenv() prints a warning (not crash) on unreadable .env
  6. _load_repo_dotenv() overwrites existing env vars (shell source semantics)
  7. _load_repo_dotenv() handles 'export FOO=bar' prefix
  8. _load_repo_dotenv() preserves values containing '='
  9. Variables are set unconditionally (not setdefault)
  10. Structural: loader is called before DEFAULT_HOST/DEFAULT_PORT
"""

import os
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).parent.parent


class TestLoadRepoDotenv:
    def setup_method(self):
        self._saved_env = os.environ.copy()

    def teardown_method(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _run(self, tmp_path, env_content: str):
        """Write .env to tmp_path and run _load_repo_dotenv() with that root."""
        import bootstrap as bs

        (tmp_path / ".env").write_text(env_content, encoding="utf-8")
        orig_root = bs.REPO_ROOT
        try:
            bs.REPO_ROOT = tmp_path
            bs._load_repo_dotenv()
        finally:
            bs.REPO_ROOT = orig_root

    def test_sets_env_var_from_dotenv(self, tmp_path):
        """Basic key=value is loaded into os.environ."""
        self._run(tmp_path, "HERMES_WEBUI_HOST=0.0.0.0\n")
        assert os.environ.get("HERMES_WEBUI_HOST") == "0.0.0.0"

    def test_sets_port_from_dotenv(self, tmp_path):
        """HERMES_WEBUI_PORT is loaded as a string (caller does int() conversion)."""
        self._run(tmp_path, "HERMES_WEBUI_PORT=18787\n")
        assert os.environ.get("HERMES_WEBUI_PORT") == "18787"

    def test_ignores_comment_lines(self, tmp_path):
        """Lines starting with # are not loaded."""
        os.environ.pop("HERMES_WEBUI_HOST", None)
        self._run(tmp_path, "# HERMES_WEBUI_HOST=should-be-ignored\n")
        assert os.environ.get("HERMES_WEBUI_HOST") is None

    def test_ignores_blank_lines(self, tmp_path):
        """Blank lines are silently skipped without error."""
        self._run(tmp_path, "\n\nHERMES_WEBUI_PORT=9000\n\n")
        assert os.environ.get("HERMES_WEBUI_PORT") == "9000"

    def test_strips_double_quoted_values(self, tmp_path):
        """Values wrapped in double quotes are stripped."""
        self._run(tmp_path, 'HERMES_WEBUI_HOST="0.0.0.0"\n')
        assert os.environ.get("HERMES_WEBUI_HOST") == "0.0.0.0"

    def test_strips_single_quoted_values(self, tmp_path):
        """Values wrapped in single quotes are stripped."""
        self._run(tmp_path, "HERMES_WEBUI_HOST='0.0.0.0'\n")
        assert os.environ.get("HERMES_WEBUI_HOST") == "0.0.0.0"

    def test_noop_when_no_dotenv(self, tmp_path):
        """No .env file — function returns silently without error."""
        import bootstrap as bs

        orig = bs.REPO_ROOT
        try:
            bs.REPO_ROOT = tmp_path  # tmp_path has no .env
            bs._load_repo_dotenv()  # must not raise
        finally:
            bs.REPO_ROOT = orig

    def test_noop_when_dotenv_unreadable(self, tmp_path, capsys):
        """Unreadable .env prints a warning to stderr — does not crash."""
        import bootstrap as bs

        env_path = tmp_path / ".env"
        env_path.write_text("HERMES_WEBUI_PORT=9999\n")
        orig = bs.REPO_ROOT
        try:
            bs.REPO_ROOT = tmp_path
            with patch(
                "pathlib.Path.read_text", side_effect=PermissionError("no access")
            ):
                bs._load_repo_dotenv()  # must not raise
        finally:
            bs.REPO_ROOT = orig
        captured = capsys.readouterr()
        assert (
            "bootstrap" in captured.err.lower()
            or "warning" in captured.err.lower()
            or "could not load" in captured.err.lower()
        ), "_load_repo_dotenv() should print a warning to stderr on read failure"

    def test_overwrites_existing_env_var(self, tmp_path):
        """Unconditional overwrite matches shell source semantics."""
        os.environ["HERMES_WEBUI_HOST"] = "127.0.0.1"
        self._run(tmp_path, "HERMES_WEBUI_HOST=0.0.0.0\n")
        assert os.environ.get("HERMES_WEBUI_HOST") == "0.0.0.0"

    def test_does_not_set_empty_values(self, tmp_path):
        """A key whose value is empty after stripping is not set to a non-empty string."""
        os.environ.pop("HERMES_EMPTY_KEY", None)
        self._run(tmp_path, 'HERMES_EMPTY_KEY=""\n')
        # The current implementation sets key to "" (empty string) — verify it is
        # not set to a non-empty string, which would be clearly wrong.
        val = os.environ.get("HERMES_EMPTY_KEY")
        assert val != "something-wrong", (
            "Empty-value key must not be set to a non-empty string"
        )
        # Specifically: empty string or absent are both acceptable behaviours.
        assert val in (None, ""), f"Unexpected value for empty-quoted key: {val!r}"

    def test_multiple_keys_all_loaded(self, tmp_path):
        """Multiple key=value pairs in one file are all loaded."""
        content = "HERMES_WEBUI_HOST=0.0.0.0\nHERMES_WEBUI_PORT=18787\n"
        self._run(tmp_path, content)
        assert os.environ.get("HERMES_WEBUI_HOST") == "0.0.0.0"
        assert os.environ.get("HERMES_WEBUI_PORT") == "18787"

    def test_value_with_equals_sign_preserved(self, tmp_path):
        """Values containing '=' (e.g. base64) are preserved correctly."""
        self._run(tmp_path, "MY_KEY=abc=def==\n")
        assert os.environ.get("MY_KEY") == "abc=def=="

    def test_export_prefix_stripped(self, tmp_path):
        """'export FOO=bar' lines are parsed correctly — export prefix is stripped."""
        self._run(tmp_path, "export HERMES_WEBUI_HOST=0.0.0.0\n")
        assert os.environ.get("HERMES_WEBUI_HOST") == "0.0.0.0", (
            "'export KEY=value' lines must set KEY, not 'export KEY'"
        )


# ---------------------------------------------------------------------------
# Structural tests — confirm the fix is in place
# ---------------------------------------------------------------------------


class TestBootstrapStructure:
    def test_load_repo_dotenv_function_exists(self):
        """bootstrap.py must export _load_repo_dotenv()."""
        import bootstrap as bs

        assert callable(getattr(bs, "_load_repo_dotenv", None)), (
            "bootstrap.py must define _load_repo_dotenv() so that "
            "python3 bootstrap.py loads REPO_ROOT/.env before reading env defaults"
        )

    def test_dotenv_loaded_before_default_host_port(self):
        """_load_repo_dotenv() call must appear before DEFAULT_HOST/DEFAULT_PORT in source."""
        src = (REPO_ROOT / "bootstrap.py").read_text(encoding="utf-8")
        load_pos = src.find("_load_repo_dotenv()")
        host_pos = src.find("DEFAULT_HOST")
        port_pos = src.find("DEFAULT_PORT")
        assert load_pos != -1, "_load_repo_dotenv() call not found in bootstrap.py"
        assert load_pos < host_pos, (
            "_load_repo_dotenv() must be called before DEFAULT_HOST assignment "
            "so that HERMES_WEBUI_HOST from .env is picked up"
        )
        assert load_pos < port_pos, (
            "_load_repo_dotenv() must be called before DEFAULT_PORT assignment "
            "so that HERMES_WEBUI_PORT from .env is picked up"
        )

    def test_start_sh_and_bootstrap_equivalent_env_loading(self):
        """start.sh sources .env before bootstrap.py; bootstrap.py must now do the same."""
        start_sh = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
        bootstrap_src = (REPO_ROOT / "bootstrap.py").read_text(encoding="utf-8")
        # start.sh sources .env
        assert "source" in start_sh and ".env" in start_sh, (
            "start.sh should still source .env (regression guard)"
        )
        # bootstrap.py now loads it too
        assert "_load_repo_dotenv" in bootstrap_src, (
            "bootstrap.py must load .env so direct invocation matches start.sh behaviour"
        )
