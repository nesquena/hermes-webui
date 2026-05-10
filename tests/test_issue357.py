"""
Tests for GitHub issue #357: Docker container fails to start without internet access.

Structural tests — verify Dockerfile and docker_init.bash contain the expected
patterns for pre-installed uv and workspace permission fixes.

Two problems fixed:
1. uv was downloaded at container startup; fails in air-gapped / firewalled environments.
   Fix: pre-install uv in the Docker image at build time (system-wide in /usr/local/bin).
2. workspace directory setup must happen before the server drops privileges;
   bind-mount dirs created by Docker as root are unwritable by hermeswebui.
   Fix: root init mkdir/chown, then runtime verifies access without sudo.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
INIT_SCRIPT = (REPO / "docker_init.bash").read_text(encoding="utf-8")


# ── Dockerfile: uv pre-installed at build time ───────────────────────────────


class TestDockerfileUvPreinstall:
    def test_dockerfile_installs_uv_at_build_time(self):
        """Dockerfile must install uv via RUN curl at build time (not only at runtime)."""
        assert "RUN curl" in DOCKERFILE and "uv/install.sh" in DOCKERFILE, (
            "Dockerfile must install uv at build time via RUN curl .../uv/install.sh"
        )

    def test_dockerfile_uv_installed_system_wide(self):
        """uv must be installed to a system-wide directory (/usr/local/bin) accessible
        to all users, not to a user-specific ~/.local/bin that another user can't see."""
        # The install command must target /usr/local/bin or use root to install globally
        uv_install_line = next(
            (line for line in DOCKERFILE.splitlines() if "uv/install.sh" in line),
            None,
        )
        assert uv_install_line is not None, (
            "Could not find uv install line in Dockerfile"
        )
        # Must either use UV_INSTALL_DIR pointing to /usr/local/bin, or run as root
        # (so the default install location is accessible to hermeswebui user)
        has_system_dir = (
            "/usr/local/bin" in uv_install_line
            or "UV_INSTALL_DIR=/usr/local/bin" in DOCKERFILE
        )
        assert has_system_dir, (
            "uv must be installed to /usr/local/bin (system-wide) so hermeswebui user "
            "can find it. Installing as hermeswebuitoo puts it in /home/hermeswebuitoo/.local/bin "
            "which is NOT on hermeswebui's PATH."
        )

    def test_dockerfile_uv_installed_before_copy(self):
        """uv installation must happen before COPY . /apptoo so it's in the image."""
        import re

        uv_pos = DOCKERFILE.find("uv/install.sh")
        # Match COPY regardless of flags (e.g. --chown=...) — only the destination matters.
        m = re.search(r"^COPY\b.*\s/apptoo\b", DOCKERFILE, re.MULTILINE)
        assert uv_pos != -1, "uv install not found in Dockerfile"
        assert m is not None, "COPY ... /apptoo not found in Dockerfile"
        copy_pos = m.start()
        assert uv_pos < copy_pos, "uv must be installed before COPY . /apptoo"

    def test_dockerfile_uv_installed_as_root_or_before_user_switch(self):
        """uv must be installed as root (USER root) to reach /usr/local/bin.
        If installed as hermeswebuitoo, it lands in ~hermeswebuitoo/.local/bin,
        which the hermeswebui user at runtime can't see.
        """
        lines = DOCKERFILE.splitlines()
        uv_line_idx = next(i for i, l in enumerate(lines) if "uv/install.sh" in l)
        # Find the last USER directive before the uv install line
        user_before = None
        for i in range(uv_line_idx - 1, -1, -1):
            if lines[i].strip().startswith("USER "):
                user_before = lines[i].strip().split()[1]
                break
        assert user_before == "root", (
            f"uv install must run as USER root (found USER {user_before!r}). "
            "Installing as hermeswebuitoo puts uv in /home/hermeswebuitoo/.local/bin "
            "which is not accessible to the hermeswebui runtime user."
        )


# ── docker_init.bash: skip uv download when already present ─────────────────


class TestInitScriptUvSkip:
    def test_init_script_checks_uv_before_download(self):
        """docker_init.bash must check 'command -v uv' before attempting download."""
        assert "command -v uv" in INIT_SCRIPT, (
            "docker_init.bash must check 'command -v uv' to skip download "
            "when uv is already pre-installed in the image (#357)"
        )

    def test_init_script_skips_download_if_present(self):
        """Init script must use conditional logic (if/else) around the uv download."""
        # Pattern: if command -v uv ... else ... fi
        assert re.search(r"if\s+command\s+-v\s+uv", INIT_SCRIPT), (
            "docker_init.bash must use 'if command -v uv' guard around the download"
        )

    def test_init_script_curl_download_in_else_branch(self):
        """The curl download must be in the else branch (only runs if uv not found)."""
        # Find the conditional block
        m = re.search(r"if\s+command\s+-v\s+uv.*?fi", INIT_SCRIPT, re.DOTALL)
        assert m, "Could not find uv conditional block in docker_init.bash"
        block = m.group(0)
        # curl must appear after 'else' not in the 'then' branch
        else_pos = block.find("else")
        curl_pos = block.find("curl")
        assert else_pos != -1, "No 'else' branch in uv conditional"
        assert curl_pos != -1, "No 'curl' in uv conditional block"
        assert curl_pos > else_pos, (
            "curl download must be in the 'else' branch, not the 'if/then' branch"
        )

    def test_init_script_error_exit_on_download_failure(self):
        """Curl download must call error_exit on failure (not silently continue)."""
        assert "error_exit" in INIT_SCRIPT and "Failed to install uv" in INIT_SCRIPT, (
            "docker_init.bash must call error_exit if uv download fails, "
            "so the container exits with a clear message instead of failing silently"
        )

    def test_init_script_path_includes_hermeswebui_local_bin(self):
        """PATH must include /home/hermeswebui/.local/bin for fallback runtime install."""
        assert "/home/hermeswebui/.local/bin" in INIT_SCRIPT, (
            "docker_init.bash must include /home/hermeswebui/.local/bin in PATH "
            "for the case where uv is installed at runtime via curl"
        )


# ── docker_init.bash: workspace directory permissions ────────────────────────


class TestWorkspacePermissions:
    def test_workspace_uses_root_init_mkdir(self):
        """docker_init.bash must create missing workspaces during root init.

        Docker auto-creates bind-mount directories as root if they don't exist,
        leaving them unwritable by hermeswebui. The production image no longer
        ships sudo, so root init handles mkdir before dropping privileges.
        """
        root_section = INIT_SCRIPT[
            INIT_SCRIPT.find('if [ "A${whoami}" == "Aroot" ]; then') : INIT_SCRIPT.find(
                "exec su"
            )
        ]
        assert 'mkdir -p "$HERMES_WEBUI_DEFAULT_WORKSPACE"' in root_section, (
            "docker_init.bash must mkdir the workspace during root init "
            "to handle Docker-created bind-mount dirs (#357)"
        )

    def test_workspace_uses_root_init_chown(self):
        """docker_init.bash must chown the workspace before dropping privileges.

        The server runtime does not have sudo; the privileged init phase may
        chown writable bind mounts, while read-only mounts continue with a warning.
        """
        root_section = INIT_SCRIPT[
            INIT_SCRIPT.find('if [ "A${whoami}" == "Aroot" ]; then') : INIT_SCRIPT.find(
                "exec su"
            )
        ]
        assert (
            'chown hermeswebui:hermeswebui "$HERMES_WEBUI_DEFAULT_WORKSPACE"'
            in root_section
        ), (
            "docker_init.bash must chown the workspace during root init "
            "so the app user can write to it when possible (#357)"
        )

    def test_workspace_mkdir_before_chown(self):
        """Root init mkdir must come before root init chown in docker_init.bash."""
        mkdir_pos = INIT_SCRIPT.find('mkdir -p "$HERMES_WEBUI_DEFAULT_WORKSPACE"')
        chown_pos = INIT_SCRIPT.find(
            'chown hermeswebui:hermeswebui "$HERMES_WEBUI_DEFAULT_WORKSPACE"'
        )
        assert mkdir_pos != -1, "root init mkdir for workspace not found"
        assert chown_pos != -1, "root init chown for workspace not found"
        assert mkdir_pos < chown_pos, "root init mkdir must come before root init chown"

    def test_workspace_error_exit_on_mkdir_failure(self):
        """Root init mkdir must call error_exit on failure."""
        assert (
            'mkdir -p "$HERMES_WEBUI_DEFAULT_WORKSPACE" || error_exit' in INIT_SCRIPT
        ), "workspace mkdir must call error_exit on failure"

    def test_workspace_write_test_is_conditional_on_writable(self):
        """Write-test must be skipped for read-only workspace mounts (#670).

        The runtime phase must check [ -w "$HERMES_WEBUI_DEFAULT_WORKSPACE" ] before
        attempting a write test, so :ro bind-mounts don't crash startup.
        """
        assert '[ -w "$HERMES_WEBUI_DEFAULT_WORKSPACE" ]' in INIT_SCRIPT, (
            "docker_init.bash must guard the workspace write-test with [ -w ] "
            "to support read-only workspace mounts (:ro) without crashing (#670)"
        )
        # Read-only path must log a clear message rather than calling error_exit
        assert "read-only workspace is supported" in INIT_SCRIPT, (
            "docker_init.bash must print a clear message when workspace is read-only (#670)"
        )

    def test_init_script_syntax_valid(self):
        """docker_init.bash must pass bash -n syntax check."""
        import subprocess

        result = subprocess.run(
            ["bash", "-n", str(REPO / "docker_init.bash")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"docker_init.bash failed bash -n syntax check:\n{result.stderr}"
        )
