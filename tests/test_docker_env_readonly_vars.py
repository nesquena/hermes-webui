"""Regression tests for start.sh's .env parsing handling readonly bash variables.

Background: docker-compose.yml's macOS instructions document
``echo "UID=$(id -u)" >> .env`` to set host UID/GID for bind-mount permission
fixing.  The repo-level .env file is then read by both:

  1. ``docker-compose.yml`` itself (for ${UID}/${GID} variable substitution)
  2. ``start.sh`` (which `source`s the .env to load HERMES_WEBUI_* settings)
  3. ``bootstrap.py`` (via ``_load_repo_dotenv()``)

The old ``set -a; source "${REPO_ROOT}/.env"; set +a`` pattern in start.sh
crashed with ``UID: readonly variable`` when the .env carried UID/GID lines —
because bash treats UID/GID/EUID/EGID/PPID as read-only.  The fix filters
those readonly vars out of the source stream while leaving them intact in the
.env file for docker-compose's substitution.

Sourced from PR #1686 (@binhpt310) — extracted to a focused follow-up after
the parent PR was deferred over an unrelated sibling-repo build-context concern.

These tests pin:
  - The filter pattern is present in start.sh
  - The ``source`` + ``.env`` regression guard at
    test_bootstrap_dotenv.py:181 still passes (both keywords present)
  - All five readonly-name forms (UID, GID, EUID, EGID, PPID) are caught
  - The optional ``export`` prefix on those names is also caught
  - Non-readonly KEY=value lines in .env still load
"""
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
START_SH = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")


class TestStartShReadonlyEnvFilter:
    """Pin start.sh's .env parser against the docker-compose macOS UID/GID flow."""

    def test_start_sh_still_sources_env_regression_guard(self):
        """The bootstrap regression guard at test_bootstrap_dotenv.py:181
        requires ``source`` AND ``.env`` to both appear in start.sh.  After
        the readonly-vars filter, both must still be present."""
        assert "source" in START_SH, (
            "start.sh must still call `source` to load .env "
            "(regression guard, see tests/test_bootstrap_dotenv.py:181)"
        )
        assert ".env" in START_SH, (
            "start.sh must still reference .env path "
            "(regression guard, see tests/test_bootstrap_dotenv.py:181)"
        )

    def test_readonly_vars_filtered_before_source(self):
        """The readonly bash names (UID/GID/EUID/EGID/PPID) must be filtered
        out of the .env stream before `source` reads it.  The filter is a
        ``grep -vE`` against the .env file."""
        # The filter must mention all five readonly names.
        for var in ("UID", "GID", "EUID", "EGID", "PPID"):
            assert var in START_SH, (
                f"start.sh's .env filter must mention readonly var {var!r} "
                "so that bash assignment to it does not crash with "
                f"'{var}: readonly variable'"
            )

    def test_filter_pattern_uses_grep_before_source(self):
        """Filter must use a pattern that strips readonly-var lines before
        the bash `source` consumes them.  The loader may use a temporary file
        rather than process substitution because some bash/macOS combinations
        can source an empty `/dev/fd/*` stream from `source <(grep ...)`."""
        grep_idx = START_SH.find("grep -vE")
        source_idx = START_SH.find("source", grep_idx)
        assert grep_idx != -1, "start.sh must filter readonly vars with grep -vE or equivalent"
        assert source_idx != -1, "start.sh must source the filtered .env stream"
        assert grep_idx < source_idx, "readonly-var filtering must happen before source"

    def test_filter_handles_optional_export_prefix(self):
        """The ``export`` prefix on env vars is optional but common.  The
        readonly-var filter must catch both bare and exported forms."""
        assert "export" in START_SH, (
            "start.sh's .env filter must account for the optional `export` "
            "prefix on readonly-var assignments (e.g. `export UID=501`), "
            "otherwise bash will still crash on the assignment"
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestStartShReadonlyEnvFilterBehavioral:
    """Behavioral tests — actually run bash to verify .env parsing succeeds.

    These tests extract the .env loader block from start.sh and run it
    against synthetic .env files.  They guard against shell-quoting
    regressions in the filter pattern itself (which the source-grep tests
    above can't catch on their own).
    """

    @staticmethod
    def _extract_env_loader(start_sh: str) -> str:
        """Pull out the `if [[ -f "${REPO_ROOT}/.env" ]] ... fi` block."""
        # Find the if-block with .env in it.
        m = re.search(
            r'(if \[\[ -f "\$\{REPO_ROOT\}/\.env" \]\]; then.*?^fi)\n',
            start_sh,
            re.DOTALL | re.MULTILINE,
        )
        assert m is not None, "could not locate .env loader block in start.sh"
        return m.group(1)

    def _run_loader(self, env_contents: str, tmp_path: Path) -> subprocess.CompletedProcess:
        """Write ``env_contents`` to a tmp .env and run start.sh's loader against it."""
        env_file = tmp_path / ".env"
        env_file.write_text(env_contents, encoding="utf-8")

        loader = self._extract_env_loader(START_SH)
        # Wrap loader in a tiny bash script that points REPO_ROOT at tmp_path
        # and then echoes a few keys we care about.
        script = textwrap.dedent(f"""\
            set -euo pipefail
            REPO_ROOT={str(tmp_path)!r}
            {loader}
            # Print loaded values (or "unset") for the test to assert against.
            echo "PORT=${{HERMES_WEBUI_PORT:-unset}}"
            echo "SOME=${{SOME_KEY:-unset}}"
            echo "ANOTHER=${{ANOTHER:-unset}}"
            echo "EXIT_OK"
        """)

        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_env_with_readonly_uid_gid_does_not_crash(self, tmp_path):
        """The exact macOS docker-compose pattern: UID + GID in .env."""
        env_contents = textwrap.dedent("""\
            UID=501
            GID=20
            HERMES_WEBUI_PORT=8888
            SOME_KEY=normal-value
        """)
        result = self._run_loader(env_contents, tmp_path)
        assert "EXIT_OK" in result.stdout, (
            f"loader crashed on .env with readonly UID/GID. "
            f"stderr: {result.stderr!r}"
        )
        assert "readonly variable" not in result.stderr, (
            f".env loader still triggered readonly-variable crash: "
            f"{result.stderr!r}"
        )
        # Non-readonly keys must still load.
        assert "PORT=8888" in result.stdout
        assert "SOME=normal-value" in result.stdout

    def test_env_with_exported_readonly_does_not_crash(self, tmp_path):
        """`export UID=501` form must also be filtered."""
        env_contents = textwrap.dedent("""\
            export UID=501
            export GID=20
            HERMES_WEBUI_PORT=9000
        """)
        result = self._run_loader(env_contents, tmp_path)
        assert "EXIT_OK" in result.stdout
        assert "readonly variable" not in result.stderr
        assert "PORT=9000" in result.stdout

    def test_all_five_readonly_names_filtered(self, tmp_path):
        """UID, GID, EUID, EGID, PPID — all five must be filtered."""
        env_contents = textwrap.dedent("""\
            UID=501
            GID=20
            EUID=501
            EGID=20
            PPID=12345
            HERMES_WEBUI_PORT=7777
        """)
        result = self._run_loader(env_contents, tmp_path)
        assert "EXIT_OK" in result.stdout, (
            f"loader crashed; stderr: {result.stderr!r}"
        )
        assert "readonly variable" not in result.stderr
        assert "PORT=7777" in result.stdout

    def test_normal_env_still_loads(self, tmp_path):
        """A .env without readonly vars must still load all keys."""
        env_contents = textwrap.dedent("""\
            HERMES_WEBUI_PORT=8787
            SOME_KEY=hello
            ANOTHER=world
        """)
        result = self._run_loader(env_contents, tmp_path)
        assert "EXIT_OK" in result.stdout
        assert "PORT=8787" in result.stdout
        assert "SOME=hello" in result.stdout
        assert "ANOTHER=world" in result.stdout

    def test_export_prefix_strips_correctly(self, tmp_path):
        """`export FOO=bar` (non-readonly) loads `FOO=bar` after `set -a; source`."""
        env_contents = textwrap.dedent("""\
            UID=501
            export ANOTHER=exported-value
            HERMES_WEBUI_PORT=6543
        """)
        result = self._run_loader(env_contents, tmp_path)
        assert "EXIT_OK" in result.stdout
        assert "ANOTHER=exported-value" in result.stdout
        assert "PORT=6543" in result.stdout


class TestDockerfileSystemPackages:
    """Pin Dockerfile system-package dependencies (#1686 Cluster 1)."""

    def test_dockerfile_installs_xz_utils(self):
        """xz-utils is required to extract xz-compressed tarballs (e.g.
        Node.js distribution archives) — without it, agent install paths
        that download xz-compressed deps fail with `xz: Cannot exec`."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(r"\bxz-utils\b", dockerfile), (
            "Dockerfile must install xz-utils (apt package) — without it, "
            "any tarball decompression of .tar.xz files fails with "
            "`xz: Cannot exec: No such file or directory`"
        )

    def test_dockerfile_installs_git(self):
        """git is needed for any agent-install path that clones a repo, plus
        for the runtime ``git describe`` that powers WEBUI_VERSION detection
        in non-baked images."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(r"^\s*git\s*\\?\s*$", dockerfile, re.MULTILINE), (
            "Dockerfile must install git (apt package) — required for "
            "version detection (`git describe`) and any agent install path "
            "that clones a repo"
        )
