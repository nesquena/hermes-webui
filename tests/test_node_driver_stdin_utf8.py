"""Node test drivers must decode stdin as one UTF-8 stream, not chunk by chunk.

Many renderer tests pipe markdown into a small node driver that collects stdin
with ``process.stdin.on('data', c => { buf += c; })``. Without
``process.stdin.setEncoding('utf8')`` each chunk is a Buffer decoded on its own,
so a multi-byte character split across a pipe chunk becomes U+FFFD. On macOS the
5000-URL case of ``test_fullwidth_autolink_behaviour.py`` rendered 4997 to 4999
links per run this way; Linux's larger chunks only make it rarer.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

from tests.test_fullwidth_autolink_behaviour import NODE, UI_JS_PATH, _DRIVER_SRC

TESTS_DIR = pathlib.Path(__file__).resolve().parent
_DATA_HANDLER = re.compile(r"process\.stdin\.on\(\s*['\"]data['\"]")
_SET_ENCODING = "process.stdin.setEncoding('utf8')"


def test_every_node_driver_sets_utf8_before_collecting_stdin():
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not _DATA_HANDLER.search(line):
                continue
            previous = next((text for text in reversed(lines[:index]) if text.strip()), "")
            if _SET_ENCODING not in previous:
                offenders.append(f"{path.name}:{index + 1}")
    assert not offenders, (
        "node drivers collecting stdin without process.stdin.setEncoding('utf8') "
        f"on the line before: {offenders}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_driver_keeps_multibyte_characters_across_pipe_chunks(tmp_path):
    # ~210 KB of a three-byte character: no chunk boundary that isn't a multiple
    # of three bytes can fall between characters, so a per-chunk decode breaks one.
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    text = "，" * 70_000

    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH)],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "�" not in result.stdout
    assert result.stdout.count("，") == len(text)
