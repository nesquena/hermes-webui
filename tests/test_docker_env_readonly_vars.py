"""Regression tests for repo-local .env handling after CLI packaging."""

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
START_SH = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")


def test_start_sh_delegates_to_packaged_cli() -> None:
    assert "-m hermes_webui.cli web --no-browser" in START_SH


def test_bootstrap_dotenv_loader_accepts_readonly_shell_names(tmp_path, monkeypatch) -> None:
    import bootstrap

    env_path = tmp_path / ".env"
    env_path.write_text(
        "UID=501\nGID=20\nEUID=501\nEGID=20\nPPID=1\nHERMES_WEBUI_PORT=18999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    for key in ("UID", "GID", "EUID", "EGID", "PPID", "HERMES_WEBUI_PORT"):
        monkeypatch.delenv(key, raising=False)

    bootstrap._load_repo_dotenv()

    assert os.environ["HERMES_WEBUI_PORT"] == "18999"
    assert os.environ["UID"] == "501"
