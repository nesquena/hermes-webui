from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _extract_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"{name} body not closed")


def _eval_filter(src: str, name: str, cases: list[str]) -> list[bool]:
    script = (
        _extract_function(src, name)
        + "\nconst cases = JSON.parse(process.argv[1]);\n"
        + f"process.stdout.write(JSON.stringify(cases.map({name})));\n"
    )
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(cases)],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return json.loads(result.stdout)


RECOVERY_VARIANTS = [
    "[System: Your previous response was cut off by a network error. Continue exactly where you left off.]",
    "[System: Your previous response was truncated by the output length limit. Continue exactly where you left off.]",
    "[System: Your previous tool call (shell_command) was too large. Do not retry the same tool call.]",
]

FALSE_POSITIVES = [
    "",
    "Continue exactly where you left off.",
    "Do not retry the same tool call.",
    "[System: Some other note.]",
    "[System: previous response was cut off by a network error.]",
    "User said: [System: continue exactly where you left off.]",
    "**Response interrupted:** the model stopped early",
]

_PARAMS = [
    pytest.param(UI_JS, "_isRecoveryControlMessageText", id="ui_js"),
    pytest.param(MESSAGES_JS, "_streamRecoveryControlMessageText", id="messages_js"),
]


@pytest.mark.parametrize(("src", "name"), _PARAMS)
def test_all_continuation_variants_filtered(src, name):
    assert _eval_filter(src, name, RECOVERY_VARIANTS) == [True, True, True]


@pytest.mark.parametrize(("src", "name"), _PARAMS)
def test_false_positives_not_filtered(src, name):
    assert _eval_filter(src, name, FALSE_POSITIVES) == [False] * len(FALSE_POSITIVES)


@pytest.mark.parametrize(("src", "name"), _PARAMS)
def test_backend_recovery_string_filtered(src, name):
    assert _eval_filter(src, name, ["The live worker stopped before this run finished."]) == [True]
