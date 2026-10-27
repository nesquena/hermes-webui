"""#8524: the Docker agent install must honor the agent's ``[tool.uv] override-dependencies``.

``docker_init.bash`` installs the staged Agent with ``uv pip install -e <stage>[all]``.
``uv pip install`` never reads a project's ``[tool.uv]`` table, and since Agent
v0.22.5 the Agent's requirements resolve only with its own overrides (it lifts
browser-harness's ``pillow==12.3.0`` pin to take Pillow 13's security fixes). The
two-container topology therefore exited during install. ``install_staged_agent``
now extracts the overrides with ``docker_venv_python.py --uv-overrides`` and passes
them as ``--overrides``; every way that can fail falls back to the plain install.

The bash tests run the real function from ``docker_init.bash`` with a fake ``uv``
on ``PATH`` that records its arguments and the overrides it was handed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")
HELPER = REPO / "scripts" / "docker_venv_python.py"

_spec = importlib.util.spec_from_file_location("docker_venv_python_8524", HELPER)
assert _spec is not None and _spec.loader is not None
dvp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dvp)

# The shape of Agent v0.22.5's table: comments, markers, an exact security pin.
AGENT_PYPROJECT = """
[project]
name = "hermes-agent"
requires-python = ">=3.11,<3.15"
dependencies = ["pillow==13.0.0; python_version >= '3.14'", "browser-harness==0.1.13"]

[tool.uv]
default-groups = []
environments = ["python_version >= '3.14'"]
override-dependencies = [
  # discord.py pins pynacl<1.6
  "pynacl>=1.6,<1.7",
  "cryptography==50.0.1",
  # browser-harness pins pillow==12.3.0
  "pillow==13.0.0",
  "spacy-curated-transformers; sys_platform == 'never'",
]
"""
EXPECTED = [
    "pynacl>=1.6,<1.7",
    "cryptography==50.0.1",
    "pillow==13.0.0",
    "spacy-curated-transformers; sys_platform == 'never'",
]
NO_TABLE = '[project]\nname = "hermes-agent"\n'


# ── the parser ───────────────────────────────────────────────────────────────

def test_reads_every_override_verbatim_including_markers():
    assert dvp.uv_override_dependencies(AGENT_PYPROJECT) == EXPECTED


@pytest.mark.parametrize(
    "text",
    [
        NO_TABLE,
        '[tool.uv]\ndefault-groups = []\n',
        '[tool]\nuv = "not a table"\n',
        '[tool.uv]\noverride-dependencies = "pillow==13.0.0"\n',
        "this is [not toml",
        "",
    ],
    ids=["no-tool-uv", "no-overrides-key", "uv-not-a-table", "overrides-not-a-list", "invalid-toml", "empty"],
)
def test_anything_but_a_list_of_strings_means_no_overrides(text):
    assert dvp.uv_override_dependencies(text) == []


def test_malformed_entries_are_dropped_and_never_span_lines():
    text = '[tool.uv]\noverride-dependencies = ["pillow==13.0.0", 7, "", "  ", "a==1\\nb==2", "c==3\\r"]\n'
    assert dvp.uv_override_dependencies(text) == ["pillow==13.0.0"]


@pytest.mark.parametrize(
    "entry",
    [
        "-r http://127.0.0.1:9/injected.txt",
        "  -r /etc/passwd",
        "-c constraints.txt",
        "-e /etc",
        "--index-url http://127.0.0.1:9/simple",
        "--extra-index-url=http://127.0.0.1:9/simple",
        "pillow==13.0.0 \\",
        "\ufeff-r http://127.0.0.1:9/injected.txt",
        "  \ufeff-r http://127.0.0.1:9/injected.txt",
        "\ufeffpillow==13.0.0",
        "# pillow==13.0.0",
        "\u00e9clair==1.0",
        "",
    ],
)
def test_requirements_file_directives_are_never_passed_to_uv(entry):
    # uv parses the overrides file as requirements.txt: a "-" line is a directive it
    # acts on (a -r URL is fetched; uv strips a leading BOM first) and a trailing
    # backslash joins the next line. Only entries that start like a PEP 508 name pass.
    text = "[tool.uv]\noverride-dependencies = [%s, \"cryptography==50.0.1\"]\n" % _toml_str(entry)
    assert dvp.uv_override_dependencies(text) == ["cryptography==50.0.1"]


@pytest.mark.parametrize("entry", ["Pillow==13.0.0", "9p==1", "zope.interface>=6; python_version >= '3.14'",
                                   "pkg @ https://example.invalid/pkg-1.0.tar.gz"])
def test_pep508_requirements_are_kept(entry):
    text = "[tool.uv]\noverride-dependencies = [%s]\n" % _toml_str(entry)
    assert dvp.uv_override_dependencies(text) == [entry]


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def test_dropped_entries_are_named_on_stderr(capsys):
    text = '[tool.uv]\noverride-dependencies = ["-r http://127.0.0.1:9/x.txt", "pillow==13.0.0"]\n'
    assert dvp.uv_override_dependencies(text) == ["pillow==13.0.0"]
    err = capsys.readouterr().err
    assert "Ignoring a [tool.uv] override-dependencies entry" in err and "-r http://127.0.0.1:9/x.txt" in err


# ── the CLI mode docker_init.bash calls ──────────────────────────────────────

def _cli(*args):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=60)


def test_cli_writes_one_override_per_line(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(AGENT_PYPROJECT, encoding="utf-8")
    out = tmp_path / "overrides.txt"
    r = _cli("--uv-overrides", str(pyproject), str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8").splitlines() == EXPECTED


def test_cli_writes_an_empty_file_when_nothing_is_overridden(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(NO_TABLE, encoding="utf-8")
    out = tmp_path / "overrides.txt"
    assert _cli("--uv-overrides", str(pyproject), str(out)).returncode == 0
    assert out.read_text(encoding="utf-8") == ""


def test_cli_treats_an_unreadable_pyproject_as_no_overrides(tmp_path):
    out = tmp_path / "overrides.txt"
    r = _cli("--uv-overrides", str(tmp_path / "missing.toml"), str(out))
    assert r.returncode == 0
    assert out.read_text(encoding="utf-8") == ""


def test_cli_fails_when_it_cannot_write_so_the_caller_falls_back(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(AGENT_PYPROJECT, encoding="utf-8")
    assert _cli("--uv-overrides", str(pyproject), str(tmp_path / "no-such-dir" / "o.txt")).returncode != 0


def test_cli_usage_error():
    assert _cli("--uv-overrides", "only-one-arg").returncode == 2


# ── install_staged_agent, the real bash function ─────────────────────────────

FAKE_UV = """#!/bin/bash
# fake uv: records `uv pip install ...` and the overrides file content it was given
[ "$1" = pip ] && [ "$2" = install ] || exit 64
printf 'ARGS:%s\\n' "$*" >> "$FAKE_UV_LOG"
prev=""
for a in "$@"; do
  if [ "$prev" = "--overrides" ]; then
    printf 'OVERRIDES_FILE:%s\\n' "$a" >> "$FAKE_UV_LOG"
    sed 's/^/OVERRIDE:/' "$a" >> "$FAKE_UV_LOG"
  fi
  prev="$a"
done
exit "${FAKE_UV_RC:-0}"
"""

pytestmark_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash and python3 are needed to run docker_init.bash code",
)


def _bash_function(src: str, name: str) -> str:
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


def _install(tmp_path: Path, pyproject_text: str | None, *, helper: Path = HELPER, tmpdir: Path | None = None,
             uv_rc: int = 0, app_writable: bool = True) -> tuple[int, str, str]:
    app = tmp_path / "app"  # the stage's parent, /app in the image
    stage = app / "hermes-agent-src"
    stage.mkdir(parents=True)
    if pyproject_text is not None:
        (stage / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "uv").write_text(FAKE_UV, encoding="utf-8")
    (bindir / "uv").chmod(0o755)
    log = tmp_path / "uv.log"
    work_tmp = tmpdir if tmpdir is not None else tmp_path / "tmp"
    if tmpdir is None:
        work_tmp.mkdir()
    script = "\n".join([
        f'_venv_helper="{helper}"',
        _bash_function(INIT_SH, "install_staged_agent"),
        f'rc=0; install_staged_agent "{stage}" || rc=$?; echo "rc=$rc"',
    ])
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_RC": str(uv_rc),
        "TMPDIR": str(work_tmp),
        "HOME": str(tmp_path),
    }
    if not app_writable:
        app.chmod(0o555)
    try:
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env, check=False)
    finally:
        app.chmod(0o755)
    assert out.returncode == 0, out.stdout + out.stderr
    rc = int(out.stdout.strip().splitlines()[-1].split("=", 1)[1])
    return rc, out.stdout, log.read_text(encoding="utf-8") if log.exists() else ""


def _stage(tmp_path: Path) -> str:
    return f"{tmp_path}/app/hermes-agent-src"


def _calls(log: str) -> list[str]:
    return [line[len("ARGS:"):] for line in log.splitlines() if line.startswith("ARGS:")]


@pytestmark_bash
def test_the_agents_overrides_are_passed_to_uv(tmp_path):
    rc, stdout, log = _install(tmp_path, AGENT_PYPROJECT)
    assert rc == 0
    calls = _calls(log)
    assert len(calls) == 1
    assert calls[0].startswith(f"pip install -e {_stage(tmp_path)}[all] --overrides ")
    assert "--trusted-host pypi.org --trusted-host files.pythonhosted.org" in calls[0]
    assert [line[len("OVERRIDE:"):] for line in log.splitlines() if line.startswith("OVERRIDE:")] == EXPECTED
    assert "override-dependencies (4 entries)" in stdout


@pytestmark_bash
def test_the_overrides_file_is_removed_afterwards(tmp_path):
    _, _, log = _install(tmp_path, AGENT_PYPROJECT)
    used = [line[len("OVERRIDES_FILE:"):] for line in log.splitlines() if line.startswith("OVERRIDES_FILE:")]
    assert used and not Path(used[0]).exists()
    assert list((tmp_path / "tmp").iterdir()) == []


@pytestmark_bash
def test_a_failed_install_is_reported_and_still_cleans_up(tmp_path):
    rc, _, _ = _install(tmp_path, AGENT_PYPROJECT, uv_rc=3)
    assert rc == 3
    assert list((tmp_path / "tmp").iterdir()) == []


PLAIN = "pip install -e {stage}[all] --trusted-host pypi.org --trusted-host files.pythonhosted.org"


FAILING_HELPER = "import sys\nsys.exit(1)\n"


@pytestmark_bash
@pytest.mark.parametrize(
    "case",
    ["no-override-table", "no-pyproject", "no-helper", "helper-fails", "no-writable-temp-dir"],
)
def test_every_fallback_is_the_plain_install_it_replaced(tmp_path, case):
    kwargs = {}
    text: str | None = AGENT_PYPROJECT
    if case == "no-override-table":
        text = NO_TABLE
    elif case == "no-pyproject":
        text = None
    elif case == "no-helper":
        kwargs["helper"] = tmp_path / "missing-helper.py"
    elif case == "helper-fails":
        failing = tmp_path / "failing-helper.py"
        failing.write_text(FAILING_HELPER, encoding="utf-8")
        kwargs["helper"] = failing
    elif case == "no-writable-temp-dir":
        kwargs["tmpdir"] = tmp_path / "does-not-exist"
        kwargs["app_writable"] = False
    rc, stdout, log = _install(tmp_path, text, **kwargs)
    assert rc == 0
    assert _calls(log) == [PLAIN.format(stage=_stage(tmp_path))]
    # A skipped overrides step is announced when the Agent declared any, so the
    # operator isn't left with only uv's "unsatisfiable" to go on.
    if case == "helper-fails":
        assert "!! Could not read hermes-agent's [tool.uv] override-dependencies" in stdout
    if case == "no-writable-temp-dir":
        assert "!! Could not create a temporary file for hermes-agent's uv overrides" in stdout
    if case in ("no-override-table", "no-pyproject", "no-helper"):
        assert "!!" not in stdout
    assert list((tmp_path / "app").iterdir()) == [Path(_stage(tmp_path))]


@pytestmark_bash
def test_an_unwritable_tmpdir_falls_back_to_the_stage_parent(tmp_path):
    # read_only podman (#1470) / userns (#2010): /tmp is not writable but /app is.
    rc, _, log = _install(tmp_path, AGENT_PYPROJECT, tmpdir=tmp_path / "does-not-exist")
    assert rc == 0
    used = [line[len("OVERRIDES_FILE:"):] for line in log.splitlines() if line.startswith("OVERRIDES_FILE:")]
    assert len(used) == 1 and Path(used[0]).parent == tmp_path / "app"
    assert [line[len("OVERRIDE:"):] for line in log.splitlines() if line.startswith("OVERRIDE:")] == EXPECTED
    assert not Path(used[0]).exists()
    assert list((tmp_path / "app").iterdir()) == [Path(_stage(tmp_path))]


@pytestmark_bash
def test_a_plain_install_failure_still_propagates(tmp_path):
    rc, _, _ = _install(tmp_path, NO_TABLE, uv_rc=5)
    assert rc == 5


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_venv_install_goes_through_install_staged_agent():
    body = _bash_function(INIT_SH, "install_venv_dependencies")
    assert 'install_staged_agent "$_stage_src"' in body
    assert 'uv pip install -e "$_stage_src[all]"' not in body
    # Defined before its first caller: docker_init.bash runs top to bottom.
    assert INIT_SH.index("install_staged_agent() {") < INIT_SH.index("install_venv_dependencies() {")
