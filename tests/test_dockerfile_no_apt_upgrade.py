from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_does_not_run_apt_get_upgrade():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "apt-get upgrade" not in dockerfile
