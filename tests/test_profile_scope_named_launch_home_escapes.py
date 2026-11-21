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


def _launcher_script(launcher: str, exports: str = "OPERATOR_ONLY_TOKEN=DUMMY_OPERATOR_OVERRIDE") -> str:
    """Sources the .env file named by ``$1`` as *launcher* does, then exports each of *exports* (the operator's)."""
    if launcher == "ctl_sh":
        body = re.search(r"\n(_apply_env_file_safely\(\) \{.*?\n\})\n", (REPO / "ctl.sh").read_text(encoding="utf-8"),
                         re.S)
        assert body, "ctl.sh no longer defines _apply_env_file_safely"
        source = body.group(1) + '\n_apply_env_file_safely "$1"\n'
    else:
        source = 'set -a; . "$1"; set +a\n'
    return source + "".join(f"export {item}\n" for item in exports.split()) + 'shift\nexec "$@"\n'


def _run_named_launch_root_active(tmp_path, launcher, child=CHILD, *, alpha_env: str | bytes = ALPHA_ENV, active="root",
                                  others=None, launch="alpha", root_env="ROOT_OWN_KEY=DUMMY_ROOT_KEY\n", exports=None):
    """Server launched under *launch* (``alpha`` by default, or ``root``) by *launcher*, which sources that home's
    ``.env`` first; *active* is the saved active profile (root by default); *others* maps further profile names to
    their ``.env`` (text, or bytes written as they are); *exports* are the operator's exports after the source."""
    run = tmp_path / "run"
    root = run / "home" / ".hermes"
    for d in (root, run / "tmp", run / "state"):
        d.mkdir(parents=True, exist_ok=True)
    _write(root / "config.yaml", ROOT_CONFIG)
    _write(root / ".env", root_env)
    if active != "root":
        _write(root / "active_profile", active + "\n")
    for name, text in {"alpha": alpha_env, **(others or {})}.items():
        home = root / "profiles" / name
        for sub in ("memories", "sessions", "skills", "logs", "cron", "workspace"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        _write(home / "config.yaml", "model:\n  default: dummy-model\n")
        if isinstance(text, bytes):
            (home / ".env").write_bytes(text)
        else:
            _write(home / ".env", text)
    launch_home = root if launch == "root" else root / "profiles" / launch
    script = run / "launch.sh"
    _write(script, _launcher_script(launcher) if exports is None else _launcher_script(launcher, exports))
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(run / "home"),
        "TMPDIR": str(run / "tmp"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_STATE_DIR": str(run / "state"),
        "HERMES_HOME": str(launch_home),
        # Any HTTP client the child might construct goes to a closed local port.
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
    }
    if AGENT_DIR is not None:
        env["HERMES_WEBUI_AGENT_DIR"] = str(AGENT_DIR)
    cp = subprocess.run(
        ["bash", str(script), str(launch_home / ".env"), sys.executable, "-c", child, str(REPO)],
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


def _record_launch_home(monkeypatch, profiles, home):
    """The launch-home state ``api.profiles`` records at import, as if it started with ``HERMES_HOME`` = *home*."""
    env_file = home / ".env"
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_ENV_BYTES", env_file.read_bytes(), raising=False)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", profiles._read_dotenv_values(env_file))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_LAUNCHER_DOTENV", profiles._launcher_dotenv_values(env_file))
    if hasattr(profiles, "_INITIAL_HERMES_HOME_LAUNCHER_UNICODE_DOTENV"):  # recorded since the Unicode reading
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_LAUNCHER_UNICODE_DOTENV",
                            profiles._launcher_dotenv_values(env_file, unicode_spaces=True))
    monkeypatch.setattr(profiles, "_NAMED_LAUNCH_HOME_AGENT_READINGS", {})


def test_a_fallback_reading_is_not_reused_once_the_agent_reader_is_available(monkeypatch, tmp_path):
    from api import profiles

    alpha = tmp_path / "profiles" / "alpha"
    _write(alpha / ".env", 'ANTHROPIC_API_KEY="DUMMY_KEY\\tPROFILE_A" # provider key\n')
    shell_export = "DUMMY_KEY\\tPROFILE_A"  # what `set -a; . file` exports: the escape stays literal
    _record_launch_home(monkeypatch, profiles, alpha)
    # Called before the Agent is importable: WebUI's fallback reader.
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: profiles._read_dotenv_values)
    assert not profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export), "precondition: WebUI's reading"
    # The Agent's reader is available afterwards (stand-in with the Agent's reading of that line).
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: lambda path: {"ANTHROPIC_API_KEY": shell_export})
    assert profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export), "the fallback reading was reused"


def test_a_late_agent_reading_is_of_the_launch_profile_env_as_it_was_at_startup(monkeypatch, tmp_path):
    """The Agent's reader is importable only after startup, by which time alpha's .env was edited in Settings: the
    reading taken then is not of the edited file, so a value added since is not taken for one the launcher sourced."""
    import api.paths as paths
    from api import profiles

    alpha = tmp_path / "profiles" / "alpha"
    _write(alpha / ".env", "ALPHA_OWN_KEY=DUMMY_KEY_PROFILE_A\n")
    _record_launch_home(monkeypatch, profiles, alpha)
    operator_only = {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    monkeypatch.setattr(paths, "STARTUP_ENV", {"ALPHA_OWN_KEY": "DUMMY_KEY_PROFILE_A", **operator_only})
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: profiles._read_dotenv_values)  # not importable
    assert profiles.operator_env() == operator_only, "precondition: alpha's value is left out"
    _write(alpha / ".env", "ALPHA_OWN_KEY=DUMMY_KEY_PROFILE_A\nOPERATOR_ONLY_TOKEN=DUMMY_OPERATOR_TOKEN\n")
    # The Agent's reader is importable now (stand-in that reads the file it is given).
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: lambda path: profiles._read_dotenv_values(path))
    assert profiles.operator_env() == operator_only, \
        "a value added to alpha's .env after startup is taken for one the launcher sourced from it"


def test_an_empty_agent_reading_of_the_launch_profile_env_is_not_kept(monkeypatch, tmp_path):
    """The Agent's reader returns nothing for a file that has values (a read error at startup): that is not kept as
    its reading, and the next call takes it."""
    from api import profiles

    alpha = tmp_path / "profiles" / "alpha"
    _write(alpha / ".env", 'ANTHROPIC_API_KEY="DUMMY_KEY\\tPROFILE_A" # provider key\n')
    shell_export = "DUMMY_KEY\\tPROFILE_A"  # what `set -a; . file` exports: the escape stays literal
    _record_launch_home(monkeypatch, profiles, alpha)
    reads = []

    def agent_reader(path):  # stand-in with the Agent's reading of that line; its first read fails
        reads.append(path)
        return {} if len(reads) == 1 else {"ANTHROPIC_API_KEY": shell_export}

    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: agent_reader)
    monkeypatch.setattr(profiles, "_agent_bytes_parser", lambda: None)  # an Agent without the byte parser
    assert not profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export), "precondition: the read failed"
    assert profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export), "the empty reading was kept"


def test_the_agent_reading_is_of_the_startup_bytes_even_after_an_edit(monkeypatch, tmp_path):
    """Where the Agent exposes its byte parser, its reading is taken from the bytes read at import: a value only
    that reading matches still matches after the file is edited, and the file's current bytes are never parsed."""
    from api import profiles

    alpha = tmp_path / "profiles" / "alpha"
    startup_line = 'ANTHROPIC_API_KEY="DUMMY_KEY\\tPROFILE_A" # provider key\n'
    _write(alpha / ".env", startup_line)
    shell_export = "DUMMY_KEY\\tPROFILE_A"  # only the Agent's reading holds this value
    _record_launch_home(monkeypatch, profiles, alpha)
    _write(alpha / ".env", "ANTHROPIC_API_KEY=DUMMY_EDITED_LATER\n")  # a Settings edit after startup
    parsed = []

    def parse_bytes(raw):  # stand-in for the Agent's decode + tokenize of raw bytes
        parsed.append(raw)
        return {"ANTHROPIC_API_KEY": shell_export} if raw == startup_line.encode() else {}

    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: (lambda path: {"ANTHROPIC_API_KEY": "WRONG"}))
    monkeypatch.setattr(profiles, "_agent_bytes_parser", lambda: parse_bytes)
    assert profiles._matches_launch_home_env("ANTHROPIC_API_KEY", shell_export)
    assert not profiles._matches_launch_home_env("ANTHROPIC_API_KEY", "DUMMY_EDITED_LATER")
    assert not profiles._matches_launch_home_env("ANTHROPIC_API_KEY", "WRONG")
    assert parsed == [startup_line.encode()]


# ctl.sh sources these shapes of alpha's .env differently from Python's text handling (see the launcher reading
# tests in tests/test_profile_secret_scope_isolation.py); the value it exported is still alpha's.
PARITY_CHILD = r"""
import json, os, sys

repo = sys.argv[1]
sys.path.insert(0, repo)
if os.environ.get("HERMES_WEBUI_AGENT_DIR"):
    sys.path.insert(1, os.environ["HERMES_WEBUI_AGENT_DIR"])
os.chdir(repo)
obs = {}
try:
    import api.config  # noqa: F401  (real startup: init_profile_state)
    from api import profiles as P

    def alpha_keys(mapping):
        return sorted(k for k, v in mapping.items() if "PROFILE_A" in str(v))

    obs["active_root"] = P._is_root_profile_home(P.get_process_profile_home())
    obs["live"] = alpha_keys(os.environ)
    obs["operator_env"] = alpha_keys(P.operator_env())
    obs["binding"] = alpha_keys(P.profile_env_binding(P._DEFAULT_HERMES_HOME))
except Exception as exc:  # surfaced by the parent assertion
    import traceback
    obs["error"] = repr(exc) + traceback.format_exc()[-1200:]
print("RESULT " + json.dumps(obs, sort_keys=True), flush=True)
os._exit(0)
"""
PARITY_ALPHA_ENVS = {
    "byte_not_utf8_elsewhere": b'# DUMMY profile file \xff\nOPENROUTER_API_KEY="DUMMY_KEY\\tPROFILE_A"\n',
    "lone_cr": b"OPENROUTER_API_KEY=DUMMY_KEY_PROFILE_A\rDUMMY_TRAILER=x\n",
    "nul": b"OPENROUTER_API_KEY=DUMMY_KEY\x00_PROFILE_A\n",
    "nbsp_after_quoted": 'OPENROUTER_API_KEY="DUMMY_KEY\\tPROFILE_A"\u00a0# provider key\n'.encode(),
}


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize("case", sorted(PARITY_ALPHA_ENVS))
def test_root_scope_takes_no_value_ctl_sh_sourced_from_these_launch_profile_env_shapes(tmp_path, case):
    obs = _run_named_launch_root_active(tmp_path, "ctl_sh", PARITY_CHILD, alpha_env=PARITY_ALPHA_ENVS[case])
    assert obs["active_root"] is True, "precondition: root is active"
    assert obs["live"] == ["OPENROUTER_API_KEY"], "precondition: ctl.sh exported alpha's value"
    assert obs["operator_env"] == [], "a value ctl.sh sourced from alpha's .env reaches operator_env()"
    assert obs["binding"] == [], "a value ctl.sh sourced from alpha's .env reaches root's ${VAR} binding"


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
NAMES = ("HERMES_WEBUI_GATEWAY_BASE_URL", "API_SERVER_KEY", "HERMES_WEBUI_GATEWAY_API_KEY")
OWNERS = {"http://alpha-gw.invalid/a\tb": "alpha-decoded", "http://alpha-gw.invalid/a\\tb": "alpha-literal",
          "DUMMY_KEY_PROFILE_A": "alpha", "DUMMY_KEY_PROFILE_B": "beta", "DUMMY_KEY_PROFILE_C": "gamma",
          "http://operator-gw.invalid/o": "operator-url", "DUMMY_OPERATOR_OVERRIDE": "operator",
          "http://root-gw.invalid/r": "root-url", "DUMMY_KEY_ROOT": "root"}
obs = {}
try:
    import api.config  # noqa: F401  (real startup: init_profile_state)
    from api import gateway_chat as G
    from api import profiles as P

    home = P.get_process_profile_home()
    obs["process_profile"] = "root" if P._is_root_profile_home(home) else home.name
    obs["live"] = sorted(OWNERS.get(os.environ[k], "other") for k in NAMES if k in os.environ)
    obs["profile_sourced"] = {k: G._env_value_is_profile_sourced(P, k, os.environ[k]) for k in NAMES if k in os.environ}
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


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize("active", ["root", "beta"])
@pytest.mark.parametrize("launcher", sorted(EXPECTED_LIVE))
def test_gateway_values_the_operator_exported_over_the_named_launch_profile_env_are_not_profile_sourced(
        tmp_path, launcher, active):
    """alpha's .env sets a Gateway URL and key; the operator exports its own after the launcher sourced that file.
    Neither export is alpha's value, so both stay operator configuration (#8432)."""
    others = {"beta": "BETA_OWN_KEY=DUMMY_KEY_PROFILE_B\n",
              "gamma": "HERMES_WEBUI_GATEWAY_API_KEY=DUMMY_KEY_PROFILE_C\n"}
    exports = "HERMES_WEBUI_GATEWAY_BASE_URL=http://operator-gw.invalid/o API_SERVER_KEY=DUMMY_OPERATOR_OVERRIDE"
    obs = _run_named_launch_root_active(tmp_path, launcher, GATEWAY_CHILD, alpha_env=GATEWAY_ALPHA_ENV,
                                        active=active, others=others, exports=exports)
    assert obs["process_profile"] == active, "precondition: the saved active profile is the process profile"
    assert obs["live"] == ["operator", "operator-url"], "precondition: the operator's exports replaced alpha's"
    assert obs["profile_sourced"] == {"HERMES_WEBUI_GATEWAY_BASE_URL": False, "API_SERVER_KEY": False}, \
        "an operator export is treated as a value from alpha's .env"
    if active != "root":
        assert obs["url_may_belong_to_a_profile"] is False, "the operator's Gateway URL is taken for a profile's"
        assert obs["gamma_endpoint"] == ["operator-url", "gamma"]


# Launched under root: the Gateway values ctl.sh sourced from root's .env are the shared settings.
GATEWAY_ROOT_ENV = "HERMES_WEBUI_GATEWAY_BASE_URL=http://root-gw.invalid/r\nAPI_SERVER_KEY=DUMMY_KEY_ROOT\n"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize("active", ["root", "beta"])
def test_gateway_values_ctl_sh_sourced_from_the_root_env_stay_shared(tmp_path, active):
    """A key-only profile keeps root's shared URL with its own key; under a named active profile root's values are
    not profile-sourced, while that profile's own Gateway key (mirrored from its .env) is (#8432)."""
    others = {"beta": "HERMES_WEBUI_GATEWAY_API_KEY=DUMMY_KEY_PROFILE_B\n",
              "gamma": "HERMES_WEBUI_GATEWAY_API_KEY=DUMMY_KEY_PROFILE_C\n"}
    obs = _run_named_launch_root_active(tmp_path, "ctl_sh", GATEWAY_CHILD, alpha_env="ALPHA_OWN_KEY=DUMMY_KEY_PROFILE_A\n",
                                        active=active, others=others, launch="root", root_env=GATEWAY_ROOT_ENV)
    assert obs["process_profile"] == active, "precondition: the saved active profile is the process profile"
    assert obs["url_may_belong_to_a_profile"] is False, "root's shared Gateway URL is taken for a profile's"
    assert obs["gamma_endpoint"] == ["root-url", "gamma"]
    if active != "root":
        assert obs["live"] == ["beta", "root", "root-url"], "precondition: ctl.sh exported root's values"
        assert obs["profile_sourced"] == {"HERMES_WEBUI_GATEWAY_BASE_URL": False, "API_SERVER_KEY": False,
                                          "HERMES_WEBUI_GATEWAY_API_KEY": True}


def test_the_profile_sourced_check_ignores_an_empty_value_and_fails_closed(monkeypatch, tmp_path):
    from api import gateway_chat, profiles

    alpha = tmp_path / "profiles" / "alpha"
    _write(alpha / ".env", "API_SERVER_KEY=DUMMY_KEY_PROFILE_A\n")
    _record_launch_home(monkeypatch, profiles, alpha)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    assert gateway_chat._env_value_is_profile_sourced(profiles, "API_SERVER_KEY", "DUMMY_KEY_PROFILE_A") is True
    assert gateway_chat._env_value_is_profile_sourced(profiles, "API_SERVER_KEY", "") is False, \
        "an empty value configures nothing"

    def unavailable(*_args, **_kwargs):
        raise OSError("DUMMY: profile home unavailable")

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", unavailable)
    assert gateway_chat._env_value_is_profile_sourced(profiles, "API_SERVER_KEY", "DUMMY_KEY_PROFILE_A") is True, \
        "an undeterminable launch home is not failed closed"


# Launched under root with a saved active named profile, the Agent's launch env re-reads the keys the launcher
# sourced from root's .env each time it is built (tests/test_profile_secret_scope_isolation.py); stand-in launch
# policy, WebUI's own .env reader.
LAUNCH_ENV_ORDER_CHILD = r"""
import json, os, sys, types
from pathlib import Path

repo = sys.argv[1]
sys.path.insert(0, repo)
os.chdir(repo)
pkg = types.ModuleType("tui_gateway")
pkg.__path__ = []
policy = types.ModuleType("tui_gateway.launch_profile_policy")
policy._launch_env = lambda: {}
pkg.launch_profile_policy = policy
sys.modules.update({"tui_gateway": pkg, "tui_gateway.launch_profile_policy": policy, "agent.secret_scope": None})
from api import profiles as P

alpha = Path(P._DEFAULT_HERMES_HOME) / "profiles" / "alpha"
P.get_process_profile_home = lambda: alpha
P._install_operator_env_as_agent_launch_env()
print("RESULT " + json.dumps(list(policy._launch_env())), flush=True)
"""


def test_the_launch_env_lists_its_keys_in_the_same_order_in_every_process(tmp_path):
    home = tmp_path / "home"
    plain = [f"DUMMY_ROOT_KEY_{i:02d}" for i in range(12)]
    escaped = [f"DUMMY_ROOT_ESC_{i:02d}" for i in range(6)]
    _write(home / ".hermes" / ".env", "".join(f"{k}=DUMMY_VALUE_{k}\n" for k in plain)
           + "".join(f'{k}="DUMMY\\tVALUE_{k}"\n' for k in escaped))
    launched = {**{k: f"DUMMY_VALUE_{k}" for k in plain}, **{k: f"DUMMY\tVALUE_{k}" for k in escaped}}  # ctl.sh's
    orders = []
    for seed in ("1", "2", "3", "4"):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp_path), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": seed, **launched}
        cp = subprocess.run([sys.executable, "-c", LAUNCH_ENV_ORDER_CHILD, str(REPO)], env=env, cwd=str(REPO),
                            capture_output=True, text=True, timeout=120)
        lines = [line for line in cp.stdout.splitlines() if line.startswith("RESULT ")]
        assert lines, f"child produced no result (rc={cp.returncode}): {cp.stderr[-1500:]}"
        orders.append([k for k in json.loads(lines[-1][len("RESULT "):]) if k.startswith("DUMMY_ROOT_")])
    assert orders[0] == plain + escaped, "the re-read keys are not in launch-env order"
    assert all(order == orders[0] for order in orders), "the launch env's key order depends on the process"


# Root as the process profile: a key only the launcher's reading of root's .env holds (ctl.sh drops a NUL in
# the key's name; the Agent's reading keeps it) is not in root's own scope, so the launch env must keep it.
ROOT_SCOPE_CHILD = r'''
import json, os, sys, threading
sys.path[:0] = [sys.argv[1], os.environ['HERMES_WEBUI_AGENT_DIR']]
import api.config
import cron.scheduler as CS
from api import profiles as P
from agent.secret_scope import current_secret_scope, get_secret
from tui_gateway import launch_profile_policy as LPP
key = 'OPENROUTER_API_KEY'
root = P._DEFAULT_HERMES_HOME
obs = {'root_active': P._is_root_profile_home(P.get_process_profile_home()),
       'live': os.environ.get(key), 'launch_scope': LPP.launch_secret_scope(root).get(key)}
with P.cron_profile_context_for_home(root):
    tokens = CS._install_fire_secret_scope()
try:
    obs['cron_scope'] = (current_secret_scope() or {}).get(key)
    obs['cron_get_secret'] = get_secret(key)
finally:
    CS._reset_fire_secret_scope(tokens)
entered, release, done = threading.Event(), threading.Event(), threading.Event()
def worker():
    try:
        with P.profile_env_for_background_worker('alpha', 'root scope test', scope_skill_modules=False):
            entered.set()
            release.wait(30)
    finally:
        entered.set()
        done.set()
threading.Thread(target=worker, daemon=True).start()
assert entered.wait(30)
try:
    obs['worker_live'] = os.environ.get(key)
    with P.cron_profile_context_for_home(root):
        tokens = CS._install_fire_secret_scope()
    try:
        obs['cron_during_worker'] = get_secret(key)
    finally:
        CS._reset_fire_secret_scope(tokens)
finally:
    release.set()
    assert done.wait(30)
print('RESULT ' + json.dumps(obs), flush=True)
os._exit(0)
'''

@needs_agent_scopes
@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize('line', [
    'OPENROU\0TER_API_KEY=DUMMY_ROOT_KEY\n',
    '\0OPENROUTER_API_KEY=DUMMY_ROOT_KEY\n',
], ids=['nul_inside_key', 'nul_before_key'])
def test_root_retains_the_provider_key_the_real_launcher_exported(tmp_path, line):
    obs = _run_named_launch_root_active(tmp_path, 'ctl_sh', ROOT_SCOPE_CHILD, launch='root', root_env=line, exports='',
                                       alpha_env='OPENROUTER_API_KEY=DUMMY_KEY_PROFILE_A\n')
    assert obs['root_active'] is True
    assert obs['live'] == 'DUMMY_ROOT_KEY'
    assert obs['worker_live'] == 'DUMMY_KEY_PROFILE_A', obs
    assert obs['cron_during_worker'] == 'DUMMY_ROOT_KEY', obs
    assert obs['launch_scope'] == 'DUMMY_ROOT_KEY', obs
    assert obs['cron_scope'] == 'DUMMY_ROOT_KEY', obs
    assert obs['cron_get_secret'] == 'DUMMY_ROOT_KEY', obs


# An operator export that is NOT what the real launcher exported from the file stays the operator's: no reading
# may invent a value the launcher didn't export (a Unicode space bash doesn't count, or an unreadable file).
OPERATOR_SCOPE_CHILD = r'''
import json, os, sys
sys.path.insert(0,sys.argv[1])
if os.environ.get('HERMES_WEBUI_AGENT_DIR'):sys.path.insert(1,os.environ['HERMES_WEBUI_AGENT_DIR'])
import api.config
from api import profiles as P
key='OPENROUTER_API_KEY'
root=P._DEFAULT_HERMES_HOME
obs={'live':os.environ.get(key),'operator_env':P.operator_env().get(key),
     'root_binding':P.profile_env_binding(root).get(key),
     'root_config':P.load_profile_config(root)['terminal']['docker_image']}
if os.environ.get('HERMES_WEBUI_AGENT_DIR'):
 from tui_gateway import launch_profile_policy as L
 obs['root_agent_scope']=L.launch_secret_scope(root).get(key)
print('RESULT '+json.dumps(obs),flush=True)
os._exit(0)
'''

@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize('line',[
    b'OPENROUTER_API_KEY=DUMMY_OPERATOR_OVERRIDE\xc2\xa0# provider comment\n',
    b'OPENROU\xc2\xa0TER_API_KEY=DUMMY_OPERATOR_OVERRIDE\n',
    b'OPENROUTER_API_KEY="DUMMY_OPERATOR_OVERRIDE\n',
],ids=['nbsp_before_comment','nbsp_inside_key','unclosed_quote'])
def test_operator_export_not_equal_to_real_launcher_export_survives(tmp_path,line):
    obs=_run_named_launch_root_active(tmp_path, 'ctl_sh', OPERATOR_SCOPE_CHILD,
        alpha_env=b'# DUMMY non-UTF8 comment \xff\n'+line,
        exports='OPENROUTER_API_KEY=DUMMY_OPERATOR_OVERRIDE')
    assert obs['live']=='DUMMY_OPERATOR_OVERRIDE'
    for field in ['operator_env','root_binding','root_config']:
        assert obs[field]=='DUMMY_OPERATOR_OVERRIDE',(field,obs)
    if 'root_agent_scope' in obs:
        assert obs['root_agent_scope']=='DUMMY_OPERATOR_OVERRIDE',obs


# A launcher-only root key kept in the launch env follows edits to root's .env and goes when removed.
ROOT_EDITS_CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], os.environ['HERMES_WEBUI_AGENT_DIR']]
import api.config
from api import profiles as P
from tui_gateway import launch_profile_policy as L
root = Path(P._DEFAULT_HERMES_HOME)
key = 'OPENROUTER_API_KEY'
obs = {'initial': L._launch_env().get(key)}
(root / '.env').write_bytes(b'OPENROU\0TER_API_KEY=DUMMY_ROOT_CHANGED\n')
obs['changed'] = L._launch_env().get(key)
(root / '.env').write_text('')
obs['removed'] = L._launch_env().get(key)
obs['scope_removed'] = L.launch_secret_scope(P.get_process_profile_home()).get(key)
obs['live_after_removal'] = os.environ.get(key)
print('RESULT ' + json.dumps(obs), flush=True)
os._exit(0)
'''

@needs_agent_scopes
@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize('active',['root','beta'])
def test_retained_launcher_only_root_key_tracks_edits_and_removal(tmp_path, active):
    obs = _run_named_launch_root_active(tmp_path, 'ctl_sh', ROOT_EDITS_CHILD, launch='root', active=active,
        root_env='OPENROU\0TER_API_KEY=DUMMY_ROOT_KEY\n', exports='', others={'beta':'BETA_OWN_KEY=DUMMY_BETA\n'})
    assert obs == {'initial':'DUMMY_ROOT_KEY','changed':'DUMMY_ROOT_CHANGED','removed':None,
                   'scope_removed':None,'live_after_removal':'DUMMY_ROOT_KEY'}
