"""Tests for issue #2558 -- container path translation for Reveal in File Manager.

Pins that _handle_file_reveal applies the same container_path_prefix /
host_path_prefix substitution used by _handle_file_open_vscode.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES = ROOT / "api" / "routes.py"


class TestRevealPathTranslation:
    def test_handler_supports_path_prefix_mapping(self):
        """_handle_file_reveal must contain container_path_prefix / host_path_prefix
        so Docker users get the same path translation as _handle_file_open_vscode."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_reveal\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m, "_handle_file_reveal not found in api/routes.py"
        body = m.group(0)
        assert "container_path_prefix" in body
        assert "host_path_prefix" in body

    def test_handler_uses_target_str_in_subprocess(self):
        """The subprocess dispatch must use the translated string, not str(target)."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_reveal\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        # After translation the variable must be named target_str (not str(target))
        assert "target_str" in body
        # Ensure the translation assignment is present
        assert "target_str = host_prefix + target_str[len(container_prefix):]" in body
