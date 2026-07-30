"""Tests for #1695 — diagnostic detail in the "AIAgent not available" ImportError.

Patrick-81 reported a symlinked hermes-agent install that produced a bare
"AIAgent not available -- check that hermes-agent is on sys.path" error with
no information about which Python was running, where it was looking, or what
to do next. The maintainer's response (which Patrick confirmed worked)
amounted to: run three diagnostic commands, then `pip install -e .` in the
agent dir.

This test suite locks the diagnostic shape of the new error message:

  - The original message string is preserved (so existing log scrapers /
    monitoring / docs-search keep working).
  - The running python interpreter path is included.
  - HERMES_WEBUI_AGENT_DIR is shown if set, "(not set)" otherwise.
  - The relevant sys.path entries are shown.
  - A pip install -e . hint is included.
  - A pointer to docs/troubleshooting.md is included.

Behavioural test for the actual raise path lives in the streaming integration
suite; this file only exercises the helper.
"""
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_helper():
    """Import _aiagent_import_error_detail without triggering the full streaming
    module side-effects.

    api/streaming.py imports a lot at top-level (gateway routing, model resolver,
    session DB, ...). For a focused unit test we just need the helper. Importing
    the module is fine — it stays cached for the rest of the suite.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from api import streaming  # noqa: F401
    return streaming._aiagent_import_error_detail


class TestAIAgentImportErrorDetail:
    """Unit tests for the diagnostic helper."""

    def test_preserves_original_message_for_log_scrapers(self):
        """The original error string must remain the FIRST line so existing
        scrapers, alerting, and docs-search keep matching.
        """
        helper = _import_helper()
        out = helper()
        first = out.splitlines()[0]
        assert first == "AIAgent not available -- check that hermes-agent is on sys.path", (
            f"first line must be the original error message verbatim, got: {first!r}"
        )

    def test_includes_running_python_interpreter(self):
        """The diagnostic must include the running python so the user knows
        which interpreter is missing the agent (most common cause of the bug).
        """
        helper = _import_helper()
        out = helper()
        assert "python:" in out
        assert sys.executable in out, (
            f"running python ({sys.executable}) must appear in the diagnostic"
        )

    def test_shows_agent_dir_env_when_set(self, monkeypatch):
        """If HERMES_WEBUI_AGENT_DIR is set, the diagnostic must show its value
        so the user can confirm whether the override is pointing at the right
        directory.
        """
        helper = _import_helper()
        monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", "/custom/agent/path")
        out = helper()
        assert "HERMES_WEBUI_AGENT_DIR: /custom/agent/path" in out

    def test_shows_agent_dir_env_unset_marker(self, monkeypatch):
        """If HERMES_WEBUI_AGENT_DIR is NOT set, the diagnostic must say so
        explicitly — silence is ambiguous (could be empty string, could be unset).
        """
        helper = _import_helper()
        monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
        out = helper()
        assert "HERMES_WEBUI_AGENT_DIR: (not set)" in out

    def test_includes_pip_install_editable_hint(self):
        """The most common fix (per #1695) is `pip install -e .` in the agent dir.
        The diagnostic must surface this as the first-line remediation.
        """
        helper = _import_helper()
        out = helper()
        assert "pip install -e ." in out, (
            "diagnostic must surface `pip install -e .` as the most common fix"
        )

    def test_points_at_troubleshooting_doc(self):
        """The diagnostic must point at the docs/troubleshooting.md entry so
        users with edge-case failures know where to look next.
        """
        helper = _import_helper()
        out = helper()
        assert "troubleshooting" in out.lower(), (
            "diagnostic must point at docs/troubleshooting.md for further help"
        )

    def test_lists_sys_path_entries_when_relevant(self, monkeypatch):
        """If sys.path contains entries mentioning hermes/agent, the diagnostic
        must list them (helps the user confirm the agent dir is or isn't
        actually present on the import path).
        """
        helper = _import_helper()
        # Force at least one relevant entry into sys.path for the test.
        monkeypatch.syspath_prepend("/fake/hermes-agent")
        out = helper()
        assert "/fake/hermes-agent" in out

    def test_handles_no_relevant_sys_path_entries(self, monkeypatch):
        """If sys.path has NO hermes/agent-related entries, the diagnostic must
        say so explicitly — this is itself a strong diagnostic signal.
        """
        helper = _import_helper()
        # Replace sys.path with entries that mention neither hermes nor agent.
        # Use monkeypatch.setattr so the change reverts cleanly.
        clean_path = ["/usr/lib/python3.11", "/usr/local/lib/python3.11", "/tmp"]
        monkeypatch.setattr(sys, "path", clean_path)
        out = helper()
        assert "no entries mention hermes or agent" in out, (
            "diagnostic must explicitly call out empty-path case (it's a strong signal)"
        )

    def test_output_is_multiline_string(self):
        """The diagnostic must be a multi-line string (newline-joined), not a
        single long line — log-readability matters when this surfaces in a
        traceback.
        """
        helper = _import_helper()
        out = helper()
        assert "\n" in out, "diagnostic must be multi-line for log readability"
        assert len(out.splitlines()) >= 5, (
            f"diagnostic must have at least 5 lines, got {len(out.splitlines())}"
        )


class TestAIAgentImportErrorDocsPresence:
    """Regression: the docs/troubleshooting.md file must exist with the
    "AIAgent not available" entry the diagnostic links to.
    """

    def test_troubleshooting_md_exists(self):
        path = REPO_ROOT / "docs" / "troubleshooting.md"
        assert path.exists(), "docs/troubleshooting.md must exist (referenced by streaming.py)"

    def test_troubleshooting_md_has_aiagent_section(self):
        path = REPO_ROOT / "docs" / "troubleshooting.md"
        content = path.read_text(encoding="utf-8")
        assert "AIAgent not available" in content, (
            "docs/troubleshooting.md must have an entry titled \"AIAgent not available\""
        )

    def test_troubleshooting_md_includes_pip_install_editable(self):
        """The doc must surface the `pip install -e .` fix."""
        path = REPO_ROOT / "docs" / "troubleshooting.md"
        content = path.read_text(encoding="utf-8")
        assert "pip install -e ." in content, (
            "docs/troubleshooting.md must include the pip install -e . fix"
        )

    def test_troubleshooting_md_describes_diagnostic_steps(self):
        """The doc must walk through diagnostic commands (readlink, ls, etc.)
        before jumping to the fix — that ordering is what worked for #1695.
        """
        path = REPO_ROOT / "docs" / "troubleshooting.md"
        content = path.read_text(encoding="utf-8")
        # Look for the symlink-resolution diagnostic chain.
        assert "readlink" in content, (
            "diagnostic flow must include `readlink` for the symlink-typo failure mode"
        )
        assert "/agent/__init__.py" in content, (
            "diagnostic flow must verify the agent module file is reachable"
        )
