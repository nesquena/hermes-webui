"""Static compatibility checks for the thin ctl.sh wrapper."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CTL_SH = REPO_ROOT / "ctl.sh"


def _read_ctl() -> str:
    return CTL_SH.read_text(encoding="utf-8")


def test_ctl_sh_sets_strict_mode() -> None:
    assert "set -euo pipefail" in _read_ctl()


def test_ctl_sh_delegates_to_packaged_cli() -> None:
    src = _read_ctl()
    assert "-m hermes_webui.cli" in src
    assert 'exec "${PYTHON}" -m hermes_webui.cli "$@"' in src


def test_no_bash4_plus_features_in_ctl() -> None:
    src = _read_ctl()
    forbidden = {
        "declare -A": r"\bdeclare\s+-A\b",
        "local -A": r"\blocal\s+-A\b",
        "mapfile": r"\bmapfile\b",
        "readarray": r"\breadarray\b",
        "[[ -v VAR ]]": r"\[\[\s*-v\s+",
        "${var^^}": r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^?\}",
        "${var,,}": r"\$\{[A-Za-z_][A-Za-z0-9_]*,,?\}",
    }
    found = [name for name, pat in forbidden.items() if re.search(pat, src)]
    assert not found
