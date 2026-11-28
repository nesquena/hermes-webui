"""Stored Steer attachment identities must survive punctuation and reload."""
import json

import pytest

from tests.test_8559_steer_row_review import _node, _sources


@pytest.mark.parametrize("paths", [
    ["/data/Smith, Jane/uploads/report.pdf"],
    ["/data/Smith, Jane/uploads/first, draft.pdf", "/data/Smith, Jane/uploads/second.txt"],
    ["/data/comma,/uploads/file.txt"],
    ["/data/with]bracket/quote\".txt"],
    ["/data/line\nbreak/file.txt"],
    [r"C:\Users\Smith, Jane\uploads\report.pdf"],
    ["/data/comma, /nested/file.txt"],
])
def test_real_upload_to_saved_row_keeps_exact_paths(paths):
    upload, signature, pending, display = _sources()
    _node("\n".join([upload, signature, pending, display, r"""
const assert=require('node:assert/strict');
const t=x=>x, setComposerStatus=()=>{}, S={session:{session_id:'sid'}};
const li=()=>'', esc=x=>x, _getCachedRender=x=>x;
const _renderAttachmentHtml=(label,url)=>JSON.stringify({label,url});
""", f"const paths={json.dumps(paths)};", r"""
const uploadPendingFiles=async()=>paths.map((path,i)=>({path,name:'file-'+i}));
const watchdog=setTimeout(()=>{console.error('roundtrip stalled');process.exit(1);},5000);
(async()=>{
  let live;
  const wire=await _steerTextWithPendingFiles('choose B','sid',[{name:'upload',size:1}],files=>{live=files;});
  const persisted=JSON.parse(JSON.stringify({role:'user',display_kind:'steer',content:wire}));
  assert.equal(_isSteerDisplayMessage(persisted),true);
  const stored=_steerDisplayParts(persisted.content);
  assert.equal(stored.text,'choose B');
  assert.deepEqual(stored.files,paths,'saved attachment identities must equal the real upload destinations');
  for(const files of [live,stored.files]){
    const html=_steerRowInnerHtml('choose B',files,'','sid');
    for(const path of paths)assert.ok(html.includes('&path='+encodeURIComponent(path)),html);
  }
  const wrapped='[OUT-OF-BAND USER MESSAGE]\n'+wire+'\n[/OUT-OF-BAND USER MESSAGE]';
  assert.deepEqual(_steerDisplayParts(wrapped),stored);
  clearTimeout(watchdog);
})().catch(e=>{clearTimeout(watchdog);console.error(e);process.exitCode=1;});
"""]))


@pytest.mark.parametrize("paths", [
    ["/data/Smith, Jane/uploads/report.pdf"],
    ["/data/Smith, Jane/uploads/first.pdf", "/data/Smith, Jane/uploads/second.txt"],
    [r"C:\Users\Smith, Jane\uploads\first.pdf", r"C:\uploads\second.txt"],
])
def test_legacy_saved_paths_keep_internal_commas(paths):
    _, _, _, display = _sources()
    content="choose B\n\n[Attached files for this steer: " + ", ".join(paths) + "]\nUse the file tools/read_file to inspect these documents if needed."
    _node("\n".join([display, "const assert=require('node:assert/strict');", f"assert.deepEqual(_steerDisplayParts({json.dumps(content)}),{{text:'choose B',files:{json.dumps(paths)}}});"]))


@pytest.mark.parametrize("payload", ['[broken]', '[1]', '[""]'])
def test_malformed_array_is_not_guessed_into_a_link(payload):
    _, _, _, display = _sources()
    content='choose B\n\n[Attached files for this steer: '+payload+']\nUse the file tools/read_file to inspect these documents if needed.'
    _node("\n".join([display, "const assert=require('node:assert/strict');", f"assert.deepEqual(_steerDisplayParts({json.dumps(content)}),{{text:{json.dumps(content)},files:[]}});"]))
