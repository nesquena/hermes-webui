"""
Tests for fixes:

- #569: docker_init.bash auto-detects WANTED_UID/WANTED_GID from mounted workspace
         so macOS users (UID 501) don't need to manually set the env var.
- #579: Topbar message count already filters tool messages (role !== 'tool').
         The legacy raw sidebar count was removed by #584, and later reintroduced
         in a gated detailed-density mode by #673.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
INIT_SH   = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")
UI_JS     = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


# ── #569: docker UID/GID auto-detect ─────────────────────────────────────────

def test_569_uid_autodetect_present():
    """docker_init.bash must have workspace-based UID auto-detection (#569)."""
    assert "stat -c '%u'" in INIT_SH or 'stat -c \'%u\'' in INIT_SH, (
        "docker_init.bash must use stat to read workspace UID (#569)"
    )


def test_569_gid_autodetect_present():
    """docker_init.bash must have workspace-based GID auto-detection (#569)."""
    assert "stat -c '%g'" in INIT_SH or 'stat -c \'%g\'' in INIT_SH, (
        "docker_init.bash must use stat to read workspace GID (#569)"
    )


def test_569_autodetect_before_usermod():
    """UID auto-detect must appear before usermod call in docker_init.bash."""
    detect_pos = INIT_SH.find("stat -c '%u'")
    if detect_pos == -1:
        detect_pos = INIT_SH.find("stat -c")
    usermod_pos = INIT_SH.find("usermod -o -u")
    assert detect_pos != -1, "stat UID detection not found"
    assert usermod_pos != -1, "usermod not found"
    assert detect_pos < usermod_pos, (
        "UID auto-detect must occur before 'usermod' so the correct UID "
        "is used when remapping the hermeswebui user"
    )


def test_569_skips_root_uid():
    """Auto-detect must not use UID 0 (root-owned mount = untrustworthy)."""
    detect_block_start = INIT_SH.find("Auto-detect from mounted volumes")
    assert detect_block_start != -1, "auto-detect comment block not found"
    block = INIT_SH[detect_block_start:detect_block_start + 1200]
    assert '"0"' in block or "'0'" in block, (
        "Auto-detect block must skip UID 0 to avoid incorrectly using root ownership"
    )


def test_569_fallback_preserved():
    """Hardcoded default 1024 fallback must still exist after auto-detect."""
    assert "WANTED_UID=${WANTED_UID:-1024}" in INIT_SH, (
        "WANTED_UID default fallback must remain so explicit env var still works"
    )
    assert "WANTED_GID=${WANTED_GID:-1024}" in INIT_SH, (
        "WANTED_GID default fallback must remain"
    )


# ── #668: UID/GID auto-detect from hermes-home shared volume (two-container) ──

def test_668_uid_autodetect_checks_hermes_home():
    """docker_init.bash must probe hermes-home dirs for UID in two-container setups.

    When hermes-agent and hermes-webui run in separate containers sharing a
    named volume, /workspace may not exist but ~/.hermes will be owned by the
    agent's UID. The init script must probe it so the webui user is remapped
    to match (#668).
    """
    assert "/home/hermeswebui/.hermes" in INIT_SH, (
        "docker_init.bash must probe /home/hermeswebui/.hermes for UID detection "
        "to support two-container setups where /workspace may not exist (#668)"
    )


def test_668_gid_autodetect_checks_hermes_home():
    """docker_init.bash must probe hermes-home dirs for GID in two-container setups (#668)."""
    # Both UID and GID detection share the same probe dirs — check GID block too
    gid_detect_start = INIT_SH.find("Auto-detect GID from mounted volumes")
    assert gid_detect_start != -1, (
        "GID auto-detect comment must be updated to mention shared volumes (#668)"
    )
    gid_block = INIT_SH[gid_detect_start:gid_detect_start + 600]
    assert "/home/hermeswebui/.hermes" in gid_block or "HERMES_HOME" in gid_block, (
        "GID auto-detect block must probe hermes-home dirs (#668)"
    )


def test_668_uid_probe_loop_uses_break():
    """UID probe loop must stop on first match (no double-detection)."""
    uid_detect_start = INIT_SH.find("Auto-detect from mounted volumes")
    assert uid_detect_start != -1, "UID auto-detect comment not found"
    uid_block = INIT_SH[uid_detect_start:uid_detect_start + 1200]
    assert "break" in uid_block, (
        "UID probe loop must break after first successful detection "
        "to avoid being overridden by a later probe dir (#668)"
    )


def test_668_hermes_home_probe_before_workspace():
    """Hermes-home probe must appear before /workspace probe in docker_init.bash (#668)."""
    hermes_home_pos = INIT_SH.find("/home/hermeswebui/.hermes")
    workspace_pos = INIT_SH.find('if [ -d "/workspace" ]')
    assert hermes_home_pos != -1, "/home/hermeswebui/.hermes probe not found"
    assert workspace_pos != -1, "/workspace probe not found"
    assert hermes_home_pos < workspace_pos, (
        "Hermes-home probe must come before /workspace probe — "
        "shared volume UID should take priority over workspace UID (#668)"
    )


# ── #579: topbar message count already filters tool messages ──────────────────

def test_579_topbar_filters_tool_messages():
    """ui.js topbar count must filter out role='tool' messages (#579).

    The sidebar previously showed raw message_count (which included tool
    messages), causing a mismatch with the topbar. PR #584 removed the
    sidebar count display entirely; the topbar was already correct.
    This test locks in the existing topbar filter so it can't regress.
    """
    # Find the topbarMeta assignment
    meta_pos = UI_JS.find("topbarMeta")
    assert meta_pos != -1, "topbarMeta assignment not found in ui.js"

    # Find the filter that precedes it — should exclude role==='tool'
    context = UI_JS[max(0, meta_pos - 400):meta_pos + 100]
    assert "role" in context and "tool" in context, (
        "topbarMeta count must filter by role — "
        "messages with role='tool' must be excluded from the displayed count"
    )
    # The filter must exclude tool messages (not include them)
    assert "!=='tool'" in context or "!= 'tool'" in context or "role!=='tool'" in context, (
        "topbar count filter must use !== 'tool' to exclude tool messages"
    )


def test_579_sidebar_count_is_gated_behind_detailed_density():
    """sessions.js may only show sidebar count inside detailed density mode.

    PR #584 removed the always-visible raw sidebar count to avoid mismatching the
    topbar's filtered count. PR #673 later reintroduced message_count as
    optional metadata, but only when the user explicitly opts into detailed
    sidebar density.
    """
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "const density=(window._sidebarDensity==='detailed'?'detailed':'compact');" in sessions_js, (
        "sessions.js must normalize sidebar density before rendering metadata"
    )
    assert "if(density==='detailed'){" in sessions_js, (
        "sessions.js must gate sidebar metadata behind detailed density mode"
    )
    assert "typeof s.message_count==='number'?s.message_count:0" in sessions_js, (
        "message_count may be rendered only inside the detailed-density branch"
    )
