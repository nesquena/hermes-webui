"""A NAMED launch profile's ``.env`` values stay out of root's scope however the launcher read the file.

With the server launched under a named profile (``HERMES_HOME`` is that profile's directory) and root active,
``api.profiles.operator_env()`` tells the operator's exports apart from the values the launcher sourced from the
launch home's ``.env`` by value. ctl.sh decodes a double-quoted ``\\t``/``\\n``/``\\r`` and drops an inline comment;
a shell that sources the file (``set -a; . file``) drops the comment and keeps the escape literal. A value from
another profile's ``.env`` read either way must not reach root's ``${VAR}`` binding, the runtime env a root
background worker applies, or root's Agent credential scope, while an operator export with a different value does.

Each scenario runs the real launcher and the real startup in a fresh interpreter with an explicit minimal env, a
synthetic home and ``DUMMY_*`` values only (harness of ``tests/test_profile_secret_scope_isolation.py``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_profile_secret_scope_isolation import AGENT_DIR, _write, needs_agent_scopes

REPO = Path(__file__).resolve().parents[1]

CHILD = r'''
import json, os, sys
from pathlib import Path

repo = sys.argv[1]
sys.path.insert(0, repo)
if os.environ.get("HERMES_WEBUI_AGENT_DIR"):
    sys.path.insert(1, os.environ["HERMES_WEBUI_AGENT_DIR"])
os.chdir(repo)

KEYS = ("ALPHA_OWN_KEY", "OPENROUTER_API_KEY", "LITELLM_API_KEY", "ANTHROPIC_API_KEY", "OPERATOR_ONLY_TOKEN")
OWNERS = {"DUMMY_KEY_PROFILE_A": "alpha", "DUMMY_KEY\tPROFILE_A": "alpha-decoded",
          "DUMMY_KEY\\tPROFILE_A": "alpha-literal", "DUMMY_OPERATOR_OVERRIDE": "operator-override"}


def owners(mapping):
    return {k: OWNERS.get(str(mapping[k]), "other") for k in KEYS if mapping.get(k) is not None}


obs = {}
try:
    import api.config  # noqa: F401  (real startup: init_profile_state)
    import cron.scheduler as CS
    from agent.secret_scope import current_secret_scope
    from api import profiles as P
    from tui_gateway import launch_profile_policy as LPP

    if not hasattr(CS, "_install_fire_secret_scope"):
        print("RESULT " + json.dumps({"skip": "Agent cron fire scope not available"}), flush=True)
        os._exit(0)
    root = P._DEFAULT_HERMES_HOME
    obs["active_root"] = P._is_root_profile_home(P.get_process_profile_home())
    obs["launch_home"] = Path(P._INITIAL_HERMES_HOME).name
    obs["live"] = owners(os.environ)
    obs["operator_env"] = owners(P.operator_env())
    obs["binding"] = owners(P.profile_env_binding(root))
    raw = {k: "${%s}" % k for k in KEYS}
    expanded = P.expand_profile_config(root, raw)
    obs["root_config"] = owners({k: v for k, v in expanded.items() if "${" not in str(v)})  # unresolved: absent
    runtime = P.get_profile_runtime_env(root)  # root config.yaml maps each key into a terminal setting
    obs["worker_runtime_env"] = sorted(OWNERS.get(v, "other") for k, v in runtime.items() if k.startswith("TERMINAL_"))
    obs["launch_scope"] = owners(LPP.launch_secret_scope(root))
    with P.cron_profile_context_for_home(root):
        tokens = CS._install_fire_secret_scope()
    try:
        obs["fire_scope"] = owners(current_secret_scope() or {})
    finally:
        CS._reset_fire_secret_scope(tokens)
    # alpha's .env loses those keys after startup (a Settings edit): the exported values are still alpha's.
    env_file = Path(P._INITIAL_HERMES_HOME) / ".env"
    kept = [line for line in env_file.read_text(encoding="utf-8").splitlines()
            if line.split("=", 1)[0] not in ("OPENROUTER_API_KEY", "LITELLM_API_KEY", "ANTHROPIC_API_KEY")]
    env_file.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    obs["binding_after_edit"] = owners(P.profile_env_binding(root))
except Exception as exc:  # surfaced by the parent assertion
    import traceback
    obs["error"] = repr(exc) + traceback.format_exc()[-1200:]
print("RESULT " + json.dumps(obs, sort_keys=True), flush=True)
os._exit(0)
'''

# alpha's .env. OPERATOR_ONLY_TOKEN is re-exported by the operator with a different value after the file is sourced.
ALPHA_ENV = (
    "ALPHA_OWN_KEY=DUMMY_KEY_PROFILE_A\n"
    'OPENROUTER_API_KEY="DUMMY_KEY_PROFILE_A" # provider key\n'
    'LITELLM_API_KEY="DUMMY_KEY\\tPROFILE_A"\n'
    'ANTHROPIC_API_KEY="DUMMY_KEY\\tPROFILE_A" # provider key\n'
    'OPERATOR_ONLY_TOKEN="DUMMY_KEY_PROFILE_A" # shared token\n'
)
ROOT_CONFIG = (
    "model:\n  default: dummy-model\n"
    "terminal:\n"
    "  docker_image: ${OPENROUTER_API_KEY}\n"
    "  modal_image: ${LITELLM_API_KEY}\n"
    "  singularity_image: ${ANTHROPIC_API_KEY}\n"
    "  docker_env: ${ALPHA_OWN_KEY}\n"
    "  cwd: ${OPERATOR_ONLY_TOKEN}\n"
)
# What each launcher exports for alpha's .env (checked against the child's live env).
EXPECTED_LIVE = {
    "ctl_sh": {"ALPHA_OWN_KEY": "alpha", "OPENROUTER_API_KEY": "alpha", "LITELLM_API_KEY": "alpha-decoded",
               "ANTHROPIC_API_KEY": "alpha-decoded", "OPERATOR_ONLY_TOKEN": "operator-override"},
    "shell_sourced": {"ALPHA_OWN_KEY": "alpha", "OPENROUTER_API_KEY": "alpha", "LITELLM_API_KEY": "alpha-literal",
                      "ANTHROPIC_API_KEY": "alpha-literal", "OPERATOR_ONLY_TOKEN": "operator-override"},
}


def _launcher_script(launcher: str) -> str:
    if launcher == "ctl_sh":
        body = re.search(r"\n(_apply_env_file_safely\(\) \{.*?\n\})\n", (REPO / "ctl.sh").read_text(encoding="utf-8"),
                         re.S)
        assert body, "ctl.sh no longer defines _apply_env_file_safely"
        source = body.group(1) + '\n_apply_env_file_safely "$1"\n'
    else:
        source = 'set -a; . "$1"; set +a\n'
    return source + 'export OPERATOR_ONLY_TOKEN=DUMMY_OPERATOR_OVERRIDE\nshift\nexec "$@"\n'


def _run_named_launch_root_active(tmp_path, launcher, child=CHILD, *, alpha_env=ALPHA_ENV, active="root", others=None):
    """Server launched under ``alpha`` by *launcher*, which sources alpha's ``.env`` first; *active* is the saved
    active profile (root by default); *others* maps further profile names to their ``.env`` text."""
    run = tmp_path / "run"
    root = run / "home" / ".hermes"
    alpha = root / "profiles" / "alpha"
    for d in (root, run / "tmp", run / "state"):
        d.mkdir(parents=True, exist_ok=True)
    _write(root / "config.yaml", ROOT_CONFIG)
    _write(root / ".env", "ROOT_OWN_KEY=DUMMY_ROOT_KEY\n")
    if active != "root":
        _write(root / "active_profile", active + "\n")
    for name, text in {"alpha": alpha_env, **(others or {})}.items():
        home = root / "profiles" / name
        for sub in ("memories", "sessions", "skills", "logs", "cron", "workspace"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        _write(home / "config.yaml", "model:\n  default: dummy-model\n")
        _write(home / ".env", text)
    script = run / "launch.sh"
    _write(script, _launcher_script(launcher))
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(run / "home"),
        "TMPDIR": str(run / "tmp"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_STATE_DIR": str(run / "state"),
        "HERMES_HOME": str(alpha),
        # Any HTTP client the child might construct goes to a closed local port.
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
    }
    if AGENT_DIR is not None:
        env["HERMES_WEBUI_AGENT_DIR"] = str(AGENT_DIR)
    cp = subprocess.run(
        ["bash", str(script), str(alpha / ".env"), sys.executable, "-c", child, str(REPO)],
        env=env, cwd=str(REPO), capture_output=True, text=True, timeout=180,
    )
    lines = [line for line in cp.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"child produced no result (rc={cp.returncode}): {cp.stderr[-1500:]}"
    obs = json.loads(lines[-1][len("RESULT "):])
    if "skip" in obs:
        pytest.skip(obs["skip"])
    assert "error" not in obs, obs["error"]
    return obs


@needs_agent_scopes
@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize("launcher", sorted(EXPECTED_LIVE))
def test_root_scope_takes_no_value_the_launcher_sourced_from_the_named_launch_profile_env(tmp_path, launcher):
    obs = _run_named_launch_root_active(tmp_path, launcher)
    assert obs["active_root"] is True, "precondition: root is active"
    assert obs["launch_home"] == "alpha", "precondition: launched under alpha"
    assert obs["live"] == EXPECTED_LIVE[launcher], "precondition: the launcher exported alpha's .env values"
    operator_only = {"OPERATOR_ONLY_TOKEN": "operator-override"}
    assert obs["operator_env"] == operator_only, "a value from alpha's .env reaches operator_env()"
    assert obs["binding"] == operator_only, "a value from alpha's .env reaches root's ${VAR} binding"
    assert obs["root_config"] == operator_only, "a value from alpha's .env resolves in root's config"
    assert obs["worker_runtime_env"] == ["operator-override"], \
        "a value from alpha's .env reaches the runtime env a root background worker applies"
    assert obs["launch_scope"] == operator_only, "a value from alpha's .env reaches root's Agent credential scope"
    assert obs["fire_scope"] == operator_only, "a value from alpha's .env reaches root's cron fire scope"
    assert obs["binding_after_edit"] == operator_only, \
        "a value the launcher sourced from alpha's .env reaches root's ${VAR} binding once the file no longer has it"



# Startup records the Agent's reading of the launch home's .env, even when the Agent builds no launch env from it.
PRIME_CHILD = r"""
import json, os, sys
from pathlib import Path

repo = sys.argv[1]
sys.path.insert(0, repo)
if os.environ.get("HERMES_WEBUI_AGENT_DIR"):
    sys.path.insert(1, os.environ["HERMES_WEBUI_AGENT_DIR"])
os.chdir(repo)
sys.modules["tui_gateway"] = None  # an Agent without the launch-profile policy: startup builds no launch env
obs = {}
try:
    import api.config  # noqa: F401  (real startup: init_profile_state)
    from api import profiles as P

    env_file = Path(P._INITIAL_HERMES_HOME) / ".env"
    env_file.write_text("ALPHA_OWN_KEY=DUMMY_KEY_PROFILE_A\n", encoding="utf-8")  # alpha's .env edited after startup
    obs["alpha_values"] = sorted(k for k, v in P.operator_env().items() if "PROFILE_A" in v)
except Exception as exc:  # surfaced by the parent assertion
    import traceback
    obs["error"] = repr(exc) + traceback.format_exc()[-1200:]
print("RESULT " + json.dumps(obs, sort_keys=True), flush=True)
os._exit(0)
"""


@needs_agent_scopes
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_startup_records_the_agent_reading_before_a_later_edit_of_the_launch_profile_env(tmp_path):
    obs = _run_named_launch_root_active(tmp_path, "shell_sourced", PRIME_CHILD)
    assert obs["alpha_values"] == [], "a value the shell sourced from alpha's .env reaches operator_env()"


def test_a_fallback_reading_is_not_reused_once_the_agent_reader_is_available(monkeypatch, tmp_path):
    from api import profiles

    alpha = tmp_path / "profiles" / "alpha"
    _write(alpha / ".env", 'ANTHROPIC_API_KEY="DUMMY_KEY\\tPROFILE_A" # provider key\n')
    shell_export = "DUMMY_KEY\\tPROFILE_A"  # what `set -a; . file` exports: the escape stays literal
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(alpha))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", profiles._read_dotenv_values(alpha / ".env"))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_LAUNCHER_DOTENV",
                        profiles._launcher_dotenv_values(alpha / ".env"))
    monkeypatch.setattr(profiles, "_NAMED_LAUNCH_HOME_AGENT_READINGS", {})
    # Called before the Agent is importable: WebUI's fallback reader.
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: profiles._read_dotenv_values)
    assert not profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export), "precondition: WebUI's reading"
    # The Agent's reader is available afterwards (stand-in with the Agent's reading of that line).
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: lambda path: {"ANTHROPIC_API_KEY": shell_export})
    assert profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export), "the fallback reading was reused"


# Gateway URL/key provenance (#8432) reads the launch home's .env the same ways.
GATEWAY_ALPHA_ENV = (
    'HERMES_WEBUI_GATEWAY_BASE_URL="http://alpha-gw.invalid/a\\tb" # gateway url\n'
    'API_SERVER_KEY="DUMMY_KEY_PROFILE_A" # gateway key\n'
)
GATEWAY_CHILD = r"""
import json, os, sys

repo = sys.argv[1]
sys.path.insert(0, repo)
if os.environ.get("HERMES_WEBUI_AGENT_DIR"):
    sys.path.insert(1, os.environ["HERMES_WEBUI_AGENT_DIR"])
os.chdir(repo)
NAMES = ("HERMES_WEBUI_GATEWAY_BASE_URL", "API_SERVER_KEY")
OWNERS = {"http://alpha-gw.invalid/a\tb": "alpha-decoded", "http://alpha-gw.invalid/a\\tb": "alpha-literal",
          "DUMMY_KEY_PROFILE_A": "alpha", "DUMMY_KEY_PROFILE_C": "gamma"}
obs = {}
try:
    import api.config  # noqa: F401  (real startup: init_profile_state)
    from api import gateway_chat as G
    from api import profiles as P

    home = P.get_process_profile_home()
    obs["process_profile"] = "root" if P._is_root_profile_home(home) else home.name
    obs["live"] = sorted(OWNERS.get(os.environ.get(k), "other") for k in NAMES)
    obs["profile_sourced"] = {k: G._env_value_is_profile_sourced(P, k, os.environ[k]) for k in NAMES}
    if obs["process_profile"] != "root":
        gamma = P.get_hermes_home_for_profile("gamma")
        obs["url_may_belong_to_a_profile"] = G._shared_url_may_belong_to_process_profile(gamma)
        obs["gamma_endpoint"] = [OWNERS.get(v, "other") for v in G._gateway_endpoint_for_profile("gamma")]
except Exception as exc:  # surfaced by the parent assertion
    import traceback
    obs["error"] = repr(exc) + traceback.format_exc()[-1200:]
print("RESULT " + json.dumps(obs, sort_keys=True), flush=True)
os._exit(0)
"""


@needs_agent_scopes
@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize("active", ["root", "beta"])
@pytest.mark.parametrize("launcher", sorted(EXPECTED_LIVE))
def test_gateway_values_the_launcher_sourced_from_the_named_launch_profile_env_are_profile_sourced(
        tmp_path, launcher, active):
    others = {"beta": "BETA_OWN_KEY=DUMMY_KEY_PROFILE_B\n",
              "gamma": "HERMES_WEBUI_GATEWAY_API_KEY=DUMMY_KEY_PROFILE_C\n"}
    obs = _run_named_launch_root_active(tmp_path, launcher, GATEWAY_CHILD, alpha_env=GATEWAY_ALPHA_ENV,
                                        active=active, others=others)
    url_owner = "alpha-decoded" if launcher == "ctl_sh" else "alpha-literal"
    assert obs["process_profile"] == active, "precondition: the saved active profile is the process profile"
    assert obs["live"] == sorted(["alpha", url_owner]), "precondition: the launcher exported alpha's gateway values"
    assert obs["profile_sourced"] == {"HERMES_WEBUI_GATEWAY_BASE_URL": True, "API_SERVER_KEY": True}, \
        "a gateway value from alpha's .env is treated as operator configuration"
    if active != "root":
        # A key-only profile does not pair its own key with a URL that may be alpha's own Gateway (#8432).
        assert obs["url_may_belong_to_a_profile"] is True
        assert obs["gamma_endpoint"] == [url_owner, "alpha"], "gamma's key is sent to alpha's gateway URL"
