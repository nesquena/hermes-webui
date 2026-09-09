"""Adversarial contract tests for the current turn-admission caller scanner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCANNER_PATH = REPO / "scripts" / "verify_turn_admission_callers.py"
MANIFEST_PATH = REPO / "scripts" / "turn_admission_signature_manifest.json"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("turn_admission_scanner_test", SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _identity(file_name, scope, symbol, ordinal, kind):
    return {"identity": [file_name, scope, symbol, ordinal, kind]}


def _manifest():
    return {
        "schema": "S2-current-admission-surface-manifest-v2",
        "p5_required_old_symbol_manifest": {
            "_run_agent_streaming": ["admission"],
            "_run_gateway_chat_streaming": ["admission"],
        },
        "implementation_signature_fields": {
            "_run_admitted_agent_streaming": ["admission"],
            "_run_admitted_gateway_chat_streaming": ["admission"],
        },
        "chosen_p6_design_b": {
            "required_signature_universe": [
                "_run_admitted_agent_streaming",
                "_run_admitted_gateway_chat_streaming",
            ],
            "expected_core_direct_calls": {
                "_run_agent_streaming_core": 1,
                "_run_gateway_chat_streaming_core": 1,
            },
        },
        "baseline": {
            "direct_call_identities": [],
            "non_call_reference_identities": [],
            "dynamic_reference_identities": [],
        },
        "behavior_only_manifest": {},
        "transitive_and_planned_cleanup": {},
        "implementation_surface": {
            "direct_calls": [],
            "thread_targets": [
                _identity(
                    "api/routes.py",
                    "launch",
                    "_run_admitted_agent_streaming",
                    1,
                    "thread_target",
                )
            ],
            "thread_target_aliases": [],
            "assignment_aliases": [],
            "callbacks": [],
            "partials": [],
            "dynamic_references": [],
            "behavior_only_calls": [],
            "core_test_references": [],
        },
    }


def _valid_files():
    return {
        "api/streaming.py": """
def _run_agent_streaming_core():
    return None


def _run_admitted_agent_streaming(*, admission):
    return _run_agent_streaming_core()
""",
        "api/gateway_chat.py": """
def _run_gateway_chat_streaming_core():
    return None


def _run_admitted_gateway_chat_streaming(*, admission):
    return _run_gateway_chat_streaming_core()
""",
        "api/routes.py": """
import threading
from api.streaming import _run_admitted_agent_streaming


def launch(admission):
    return threading.Thread(
        target=_run_admitted_agent_streaming,
        kwargs={"admission": admission},
    )
""",
    }


def _scan_tmp_repo(tmp_path, *, files=None, manifest=None):
    scanner = _load_scanner()
    content = _valid_files()
    content.update(files or {})
    for relative, source in content.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    effective_manifest = manifest or _manifest()
    result = scanner.verify(tmp_path, "implementation", manifest=effective_manifest)
    return scanner.diagnostic_codes(scanner.check_implementation(result, effective_manifest))


def test_current_repository_matches_regenerated_five_part_manifest():
    scanner = _load_scanner()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    result = scanner.verify(REPO, "implementation", manifest=manifest)
    assert scanner.check_implementation(result, manifest) == []


@pytest.mark.parametrize(
    ("files", "expected_code"),
    [
        (
            {
                "api/routes.py": "from api.streaming import _run_admitted_agent_streaming\n",
                "api/other.py": """
import threading
from api.streaming import _run_admitted_agent_streaming


def launch(admission):
    return threading.Thread(
        target=_run_admitted_agent_streaming,
        kwargs={"admission": admission},
    )
""",
            },
            "E_THREAD_TARGET_EXTRA",
        ),
        (
            {
                "api/routes.py": """
import threading
from api.streaming import _run_admitted_agent_streaming
worker = _run_admitted_agent_streaming


def launch(admission):
    return threading.Thread(target=worker, kwargs={"admission": admission})
"""
            },
            "E_ALIAS_EXTRA",
        ),
        (
            {
                "api/routes.py": """
import threading
from api.streaming import _run_admitted_agent_streaming


def consume(callback):
    return callback


def launch(admission):
    consume(_run_admitted_agent_streaming)
    return threading.Thread(target=_run_admitted_agent_streaming, kwargs={"admission": admission})
"""
            },
            "E_CALLBACK_EXTRA",
        ),
        (
            {
                "api/routes.py": """
import threading
from functools import partial
from api.streaming import _run_admitted_agent_streaming


def launch(admission):
    partial(_run_admitted_agent_streaming, admission=admission)
    return threading.Thread(target=_run_admitted_agent_streaming, kwargs={"admission": admission})
"""
            },
            "E_PARTIAL_EXTRA",
        ),
        (
            {
                "api/routes.py": """
import threading
import api.streaming as streaming
from api.streaming import _run_admitted_agent_streaming


def launch(admission):
    getattr(streaming, "_run_admitted_agent_streaming")
    return threading.Thread(target=_run_admitted_agent_streaming, kwargs={"admission": admission})
"""
            },
            "E_DYNAMIC_EXTRA",
        ),
        (
            {
                "api/routes.py": """
import threading
from api.streaming import _run_admitted_agent_streaming


def launch(admission):
    return threading.Thread(target=_run_admitted_agent_streaming, kwargs={})
"""
            },
            "E_THREAD_KWARGS_MISSING_REQUIRED",
        ),
        (
            {
                "api/streaming.py": """
def _run_agent_streaming_core():
    return None


def _run_admitted_agent_streaming(*, admission=None):
    return _run_agent_streaming_core()
"""
            },
            "E_SIGNATURE_FIELD_HAS_DEFAULT",
        ),
        (
            {
                "api/other.py": (
                    "import api.streaming as streaming\n"
                    "def bad():\n"
                    "    return getattr(streaming, '_run_agent_streaming')\n"
                )
            },
            "E_RETIRED_SYMBOL_REFERENCE",
        ),
        (
            {"api/other.py": "def bypass():\n    return _run_agent_streaming_core()\n"},
            "E_CORE_BYPASS_FORBIDDEN",
        ),
    ],
    ids=[
        "wrong-topology-equal-count",
        "assignment-alias",
        "callback",
        "partial",
        "dynamic",
        "kwargs",
        "signature",
        "retired-symbol",
        "production-core-bypass",
    ],
)
def test_adversarial_surfaces_fail_closed(tmp_path, files, expected_code):
    assert expected_code in _scan_tmp_repo(tmp_path, files=files)
