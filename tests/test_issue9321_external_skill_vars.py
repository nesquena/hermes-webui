"""``skills.external_dirs`` expansion parity with ``os.path.expandvars`` (#9321).

Follow-up to #7842: the resolver tokenized ``$VAR`` itself, so it diverged from
the stdlib for undocumented-but-accepted forms — a bare ``$1abc`` never expanded
on POSIX (``posixpath`` accepts any ``\\w+``), and on Windows ``%NAME%``, a
hyphenated bare ``$NAME`` and ``$$`` were not handled as ``ntpath`` does.

These tests pin the resolver to the stdlib result on both platforms (the
``windows`` switch makes the Windows rules testable on Linux, #9321) while the
values keep coming from the profile binding: the live process environment is
never read.
"""

import os
import types
from types import MappingProxyType

import pytest
import yaml

# The names the issue measured with (documented in the PR body).
BINDING = {"1abc": "/X", "FOO": "/Y", "MY-VAR": "/Z"}
FORMS = [
    "$1abc",
    "${1abc}",
    "$FOO",
    "$$FOO",
    "%FOO%",
    "%MY-VAR%",
    "$MY-VAR",
    "~/$FOO",
]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A named profile home with the WebUI globals pointed at tmp_path."""
    import api.paths as paths
    import api.profiles as profiles

    root = tmp_path / ".hermes"
    work = root / "profiles" / "work"
    shell_home = tmp_path / "user"
    for path in (root / "skills", work / "skills", shell_home):
        path.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(paths, "HOME", shell_home)
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({}))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {})
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_LAUNCHER_DOTENV", {})
    monkeypatch.setattr(profiles, "_NAMED_LAUNCH_HOME_AGENT_READINGS", {})
    monkeypatch.setenv("HERMES_HOME", str(root))
    return types.SimpleNamespace(root=root, work=work, shell_home=shell_home, tmp=tmp_path)


def _dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _config(home, external_dirs):
    (home / "config.yaml").write_text(yaml.safe_dump({"skills": {"external_dirs": external_dirs}}))


def _resolve(home, **kwargs):
    from api.skill_runtime import profile_external_skill_dirs

    return profile_external_skill_dirs(home, **kwargs)


@pytest.mark.parametrize("form", FORMS)
def test_expansion_equals_the_stdlib_on_each_platform(form, monkeypatch):
    """For every measured form the helper returns what the stdlib returns."""
    import ntpath
    import posixpath

    from api.skill_runtime import expand_path_vars

    for name, value in BINDING.items():
        monkeypatch.setenv(name, value)  # what ``*.expandvars`` reads

    assert expand_path_vars(form, BINDING, windows=False) == posixpath.expandvars(form)
    assert expand_path_vars(form, BINDING, windows=True) == ntpath.expandvars(form)

    # No flag: the running platform's rules, as ``os.path.expandvars`` would use.
    stdlib = ntpath if os.name == "nt" else posixpath
    assert expand_path_vars(form, BINDING) == stdlib.expandvars(form)


@pytest.mark.parametrize("form", ["$ISSUE9321_MISSING", "${ISSUE9321_MISSING}", "%ISSUE9321_MISSING%"])
def test_unbound_names_stay_literal_text(form):
    from api.skill_runtime import expand_path_vars

    assert expand_path_vars(form, {}, windows=False) == form
    assert expand_path_vars(form, {}, windows=True) == form


@pytest.mark.parametrize("entry", ["$1abc", "${1abc}"])
def test_bare_numeric_name_expands_from_the_profile_dotenv(profile, entry):
    """POSIX ``$1abc`` is a real reference: bound in the profile .env it resolves."""
    target = _dir(profile.tmp / "numeric-root")
    (profile.work / ".env").write_text(f"1abc={target}\n")
    _config(profile.work, [entry])

    assert _resolve(profile.work) == [target]


def test_percent_form_resolves_only_under_the_windows_rules(profile):
    """``%NAME%`` is Windows syntax: skipped on POSIX, expanded under ``windows=True``."""
    target = _dir(profile.tmp / "percent-root")
    (profile.work / ".env").write_text(f"FOO={target}\nMY-VAR={target}\n")
    _config(profile.work, ["%FOO%", "$MY-VAR"])

    assert _resolve(profile.work, windows=False) == []
    assert _resolve(profile.work, windows=True) == [target]


def test_doubled_dollar_keeps_the_platform_result(profile):
    """``$$FOO`` is ``$/Y`` on POSIX and ``$FOO`` on Windows — never the bound dir."""
    from api.skill_runtime import expand_path_vars

    target = _dir(profile.tmp / "doubled-root")
    (profile.work / ".env").write_text(f"FOO={target}\n")
    _config(profile.work, ["$$FOO"])

    assert expand_path_vars("$$FOO", {"FOO": str(target)}, windows=False) == f"${target}"
    assert expand_path_vars("$$FOO", {"FOO": str(target)}, windows=True) == "$FOO"
    assert _resolve(profile.work, windows=False) == []
    assert _resolve(profile.work, windows=True) == []


def test_resolver_keeps_the_other_rules_with_the_new_expansion(profile):
    """Relative entries, unknown names, the local skills dir and duplicates are unchanged."""
    shared = _dir(profile.tmp / "shared")
    relative = _dir(profile.work / "relative")
    _config(profile.work, [
        "relative", "$ISSUE9321_MISSING", "$HOME/../shared", str(profile.work / "skills"),
        str(shared), str(shared) + "/", str(profile.tmp / "absent"),
    ])

    assert _resolve(profile.work) == [relative, shared]
