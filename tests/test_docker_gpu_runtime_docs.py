from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _repo_text(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_dockerfile_gpu_libraries_are_opt_in():
    """The production image must stay CPU-only unless the GPU build arg is set."""
    dockerfile = _repo_text("Dockerfile")

    assert "ARG INSTALL_GPU_LIBS=0" in dockerfile
    assert 'if [ "$INSTALL_GPU_LIBS" = "1" ]' in dockerfile

    opt_in_block = dockerfile[dockerfile.index("ARG INSTALL_GPU_LIBS=0"):]
    for package in (
        "libva2",
        "vainfo",
        "mesa-va-drivers",
        "intel-media-va-driver-non-free",
    ):
        assert package in opt_in_block, (
            f"{package} must only appear in the INSTALL_GPU_LIBS opt-in block."
        )
        assert package not in dockerfile[:dockerfile.index("ARG INSTALL_GPU_LIBS=0")]


def test_dockerfile_handles_missing_intel_non_free_driver():
    """Debian slim repos may not expose the non-free Intel VA-API package."""
    dockerfile = _repo_text("Dockerfile")

    assert "apt-cache show intel-media-va-driver-non-free" in dockerfile
    assert "skipping Intel non-free VA-API driver" in dockerfile


def test_docker_docs_show_gpu_build_command():
    docker_docs = _repo_text("docs/docker.md")

    assert "Optional GPU runtime image" in docker_docs
    assert "--build-arg INSTALL_GPU_LIBS=1" in docker_docs
    assert "default Hermes WebUI Docker image stays CPU-only" in docker_docs


def test_docker_docs_cover_intel_amd_dri_mapping():
    docker_docs = _repo_text("docs/docker.md")

    assert "Intel and AMD VA-API" in docker_docs
    assert "--device /dev/dri:/dev/dri" in docker_docs
    assert "/dev/dri:/dev/dri" in docker_docs
    assert "group_add:" in docker_docs
    assert "video" in docker_docs
    assert "render" in docker_docs
    assert "vainfo" in docker_docs
    assert "preserves Docker-provided supplemental groups" in docker_docs


def test_docker_docs_cover_nvidia_host_runtime_guidance():
    docker_docs = _repo_text("docs/docker.md")

    assert "NVIDIA Container Toolkit" in docker_docs
    assert "--gpus all" in docker_docs
    assert "gpus: all" in docker_docs
    assert "host NVIDIA driver" in docker_docs
    assert "host kernel drivers" in docker_docs
    assert "NVIDIA runtime" in docker_docs


def test_docker_docs_do_not_claim_native_gpu_passthrough_verification():
    docker_docs = _repo_text("docs/docker.md")

    assert "not a claim that native GPU passthrough was verified" in docker_docs
    assert "depends on host drivers" in docker_docs


def test_docker_init_preserves_supplemental_device_groups_for_runtime_user():
    docker_init = _repo_text("docker_init.bash")
    root_phase = docker_init[:docker_init.index("exec su -s /bin/bash")]

    assert "for gid in $(id -G)" in root_phase
    assert "groupadd -g \"$gid\"" in root_phase
    assert "Could not create supplemental group for GID $gid" in root_phase
    assert "usermod -a -G \"$group_name\" hermeswebui" in root_phase
    assert "Docker --group-add supplemental groups" in root_phase


def test_changelog_mentions_optional_gpu_runtime_path():
    changelog = _repo_text("CHANGELOG.md")
    unreleased = changelog[changelog.index("## [Unreleased]"):changelog.index("## [v0.51.293]")]

    assert "Optional GPU runtime image path" in unreleased
    assert "INSTALL_GPU_LIBS=1" in unreleased
    assert "supplemental device groups" in unreleased
