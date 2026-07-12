import json
import subprocess
import shutil
from pathlib import Path

import pytest


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for boot behavior checks")


def _boot_completion_branch() -> str:
    source = (Path(__file__).resolve().parents[1] / "static" / "boot.js").read_text(encoding="utf-8")
    marker = "    if(S.session) syncTopbar();"
    start = source.index(marker)
    end = source.index("\n  }).catch(e=>", start)
    return source[start:end]


def _run_boot_branch() -> list[dict[str, int | str | None]]:
    branch = _boot_completion_branch()
    script = f"""
const branch = {json.dumps(branch)};
function run(session) {{
  const S = {{session}};
  let syncTopbarCalls = 0;
  let syncReasoningChipCalls = 0;
  function syncTopbar() {{ syncTopbarCalls += 1; }}
  function syncReasoningChip() {{ syncReasoningChipCalls += 1; }}
  eval(branch);
  return {{session: session ? 'present' : null, syncTopbarCalls, syncReasoningChipCalls}};
}}
console.log(JSON.stringify([run(null), run({{id: 'session'}})]));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=True,
    )
    return json.loads(result.stdout)


def test_boot_hydration_refreshes_chip_only_without_a_session():
    assert _run_boot_branch() == [
        {"session": None, "syncTopbarCalls": 0, "syncReasoningChipCalls": 1},
        {"session": "present", "syncTopbarCalls": 1, "syncReasoningChipCalls": 0},
    ]
