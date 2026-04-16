"""
Tests for fixes:

- #569: docker_init.bash auto-detects WANTED_UID/WANTED_GID from mounted workspace
         so macOS users (UID 501) don't need to manually set the env var.
- #579: Topbar message count already filters tool messages (role !== 'tool') —
         confirmed present. Closing as already fixed by #584 which removed the
         sidebar meta row (the only place raw message_count was ever displayed).
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
    usermod_pos = INIT_SH.find("sudo usermod")
    assert detect_pos != -1, "stat UID detection not found"
    assert usermod_pos != -1, "sudo usermod not found"
    assert detect_pos < usermod_pos, (
        "UID auto-detect must occur before 'sudo usermod' so the correct UID "
        "is used when remapping the hermeswebui user"
    )


def test_569_skips_root_uid():
    """Auto-detect must not use UID 0 (root-owned mount = untrustworthy)."""
    detect_block_start = INIT_SH.find("Auto-detect from mounted workspace")
    assert detect_block_start != -1, "auto-detect comment block not found"
    block = INIT_SH[detect_block_start:detect_block_start + 600]
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


def test_579_sidebar_no_longer_shows_raw_count():
    """sessions.js must not reference message_count in the render path (#579).

    After PR #584, the sidebar no longer shows message_count at all,
    eliminating the inconsistency between sidebar (raw) and topbar (filtered).
    """
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    # message_count should not appear in the client-side session renderer
    assert "message_count" not in sessions_js, (
        "sessions.js must not reference message_count — "
        "the meta row that displayed it was removed in PR #584"
    )
