from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(source: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-e", source], cwd=ROOT, text=True, capture_output=True, timeout=20
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_token_detection_ranking_dedupe_and_payload_semantics():
    result = _run_node(
        r"""
const p=require('./static/plugin_mentions.js');
const plugins=[
  {name:'Research Tools',path:'plugin://research',description:'Search journals',keywords:['papers']},
  {name:'Paper Helper',path:'plugin://paper',description:'Research assistant',keywords:['citations']},
  {name:'Other',path:'plugin://other',description:'Misc',keywords:['research']},
  {name:'research-z',path:'plugin://z',description:'',keywords:[]}
];
let mentions=p.addMention([],plugins[0]);
mentions=p.addMention(mentions,{name:'Renamed',path:'plugin://research'});
const plain={session_id:'s',message:'hello'};
console.log(JSON.stringify({
  start:p.activeToken('@res',4),
  spaced:p.activeToken('ask @res now',8),
  embedded:p.activeToken('mail@res',8),
  afterWhitespace:p.activeToken('x\n@res',6),
  ranked:p.rankPlugins(plugins,'res').map(x=>x.path),
  mentions,
  removed:p.removeToken('ask @res now',{start:4,end:8}),
  plain:p.withPayload(plain,[]),
  enriched:p.withPayload(plain,mentions),
  retainedOnFailure:p.afterSend(mentions,mentions,false),
  retainedNewOnSuccess:p.afterSend(mentions.concat({name:'New',path:'plugin://new'}),mentions,true),
  unchanged:!Object.prototype.hasOwnProperty.call(plain,'plugin_mentions')
}));
"""
    )
    assert result["start"] == {"start": 0, "end": 4, "query": "res"}
    assert result["spaced"] == {"start": 4, "end": 8, "query": "res"}
    assert result["embedded"] is None
    assert result["afterWhitespace"] == {"start": 2, "end": 6, "query": "res"}
    assert result["ranked"] == ["plugin://research", "plugin://z", "plugin://other", "plugin://paper"]
    assert result["mentions"] == [{"name": "Research Tools", "path": "plugin://research"}]
    assert result["removed"] == "ask  now"
    assert result["plain"] == {"session_id": "s", "message": "hello"}
    assert result["enriched"]["plugin_mentions"] == result["mentions"]
    assert result["retainedOnFailure"] == result["mentions"]
    assert result["retainedNewOnSuccess"] == [{"name": "New", "path": "plugin://new"}]
    assert result["unchanged"] is True


def test_integration_wires_accept_clear_after_chat_start_only():
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert "typeof PluginMentions!=='undefined'" in messages
    assert "PluginMentions.withPayload(_plainStartPayload,_submittedPluginMentions)" in messages
    assert "if(_submittedPluginMentions.length&&!_pluginPayloadHelperAvailable)" in messages
    clear = "if(_submittedPluginMentions.length&&typeof clearPluginMentions==='function') clearPluginMentions(_submittedPluginMentions);"
    assert messages.index(clear) > messages.index("const startData=await api('/api/chat/start'")
    assert 'static/plugin_mentions.js?v=__WEBUI_VERSION__' in index
    assert 'role="listbox"' in index


def test_busy_send_keeps_mentions_and_declines_queue_or_steer():
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    guard = "if(_submittedPluginMentions.length){"
    busy = "if(S.busy||compressionRunning){"
    assert messages.index(guard, messages.index(busy)) < messages.index("defaultMessageMode", messages.index(busy))
    assert "Plugin mentions cannot be queued or steered" in messages


def test_plugin_popup_handles_ime_and_excludes_slash_popup():
    plugin_js = (ROOT / "static/plugin_mentions.js").read_text(encoding="utf-8")
    boot_js = (ROOT / "static/boot.js").read_text(encoding="utf-8")
    assert "event.isComposing||event.inputType==='insertCompositionText'" in plugin_js
    assert "if(event.isComposing||event.keyCode===229)return false" in plugin_js
    assert "root.hideCmdDropdown" in plugin_js
    assert "updatePluginMentionAutocomplete(event)" in boot_js
