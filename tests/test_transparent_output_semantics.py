"""Regression coverage for invocation previews leaking into the Output tab."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

const esc = value => String(value)
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;');
const _redactToolTargetLabel = value => value;
const renderToolDetail = eval('(' + extractFunc('_transparentToolDetailHtml') + ')');

let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  process.stdout.write(renderToolDetail(JSON.parse(input), 'Failed'));
});
"""


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("transparent-output") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return path


def _render(driver: Path, tool_call: dict) -> str:
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)],
        input=json.dumps(tool_call),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node driver failed:\n{result.stderr}")
    return result.stdout


def test_execute_code_preview_is_input_not_output(driver):
    html = _render(
        driver,
        {
            "name": "execute_code",
            "args": {"code": "print('Hello, world!')"},
            "preview": "print('Hello, world!')",
            "done": True,
            "is_error": True,
        },
    )

    assert "tool-card-args" in html
    assert "print('Hello, world!')" in html
    assert "tool-card-result" not in html


@pytest.mark.parametrize("field", ["snippet", "result", "output"])
def test_explicit_result_fields_render_in_output(driver, field):
    html = _render(
        driver,
        {
            "name": "execute_code",
            "args": {"code": "print('Hello, world!')"},
            "preview": "print('Hello, world!')",
            field: "Hello, world!\n",
            "done": True,
        },
    )

    assert "tool-card-result" in html
    assert "Hello, world!" in html
