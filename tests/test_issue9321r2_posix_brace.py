"""POSIX braced references need their closing brace (#9321 follow-up).

``posixpath.expandvars`` tokenizes ``${name}`` with a *required* closing brace, so an
unmatched ``${`` is plain text and a later ``$name`` still expands. The first revision
of ``expand_path_vars`` ported the module-level ``_varpattern`` instead, whose brace is
optional: the opener then swallowed the rest of the entry, including a later valid
reference, and a directory that master resolves became undiscoverable.

These tests pin that contract on the helper and end-to-end through the resolver, with
the ordinary bound-reference, unknown-name and closed-brace forms kept as controls.
"""

import types
from types import MappingProxyType

import pytest
import yaml

BINDING = {"ROOT": "external", "NAME": "value"}


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


def test_unmatched_opener_does_not_swallow_a_later_reference():
    """``${archive/$ROOT`` is text plus a live reference, not one unclosed ``${``."""
    from api.skill_runtime import expand_path_vars

    assert expand_path_vars("${archive/$ROOT", BINDING, windows=False) == "${archive/external"
    # The same class with the reference up front, and one with two later references.
    assert expand_path_vars("${a $ROOT/$NAME", BINDING, windows=False) == "${a external/value"


def test_unmatched_opener_entry_stays_discoverable(profile):
    """The directory master resolves must still be a root, not skipped as non-existent."""
    literal = _dir(profile.work / "${archive" / "external")
    (profile.work / ".env").write_text("ROOT=external\n")
    _config(profile.work, ["${archive/$ROOT"])

    assert _resolve(profile.work, windows=False) == [literal]


def test_ordinary_reference_forms_are_unchanged(profile):
    """Controls: bound reference, braced reference and unknown names keep their result."""
    from api.skill_runtime import expand_path_vars

    assert expand_path_vars("$ROOT/sub", BINDING, windows=False) == "external/sub"
    assert expand_path_vars("${ROOT}/sub", BINDING, windows=False) == "external/sub"
    assert expand_path_vars("${NOPE}/$NAME", BINDING, windows=False) == "${NOPE}/value"
    # A closed brace that names no binding stays literal: only the *unclosed* opener
    # is plain text, and its closing counterpart is not a reference at all.
    assert expand_path_vars("${archive/$ROOT}", BINDING, windows=False) == "${archive/$ROOT}"

    target = _dir(profile.tmp / "bound-root")
    (profile.work / ".env").write_text(f"ROOT={target}\n")
    _config(profile.work, ["${ROOT}", "${NOPE}", "${archive/$ROOT}"])
    assert _resolve(profile.work, windows=False) == [target]
