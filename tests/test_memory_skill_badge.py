"""Behavioral test for the Activity-summary memory/skill-save counter (#3340, #3544).

The original PR (#3544) shipped only *static source* assertions and gated detection
on action names {save, create, update, upsert} — which do NOT match the real agent
tool enums (memory.action = add|replace|remove; skill_manage.action = create|patch|
edit|delete|write_file|remove_file), so the counter never fired on real saves.

This test EXTRACTS the real detection helpers from static/ui.js and DRIVES them with
the authentic tool-call shapes, asserting the counts are correct. It is the
RED/GREEN guard for the corrected action vocabularies.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_UI_JS = (_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_helpers() -> str:
    """Pull the memory/skill detection helper block out of ui.js verbatim."""
    start = _UI_JS.index("const _MEMORY_SAVE_ACTIONS")
    end = _UI_JS.index("function _isSkillUpdate")
    end = _UI_JS.index("}", end) + 1
    block = _UI_JS[start:end]
    # sanity: the block must define both predicates
    assert "_isMemorySave" in block and "_isSkillUpdate" in block
    return block


def _run(tool_calls):
    assert NODE is not None  # guarded by pytestmark skipif
    helpers = _extract_helpers()
    js = (
        helpers
        + "\nconst tcs = " + json.dumps(tool_calls) + ";\n"
        + "const mem = tcs.filter(_isMemorySave).length;\n"
        + "const skill = tcs.filter(_isSkillUpdate).length;\n"
        + "console.log(JSON.stringify({mem, skill}));\n"
    )
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip())


def test_real_memory_actions_are_counted_as_saved():
    # The authoritative memory tool enum is add | replace | remove.
    out = _run([
        {"name": "memory", "args": {"action": "add"}, "done": True},
        {"name": "memory", "args": {"action": "replace"}, "done": True},
    ])
    assert out["mem"] == 2, "add/replace must count as memory saves"


def test_memory_remove_is_not_counted_as_saved():
    out = _run([{"name": "memory", "args": {"action": "remove"}, "done": True}])
    assert out["mem"] == 0, "'remove' is a deletion, not a save"


def test_legacy_action_names_do_not_falsely_match():
    # The original PR's vocabulary {save, update, upsert} is NOT the real enum.
    out = _run([
        {"name": "memory", "args": {"action": "save"}, "done": True},
        {"name": "memory", "args": {"action": "upsert"}, "done": True},
    ])
    assert out["mem"] == 0, "non-enum action names must not match"


def test_real_skill_actions_are_counted_as_updated():
    # skill_manage enum: create | patch | edit | delete | write_file | remove_file
    out = _run([
        {"name": "skill_manage", "args": {"action": "create"}, "done": True},
        {"name": "skill_manage", "args": {"action": "patch"}, "done": True},
        {"name": "skill_manage", "args": {"action": "edit"}, "done": True},
        {"name": "skill_manage", "args": {"action": "write_file"}, "done": True},
    ])
    assert out["skill"] == 4, "create/patch/edit/write_file must count as skill updates"


def test_skill_deletions_are_not_counted_as_updated():
    out = _run([
        {"name": "skill_manage", "args": {"action": "delete"}, "done": True},
        {"name": "skill_manage", "args": {"action": "remove_file"}, "done": True},
    ])
    assert out["skill"] == 0, "delete/remove_file are not 'updates'"


def test_running_and_errored_writes_are_excluded():
    out = _run([
        {"name": "memory", "args": {"action": "add"}, "done": False},
        {"name": "memory", "args": {"action": "add"}, "is_error": True, "done": True},
        {"name": "skill_manage", "args": {"action": "create"}, "done": False},
    ])
    assert out == {"mem": 0, "skill": 0}, "in-progress/errored writes must not count"


def test_non_memory_skill_tools_ignored():
    out = _run([
        {"name": "terminal", "args": {"action": "add"}, "done": True},
        {"name": "read_file", "args": {}, "done": True},
        {"name": "skills_list", "args": {"action": "create"}, "done": True},
    ])
    assert out == {"mem": 0, "skill": 0}, "only memory/skill_manage are inspected"


def test_action_matching_is_case_insensitive():
    out = _run([
        {"name": "memory", "args": {"action": "ADD"}, "done": True},
        {"name": "skill_manage", "args": {"action": "Patch"}, "done": True},
    ])
    assert out == {"mem": 1, "skill": 1}


def test_missing_args_does_not_throw():
    out = _run([
        {"name": "memory", "done": True},
        {"name": "skill_manage", "args": None, "done": True},
    ])
    assert out == {"mem": 0, "skill": 0}


def test_classification_persisted_as_durable_dom_attributes():
    """Regression guard (Codex catch on #3544): _tcData is a JS property that does
    NOT survive the outerHTML/innerHTML snapshot+restore the live tool-call group
    uses on session switch/restore. If classification lived ONLY on _tcData, a
    restored memory/skill row would be re-counted as a generic tool and the suffix
    would silently vanish. buildToolCard must therefore ALSO stamp durable data-*
    attributes, and the summary must count them as a fallback.
    """
    # (a) buildToolCard stamps durable attributes for classified rows
    assert "data-memory-save" in _UI_JS, "buildToolCard must stamp a durable memory flag"
    assert "data-skill-update" in _UI_JS, "buildToolCard must stamp a durable skill flag"
    build_start = _UI_JS.index("function buildToolCard(tc)")
    build_end = _UI_JS.index("\nfunction ", _UI_JS.index("return row;", build_start))
    build_block = _UI_JS[build_start:build_end]
    assert "setAttribute('data-memory-save'" in build_block
    assert "setAttribute('data-skill-update'" in build_block

    # (b) the summary counts the durable attributes as a fallback when _tcData is gone
    sync_start = _UI_JS.index("function _syncToolCallGroupSummary(group)")
    sync_block = _UI_JS[sync_start:_UI_JS.index("if(durationEl)", sync_start)]
    assert "data-memory-save" in sync_block, "summary must fall back to the durable memory flag"
    assert "data-skill-update" in sync_block, "summary must fall back to the durable skill flag"


def test_durable_flags_match_live_classification():
    """The data-* fallback must classify identically to the live _tcData predicates,
    so a restored row counts the same as a fresh one. Drive both predicates and
    assert the attribute logic mirrors them for the real action vocabularies.
    """
    # add/replace/patch/edit/create/write_file → flagged; remove/delete/remove_file → not
    saved = _run([
        {"name": "memory", "args": {"action": "add"}, "done": True},
        {"name": "memory", "args": {"action": "replace"}, "done": True},
        {"name": "skill_manage", "args": {"action": "create"}, "done": True},
        {"name": "skill_manage", "args": {"action": "write_file"}, "done": True},
    ])
    assert saved == {"mem": 2, "skill": 2}
    excluded = _run([
        {"name": "memory", "args": {"action": "remove"}, "done": True},
        {"name": "skill_manage", "args": {"action": "delete"}, "done": True},
        {"name": "skill_manage", "args": {"action": "remove_file"}, "done": True},
    ])
    assert excluded == {"mem": 0, "skill": 0}
