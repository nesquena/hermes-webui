"""#8737: a named profile's Gateway session config resolves a ``${VAR}`` that its
own ``.env`` defines from that ``.env`` (#8744, decision A: an overlay).

The Gateway worker reads a named profile's config through
``api.gateway_chat._gateway_config_for_profile``. That worker thread carries no
request profile env, so master expands every ``${VAR}`` from ``os.environ``: a
Gateway setting that only the profile's ``.env`` defines stays the literal
``${VAR}`` and fails closed. The fix runs master's own
``get_config_for_profile_home()`` unchanged, with the profile's ``.env``
assignments overlaid on the calling thread's env for that one read:

* a name the profile's ``.env`` defines resolves to that value, over the process
  env and over a thread env of the same name;
* every other name resolves exactly as on master (thread env, block flag, process
  env); an undefined one stays the literal ``${VAR}``; nothing is dropped;
* a ``.env`` that cannot be read means no overlay, so the read is master's, and
  ``_gateway_endpoint_for_profile`` still raises as on master;
* the thread env is restored (nested and on error), and no other thread sees it.

The differential tests compare against ``api.config.get_config_for_profile_home``,
which this change leaves byte-identical to master.
"""

import copy
import os
import threading
from types import MappingProxyType

import pytest

PROCESS_LEAK = "process-leak"

ALPHA_DOTENV = (
    "ALPHA_GW_RUNS=true\n"
    "SHARED_PROVIDER=openrouter\n"
    "SHARED_MODEL=model-alpha\n"
    "SHARED_EFFORT=high\n"
    "ALPHA_ONLY=alpha-secret\n"
    "EMPTY_IN_DOTENV=\n"  # an empty value is not an assignment (master's .env loaders)
    "# COMMENTED_OUT=never\n"
)
BETA_DOTENV = (
    "SHARED_PROVIDER=anthropic\nSHARED_MODEL=model-beta\nSHARED_EFFORT=low\nBETA_ONLY=beta-secret\n"
)
ALPHA_NAMES = {"ALPHA_GW_RUNS", "SHARED_PROVIDER", "SHARED_MODEL", "SHARED_EFFORT", "ALPHA_ONLY"}

ALPHA_CONFIG = """\
webui_gateway_use_runs_api: ${ALPHA_GW_RUNS}
model:
  provider: ${SHARED_PROVIDER}
  default: ${SHARED_MODEL}
agent:
  reasoning_effort: ${SHARED_EFFORT}
"""

PROCESS_ENV = {
    "SHARED_PROVIDER": PROCESS_LEAK,
    "SHARED_MODEL": PROCESS_LEAK,
    "SHARED_EFFORT": PROCESS_LEAK,
    "PROC_ONLY": "proc-value",
    "LAUNCHER_ONLY": "launcher-value",
    "EMPTY_IN_DOTENV": "proc-empty-in-dotenv",
    # Codex's reproductions: launcher-exported values the profile's .env does not define.
    "PREFILL_GUARD": "-n",
    "IMAGE_MODE": "text",
}
ABSENT_ENV = ("ALPHA_GW_RUNS", "ALPHA_ONLY", "BETA_ONLY", "ROOT_ONLY", "UNDEFINED_8744", "THREAD_ONLY",
              "COMMENTED_OUT", "GAMMA_ONLY", "HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Root home plus named profiles ``alpha`` and ``beta`` whose ``.env`` files disagree.

    The process env holds a third value for every shared key, as a launcher export,
    a profile switch or another profile's worker leaves it.
    """
    import api.paths as paths
    import api.profiles as profiles

    root = tmp_path / ".hermes"
    root.mkdir()
    (root / ".env").write_text("ROOT_ONLY=root-secret\nSHARED_MODEL=model-root\n", encoding="utf-8")
    (root / "config.yaml").write_text("model:\n  default: plain-root\n", encoding="utf-8")
    homes = {"default": root}
    for name, dotenv in (("alpha", ALPHA_DOTENV), ("beta", BETA_DOTENV)):
        homes[name] = _add_profile(root, name, dotenv, ALPHA_CONFIG)

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "default", "is_default": True}, {"name": "alpha"}, {"name": "beta"}],
    )
    monkeypatch.setattr(
        paths, "STARTUP_ENV", MappingProxyType({"LAUNCHER_ONLY": "launcher-value"}), raising=False
    )
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    for key, value in PROCESS_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ABSENT_ENV:
        monkeypatch.delenv(key, raising=False)
    profiles._invalidate_root_profile_cache()
    profiles._tls.profile = None

    yield homes

    profiles._tls.profile = None
    profiles._invalidate_root_profile_cache()


def _add_profile(root, name, dotenv, config_text):
    home = root / "profiles" / name
    home.mkdir(parents=True)
    if dotenv is not None:
        (home / ".env").write_text(dotenv, encoding="utf-8")
    (home / "config.yaml").write_text(config_text, encoding="utf-8")
    return home


def _master_read(home):
    """master's reader, called directly (api/config.py is unchanged by the fix)."""
    from api.config import get_config_for_profile_home

    return get_config_for_profile_home(home)


_MISSING = object()


class _ThreadState:
    """Set, snapshot and restore the calling thread's ``_thread_ctx`` env and block flag."""

    def __init__(self, env=_MISSING, block=_MISSING):
        from api.config import _thread_ctx

        self.ctx = _thread_ctx
        self.env, self.block = env, block

    def __enter__(self):
        self.saved = self.current()
        self._set(self.env, self.block)
        return self

    def __exit__(self, *exc):
        self._set(*self.saved)
        return False

    def _set(self, env, block):
        for attr, value in (("env", env), ("block_process_env_fallback", block)):
            if value is _MISSING:
                if hasattr(self.ctx, attr):
                    delattr(self.ctx, attr)
            else:
                setattr(self.ctx, attr, value)

    def current(self):
        return (getattr(self.ctx, "env", _MISSING), getattr(self.ctx, "block_process_env_fallback", _MISSING))


@pytest.fixture
def config_cache_snapshot():
    """Restore api.config's shared ambient cache after a test that reloads it."""
    import api.config as config

    with config._cfg_lock:
        saved = (copy.deepcopy(config._cfg_cache), config._cfg_path, config._cfg_mtime,
                 config._cfg_fingerprint, config.cfg, config.cfg is config._cfg_cache)
    yield
    with config._cfg_lock:
        config._cfg_cache.clear()
        config._cfg_cache.update(saved[0])
        config._cfg_path, config._cfg_mtime, config._cfg_fingerprint = saved[1:4]
        config.cfg = config._cfg_cache if saved[5] else saved[4]


# ── #8737 ─────────────────────────────────────────────────────────────────────


def test_8737_gateway_setting_defined_only_in_the_profiles_dotenv_resolves(tree):
    """#8737's reproduction: RED on master (the setting stays ``${ALPHA_GW_RUNS}``)."""
    from api.gateway_chat import _gateway_config_for_profile, _gateway_use_runs_api_enabled

    cfg = _gateway_config_for_profile("alpha")

    assert cfg["webui_gateway_use_runs_api"] == "true"
    assert _gateway_use_runs_api_enabled(cfg) is True


def test_the_profiles_dotenv_wins_over_the_process_env_for_the_names_it_defines(tree):
    from api.gateway_chat import _gateway_config_for_profile

    alpha = _gateway_config_for_profile("alpha")
    beta = _gateway_config_for_profile("beta")

    assert (alpha["model"]["provider"], alpha["model"]["default"]) == ("openrouter", "model-alpha")
    assert alpha["agent"]["reasoning_effort"] == "high"
    assert (beta["model"]["provider"], beta["model"]["default"]) == ("anthropic", "model-beta")
    assert beta["agent"]["reasoning_effort"] == "low"
    # beta's .env does not define ALPHA_GW_RUNS and nothing else does: master's literal, kept.
    assert beta["webui_gateway_use_runs_api"] == "${ALPHA_GW_RUNS}"


def test_gateway_parity_blocked_and_protected_names_keep_masters_value(tree):
    """Shell-identity and operator keys in a profile .env never overlay (as on every runtime path)."""
    from api.gateway_chat import _gateway_config_for_profile

    home = _add_profile(
        tree["default"], "gamma",
        "HOME=/gamma-home\nXDG_CONFIG_HOME=/gamma-xdg\nHERMES_WEBUI_ISOLATED_PROFILE=1\nGAMMA_ONLY=gamma-value\n",
        "home: ${HOME}\nxdg: ${XDG_CONFIG_HOME}\niso: ${HERMES_WEBUI_ISOLATED_PROFILE}\nown: ${GAMMA_ONLY}\n",
    )
    master = _master_read(home)
    cfg = _gateway_config_for_profile("gamma")

    assert {k: cfg[k] for k in ("home", "xdg", "iso")} == {k: master[k] for k in ("home", "xdg", "iso")}
    assert cfg["home"] != "/gamma-home" and cfg["xdg"] != "/gamma-xdg"
    assert cfg["own"] == "gamma-value"


def test_another_profiles_open_worker_scope_own_names_win_the_rest_is_master(tree):
    """alpha's worker mirrors alpha's .env into os.environ while beta is read: the names
    beta's .env defines are beta's; a name only alpha defines resolves exactly as master
    reads it at that moment (the cross-profile process-env mixing A deliberately leaves)."""
    import api.profiles as profiles
    from api.gateway_chat import _gateway_config_for_profile

    (tree["beta"] / "config.yaml").write_text(
        ALPHA_CONFIG + "alpha_only: ${ALPHA_ONLY}\n", encoding="utf-8"
    )
    entered, release, errors = threading.Event(), threading.Event(), []

    def body():
        try:
            with profiles.profile_env_for_background_worker("alpha", scope_skill_modules=False):
                entered.set()
                release.wait(10)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            entered.set()

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    try:
        assert entered.wait(10) and not errors, errors
        beta = _gateway_config_for_profile("beta")
        master = _master_read(tree["beta"])
    finally:
        release.set()
        worker.join(10)
    assert (beta["model"]["provider"], beta["model"]["default"]) == ("anthropic", "model-beta")
    assert beta["agent"]["reasoning_effort"] == "low"
    assert beta["alpha_only"] == master["alpha_only"]
    assert beta["webui_gateway_use_runs_api"] == master["webui_gateway_use_runs_api"]


# ── Differential: everything the profile's .env does not define is master's ──

# Leaf values whose ${...} names alpha's .env does NOT define.
_LEAVES = [
    "${PROC_ONLY}",                    # process env
    "${LAUNCHER_ONLY}",                # launcher-only (process env + launch snapshot)
    "${UNDEFINED_8744}",               # undefined anywhere: master's literal
    "${ROOT_ONLY}",                    # root's .env only
    "${BETA_ONLY}",                    # another profile's .env only
    "${EMPTY_IN_DOTENV}",              # alpha's .env has it EMPTY: not an assignment
    "${COMMENTED_OUT}",                # commented out in alpha's .env
    "${THREAD_ONLY}",                  # thread env only (scenario-dependent)
    "${HERMES_HOME}",                  # never in a .env: master's resolution
    "pre-${PROC_ONLY}-${UNDEFINED_8744}-post",
    "${not closed",
    "$${PROC_ONLY}",
    "${}",
    "${PROC_ONLY:-fallback}",
    "${ PROC_ONLY }",
    "\\${PROC_ONLY}",
    "plain text",
    "",
    7,
    2.5,
    True,
    None,
]


def _shapes(leaf):
    return [
        {"top": leaf},
        {"nested": {"deeper": {"leaf": leaf}}},
        {"items": [leaf, "fixed", leaf]},
        {"records": [{"k": leaf, "other": "fixed"}, [leaf]]},
    ]


def _differential_corpus():
    corpus = []
    for leaf in _LEAVES:
        corpus.extend(_shapes(leaf))
    corpus.extend([
        # Codex #2: one unresolved element of a prefill argv.
        {"webui_prefill_messages_script": ["/bin/sh", "${PREFILL_GUARD}", "-c", "echo should-not-run"]},
        {"webui_prefill_messages_script": ["/bin/sh", "${UNDEFINED_8744}", "-c", "echo should-not-run"]},
        # Codex #3: an image mode only the launcher defines.
        {"agent": {"image_input_mode": "${IMAGE_MODE}"}},
        {"agent": {"image_input_mode": "${UNDEFINED_8744}"}},
        # Codex #1's config side: a literal URL plus references (the .env is readable here).
        {"webui_gateway_base_url": "http://literal.example:8642", "terminal": {"backend": "ssh"},
         "model": {"default": "${PROC_ONLY}"}},
        # Everything at once.
        {"all": list(_LEAVES), "map": {str(i): leaf for i, leaf in enumerate(_LEAVES)},
         "webui_gateway_use_runs_api": "${UNDEFINED_8744}", "fallback_providers": [
             {"provider": "${PROC_ONLY}", "key_env": "${UNDEFINED_8744}", "api_key": "inline"}]},
    ])
    return corpus


_SCENARIOS = {
    "no-thread-env": dict(),
    "thread-env": dict(env={"PROC_ONLY": "thread-value", "THREAD_ONLY": "thread-only"}, block=False),
    "thread-env-blocked": dict(env={"THREAD_ONLY": "thread-only"}, block=True),
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_differential_equals_master_where_the_profiles_dotenv_does_not_define_the_name(tree, scenario):
    import yaml

    from api.gateway_chat import _gateway_config_for_profile

    corpus = _differential_corpus()
    assert len(corpus) == 94
    root = tree["default"]
    compared = 0
    for index, document in enumerate(corpus):
        name = f"diff{index}"  # one home per case: no (path, mtime, size) parse-cache collision
        home = _add_profile(root, name, ALPHA_DOTENV, yaml.safe_dump(document, sort_keys=False))
        with _ThreadState(**_SCENARIOS[scenario]) as state:
            before = state.current()
            expected = _master_read(home)
            got = _gateway_config_for_profile(name)
            after = state.current()
        assert after[0] is before[0] and after[1] is before[1], f"case {index}: thread state changed"
        assert got == expected and repr(got) == repr(expected), f"case {index}: {document!r}"
        compared += 1
    assert compared == len(corpus)


def test_overlay_oracle_is_masters_reader_under_the_process_env_plus_the_profiles_dotenv(tree, monkeypatch):
    """Names alpha's .env defines (mixed with ones it does not): exactly master's read with
    that .env loaded over the process env."""
    import yaml

    from api.gateway_chat import _gateway_config_for_profile

    leaves = ["${ALPHA_ONLY}", "${SHARED_MODEL}", "${ALPHA_GW_RUNS}",
              "${ALPHA_ONLY}/${PROC_ONLY}/${UNDEFINED_8744}/${BETA_ONLY}", "${ ALPHA_ONLY }"]
    corpus = [shape for leaf in leaves for shape in _shapes(leaf)]
    corpus.append({"webui_prefill_messages_script": ["/bin/sh", "${ALPHA_ONLY}", "-c", "${UNDEFINED_8744}"]})
    root = tree["default"]
    for index, document in enumerate(corpus):
        name = f"over{index}"
        home = _add_profile(root, name, ALPHA_DOTENV, yaml.safe_dump(document, sort_keys=False))
        got = _gateway_config_for_profile(name)
        with monkeypatch.context() as m:
            for line in ALPHA_DOTENV.splitlines():
                key, _, value = line.partition("=")
                if key in ALPHA_NAMES:
                    m.setenv(key, value)
            expected = _master_read(home)
        assert got == expected and repr(got) == repr(expected), f"case {index}: {document!r}"
        assert "alpha-secret" in repr(got) or "model-alpha" in repr(got) or "'true'" in repr(got)


# ── Codex's three fail-open reproductions against the previous fold ───────────


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can search a chmod 0 dir")
def test_codex_1_unreadable_dotenv_keeps_masters_endpoint_error_and_config(tree, tmp_path):
    """A .env behind a directory this process cannot search: no overlay. The endpoint
    raises as on master (the previous fold fell back to the SHARED Gateway URL)."""
    from api.gateway_chat import _gateway_config_for_profile, _gateway_endpoint_for_profile

    home = tree["beta"]
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "real.env").write_text(
        "HERMES_WEBUI_GATEWAY_BASE_URL=http://profile-own.example:8642\nSHARED_MODEL=model-beta\n",
        encoding="utf-8",
    )
    (home / ".env").unlink()
    (home / ".env").symlink_to(locked / "real.env")
    # The profile's own Gateway URL lives in that .env only (Codex's reproduction).
    (home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\nmodel:\n  default: ${SHARED_MODEL}\n", encoding="utf-8"
    )
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            _gateway_endpoint_for_profile("beta")
        with _ThreadState() as state:
            cfg = _gateway_config_for_profile("beta")
            assert state.current() == (_MISSING, _MISSING)
        assert cfg == _master_read(home)
        assert cfg["model"]["default"] == PROCESS_LEAK  # master's value, nothing dropped
        assert cfg["terminal"]["backend"] == "ssh"
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize("guard", ["${PREFILL_GUARD}", "${UNDEFINED_8744}"])
def test_codex_2_prefill_argv_is_masters(tree, monkeypatch, guard):
    """The previous fold dropped the unresolved element and ran ``/bin/sh -c cmd``."""
    import api.streaming as streaming
    from api.gateway_chat import _gateway_config_for_profile

    home = tree["beta"]
    (home / "config.yaml").write_text(
        f"webui_prefill_messages_script: ['/bin/sh', '{guard}', '-c', 'echo should-not-run']\n",
        encoding="utf-8",
    )
    launched = []

    def fake_run(command, **kwargs):
        launched.append(list(command))
        raise OSError("blocked by the test")

    monkeypatch.setattr(streaming.subprocess, "run", fake_run)
    head_cfg = _gateway_config_for_profile("beta")
    master_cfg = _master_read(home)
    streaming._load_prefill_messages_script(head_cfg)
    streaming._load_prefill_messages_script(master_cfg)

    expected_guard = "-n" if guard == "${PREFILL_GUARD}" else guard
    assert launched[0] == launched[1] == ["/bin/sh", expected_guard, "-c", "echo should-not-run"]
    assert head_cfg == master_cfg


@pytest.mark.parametrize("mode", ["${IMAGE_MODE}", "${UNDEFINED_8744}"])
def test_codex_3_image_input_mode_is_masters(tree, mode):
    """The previous fold dropped ``image_input_mode`` and defaulted to native upload."""
    from api.gateway_chat import _gateway_config_for_profile
    from api.streaming import _explicit_text_signal

    home = tree["beta"]
    (home / "config.yaml").write_text(f"agent:\n  image_input_mode: '{mode}'\n", encoding="utf-8")
    head_cfg = _gateway_config_for_profile("beta")
    master_cfg = _master_read(home)

    assert head_cfg == master_cfg
    assert head_cfg["agent"]["image_input_mode"] == ("text" if mode == "${IMAGE_MODE}" else mode)
    assert _explicit_text_signal(head_cfg) is _explicit_text_signal(master_cfg) is (mode == "${IMAGE_MODE}")


# ── Thread env: merged over, restored, nested, on error, per thread ───────────


@pytest.mark.parametrize("scenario", ["absent", "thread-env", "thread-env-blocked", "non-dict"])
def test_the_callers_thread_env_and_block_flag_are_preserved(tree, scenario):
    from api.gateway_chat import _gateway_config_for_profile

    (tree["alpha"] / "config.yaml").write_text(
        ALPHA_CONFIG + "thread: ${THREAD_ONLY}\nproc: ${PROC_ONLY}\n", encoding="utf-8"
    )
    thread_env = {"THREAD_ONLY": "thread-only", "SHARED_MODEL": "thread-model"}
    settings = {
        "absent": dict(),
        "thread-env": dict(env=thread_env, block=False),
        "thread-env-blocked": dict(env=thread_env, block=True),
        "non-dict": dict(env=None, block=False),
    }[scenario]
    snapshot = dict(thread_env)
    with _ThreadState(**settings) as state:
        before = state.current()
        cfg = _gateway_config_for_profile("alpha")
        after = state.current()
    assert after[0] is before[0] and after[1] is before[1]
    assert thread_env == snapshot  # never mutated in place
    assert cfg["model"]["default"] == "model-alpha"  # the .env wins over the thread env too
    if scenario.startswith("thread-env"):
        assert cfg["thread"] == "thread-only"  # merged over, not replaced
    else:
        assert cfg["thread"] == "${THREAD_ONLY}"
    assert cfg["proc"] == ("${PROC_ONLY}" if scenario == "thread-env-blocked" else "proc-value")


def test_overlay_nests_and_restores_on_exit_and_on_error(tree):
    from api.config import _expand_env_vars
    from api.gateway_chat import _profile_dotenv_overlay

    probe = "${ALPHA_ONLY}|${SHARED_MODEL}|${BETA_ONLY}|${PROC_ONLY}|${UNDEFINED_8744}"
    with _ThreadState(env={"PROC_ONLY": "thread-value"}, block=False) as state:
        outer = state.current()
        assert _expand_env_vars(probe) == "${ALPHA_ONLY}|process-leak|${BETA_ONLY}|thread-value|${UNDEFINED_8744}"
        with _profile_dotenv_overlay(tree["alpha"]):
            assert _expand_env_vars(probe) == "alpha-secret|model-alpha|${BETA_ONLY}|thread-value|${UNDEFINED_8744}"
            with _profile_dotenv_overlay(tree["beta"]):
                assert _expand_env_vars(probe) == "alpha-secret|model-beta|beta-secret|thread-value|${UNDEFINED_8744}"
            assert _expand_env_vars(probe) == "alpha-secret|model-alpha|${BETA_ONLY}|thread-value|${UNDEFINED_8744}"
        assert state.current()[0] is outer[0] and state.current()[1] is outer[1]
        with pytest.raises(RuntimeError, match="inside"):
            with _profile_dotenv_overlay(tree["alpha"]):
                with _profile_dotenv_overlay(tree["beta"]):
                    raise RuntimeError("inside")
        assert state.current()[0] is outer[0] and state.current()[1] is outer[1]
        assert outer[0] == {"PROC_ONLY": "thread-value"}
    with _ThreadState() as state:
        with _profile_dotenv_overlay(tree["alpha"]):
            assert state.ctx.env["ALPHA_ONLY"] == "alpha-secret"
        assert state.current() == (_MISSING, _MISSING)


def test_an_error_inside_the_read_restores_the_thread_env(tree, monkeypatch):
    import api.config as config
    from api.gateway_chat import _gateway_config_for_profile

    def boom(_cfg):
        raise RuntimeError("inside the read")

    monkeypatch.setattr(config, "_apply_config_defaults", boom)
    thread_env = {"THREAD_ONLY": "thread-only"}
    with _ThreadState(env=thread_env, block=True) as state:
        with pytest.raises(RuntimeError, match="inside the read"):
            _gateway_config_for_profile("alpha")
        assert state.current()[0] is thread_env and state.current()[1] is True
    assert thread_env == {"THREAD_ONLY": "thread-only"}


def test_two_profiles_on_two_threads_concurrently_see_only_their_own_dotenv(tree, monkeypatch):
    """Both reads sit inside their overlay at the same time (lock-step on every lookup),
    with a third thread on master's plain reader: no value crosses threads."""
    import api.config as config
    from api.gateway_chat import _gateway_config_for_profile

    shape = "a: ${SHARED_MODEL}\nb: ${ALPHA_ONLY}\nc: ${BETA_ONLY}\nd: ${PROC_ONLY}\ne: ${UNDEFINED_8744}\n"
    for name in ("alpha", "beta"):
        (tree[name] / "config.yaml").write_text(shape, encoding="utf-8")
    names = {"SHARED_MODEL", "ALPHA_ONLY", "BETA_ONLY", "PROC_ONLY", "UNDEFINED_8744"}
    barrier = threading.Barrier(3, timeout=20)
    real = config._thread_local_env_value
    gated_calls = []

    def lockstep(name, default=""):
        if str(name).strip() in names:
            gated_calls.append(threading.get_ident())
            barrier.wait()
        return real(name, default)

    monkeypatch.setattr(config, "_thread_local_env_value", lockstep)
    results = {}

    def run(label, read):
        try:
            results[label] = read()
        except BaseException as exc:  # surfaced below
            results[label] = exc
            barrier.abort()

    threads = [
        threading.Thread(target=run, args=("alpha", lambda: _gateway_config_for_profile("alpha"))),
        threading.Thread(target=run, args=("beta", lambda: _gateway_config_for_profile("beta"))),
        threading.Thread(target=run, args=("plain", lambda: config.get_config_for_profile_home(tree["beta"]))),
    ]
    main_before = _ThreadState().current()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(t.is_alive() for t in threads)
    assert len(gated_calls) == 15, results

    def picked(cfg):
        assert isinstance(cfg, dict), cfg
        return [cfg[k] for k in "abcde"]

    assert picked(results["alpha"]) == ["model-alpha", "alpha-secret", "${BETA_ONLY}", "proc-value", "${UNDEFINED_8744}"]
    assert picked(results["beta"]) == ["model-beta", "${ALPHA_ONLY}", "beta-secret", "proc-value", "${UNDEFINED_8744}"]
    assert picked(results["plain"]) == [PROCESS_LEAK, "${ALPHA_ONLY}", "${BETA_ONLY}", "proc-value", "${UNDEFINED_8744}"]
    assert _ThreadState().current() == main_before


# ── master's behaviour, pinned ────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["default", "", None, "   "])
def test_root_or_no_profile_returns_none_as_on_master(tree, name):
    from api.gateway_chat import _gateway_config_for_profile

    assert _gateway_config_for_profile(name) is None


def test_ambient_branch_returns_masters_cached_get_config(tree, monkeypatch, config_cache_snapshot):
    """A process-active named profile reads the cached ``get_config()`` (master): its .env is
    already in os.environ there, loaded by ``_reload_dotenv`` on the switch."""
    import api.config as config
    import api.profiles as profiles
    from api.gateway_chat import _gateway_config_for_profile

    for line in ALPHA_DOTENV.splitlines():
        key = line.partition("=")[0]
        if key in ALPHA_NAMES:
            monkeypatch.setenv(key, "placeholder")  # recorded so teardown removes the switch's values
    monkeypatch.setattr(profiles, "_active_profile", "alpha")
    profiles._reload_dotenv(tree["alpha"])  # what switch_profile(process_wide=True) does

    cfg = _gateway_config_for_profile("alpha")

    assert cfg is config.get_config()
    assert cfg["webui_gateway_use_runs_api"] == "true"
    assert cfg["model"]["default"] == "model-alpha"


def test_control_general_reader_keeps_masters_process_env_binding(tree):
    from api.config import get_config_for_profile_home

    alpha = get_config_for_profile_home(tree["alpha"])
    assert alpha["model"]["default"] == PROCESS_LEAK
    assert alpha["webui_gateway_use_runs_api"] == "${ALPHA_GW_RUNS}"


def test_control_ambient_cache_never_takes_an_overlay(tree, config_cache_snapshot):
    """master's _refresh_config_cache pins the process env (#798), even inside an overlay."""
    import api.config as config
    from api.gateway_chat import _profile_dotenv_overlay

    with _profile_dotenv_overlay(tree["alpha"]):
        with config._cfg_lock:
            config._refresh_config_cache(tree["alpha"] / "config.yaml")
        assert config._thread_ctx.env["ALPHA_GW_RUNS"] == "true"  # the overlay survived the pin
    assert config._cfg_cache["webui_gateway_use_runs_api"] == "${ALPHA_GW_RUNS}"
    assert config._cfg_cache["model"]["default"] == PROCESS_LEAK


def test_control_delegation_wakeup_keeps_the_general_reader(tree, monkeypatch):
    """Only the Gateway session config takes the overlay."""
    import api.config as config
    import api.gateway_delegation_wakeup as wakeup

    seen = []
    real = config.get_config_for_profile_home

    def spy(home, **kwargs):
        seen.append((home, dict(getattr(config._thread_ctx, "env", None) or {})))
        return real(home, **kwargs)

    monkeypatch.setattr(config, "get_config_for_profile_home", spy)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    wakeup._profile_uses_gateway("beta")
    assert seen == [(tree["beta"], {})]
