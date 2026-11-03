"""Readers of a profile's RAW config.yaml resolve ``${VAR}`` under THAT profile's
env binding (#8032 / #8129 round 5).

#8129 keeps ``${VAR}`` references on disk, which exposed readers that only
worked because Settings saves used to write expanded values. Each reader now
goes through ``api.profiles.load_profile_config`` / ``expand_profile_config``:
a named profile resolves against its own ``.env``; the root profile against an
explicit binding (launch-time env snapshot + root's ``.env``) instead of the
live process env, which a named-profile worker mirrors its own values into.

The last test is a static guard: a new raw config load in ``api/`` must be an
allowlisted writer / loader internal, or go through the helper.
"""

import ast
import os
import sys
import threading
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import yaml

API_DIR = Path(__file__).resolve().parents[1] / "api"
MODEL_REFS = "model:\n  default: ${GATE_MODEL}\n  provider: ${GATE_PROVIDER}\n"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A root home with one named profile ``writer``, and a launch env with no GATE_* keys."""
    import api.paths as paths
    import api.profiles as profiles

    root = tmp_path / ".hermes"
    writer = root / "profiles" / "writer"
    writer.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "default", "is_default": True}, {"name": "writer"}],
    )
    for key in [k for k in os.environ if k.startswith("GATE_")]:
        monkeypatch.delenv(key)
    launch = {k: v for k, v in os.environ.items() if not k.startswith("GATE_")}
    # raising=False: STARTUP_ENV is new with the fix, so before it the tests
    # still run (and fail on their behavioural assertions).
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType(launch), raising=False)
    profiles._invalidate_root_profile_cache()
    profiles._tls.profile = None
    yield SimpleNamespace(root=root, writer=writer, profiles=profiles, paths=paths)
    profiles._tls.profile = None
    profiles._invalidate_root_profile_cache()


def _launch_env(tree, monkeypatch, **values):
    monkeypatch.setattr(
        tree.paths, "STARTUP_ENV", MappingProxyType({**tree.paths.STARTUP_ENV, **values}), raising=False
    )


def _hold_named_worker(profiles, name):
    """Run a real named-profile background worker scope on another thread and
    hold it open: while it runs, its profile env is mirrored into os.environ."""
    entered, release, errors = threading.Event(), threading.Event(), []

    def body():
        try:
            with profiles.profile_env_for_background_worker(name, scope_skill_modules=False):
                entered.set()
                release.wait(10)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            entered.set()

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    assert entered.wait(10) and not errors, errors
    return release, worker


# ── r5 #1 [CORE] restore + streaming repair forwarded ${GATE_MODEL}/${GATE_PROVIDER} ──


def test_r5_1_restore_resolves_profile_model_refs(tree, monkeypatch):
    from api import routes

    (tree.writer / "config.yaml").write_text(MODEL_REFS, encoding="utf-8")
    (tree.writer / ".env").write_text("GATE_MODEL=gpt-4o\nGATE_PROVIDER=openai\n", encoding="utf-8")
    monkeypatch.setattr(routes, "get_available_models", lambda *a, **k: {"default_model": "", "active_provider": ""})
    session = SimpleNamespace(profile="writer", model="gpt-4o", model_provider=None)

    provider, default, _cfg = routes._read_profile_model_config(session, None)
    assert (provider, default) == ("openai", "gpt-4o")
    assert routes._resolve_effective_session_model_provider_for_display(session) == "openai"


def test_r5_1_streaming_repair_resolves_profile_model_refs(tree):
    from api import streaming

    (tree.writer / "config.yaml").write_text(MODEL_REFS, encoding="utf-8")
    (tree.writer / ".env").write_text("GATE_MODEL=gpt-4o\nGATE_PROVIDER=openai\n", encoding="utf-8")
    assert streaming._apply_profile_home_context_to_streaming_model(
        "gpt-4o", None, str(tree.writer), True
    ) == ("gpt-4o", "openai", False)
    # The repair branch: a stale cross-family model is replaced by the
    # RESOLVED profile default, never by the ``${GATE_MODEL}`` literal.
    assert streaming._apply_profile_home_context_to_streaming_model(
        "claude-3-haiku", None, str(tree.writer), True
    ) == ("gpt-4o", "openai", True)


def test_r5_1_unresolvable_profile_model_ref_is_absent_not_forwarded(tree, monkeypatch):
    """A reference only the server process env defines does not resolve for a
    named profile, and is never forwarded as a provider or a repair target."""
    from api import routes, streaming

    (tree.writer / "config.yaml").write_text(MODEL_REFS, encoding="utf-8")
    (tree.writer / ".env").write_text("UNRELATED=1\n", encoding="utf-8")
    monkeypatch.setenv("GATE_MODEL", "process-only-model")
    monkeypatch.setenv("GATE_PROVIDER", "process-only-provider")
    session = SimpleNamespace(profile="writer", model="gpt-4o", model_provider=None)

    provider, default, _cfg = routes._read_profile_model_config(session, None)
    assert (provider, default) == (None, None)
    assert streaming._apply_profile_home_context_to_streaming_model(
        "gpt-4o", None, str(tree.writer), True
    ) == ("gpt-4o", None, False)


# ── r5 #2 [CORE] switching to DEFAULT during a named worker returned the worker's values ──


def test_r5_2_switch_to_default_during_named_worker_uses_root_env(tree):
    profiles = tree.profiles
    (tree.root / "config.yaml").write_text(MODEL_REFS, encoding="utf-8")
    (tree.root / ".env").write_text("GATE_MODEL=root-model\nGATE_PROVIDER=openai\n", encoding="utf-8")
    (tree.writer / "config.yaml").write_text(MODEL_REFS, encoding="utf-8")
    (tree.writer / ".env").write_text(
        "GATE_MODEL=claude-sonnet-4.6\nGATE_PROVIDER=anthropic\n", encoding="utf-8"
    )

    release, worker = _hold_named_worker(profiles, "writer")
    try:
        assert os.environ.get("GATE_MODEL") == "claude-sonnet-4.6"  # the worker's mirror is live
        during = profiles.switch_profile("default", process_wide=False)
    finally:
        release.set()
        worker.join(10)
        profiles._tls.profile = None
    assert (during["default_model"], during["default_model_provider"]) == ("root-model", "openai")


# ── r5 #3 [CORE] env-backed terminal settings reached the Agent unresolved ──


def test_r5_3_terminal_settings_resolve_before_projection(tree, monkeypatch):
    profiles = tree.profiles
    (tree.writer / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  ssh_host: ${GATE_SSH_HOST}\n"
        "  ssh_user: ${GATE_MISSING}\n  cwd: /srv/work\n",
        encoding="utf-8",
    )
    (tree.writer / ".env").write_text("GATE_SSH_HOST=host.example.test\n", encoding="utf-8")
    env = profiles.get_profile_runtime_env(tree.writer)
    assert env["TERMINAL_SSH_HOST"] == "host.example.test"
    assert env["TERMINAL_ENV"] == "ssh" and env["TERMINAL_CWD"] == "/srv/work"
    # Unresolvable: left unset, never projected as a literal placeholder.
    assert "TERMINAL_SSH_USER" not in env

    # Root resolves from its launch env, not a value a worker mirrored live.
    (tree.root / "config.yaml").write_text("terminal:\n  docker_image: ${GATE_IMAGE}\n", encoding="utf-8")
    _launch_env(tree, monkeypatch, GATE_IMAGE="img:launch")
    monkeypatch.setenv("GATE_IMAGE", "img:mirrored-by-a-worker")
    assert profiles.get_profile_runtime_env(tree.root)["TERMINAL_DOCKER_IMAGE"] == "img:launch"


def test_terminal_projection_keeps_dotenv_precedence(tree):
    """Unchanged contract: a profile .env TERMINAL_* still overrides config."""
    (tree.writer / "config.yaml").write_text("terminal:\n  ssh_host: ${GATE_SSH_HOST}\n", encoding="utf-8")
    (tree.writer / ".env").write_text(
        "GATE_SSH_HOST=host.example.test\nTERMINAL_SSH_HOST=override.example.test\n", encoding="utf-8"
    )
    assert tree.profiles.get_profile_runtime_env(tree.writer)["TERMINAL_SSH_HOST"] == "override.example.test"


# ── r5 #4 [SILENT] root provider-key removal during a named worker kept the key ──


def test_r5_4_root_provider_key_removal_during_named_worker(tree, monkeypatch):
    import api.config as config
    import api.providers as providers

    root_cfg = tree.root / "config.yaml"
    root_cfg.write_text(
        "model:\n  default: gpt-4o\n  provider: ${GATE_PROVIDER}\n  api_key: ${GATE_KEY}\n",
        encoding="utf-8",
    )
    (tree.root / ".env").write_text("GATE_PROVIDER=openai\n", encoding="utf-8")
    (tree.writer / ".env").write_text("GATE_PROVIDER=anthropic\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: root_cfg)
    monkeypatch.setattr(providers, "reload_config", lambda: None)
    monkeypatch.setattr(providers, "invalidate_providers_cache", lambda: None)

    release, worker = _hold_named_worker(tree.profiles, "writer")
    try:
        assert os.environ.get("GATE_PROVIDER") == "anthropic"
        providers._clean_provider_key_from_config("openai")
    finally:
        release.set()
        worker.join(10)
    model = yaml.safe_load(root_cfg.read_text(encoding="utf-8"))["model"]
    assert "api_key" not in model
    assert model["provider"] == "${GATE_PROVIDER}"


# ── r5 #5 [SILENT] profile skill counts treated an env-disabled skill as enabled ──


def _hermetic_skill_utils(monkeypatch):
    """A self-contained ``agent.skill_utils`` with the real helpers' semantics.

    ``_compute_profile_skills_stats`` reports ``(0, 0)`` whenever the agent
    package is absent (#7305), and other test files leave stubs of this module
    behind, so the count assertions below must not depend on either: install
    the four helpers the function uses, with ``parse_config_string_list``
    matching hermes-agent's (a bare string is one name; a ``[...]`` string is a
    serialized list).
    """
    import ast
    import types

    su = types.ModuleType("agent.skill_utils")
    su.iter_skill_index_files = lambda skills_dir, filename: sorted(Path(skills_dir).rglob(filename))

    def parse_frontmatter(content):
        if not content.startswith("---"):
            return {}, content
        _, block, _, rest = content.split("---", 3) if content.count("---") >= 3 else ("", "", "", content)
        fm = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        return fm, rest

    def parse_config_string_list(value):
        if isinstance(value, str):
            if value.strip().startswith("["):
                try:
                    parsed = ast.literal_eval(value.strip())
                except (ValueError, SyntaxError):
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            return [value]
        return [str(item) for item in value] if isinstance(value, (list, tuple, set, frozenset)) else []

    su.parse_frontmatter = parse_frontmatter
    su.skill_matches_platform = lambda frontmatter: True
    su.parse_config_string_list = parse_config_string_list
    agent_pkg = types.ModuleType("agent")
    agent_pkg.skill_utils = su
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_utils", su)
    return su


def test_r5_5_profile_skill_counts_expand_env_backed_disabled_list(tree, monkeypatch):
    _hermetic_skill_utils(monkeypatch)
    skill_dir = tree.writer / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (tree.writer / "config.yaml").write_text("skills:\n  disabled: ${GATE_DISABLED}\n", encoding="utf-8")

    (tree.writer / ".env").write_text("GATE_DISABLED=demo\n", encoding="utf-8")
    assert tree.profiles._compute_profile_skills_stats(tree.writer) == (0, 1)
    # A serialized list behind the reference decodes like the Skills reader's.
    (tree.writer / ".env").write_text('GATE_DISABLED=["demo", "other"]\n', encoding="utf-8")
    assert tree.profiles._compute_profile_skills_stats(tree.writer) == (0, 1)


# ── The binding itself ──


def test_root_binding_keeps_launcher_values_the_loader_mirrored_under_root_dotenv(tree, monkeypatch):
    profiles = tree.profiles
    _launch_env(tree, monkeypatch, GATE_LAUNCH="launch", GATE_LOADED="named-profile", GATE_BOTH="launch")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"GATE_LOADED"})
    monkeypatch.setenv("GATE_LIVE_ONLY", "mirrored")
    (tree.root / ".env").write_text("GATE_BOTH=root-dotenv\n", encoding="utf-8")

    root = profiles.profile_env_binding(tree.root)
    assert root["GATE_LAUNCH"] == "launch"
    assert root["GATE_BOTH"] == "root-dotenv"
    assert root["GATE_LOADED"] == "named-profile"  # launcher value predates the loader
    assert "GATE_LIVE_ONLY" not in root
    assert root["HERMES_HOME"] == str(tree.root)

    (tree.writer / ".env").write_text("GATE_OWN=1\n", encoding="utf-8")
    named = profiles.profile_env_binding(tree.writer)
    assert named == {"GATE_OWN": "1", "HERMES_HOME": str(tree.writer)}


def _launched_in(tree, monkeypatch, home, dotenv):
    """Simulate ctl.sh: it sourced *home*'s .env (recorded at startup as *dotenv*) before Python started."""
    monkeypatch.setattr(tree.profiles, "_INITIAL_HERMES_HOME", str(home))
    monkeypatch.setattr(tree.profiles, "_INITIAL_HERMES_HOME_DOTENV", dict(dotenv))


def test_root_binding_drops_values_a_named_launch_home_sourced(tree, monkeypatch):
    """#8129 r7 [CORE]: launched in a named profile, root must not resolve that profile's secrets.

    The loaded-key set is replaced by every later .env reload (a switch, a self-hosted setup in
    another profile), so it cannot identify the launch profile's values; the startup provenance
    record can.
    """
    profiles = tree.profiles
    _launched_in(tree, monkeypatch, tree.writer, {"GATE_SECRET": "named-secret", "GATE_SHARED": "profile-value"})
    _launch_env(
        tree, monkeypatch, GATE_SECRET="named-secret", GATE_SHARED="launcher-export", GATE_EMPTY="", GATE_LAUNCH="launch"
    )
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"SOMETHING_ELSE"})

    root = profiles.profile_env_binding(tree.root)
    assert "GATE_SECRET" not in root, "the named launch profile's secret reached root's binding"
    assert root["GATE_SHARED"] == "launcher-export", "an independent launcher export was dropped"
    assert root["GATE_EMPTY"] == "", "an empty launcher value is not a profile's value"
    assert root["GATE_LAUNCH"] == "launch"
    assert profiles.expand_profile_config(tree.root, {"default": "${GATE_SECRET}"}) == {"default": "${GATE_SECRET}"}

    (tree.root / ".env").write_text("GATE_SECRET=root-own\n", encoding="utf-8")
    assert profiles.profile_env_binding(tree.root)["GATE_SECRET"] == "root-own", "root's own .env still applies"


def test_root_binding_keeps_values_when_the_launch_home_is_root(tree, monkeypatch):
    """Root's .env is shared by definition: launching in root sources nothing profile-private."""
    _launched_in(tree, monkeypatch, tree.root, {"GATE_SECRET": "root-launch"})
    _launch_env(tree, monkeypatch, GATE_SECRET="root-launch")
    assert tree.profiles.profile_env_binding(tree.root)["GATE_SECRET"] == "root-launch"


def test_expand_profile_config_serves_a_recursive_alias_document_unexpanded(tree):
    """A self-referencing YAML alias must not hide the profile's ordinary settings."""
    from api.config import _thread_ctx

    (tree.writer / ".env").write_text("GATE_X=named\n", encoding="utf-8")
    (tree.writer / "config.yaml").write_text(
        "terminal:\n  backend: local\nmodel:\n  default: ${GATE_X}\nloop: &loop\n  - *loop\n", encoding="utf-8"
    )
    previous = getattr(_thread_ctx, "env", None)
    loaded = tree.profiles.load_profile_config(tree.writer)
    assert loaded["terminal"] == {"backend": "local"}
    assert loaded["model"]["default"] == "${GATE_X}", "unexpanded: the reference reads as absent, never as another value"
    assert getattr(_thread_ctx, "env", None) == previous, "the caller's thread env was not restored"


def test_expand_profile_config_restores_the_callers_thread_env(tree):
    from api.config import _thread_ctx

    (tree.writer / ".env").write_text("GATE_X=named\n", encoding="utf-8")
    _thread_ctx.env = {"GATE_X": "caller"}
    _thread_ctx.block_process_env_fallback = False
    try:
        assert tree.profiles.expand_profile_config(tree.writer, {"v": "${GATE_X}"}) == {"v": "named"}
        assert _thread_ctx.env == {"GATE_X": "caller"}
        assert _thread_ctx.block_process_env_fallback is False
    finally:
        _thread_ctx.env = {}
        _thread_ctx.block_process_env_fallback = False


# ── Static guard: every raw config load in api/ is classified ──

# Calls that return a RAW (un-expanded) YAML document.
_RAW_LOADERS = {
    "safe_load", "full_load", "unsafe_load",
    "_load_yaml_config_file_raw", "_read_profile_config_cached",
    "_load_mcp_config_for_write", "_load_yaml_config",
}

_WRITER = "writer: read-modify-write that must save the RAW document"
_LOADER = "loader internal"
_ALLOWED = {
    ("commands.py", "_run_codex_runtime_command._persist_config"): _WRITER,
    ("config.py", "set_max_tokens"): _WRITER,
    ("config.py", "set_reasoning_display"): _WRITER,
    # #8253 moved the file write out of set_reasoning_effort into write_reasoning_effort.
    ("config.py", "write_reasoning_effort"): _WRITER,
    ("config.py", "set_hermes_default_model"): _WRITER,
    ("config.py", "set_auxiliary_model"): _WRITER,
    ("dashboard_probe.py", "save_dashboard_config"): _WRITER,
    ("kanban_bridge.py", "_update_config_payload"): _WRITER,
    ("onboarding.py", "_load_yaml_config"): _WRITER + " (onboarding's loader)",
    ("onboarding.py", "apply_onboarding_setup"): _WRITER + " (key check expands a copy)",
    ("onboarding.py", "apply_self_hosted_provider_setup"): _WRITER,
    ("profiles.py", "_write_endpoint_to_config"): _WRITER,
    ("profiles.py", "_write_model_defaults_to_config"): _WRITER,
    ("providers.py", "_clean_provider_key_from_config"): _WRITER + " (identity via the helper)",
    ("routes.py", "_handle_skill_toggle"): _WRITER,
    ("routes.py", "_load_mcp_config_for_write"): _WRITER + " (MCP editor's loader)",
    ("routes.py", "_handle_mcp_server_delete"): _WRITER,
    ("routes.py", "_handle_mcp_server_toggle"): _WRITER,
    ("routes.py", "_handle_mcp_server_update"): _WRITER,
    ("config.py", "_load_yaml_config_file_raw"): _LOADER,
    ("config.py", "_load_yaml_config_file"): _LOADER + " (expands under the caller's scope)",
    ("config.py", "_refresh_config_cache"): _LOADER + " (expands under the pinned process env)",
    ("routes.py", "_read_profile_config_cached"): _LOADER + " (raw parse cache)",
    ("yaml_compat.py", "safe_load"): _LOADER,
    ("profiles.py", "load_profile_config"): "THE helper",
    ("routes.py", "_read_profile_model_config"): "value reader: expands via expand_profile_config",
    ("profiles.py", "_profile_secret_env_names"): "reads ${NAME} reference NAMES; expanding destroys them",
    ("config.py", "_declares_model_provider_kind"): "plugin.yaml, not config.yaml",
    ("profiles.py", "_profile_visible_from_meta"): "profile.yaml, not config.yaml",
    ("gateway_chat.py", "_config_url_may_belong_to_a_profile"): "reads ${VAR} reference NAMES of an operator config file (#8435)",
    ("gateway_chat.py", "_gateway_endpoint_for_profile"): "value reader: expands under its own explicit profile env (#8435)",
}


def _collect_raw_loads(node, module, stack, sites):
    scoped = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    if scoped:
        stack.append(node.name)
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _RAW_LOADERS:
            sites.setdefault((module, ".".join(stack) or "<module>"), []).append(node.lineno)
    for child in ast.iter_child_nodes(node):
        _collect_raw_loads(child, module, stack, sites)
    if scoped:
        stack.pop()


def _raw_load_sites():
    sites = {}
    for path in sorted(API_DIR.glob("*.py")):
        _collect_raw_loads(ast.parse(path.read_text(encoding="utf-8")), path.name, [], sites)
    return sites


def test_every_raw_config_load_in_api_is_a_writer_loader_or_the_helper():
    sites = _raw_load_sites()
    unclassified = {key: lines for key, lines in sites.items() if key not in _ALLOWED}
    assert not unclassified, (
        "New raw YAML load(s) in api/ — a config VALUE reader must use "
        "api.profiles.load_profile_config()/expand_profile_config() (the profile's "
        "${VAR} binding); a writer must be added to _ALLOWED with its reason: "
        f"{unclassified}"
    )
    stale = sorted(key for key in _ALLOWED if key not in sites)
    assert not stale, f"_ALLOWED lists raw loads that no longer exist: {stale}"


def test_routed_value_readers_go_through_the_helper():
    """The readers fixed in round 5 must keep calling the helper."""
    expected = {
        ("profiles.py", "get_profile_runtime_env"): "load_profile_config",
        ("profiles.py", "switch_profile"): "load_profile_config",
        ("profiles.py", "_compute_profile_skills_stats"): "load_profile_config",
        ("streaming.py", "_apply_profile_home_context_to_streaming_model"): "load_profile_config",
        ("routes.py", "_read_profile_model_config"): "expand_profile_config",
        ("providers.py", "_clean_provider_key_from_config"): "expand_profile_config",
        ("onboarding.py", "apply_onboarding_setup"): "expand_profile_config",
    }
    for (module, function), helper in expected.items():
        tree = ast.parse((API_DIR / module).read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
        )
        calls = {
            (c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None))
            for c in ast.walk(fn) if isinstance(c, ast.Call)
        }
        assert helper in calls, f"{module}:{function} no longer reads through {helper}()"
