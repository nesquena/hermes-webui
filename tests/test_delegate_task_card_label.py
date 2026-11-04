"""delegate_task cards name their work instead of a bare "Delegated a task"."""
import json
import re

from tests.test_anchor_fallback_ownership import _function_source, _run_node_script, _ui_js

UI = _ui_js()
STUBS = """const esc=s=>s; const _toolI18n=(k,f,...a)=>f(...a); const _redactToolTargetLabel=s=>s;
const _decodeToolLabelEntities=s=>s; const _toolDisplayName=tc=>tc.name; const _toolPathBasename=s=>s;
const _toolQueryTitle=s=>s; const _toolReadRangeLabel=()=>'';
"""
NAMES = ["_toolActionKind", "_toolTargetLabel", "_toolVisibleTargetLabel",
         "_toolActionLabelText", "_shortToolLabel", "_toolDelegateTargetLabel"]


def _labels(cards):
    src = STUBS + "\n".join(_function_source(UI, n) for n in NAMES)
    src += f"\nconsole.log(JSON.stringify({json.dumps(cards)}.map(tc=>_toolActionLabelText(tc,{{limit:112}}))));"
    return json.loads(_run_node_script(src))


def test_delegate_task_cards_name_their_work():
    batch = {"name": "delegate_task", "done": True, "snippet":
             '{"status": "dispatched", "mode": "background", "count": 64, "delegation_id": "d"}',
             "args": {"tasks": "[{'goal': 'hx11 wave1 unit binance: type ts/src/binance.ts', '..."}}
    listing = {"name": "delegate_task", "done": True, "snippet": '{"action": "list", "count": 0}',
               "args": {"tasks": "[{'goal': 'WAVE template', 'context': 'x'}]", "action": "list"}}
    single = {"name": "delegate_task", "done": False, "args": {"goal": "Fix the sidebar leak"}}
    labels = _labels([batch, listing, single])
    assert labels == ["Delegated 64 subagents", "Delegated subagents (list)", "Delegating Fix the sidebar leak"]
    assert not any(re.fullmatch(r"Delegat\w+ a task", label) for label in labels)
