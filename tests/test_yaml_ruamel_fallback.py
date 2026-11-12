"""api.yaml_compat must work when PyYAML is absent (Hermes Agent's managed runtime ships ruamel only)."""

import builtins
import importlib
import io
import subprocess
import sys
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
