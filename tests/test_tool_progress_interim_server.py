"""Server-side tool progress prose for WebUI interim assistant events."""

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
STREAMING = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def function_body(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    assert start != -1, f"{name} not found"
    # Nested helpers are indented by 12 spaces in streaming.py; stop at the next
    # sibling helper to avoid static assertions spanning unrelated functions.
    match = re.search(
        rf"def {re.escape(name)}\([^\n]*\):\s*\n(.*?)(?=\n\s{{12}}def |\n\s{{8}}def |\n\s{{4}}def |\Z)",
        src[start:],
        re.DOTALL,
    )
    assert match, f"{name} body not found"
    return match.group(1)


def test_tool_progress_interim_uses_interim_assistant_event():
    body = function_body(STREAMING, "_emit_tool_progress_interim")
    assert "put('interim_assistant'" in body
    assert "generated_tool_progress" in body
    assert "already_streamed" in body
    assert "put('reasoning'" not in body


def test_tool_start_paths_emit_interim_before_tool_card():
    legacy = function_body(STREAMING, "on_tool")
    structured = function_body(STREAMING, "on_tool_start")
    assert "_emit_tool_progress_interim(name, args_snap)" in legacy
    assert "_emit_tool_progress_interim(name, args_snap)" in structured
    assert legacy.index("_emit_tool_progress_interim(name, args_snap)") < legacy.index("put('tool'")
    assert structured.index("_emit_tool_progress_interim(name, args_snap)") < structured.index("put('tool'")


def test_french_progress_is_localized_and_does_not_echo_terminal_command():
    body = function_body(STREAMING, "_safe_tool_progress_text")
    assert "_tool_progress_language().startswith('fr')" in body
    assert "Je charge la compétence" in body
    assert "Je lance une commande de diagnostic." in body
    assert "J’utilise l’outil" in body
    assert "args.command" not in body
    assert "payload.command" not in body


def test_non_french_fallback_exists_for_upstream_users():
    body = function_body(STREAMING, "_safe_tool_progress_text")
    assert "Running a diagnostic command." in body
    assert "Using `{tool_name}`" in body
