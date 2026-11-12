"""Regression tests for #7027 — UID/GID auto-detection probe order.

Background: ``docker_init.bash`` auto-detects the UID/GID to remap the
``hermeswebui`` user to, by stat-ing mounted directories. Before this fix the
probe list was:

    priority 1: /home/hermeswebui/.hermes, $HERMES_HOME, /opt/data
    priority 2: /workspace
    fallback:   1024

In a stock single-container image none of the priority-1 candidates exist, but
``/workspace`` *does* — owned by the image's build-time ``1024:1024``. Detection
therefore returned the image's own owner, which carries no information about the
host, while the one directory that carries the host UID by definition — the
state-dir bind mount — was never probed. With a host-owned state mount and no
explicit ``WANTED_UID``, the container remapped to 1024, failed its own
writability check on the state dir, and restart-looped:

    touch: cannot touch '/app/data/.testfile': Permission denied
    !! ERROR: Failed to verify state directory at /app/data

The second half of the bug: ``1024`` was both the fallback sentinel and a
legitimate UID, so ``WANTED_UID=1024`` supplied explicitly by an operator was
overwritten by detection anyway.

The behavioural tests below extract the UID/GID resolution block from
``docker_init.bash`` and run it under real bash, with ``stat`` stubbed so
ownership can be simulated without root. Source-level assertions alone cannot
catch shell-quoting or ordering regressions inside the block (the pattern
mirrors ``tests/test_docker_env_readonly_vars.py``).

The startup-health half of this contract — a real container coming up on a
host-owned state mount — is covered by the ``state-dir-uid`` job in
``.github/workflows/docker-smoke.yml``.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_SH = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")

# Logical probe name -> path basename used inside the sandbox. The extracted
# block's absolute paths are rewritten to these so the test can create/own them.
PROBE_PATHS = {
    "state": "state",
    "app_data": "app-data",
    "hermes_home": "hermes-home",
    "opt_data": "opt-data",
    "workspace": "workspace",
}

_BLOCK_START = "it=$itdir/hermeswebui_user_uid\n"
_BLOCK_END = 'echo "-- WANTED_GID: \\"${WANTED_GID}\\""'


# ── source-level invariants ───────────────────────────────────────────────────

def _probe_loop_lines():
    """Both `for _probe_dir in ...` lines (UID block, then GID block)."""
    return [ln for ln in INIT_SH.splitlines() if "for _probe_dir in" in ln]


def test_7027_both_probe_loops_include_the_state_dir():
    """UID *and* GID detection must probe the configured state directory."""
    loops = _probe_loop_lines()
    assert len(loops) == 2, f"expected one probe loop for UID and one for GID, found {len(loops)}"
    for line in loops:
        assert "HERMES_WEBUI_STATE_DIR" in line, (
            "probe loop must include the configured state dir — it is the only "
            f"path that is a bind mount by definition (#7027): {line.strip()!r}"
        )


def test_7027_state_dir_is_probed_first_in_both_loops():
    """The state dir must be the first candidate, ahead of hermes-home/workspace."""
    for line in _probe_loop_lines():
        candidates = line.split("for _probe_dir in", 1)[1].rstrip("; do").strip()
        first = candidates.split()[0]
        assert "HERMES_WEBUI_STATE_DIR" in first, (
            "the state dir must be probed before any image-owned path, otherwise "
            f"a stock /workspace wins with the build-time 1024 (#7027): {first!r}"
        )


def test_7027_workspace_probe_still_exists_as_lower_priority():
    """/workspace stays as a fallback signal — it is not removed (#569, #668)."""
    assert 'if [ -d "/workspace" ]' in INIT_SH, (
        "/workspace must remain a lower-priority probe for setups that bind-mount it"
    )
    state_pos = INIT_SH.find("HERMES_WEBUI_STATE_DIR:-/app/data")
    workspace_pos = INIT_SH.find('if [ -d "/workspace" ]')
    assert state_pos != -1 and workspace_pos != -1
    assert state_pos < workspace_pos, "state-dir probe must precede the /workspace probe"


def test_7027_explicit_value_guard_present_on_every_detection_branch():
    """Each detection branch must be gated on the value not being explicit.

    Without this, `WANTED_UID=1024` set deliberately by an operator is
    indistinguishable from the unset fallback and gets overwritten.
    """
    uid_guards = INIT_SH.count('[ "$_wanted_uid_source" != "explicit" ]')
    gid_guards = INIT_SH.count('[ "$_wanted_gid_source" != "explicit" ]')
    assert uid_guards == 2, f"both UID detection branches must be guarded, found {uid_guards}"
    assert gid_guards == 2, f"both GID detection branches must be guarded, found {gid_guards}"


def test_7027_id_source_is_persisted_for_the_runtime_pass():
    """The explicit/detected origin must be persisted next to the value.

    `su` drops the environment when the script re-enters as the runtime user,
    so without persistence the second pass cannot tell an operator's explicit
    1024 from the fallback.
    """
    assert "hermeswebui_user_uid_source" in INIT_SH
    assert "hermeswebui_user_gid_source" in INIT_SH
    assert INIT_SH.count('write_privtmpfile $it_source') == 2, (
        "both the UID and GID source markers must be written to $itdir"
    )


def test_7027_fallback_default_preserved():
    """The 1024 fallback must survive (guard shared with #569)."""
    assert "WANTED_UID=${WANTED_UID:-1024}" in INIT_SH
    assert "WANTED_GID=${WANTED_GID:-1024}" in INIT_SH


# ── behavioural harness ───────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestIdResolutionBehaviour:
    """Run the real resolution block under bash against simulated mounts."""

    @staticmethod
    def _extract_block(init_sh: str) -> str:
        start = init_sh.find(_BLOCK_START)
        assert start != -1, "UID/GID resolution block not found in docker_init.bash"
        end = init_sh.find(_BLOCK_END, start)
        assert end != -1, "end of the UID/GID resolution block not found"
        return init_sh[start:end + len(_BLOCK_END)]

    def _run(self, tmp_path, *, owners, env=None, itdir=None, state_dir_env=True):
        """Execute the resolution block.

        owners: {logical name: (uid, gid)} — each named directory is created and
                its simulated ownership is returned by the stubbed `stat`.
                Directories not listed are simply absent.
        state_dir_env: when False, HERMES_WEBUI_STATE_DIR is left unset so the
                block falls back to its built-in default path.
        """
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir(exist_ok=True)
        itdir = itdir or (tmp_path / "itdir")
        Path(itdir).mkdir(exist_ok=True)

        paths = {name: sandbox / base for name, base in PROBE_PATHS.items()}
        for name in owners:
            paths[name].mkdir(parents=True, exist_ok=True)

        block = self._extract_block(INIT_SH)
        # Rewrite the block's absolute probe paths into the sandbox. Longest
        # first so /app/data is not clipped by a shorter prefix.
        for absolute, name in (
            ("/home/hermeswebui/.hermes", "hermes_home"),
            ("/opt/data", "opt_data"),
            ("/app/data", "app_data"),
            ("/workspace", "workspace"),
        ):
            block = block.replace(absolute, str(paths[name]))

        # `stat -c FMT PATH` stub: $2 is the format, $3 the path. Unknown paths
        # exit nonzero so the block's `|| echo ""` yields an empty detection.
        cases = []
        for name, (uid, gid) in owners.items():
            cases.append(
                f'    {str(paths[name])!r}) '
                f'if [ "$_fmt" = "%u" ]; then echo "{uid}"; else echo "{gid}"; fi; return 0 ;;'
            )
        stat_cases = "\n".join(cases) if cases else "    __never__) return 1 ;;"

        script = textwrap.dedent("""\
            set -e
            itdir={itdir}
            write_privtmpfile() {{
              tmpfile=$1
              if [ -f "$tmpfile" ]; then rm -f "$tmpfile"; fi
              printf '%s' "$2" > "$tmpfile"
              chmod 600 "$tmpfile"
            }}
            error_exit() {{ echo "!! ERROR: $*"; exit 1; }}
            stat() {{
              _fmt=$2
              _path=$3
            case "$_path" in
            {stat_cases}
              esac
              return 1
            }}
            {block}
            echo "RESULT_UID=${{WANTED_UID}}"
            echo "RESULT_GID=${{WANTED_GID}}"
            echo "RESULT_UID_SOURCE=$(cat "$itdir/hermeswebui_user_uid_source" 2>/dev/null || echo missing)"
            echo "RESULT_GID_SOURCE=$(cat "$itdir/hermeswebui_user_gid_source" 2>/dev/null || echo missing)"
        """).format(
            itdir=str(itdir),
            stat_cases=stat_cases,
            block=block,
        )

        run_env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        if state_dir_env:
            run_env["HERMES_WEBUI_STATE_DIR"] = str(paths["state"])
        run_env.update(env or {})

        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"resolution block failed: rc={result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        parsed = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if line.startswith("RESULT_")
        )
        parsed["_stdout"] = result.stdout
        parsed["_state_dir"] = str(paths["state"])
        return parsed

    # ── the reported bug ──────────────────────────────────────────────────────

    def test_7027_state_mount_beats_stock_workspace(self, tmp_path):
        """The reproduction from the issue: host-owned state dir, stock /workspace.

        Host directory owned by 1001:1001 bind-mounted as the state dir, no
        explicit IDs, and an image-owned /workspace at 1024:1024. Detection must
        return 1001 — the identity that makes the state dir writable.
        """
        out = self._run(
            tmp_path,
            owners={"state": (1001, 1001), "workspace": (1024, 1024)},
        )
        assert out["RESULT_UID"] == "1001", (
            "UID must be detected from the state bind mount, not from the "
            f"image-owned /workspace (#7027). stdout:\n{out['_stdout']}"
        )
        assert out["RESULT_GID"] == "1001", (
            f"GID must be detected from the state bind mount too. stdout:\n{out['_stdout']}"
        )
        assert f"(from {out['_state_dir']})" in out["_stdout"], (
            "the log line must name the state dir as the detection source so the "
            "operator can see which path decided the identity"
        )

    def test_7027_non_1024_state_mount_with_no_workspace_at_all(self, tmp_path):
        """Same, in the single-container shape where /workspace is not mounted."""
        out = self._run(tmp_path, owners={"state": (501, 20)})
        assert out["RESULT_UID"] == "501", "macOS-style host UID must be detected"
        assert out["RESULT_GID"] == "20"

    def test_7027_default_state_path_probed_when_env_unset(self, tmp_path):
        """With HERMES_WEBUI_STATE_DIR unset, the built-in default is still probed."""
        out = self._run(
            tmp_path,
            owners={"app_data": (1001, 1001), "workspace": (1024, 1024)},
            state_dir_env=False,
        )
        assert out["RESULT_UID"] == "1001"
        assert out["RESULT_GID"] == "1001"

    # ── explicit operator values ──────────────────────────────────────────────

    def test_7027_explicit_1024_is_not_overwritten(self, tmp_path):
        """`WANTED_UID=1024` set deliberately must survive detection.

        1024 is both the fallback default and a legitimate UID; before the fix
        the two were indistinguishable and detection clobbered the explicit value.
        """
        out = self._run(
            tmp_path,
            owners={"state": (1001, 1001), "workspace": (1001, 1001)},
            env={"WANTED_UID": "1024", "WANTED_GID": "1024"},
        )
        assert out["RESULT_UID"] == "1024", (
            "an explicitly supplied 1024 must be preserved, not treated as unset (#7027)"
        )
        assert out["RESULT_GID"] == "1024"
        assert out["RESULT_UID_SOURCE"] == "explicit"
        assert out["RESULT_GID_SOURCE"] == "explicit"

    def test_7027_explicit_non_default_still_wins(self, tmp_path):
        """The pre-existing contract: any explicit value beats detection."""
        out = self._run(
            tmp_path,
            owners={"state": (1001, 1001)},
            env={"WANTED_UID": "1500", "WANTED_GID": "1500"},
        )
        assert out["RESULT_UID"] == "1500"
        assert out["RESULT_GID"] == "1500"

    def test_7027_explicit_choice_survives_the_privilege_drop(self, tmp_path):
        """Second pass (as the runtime user) must not re-detect over an explicit 1024.

        docker_init.bash runs twice: once as root, then `exec su` re-enters it as
        hermeswebui with the environment dropped. The second pass reads the value
        back from $itdir — so the *origin* has to persist too, or the explicit
        1024 gets auto-detected away and the UID no longer matches the running
        user, which is a hard startup failure.
        """
        itdir = tmp_path / "itdir-shared"
        first = self._run(
            tmp_path,
            owners={"state": (1001, 1001)},
            env={"WANTED_UID": "1024", "WANTED_GID": "1024"},
            itdir=itdir,
        )
        assert first["RESULT_UID"] == "1024"

        second = self._run(  # no WANTED_* in env — `su` dropped it
            tmp_path,
            owners={"state": (1001, 1001)},
            itdir=itdir,
        )
        assert second["RESULT_UID"] == "1024", (
            "the runtime pass must honour the persisted explicit choice; "
            "re-detecting here would make WANTED_UID disagree with the user the "
            "script is already running as"
        )
        assert second["RESULT_GID"] == "1024"

    def test_7027_persisted_default_is_still_re_detected(self, tmp_path):
        """A *detected*/fallback 1024 stays re-detectable on a later run.

        This is what the `= 1024` sentinel was for: a container that fell back to
        1024 with nothing mounted must pick up the right identity once a state
        volume appears. Only explicit values are frozen.
        """
        itdir = tmp_path / "itdir-shared"
        first = self._run(tmp_path, owners={}, itdir=itdir)
        assert first["RESULT_UID"] == "1024", "nothing mounted -> fallback"
        assert first["RESULT_UID_SOURCE"] == "detected"

        second = self._run(tmp_path, owners={"state": (1001, 1001)}, itdir=itdir)
        assert second["RESULT_UID"] == "1001", (
            "a persisted fallback 1024 must not freeze detection for later runs"
        )
        assert second["RESULT_GID"] == "1001"

    # ── previously shipped behaviour that must not regress ────────────────────

    def test_7027_workspace_fallback_preserved(self, tmp_path):
        """With no state mount, /workspace still decides (#569)."""
        out = self._run(tmp_path, owners={"workspace": (501, 20)})
        assert out["RESULT_UID"] == "501"
        assert out["RESULT_GID"] == "20"
        assert "from /workspace" in out["_stdout"] or "workspace UID" in out["_stdout"]

    def test_7027_hermes_home_still_beats_workspace(self, tmp_path):
        """Two-container setups keep detecting from the shared hermes-home (#668)."""
        out = self._run(
            tmp_path,
            owners={"hermes_home": (1001, 1001), "workspace": (1024, 1024)},
        )
        assert out["RESULT_UID"] == "1001"
        assert out["RESULT_GID"] == "1001"

    def test_7027_hermes_home_env_probe_preserved(self, tmp_path):
        """$HERMES_HOME remains a probe candidate (#668)."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir(exist_ok=True)
        hermes_home = sandbox / "hermes-home"
        out = self._run(
            tmp_path,
            owners={"hermes_home": (1001, 1001)},
            env={"HERMES_HOME": str(hermes_home)},
        )
        assert out["RESULT_UID"] == "1001"

    def test_7027_root_owned_state_dir_is_skipped(self, tmp_path):
        """A root-owned state dir (fresh named volume) is not a host signal.

        Docker creates a brand-new named volume owned by 0:0. Detection must skip
        it — as it already does for every probe — and fall through.
        """
        out = self._run(
            tmp_path,
            owners={"state": (0, 0), "workspace": (501, 20)},
        )
        assert out["RESULT_UID"] == "501", "root-owned probes must be ignored"
        assert out["RESULT_GID"] == "20"

    def test_7027_nothing_mounted_falls_back_to_1024(self, tmp_path):
        """No probe resolves -> the documented 1024 default."""
        out = self._run(tmp_path, owners={})
        assert out["RESULT_UID"] == "1024"
        assert out["RESULT_GID"] == "1024"
