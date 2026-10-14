"""api.yaml_compat must work when PyYAML is absent (Hermes Agent's managed runtime ships ruamel only)."""

import builtins
import importlib
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

CFG = {
    "model": {"default": "claude-opus", "provider": "anthropic"},
    "toolsets": ["web", "terminal"],
    "webui_chat_backend": "gateway",
    "webui_gateway_use_runs_api": True,
    "name": "caf\u00e9",
    "count": 3,
    "empty": None,
}


@pytest.fixture
def ruamel_compat(monkeypatch):
    pytest.importorskip("ruamel.yaml")
    real_import = builtins.__import__

    def _no_pyyaml(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    for mod in [m for m in sys.modules if m == "yaml" or m.startswith("yaml.")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.delitem(sys.modules, "api.yaml_compat", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_pyyaml)
    mod = importlib.import_module("api.yaml_compat")
    yield mod
    sys.modules.pop("api.yaml_compat", None)


def test_ruamel_backend_selected_without_pyyaml(ruamel_compat):
    assert ruamel_compat.BACKEND == "ruamel"


def test_ruamel_round_trip(ruamel_compat):
    text = ruamel_compat.safe_dump(CFG, sort_keys=False, allow_unicode=True)
    assert ruamel_compat.safe_load(text) == CFG
    assert "caf\u00e9" in text
    assert ruamel_compat.safe_load(io.StringIO(text)) == CFG
    assert ruamel_compat.safe_load("") is None


def test_ruamel_dump_to_stream(ruamel_compat):
    buf = io.StringIO()
    assert ruamel_compat.dump(CFG, buf, default_flow_style=False, allow_unicode=True) is None
    assert ruamel_compat.safe_load(buf.getvalue()) == CFG


def test_ruamel_output_matches_pyyaml(ruamel_compat):
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, json, yaml; print(yaml.safe_dump(json.loads(sys.argv[1]), sort_keys=False, allow_unicode=True), end='')",
         __import__("json").dumps(CFG)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        pytest.skip("PyYAML not installed in test interpreter")
    assert ruamel_compat.safe_dump(CFG, sort_keys=False, allow_unicode=True) == out.stdout


def test_config_loader_reads_yaml_without_pyyaml(ruamel_compat, tmp_path, monkeypatch):
    import api.onboarding as onboarding

    cfg = tmp_path / "config.yaml"
    cfg.write_text(ruamel_compat.safe_dump(CFG), encoding="utf-8")
    assert onboarding._load_yaml_config(cfg) == CFG


def test_skills_import_and_route_work_with_ruamel_only(tmp_path):
    pytest.importorskip('ruamel.yaml')
    home = tmp_path / 'home'
    (home / 'skills').mkdir(parents=True)
    (home / 'shared' / 'shared-one').mkdir(parents=True)
    (home / 'shared' / 'shared-one' / 'SKILL.md').write_text(
        '---\nname: shared-one\ndescription: Test skill\n---\n', encoding='utf-8',
    )
    (home / 'config.yaml').write_text(
        'skills:\n  external_dirs: ["${HERMES_HOME}/shared"]\n', encoding='utf-8',
    )
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('HERMES_') and key != 'PYTHONPATH'}
    env.update(HERMES_HOME=str(home), HERMES_BASE_HOME=str(home),
               HERMES_WEBUI_STATE_DIR=str(tmp_path / 'state'))
    script = textwrap.dedent('''
        import builtins, sys, types
        from pathlib import Path
        from unittest.mock import MagicMock
        from urllib.parse import urlparse
        original = builtins.__import__
        def no_pyyaml(name, *args, **kwargs):
            if name == 'yaml' or name.startswith('yaml.'):
                raise ImportError('PyYAML disabled for skills regression')
            return original(name, *args, **kwargs)
        builtins.__import__ = no_pyyaml
        from api import profiles, routes, yaml_compat
        assert yaml_compat.BACKEND == 'ruamel'
        home = Path(sys.argv[1])
        # Stub only the Agent routing and scanner API; YAML parsing and the
        # profile environment/resolver/HTTP route remain production code.
        constants = types.ModuleType('hermes_constants')
        constants.set_hermes_home_override = lambda value: None
        constants.reset_hermes_home_override = lambda token: None
        constants.hermes_home_key = lambda path: str(path)
        agent = types.ModuleType('agent')
        agent.__path__ = []
        scope = types.ModuleType('agent.secret_scope')
        scope.serves_routed_profile = lambda: False
        scanner = types.ModuleType('agent.skill_utils')
        scanner.iter_skill_index_files = lambda root, pattern: root.rglob(pattern)
        tools = types.ModuleType('tools')
        tools.__path__ = []
        skills = types.ModuleType('tools.skills_tool')
        skills.MAX_DESCRIPTION_LENGTH = 512
        skills._EXCLUDED_SKILL_DIRS = set()
        skills._parse_frontmatter = lambda text: (yaml_compat.safe_load(text.split('---')[1]), '')
        skills._sort_skills = lambda values: values
        skills.skill_matches_platform = lambda frontmatter: True
        sys.modules.update({'hermes_constants':constants, 'agent':agent,
            'agent.secret_scope':scope, 'agent.skill_utils':scanner,
            'tools':tools, 'tools.skills_tool':skills})
        profiles.get_active_profile_name = lambda: 'default'
        profiles.get_hermes_home_for_profile = lambda name: home
        profiles.get_active_hermes_home = lambda: home
        profiles.get_process_profile_home = lambda: home
        payloads = []
        routes.j = lambda handler, payload, **kwargs: payloads.append(payload)
        routes.handle_get(MagicMock(), urlparse('/api/skills'))
        assert payloads[-1]['runtime_scope'] == 'profile', payloads
        assert [s['name'] for s in payloads[-1]['skills']] == ['shared-one'], payloads
        from api.skill_runtime import profile_external_skill_dirs
        assert profile_external_skill_dirs(home) == [home / 'shared']
    ''')
    proc = subprocess.run([sys.executable, '-c', script, str(home)], cwd=REPO,
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


# YAML 1.1 words that PyYAML (and the Agent's hermes_yaml reader) treat as booleans.
YAML11_DOC = "tool_progress: off\nshow_reasoning: no\nstreaming: on\nauto: yes\nquoted: 'off'\n"
YAML11_STRINGS = {"tool_progress": "off", "reasoning": "no", "mode": "on", "flag": "yes", "n": "n"}


def test_ruamel_load_keeps_yaml11_booleans(ruamel_compat):
    assert ruamel_compat.safe_load(YAML11_DOC) == {
        "tool_progress": False, "show_reasoning": False, "streaming": True, "auto": True,
        "quoted": "off",
    }


def test_ruamel_load_duplicate_keys_last_wins_like_pyyaml(ruamel_compat):
    # PyYAML keeps the last value; a config that loaded on PyYAML must not become {}.
    doc = "model: a\nmodel: b\ndisplay:\n  tool_progress: all\n  tool_progress: off\n"
    assert ruamel_compat.safe_load(doc) == {"model": "b", "display": {"tool_progress": False}}


def test_ruamel_load_bare_y_n_stay_strings_like_pyyaml(ruamel_compat):
    # ruamel's YAML 1.1 table makes bare y/n booleans; PyYAML (and every file it wrote) keeps them strings.
    assert ruamel_compat.safe_load("model:\n  default: n\nflag: y\n") == {"model": {"default": "n"}, "flag": "y"}


def test_ruamel_load_repeated_merge_keys_like_pyyaml(ruamel_compat):
    doc = "b: &b {x: 1, y: 2}\no: &o {y: 3, z: 4}\nm:\n  <<: *b\n  <<: *o\n  w: 5\n"
    loaded = ruamel_compat.safe_load(doc)
    assert loaded["m"] == {"x": 1, "y": 3, "z": 4, "w": 5}


def test_ruamel_load_matches_pyyaml_on_corpus(ruamel_compat):
    corpus = (
        "o: 010\np: 0x1F\nq: 1:30\nr: 1_000\ns: 1e3\nt: 1.5\nu: .inf\n",
        "d: 2026-01-01\ne: ~\nf: null\ng:\nh: Null\n",
        "base: &b {x: 1}\nm:\n  <<: [*b, {x: 9, q: 1}]\n  x: 7\n",
        "=: value\n",
    )
    for doc in corpus:
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, yaml; print(repr(yaml.safe_load(sys.argv[1])), end='')", doc],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            pytest.skip("PyYAML not installed in test interpreter")
        assert repr(ruamel_compat.safe_load(doc)) == out.stdout, doc


@pytest.mark.parametrize("fn", ["safe_dump", "dump"])
def test_ruamel_dump_quotes_yaml11_ambiguous_strings(ruamel_compat, fn):
    text = getattr(ruamel_compat, fn)(YAML11_STRINGS, sort_keys=False, allow_unicode=True)
    # Own loader round-trips the strings as strings.
    assert ruamel_compat.safe_load(text) == YAML11_STRINGS
    # A YAML 1.1 reader (PyYAML semantics) must also see strings, not booleans.
    from ruamel.yaml import YAML

    y11 = YAML(typ="safe", pure=True)
    y11.version = (1, 1)
    assert y11.load(text) == YAML11_STRINGS
    assert "%YAML" not in text


def test_ruamel_dump_matches_pyyaml_for_yaml11_strings(ruamel_compat):
    # Bare y/n are excluded: ruamel's YAML 1.1 resolver (like the Agent's hermes_yaml)
    # quotes them and PyYAML does not; both forms load back as the same strings.
    words = {k: v for k, v in YAML11_STRINGS.items() if v not in ("y", "n")}
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, json, yaml; print(yaml.safe_dump(json.loads(sys.argv[1]), sort_keys=False), end='')",
         __import__("json").dumps(words)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        pytest.skip("PyYAML not installed in test interpreter")
    assert ruamel_compat.safe_dump(words, sort_keys=False) == out.stdout


def test_no_bare_pyyaml_imports_in_server_code():
    offenders = []
    for path in [REPO / "server.py", *sorted((REPO / "api").glob("*.py"))]:
        if path.name == "yaml_compat.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith(("import yaml", "from yaml ")):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {s}")
    assert offenders == [], "import YAML via api.yaml_compat:\n" + "\n".join(offenders)


def test_bootstrap_probe_accepts_ruamel_only():
    import bootstrap

    src = Path(bootstrap.__file__).read_text(encoding="utf-8")
    start = src.index("def _python_can_run_webui_and_agent")
    body = src[start:start + 600]
    assert "import ruamel.yaml" in body
    assert body.index("from run_agent import AIAgent") < body.index("import yaml")
