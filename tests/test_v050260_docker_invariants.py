"""Regression tests for v0.50.260 — Docker compose file invariants.

PR #1428 fixed a UID/GID mismatch between the agent container and the webui
container in the two- and three-container compose files. This test module
pins the invariants that prevented the original bug from coming back AND
extends coverage to the related fixes shipped alongside #1428:

- All compose files reference the same UID/GID source (`${UID}` / `${GID}`)
- All compose files document the bind-mount permission escape hatches
  (`HERMES_SKIP_CHMOD`, `HERMES_HOME_MODE`) inline so users hit by #1389
  or #1399 see the fix in the file they're reading
- The `.env.docker.example` template ships and documents the same vars
- `docs/docker.md` exists and covers the multi-container architecture
- Stale README references to `/root/.hermes` are gone (the agent images
  use `/home/hermes/.hermes`)
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# ── 1: UID/GID alignment across compose files (PR #1428) ────────────────────


def test_two_container_compose_aligns_agent_uid_with_webui():
    """REGRESSION (#1399, fixed in #1428): the two-container compose file
    must align the agent's UID/GID with the webui's. Before #1428 the
    agent had no HERMES_UID/HERMES_GID at all and used the image default
    of 10000, while the webui used 1000 — bind-mounted files written by
    the agent were unreadable by the webui."""
    src = (REPO / "docker-compose.two-container.yml").read_text(encoding="utf-8")

    # Agent must declare HERMES_UID/HERMES_GID
    assert "HERMES_UID=${UID:-1000}" in src, (
        "two-container: hermes-agent must set HERMES_UID=${UID:-1000} so it "
        "matches the webui's WANTED_UID=${UID:-1000}. Before #1428 the agent "
        "ran as the image default (10000), causing PermissionError on the "
        "shared hermes-home volume."
    )
    assert "HERMES_GID=${GID:-1000}" in src, (
        "two-container: hermes-agent must set HERMES_GID=${GID:-1000}"
    )

    # WebUI must use ${UID}/${GID} (same source)
    assert "WANTED_UID=${UID:-1000}" in src
    assert "WANTED_GID=${GID:-1000}" in src


def test_three_container_compose_aligns_all_three_services():
    """REGRESSION (#1399, fixed in #1428): all three services in the
    three-container compose file must use ${UID}/${GID} as the source.
    Before #1428 the agent and dashboard defaulted to 10000 while the
    webui defaulted to 1000."""
    src = (REPO / "docker-compose.three-container.yml").read_text(encoding="utf-8")

    # Agent + dashboard both use HERMES_UID/HERMES_GID with ${UID:-1000} as source
    # (Two occurrences each — once per service)
    assert src.count("HERMES_UID=${UID:-1000}") >= 2, (
        "three-container: both hermes-agent and hermes-dashboard must set "
        "HERMES_UID=${UID:-1000}"
    )
    assert src.count("HERMES_GID=${GID:-1000}") >= 2

    # WebUI uses WANTED_UID=${UID:-1000}
    assert "WANTED_UID=${UID:-1000}" in src
    assert "WANTED_GID=${GID:-1000}" in src

    # The pre-#1428 default of 10000 must NOT appear anywhere
    # (negative-pattern guard prevents revert)
    assert "HERMES_UID:-10000" not in src, (
        "Pre-#1428 default (HERMES_UID:-10000) must not return — that's the "
        "bug shape. All UIDs should pull from ${UID:-1000}."
    )
    assert "HERMES_GID:-10000" not in src


def test_single_container_compose_uses_same_uid_source():
    """The single-container compose file should use the same ${UID} default
    as the multi-container files for consistency."""
    src = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "WANTED_UID=${UID:-1000}" in src
    assert "WANTED_GID=${GID:-1000}" in src


# ── 2: bind-mount permission escape hatches documented (#1389, #1399) ──────


def test_compose_files_document_skip_chmod_escape_hatch():
    """Every compose file must mention HERMES_SKIP_CHMOD inline so users
    hit by #1389 (the auth.json/.env chmod-override bug) can find the fix
    in the file they're reading. The fix shipped in v0.50.254 but Docker
    users may not be reading CHANGELOGs."""
    for fname in (
        "docker-compose.yml",
        "docker-compose.two-container.yml",
        "docker-compose.three-container.yml",
    ):
        src = (REPO / fname).read_text(encoding="utf-8")
        assert "HERMES_SKIP_CHMOD" in src, (
            f"{fname}: must document HERMES_SKIP_CHMOD as a bind-mount "
            f"escape hatch so users hit by #1389 find the fix inline."
        )
        assert "HERMES_HOME_MODE" in src, (
            f"{fname}: must document HERMES_HOME_MODE alongside HERMES_SKIP_CHMOD"
        )


# ── 3: .env.docker.example exists and documents the same vars ──────────────


def test_env_docker_example_exists():
    """The .env.docker.example template must ship in the repo so users
    can `cp .env.docker.example .env` as the first step of the quickstart."""
    p = REPO / ".env.docker.example"
    assert p.exists(), ".env.docker.example must exist in repo root"
    src = p.read_text(encoding="utf-8")

    # Must document the critical vars
    for var in (
        "UID",
        "GID",
        "HERMES_HOME",
        "HERMES_WORKSPACE",
        "HERMES_WEBUI_PASSWORD",
        "HERMES_SKIP_CHMOD",
        "HERMES_HOME_MODE",
    ):
        assert var in src, (
            f".env.docker.example must document {var} — without it, users "
            f"hit by the related failure mode have no in-template hint."
        )


# ── 4: docs/docker.md comprehensive guide ──────────────────────────────────


def test_docs_docker_md_exists_and_covers_failure_modes():
    """The docs/docker.md guide must exist and cover the recurring failure
    modes seen in #1399, #1389, #858, #681, #668."""
    p = REPO / "docs" / "docker.md"
    assert p.exists(), "docs/docker.md must exist as the comprehensive guide"
    src = p.read_text(encoding="utf-8")

    # Must mention each documented failure mode by issue ref
    for issue in ("#1389", "#1399", "#858", "#681"):
        assert issue in src, (
            f"docs/docker.md must reference issue {issue} so users searching "
            f"for the symptom find the right diagnostic path."
        )

    # Must explicitly link the alternate single-container community image
    assert "sunnysktsang/hermes-suite" in src, (
        "docs/docker.md should point Podman 3.4 / multi-arch users to the "
        "community all-in-one image as a documented escape hatch."
    )


# ── 5: stale /root/.hermes references removed from README ──────────────────


def test_readme_no_stale_root_hermes_path():
    """REGRESSION: the README's two-container Docker section used to claim
    'the agent writes to /root/.hermes' which is wrong — current agent
    images use /home/hermes/.hermes. Stale paths confuse users reading
    the README to debug their own setup."""
    src = (REPO / "README.md").read_text(encoding="utf-8")
    assert "/root/.hermes" not in src, (
        "README.md must not reference /root/.hermes — the current agent "
        "image uses /home/hermes/.hermes. Stale paths in docs are worse "
        "than no docs at all."
    )


def test_readme_links_to_docker_md():
    """The README Docker section should point at docs/docker.md for the
    deep dive so we don't have to keep two copies of the same content
    in sync."""
    src = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docs/docker.md" in src, (
        "README.md should reference docs/docker.md so users with deeper "
        "needs (multi-container, bind mounts, Podman) find the full guide."
    )


# ── 6: compose files all parse as valid YAML ───────────────────────────────


def test_compose_files_parse_as_valid_yaml():
    """Every compose file must parse as valid YAML — without this guard,
    a stray indentation or unquoted ${VAR} could ship a broken compose
    file that breaks `docker compose up` for everyone."""
    import yaml

    for fname in (
        "docker-compose.yml",
        "docker-compose.two-container.yml",
        "docker-compose.three-container.yml",
    ):
        path = REPO / fname
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise AssertionError(f"{fname} is not valid YAML: {e}")
        assert isinstance(data, dict), f"{fname} must parse to a dict"
        assert "services" in data, f"{fname} must define a `services:` block"


# ── 7: agent vs webui HERMES_HOME_MODE semantic asymmetry ──────────────────


def test_agent_service_does_not_recommend_invalid_home_mode():
    """REGRESSION (Opus pre-release advisor): the WebUI's HERMES_HOME_MODE
    is a credential-file threshold (0640 = allow group bits). The agent's
    HERMES_HOME_MODE is a DIRECTORY mode (default 0700). 0640 on a directory
    has no owner-execute bit, so the agent can't traverse its own home and
    bricks. The agent service blocks must NOT recommend HERMES_HOME_MODE=0640
    as their example value."""
    import re

    BAD_VALUES = (
        "HERMES_HOME_MODE=0640",
        "HERMES_HOME_MODE=0644",
        "HERMES_HOME_MODE=0600",
        "HERMES_HOME_MODE=0660",
    )

    for fname in (
        "docker-compose.two-container.yml",
        "docker-compose.three-container.yml",
    ):
        src = (REPO / fname).read_text(encoding="utf-8")

        # Find each agent/dashboard service block by name and slice to the next
        # top-level service or root key.
        for service_name in ("hermes-agent", "hermes-dashboard"):
            service_marker = "  " + service_name + ":"
            idx = src.find(service_marker)
            if idx == -1:
                continue
            # Find next service line (2-space-indented name + colon) or root key
            after = idx + len(service_marker)
            # Match next "  name:" at indent 2 or root-level (no indent)
            next_match = re.search(r"\n  [a-z][a-z0-9-]*:\n|\n[a-z]", src[after:])
            block_end = after + next_match.start() if next_match else len(src)
            block = src[idx:block_end]

            for bad in BAD_VALUES:
                assert bad not in block, (
                    f"{fname} service `{service_name}` recommends `{bad}` — "
                    f"the agent's HERMES_HOME_MODE applies to the HERMES_HOME "
                    f"directory, and a mode without owner-execute prevents "
                    f"traversal. Use 0750 (group-traversable) or 0701 (x-only). "
                    f"See Opus pre-release advisor finding for v0.50.260."
                )


def test_compose_files_warn_about_home_mode_asymmetry():
    """The compose files must explicitly warn about the WebUI vs agent
    HERMES_HOME_MODE semantic asymmetry so users don't copy the WebUI's
    valid 0640 value into the agent service."""
    for fname in (
        "docker-compose.two-container.yml",
        "docker-compose.three-container.yml",
    ):
        src = (REPO / fname).read_text(encoding="utf-8").lower()
        # Look for a comment that distinguishes directory mode from credential file mode
        assert "directory" in src and "credential" in src, (
            f"{fname} must contain comments explaining that HERMES_HOME_MODE "
            f"means different things for the agent (directory mode) vs the "
            f"WebUI (credential file threshold)."
        )


def test_env_docker_example_warns_about_home_mode_asymmetry():
    """The .env.docker.example template must warn that HERMES_HOME_MODE has
    different semantics across services."""
    src = (REPO / ".env.docker.example").read_text(encoding="utf-8")
    assert "MULTI-CONTAINER WARNING" in src, (
        ".env.docker.example must include a MULTI-CONTAINER WARNING about "
        "the HERMES_HOME_MODE semantic asymmetry between WebUI and agent."
    )
