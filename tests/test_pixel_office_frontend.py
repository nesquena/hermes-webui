"""
Smoke tests for the Pixel Office frontend scaffold (section 8-9 of
openspec/changes/add-dashboards-and-pixel-office/tasks.md).

Covers only presence of files and the banned-phrase lint promised by
task 3.10. Engine behaviour tests land with tasks 8.7 and 9.7.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PIXEL_DIR = REPO_ROOT / "static" / "pixel"


def test_pixel_module_files_exist():
    """All 8 JS files must exist under static/pixel/."""
    expected = [
        "engine/tile-map.js",
        "engine/state.js",
        "engine/characters.js",
        "engine/renderer.js",
        "engine/game-loop.js",
        "sprites.js",
        "surface-bridge.js",
        "pixel-office.js",
    ]
    for rel in expected:
        p = PIXEL_DIR / rel
        assert p.is_file(), f"static/pixel/{rel} missing"


def test_pixel_files_attribute_openclaw():
    """Engine files ported from OpenClaw must credit the MIT source."""
    for rel in (
        "engine/tile-map.js",
        "engine/state.js",
        "engine/characters.js",
        "engine/renderer.js",
        "engine/game-loop.js",
        "sprites.js",
    ):
        text = (PIXEL_DIR / rel).read_text(encoding="utf-8")
        assert "OpenClaw" in text and "MIT" in text, \
            f"{rel} must attribute OpenClaw (MIT) in a header comment"


def test_pixel_no_runtime_perception_phrasing():
    """
    Task 3.10: no pixel-office string may imply webui knows the agent's
    internal runtime state. Banned substrings (case-insensitive).
    """
    banned = (
        "currently running",
        "waiting for your reply",
        "agent is running",
        "is running tool",
    )
    for js in PIXEL_DIR.rglob("*.js"):
        text = js.read_text(encoding="utf-8").lower()
        for phrase in banned:
            assert phrase not in text, \
                f"banned phrase {phrase!r} appears in {js.relative_to(REPO_ROOT)}"
