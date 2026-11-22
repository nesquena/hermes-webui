"""Agent-free tests: ``skills.external_dirs`` resolves from the request profile (#7842).

The Agent's ``get_external_skills_dirs()`` (``agent/skill_utils.py``) reads the config
of the process's ``HERMES_HOME``. The stand-in below does the same, so the route test
fails on a WebUI that asks the Agent helper instead of reading the request profile.
``test_profile_external_skills_api.py`` drives the real Agent scanner.
"""

import os
import sys
import types
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml


@pytest.fixture
def homes(tmp_path, monkeypatch):
    import api.paths as paths
    import api.profiles as profiles

    root = tmp_path / ".hermes"
    work = root / "profiles" / "work"
    shell_home = tmp_path / "user"
    for d in (root / "skills", work / "skills", shell_home):
        d.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(paths, "HOME", shell_home)
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({}))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {})
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_LAUNCHER_DOTENV", {})
    monkeypatch.setattr(profiles, "_NAMED_LAUNCH_HOME_AGENT_READINGS", {})
    monkeypatch.setenv("HERMES_HOME", str(root))
    return types.SimpleNamespace(root=root, work=work, shell_home=shell_home, tmp=tmp_path)


def _config(home, external_dirs):
    (home / "config.yaml").write_text(yaml.safe_dump({"skills": {"external_dirs": external_dirs}}))


def _dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _resolve(home):
    from api.skill_runtime import profile_external_skill_dirs

    return profile_external_skill_dirs(home)


@pytest.fixture
def process_profile_agent(monkeypatch):
    """Stand-in for the Agent helper: the process home's config, process-env expansion."""
    skill_utils = types.ModuleType("agent.skill_utils")

    def get_external_skills_dirs():
        config = Path(os.environ["HERMES_HOME"]) / "config.yaml"
        entries = (yaml.safe_load(config.read_text()) or {}).get("skills", {}).get("external_dirs", [])
        return [Path(os.path.expandvars(entry)) for entry in entries]

    skill_utils.get_external_skills_dirs = get_external_skills_dirs
    agent = types.ModuleType("agent")
    agent.__path__ = []
    agent.skill_utils = skill_utils
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.skill_utils", skill_utils)


def test_skills_search_dirs_follow_the_request_profile(homes, process_profile_agent):
    """A named profile sharing root's skills lists them; root's own entries stay root's."""
    from api import routes

    root_only = _dir(homes.tmp / "root-only")
    _config(homes.root, [str(root_only), str(homes.root / "skills")])
    _config(homes.work, [str(homes.root / "skills")])  # the reported setup

    assert routes._active_skill_search_dirs(homes.work / "skills") == [
        homes.work / "skills", (homes.root / "skills").resolve(),
    ]

    # Root's own skills dir is its local root, not an external one.
    assert routes._active_skill_search_dirs(homes.root / "skills") == [homes.root / "skills", root_only]


@pytest.mark.parametrize("entry, dotenv, expected", [
    ("{abs}", "", "abs"),
    ("~/shared", "", "shell/shared"),
    ("$HOME/shared", "", "shell/shared"),
    ("${HERMES_HOME}/shared", "", "work/shared"),
    ("$HERMES_HOME/shared", "", "work/shared"),
    ("relative", "", "work/relative"),
    ("$WORK_SKILLS", "WORK_SKILLS={abs}\n", "abs"),
    ("${WORK_SKILLS}/nested", "WORK_SKILLS={abs}\n", "abs/nested"),
])
def test_entries_expand_like_the_agent_under_the_profile_binding(homes, entry, dotenv, expected):
    abs_dir = homes.tmp / "abs"
    bases = {"abs": abs_dir, "shell": homes.shell_home, "work": homes.work}
    base, _, rest = expected.partition("/")
    target = _dir(bases[base] / rest if rest else bases[base])
    _dir(abs_dir)
    (homes.work / ".env").write_text(dotenv.replace("{abs}", str(abs_dir)))
    _config(homes.work, [entry.replace("{abs}", str(abs_dir))])
    assert _resolve(homes.work) == [target]


def test_unresolved_and_skipped_entries_keep_the_others(homes, monkeypatch):
    shared = _dir(homes.tmp / "shared")
    leaked = _dir(homes.tmp / "process-only")
    monkeypatch.setenv("REL7842_PROCESS_ONLY", str(leaked))  # e.g. a streaming turn's mirror
    _config(homes.work, [
        "$REL7842_MISSING", "$REL7842_PROCESS_ONLY", str(homes.tmp / "absent"),
        "skills", str(shared), str(shared) + "/",
    ])
    assert _resolve(homes.work) == [shared]


@pytest.mark.parametrize("text", [
    None, "", "skills:\n", "skills: []\n", "skills:\n  external_dirs: 7\n", "skills: [\n", b"\xff\xfe\x00",
])
def test_missing_or_malformed_config_lists_no_external_roots(homes, text):
    if isinstance(text, bytes):
        (homes.work / "config.yaml").write_bytes(text)
    elif text is not None:
        (homes.work / "config.yaml").write_text(text)
    assert _resolve(homes.work) == []


def test_scalar_external_dirs_is_one_entry(homes):
    shared = _dir(homes.tmp / "shared")
    (homes.work / "config.yaml").write_text(f"skills:\n  external_dirs: {shared}\n")
    assert _resolve(homes.work) == [shared]


@pytest.mark.parametrize("case", ["root", "root-dotenv", "named", "named-launch", "named-launch-export"])
def test_variables_bind_as_profile_env_binding(homes, monkeypatch, case):
    """Root sees launch variables (root .env first); a named profile sees only its .env;
    root never sees a value a launcher sourced from a named launch home's .env."""
    import api.paths as paths
    import api.profiles as profiles

    launch, own, private = (_dir(homes.tmp / n) for n in ("launch", "own", "private"))
    exported = private if case == "named-launch" else launch
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({"REL7842_ROOT": str(exported)}))
    if case.startswith("named-launch"):
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.work))
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {"REL7842_ROOT": str(private)})
        (homes.work / ".env").write_text(f"REL7842_ROOT={private}\n")
    if case == "root-dotenv":
        (homes.root / ".env").write_text(f"REL7842_ROOT={own}\n")
    home = homes.work if case == "named" else homes.root
    _config(home, ["$REL7842_ROOT"])
    expected = {"root": [launch], "root-dotenv": [own], "named": [],
                "named-launch": [], "named-launch-export": [launch]}[case]
    assert _resolve(home) == expected


def test_config_and_env_edits_apply_on_the_next_lookup(homes):
    first, second = _dir(homes.tmp / "first"), _dir(homes.tmp / "second")
    _config(homes.work, ["$WORK_SKILLS"])
    (homes.work / ".env").write_text(f"WORK_SKILLS={first}\n")
    assert _resolve(homes.work) == [first]
    (homes.work / ".env").write_text(f"WORK_SKILLS={second}\n")
    assert _resolve(homes.work) == [second]
