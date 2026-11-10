"""Guards the temp-root isolation added to ``tests/conftest.py``.

The Hermes runtime exports TMPDIR/TMP/TEMP pointing inside ``~/.hermes``. If a
test process inherits those unchanged, ``tempfile.gettempdir()`` lands in
``~/.hermes/cache/scratch`` and the Agent folds any temp home under the native
home back onto the real ``~/.hermes/profiles/`` — a test-created profile then
appears in the live profile list. ``conftest.py`` now pins all three variables
to a root verifiably outside the production home, and only accepts a root it
can actually write to.

The two smoke assertions at the bottom only observe whatever environment the
runner happens to export, so they pass even on an unrepaired conftest (a clean
runner with the three variables unset sees a safe OS temp dir and compares three
``None`` values). The discriminating tests therefore seed the REAL unsafe
environment — a fake ``HOME`` whose ``.hermes/cache/scratch`` is exported as
TMPDIR/TEMP/TMP — inside a FRESH CHILD PROCESS, import ``tests/conftest.py``
there, and assert the exported variables and ``tempfile.gettempdir()`` all moved
outside that fake production home. They were verified red against the
pre-repair conftest and green on the repaired one.

The seeding has to cover every variable the platform reads the home from.
``pathlib.Path.home()`` is ``Path("~").expanduser()``, which resolves through
``HOME`` on POSIX but through ``USERPROFILE`` on Windows — so a child that only
inherits a repointed ``HOME`` still anchors its production-home check on the
REAL user's ``~/.hermes`` on Windows, accepts the seeded scratch directory, and
fails both unsafe-inheritance assertions even though the repair works. The
probe therefore exports ``USERPROFILE`` alongside ``HOME``, asserts the child
resolved the seeded home, and can re-run the discriminator under the Windows
rule (``PROBE_WINDOWS_HOME``) so that half is covered on a POSIX runner too —
``tests.yml`` only ever runs this suite on ubuntu.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFTEST = REPO_ROOT / "tests" / "conftest.py"

# Cold-process probe: import the real conftest in this child and report the
# environment it leaves behind. ``PROBE_PRECACHE_TEMPDIR`` seeds
# ``tempfile.tempdir`` BEFORE the import — gettempdir() caches its first answer
# for the life of the process, so the repair's cache reset must beat it.
# ``PROBE_WINDOWS_HOME`` makes the child resolve its home the way Windows does
# (``Path.home()`` is ``Path("~").expanduser()``, i.e. ``ntpath.expanduser``,
# which reads USERPROFILE and ignores HOME), so the Windows half of the seeding
# is exercised on a POSIX runner.
_PROBE = """
import importlib.util, json, ntpath, os, pathlib, sys, tempfile

if os.environ.pop("PROBE_WINDOWS_HOME", ""):
    pathlib.Path.home = classmethod(lambda cls: pathlib.Path(ntpath.expanduser("~")))

precached = os.environ.pop("PROBE_PRECACHE_TEMPDIR", "")
if precached:
    tempfile.tempdir = precached

spec = importlib.util.spec_from_file_location("webui_conftest_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

print(json.dumps({
    "home": str(pathlib.Path.home()),
    "TMPDIR": os.environ.get("TMPDIR"),
    "TEMP": os.environ.get("TEMP"),
    "TMP": os.environ.get("TMP"),
    "gettempdir": tempfile.gettempdir(),
}))
"""


def _run_conftest_probe(
    tmp_path: Path,
    *,
    temp_root: Path,
    pre_cache: bool = False,
    windows_home: bool = False,
) -> dict:
    """Import ``tests/conftest.py`` in a fresh child with a seeded environment.

    ``temp_root`` is exported as TMPDIR/TEMP/TMP and the fake production home
    under ``tmp_path`` is exported as BOTH ``HOME`` and ``USERPROFILE``, so the
    child's ``Path.home()`` — which reads HOME on POSIX and USERPROFILE on
    Windows — always lands on a tree this test owns. Seeding only HOME left a
    Windows child anchored on the real user's ``~/.hermes``: it accepted the
    seeded scratch dir and both unsafe-inheritance tests failed even on the
    repaired conftest (Greptile review of #8898). ``windows_home`` additionally
    makes the child resolve its home by the Windows rule, to reproduce that on a
    POSIX runner.

    An explicit safe state dir keeps the cold import alive on the UNREPAIRED
    conftest as well: its default state dir derives from ``gettempdir()`` and the
    production-proximity guard refuses to import once that lands inside the
    seeded home — pinning it isolates the temp-root behaviour under test instead
    of tripping that guard.
    """
    home = tmp_path / "home"
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(temp_root)
    env.pop("HERMES_HOME", None)
    env.pop("HERMES_WEBUI_TEST_STATE_ROOT", None)
    env.pop("HERMES_WEBUI_TEST_PORT", None)
    env["HERMES_WEBUI_TEST_STATE_DIR"] = str(tmp_path / "safe-state")
    env.pop("PROBE_PRECACHE_TEMPDIR", None)
    env.pop("PROBE_WINDOWS_HOME", None)
    if pre_cache:
        env["PROBE_PRECACHE_TEMPDIR"] = str(temp_root)
    if windows_home:
        env["PROBE_WINDOWS_HOME"] = "1"
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(CONFTEST)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, (
        f"importing {CONFTEST} in a fresh process failed:\n{out.stderr}"
    )
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload.get("home") == str(home), (
        f"the conftest child resolved its home to {payload.get('home')!r}, not "
        f"the seeded fake home {str(home)!r}; conftest derives its "
        "production-home guard from Path.home(), so it must repoint every "
        "variable the platform resolves the home from (HOME on POSIX, "
        "USERPROFILE on Windows) or the check anchors on the real ~/.hermes "
        "and accepts the unsafe inherited temp root"
    )
    return payload


def _assert_temp_root_escaped_home(payload: dict, production_home: Path, label: str):
    """All three exported variables and the cached answer must leave the home.

    Returns the selected root and ``tempfile.gettempdir()``. The failure marker
    ``TEMP_ROOT_STILL_INSIDE_HOME`` is what an unrepaired conftest trips.
    """
    exported = {}
    for key in ("TMPDIR", "TEMP", "TMP"):
        value = payload.get(key)
        assert value, f"{label}: conftest did not export {key}"
        exported[key] = Path(value).resolve()
    cached = Path(payload["gettempdir"]).resolve()

    assert len(set(exported.values())) == 1, (
        f"{label}: TMPDIR/TEMP/TMP disagree ({exported!r}); gettempdir() walks "
        "them in order and falls through to whichever one still points inside "
        "~/.hermes"
    )
    selected = next(iter(set(exported.values())))

    for name, path in (*exported.items(), ("tempfile.gettempdir()", cached)):
        assert path != production_home and production_home not in path.parents, (
            f"TEMP_ROOT_STILL_INSIDE_HOME: {label}: {name}={path} is inside the "
            f"production Hermes home {production_home}; conftest must repoint "
            "TMPDIR/TEMP/TMP outside the production home"
        )
    return selected, cached


def test_unsafe_inherited_temp_env_is_repointed_outside_the_fake_home(tmp_path):
    """Cold-process discriminator for the reported bug.

    Seeds the environment the Hermes runtime actually exports — all three
    variables pointing at the fake home's ``.hermes/cache/scratch`` — and
    imports conftest in a fresh child. Fails on the unrepaired conftest with
    ``TEMP_ROOT_STILL_INSIDE_HOME`` (the variables are never repointed).
    """
    scratch = tmp_path / "home" / ".hermes" / "cache" / "scratch"
    scratch.mkdir(parents=True)
    payload = _run_conftest_probe(tmp_path, temp_root=scratch)
    production_home = (tmp_path / "home" / ".hermes").resolve()
    _assert_temp_root_escaped_home(payload, production_home, "unsafe inherited env")


def test_unsafe_inherited_temp_env_is_repointed_under_windows_home_rules(tmp_path):
    """The discriminator again, with the child resolving its home like Windows.

    ``Path.home()`` reads USERPROFILE on Windows and ignores HOME, so a probe
    that seeds only HOME leaves the child's production-home anchor on the real
    user's ``~/.hermes``: it then accepts the seeded scratch directory and the
    unsafe-inheritance assertions fail even though the repair works. Fails on a
    probe that seeds only HOME (the child reports the real home back, and
    TEMP_ROOT_STILL_INSIDE_HOME follows); this suite only runs on ubuntu in CI,
    so without the emulated rule nothing would catch that on a POSIX runner.
    """
    scratch = tmp_path / "home" / ".hermes" / "cache" / "scratch"
    scratch.mkdir(parents=True)
    payload = _run_conftest_probe(tmp_path, temp_root=scratch, windows_home=True)
    production_home = (tmp_path / "home" / ".hermes").resolve()
    _assert_temp_root_escaped_home(payload, production_home, "windows home rule")


def test_precached_tempdir_inside_the_home_is_reset_and_repointed(tmp_path):
    """Same unsafe inheritance, but ``tempfile.tempdir`` is already cached there.

    gettempdir() returns its cached answer without consulting the environment,
    so the repair must reset the cache (not just re-export the variables).
    Fails on the unrepaired conftest: the cached inside-home answer survives.
    """
    scratch = tmp_path / "home" / ".hermes" / "cache" / "scratch"
    scratch.mkdir(parents=True)
    payload = _run_conftest_probe(tmp_path, temp_root=scratch, pre_cache=True)
    production_home = (tmp_path / "home" / ".hermes").resolve()
    selected, cached = _assert_temp_root_escaped_home(
        payload, production_home, "pre-cached tempdir"
    )
    assert cached != scratch.resolve(), (
        f"tempfile.gettempdir() still returns the pre-cached {scratch}; "
        "conftest must reset the tempfile cache after repointing the variables"
    )


def test_safe_custom_temp_root_is_honoured(tmp_path):
    """Positive control: a SAFE inherited root must be kept, not overridden.

    Passes on both the unrepaired and the repaired conftest — the control that
    keeps the discriminator honest — and pins the precedence rule: the
    inherited safe root wins over a hardcoded fallback.
    """
    safe_root = tmp_path / "custom-tmp"
    safe_root.mkdir()
    payload = _run_conftest_probe(tmp_path, temp_root=safe_root)
    production_home = (tmp_path / "home" / ".hermes").resolve()
    selected, cached = _assert_temp_root_escaped_home(
        payload, production_home, "safe custom root"
    )
    assert selected == safe_root.resolve() and cached == safe_root.resolve(), (
        f"the safe custom root {safe_root} must be honoured, got "
        f"TMPDIR/TEMP/TMP={selected} and gettempdir()={cached}"
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod cannot make a directory unwritable on Windows"
)
def test_unwritable_temp_root_falls_back_to_a_writable_root(tmp_path):
    """An existing-but-unwritable root must never be selected (review of #8898).

    ``mkdir(exist_ok=True)`` succeeds on a read-only directory, so the repair
    proves writability by creating and removing a probe file before accepting a
    candidate. Fails on the unrepaired conftest: the read-only root stays
    exported and no temp file can be created under it.
    """
    unwritable = tmp_path / "unwritable-tmp"
    unwritable.mkdir()
    os.chmod(unwritable, 0o555)
    try:
        (unwritable / ".probe").write_text("x")
    except OSError:
        pass
    else:
        pytest.skip("cannot make a directory unwritable here (running as root?)")

    payload = _run_conftest_probe(tmp_path, temp_root=unwritable)
    production_home = (tmp_path / "home" / ".hermes").resolve()
    selected, _ = _assert_temp_root_escaped_home(
        payload, production_home, "unwritable inherited root"
    )
    assert selected != unwritable.resolve(), (
        f"the unwritable root {unwritable} was selected; conftest must fall "
        "back to a writable temp root outside the production home"
    )
    with tempfile.NamedTemporaryFile(dir=selected):  # must actually accept writes
        pass


def test_temp_root_is_outside_the_production_hermes_home():
    production = (Path.home() / ".hermes").resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()

    assert temp_root != production, "tempfile.gettempdir() resolved to the production Hermes home"
    assert production not in temp_root.parents, (
        f"tempfile.gettempdir() ({temp_root}) is inside the production Hermes home "
        f"({production}); conftest must repoint TMPDIR/TMP/TEMP"
    )


def test_all_three_temp_env_vars_agree():
    assert os.environ.get("TMPDIR") == os.environ.get("TEMP") == os.environ.get("TMP"), (
        "TMPDIR, TEMP and TMP must all point at the same isolated temp root; "
        "tempfile.gettempdir() skips a missing candidate and falls through to the next"
    )
