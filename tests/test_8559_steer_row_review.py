"""Executable review regressions for request-owned Steer uploads and stored rows."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _between(src, start, end):
    a = src.index(start)
    return src[a:src.index(end, a)]


def _node(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, cwd=ROOT, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def _sources():
    commands = (ROOT / "static/commands.js").read_text()
    ui = (ROOT / "static/ui.js").read_text()
    upload_paths = _between(commands, "function _steerUploadedAttachmentPaths", "\nfunction _steerFilesSignature")
    # The intervening ownership helpers are function declarations, not invoked
    # until the test calls _steerTextWithPendingFiles below.
    pending = _between(commands, "async function _steerTextWithPendingFiles", "\nasync function _trySteer")
    signature = _between(commands, "function _steerFilesSignature", "\nasync function _steerTextWithPendingFiles")
    display = _between(ui, "const _STEER_OOB_OPEN_RE", "\nfunction setStatus")
    return upload_paths, signature, pending, display


def test_request_owned_destinations_survive_sanitization_collision_partial_and_retry():
    paths, signature, pending, display = _sources()
    _node("\n".join([paths, signature, pending, display, r"""
      const assert = require('node:assert/strict');
      let uploadedCalls = 0;
      let uploads = [
        {path:'/s/report_1.pdf',name:'Original report'},  // 'report.pdf' collided on disk
        {path:'/s/unsafe_name.txt',name:'Sanitized upload'}, // '../unsafe name.txt' was sanitized
      ];
      const staged = [{name:'report.pdf',size:1},{name:'../unsafe name.txt',size:2}];
      const t = x=>x;
      const setComposerStatus = ()=>{};
      const S = {session:{session_id:'sid'}};
      const uploadPendingFiles = async ({sessionId,files,clearPending}) => {
        assert.equal(sessionId,'sid');
        assert.equal(clearPending,false);
        assert.deepEqual(files,staged);
        uploadedCalls++;
        return uploads;
      };
      const _renderAttachmentHtml = (name,url)=>`<a href="${url}">${name}</a>`;
      const _getCachedRender = x=>x;
      const li = ()=>'';
      const esc = x=>x;
      (async()=>{
        let live = null;
        const enriched = await _steerTextWithPendingFiles('choose B','sid',staged,p=>{live=p;});
        assert.deepEqual(live,[{path:'/s/report_1.pdf',name:'Original report'},
                               {path:'/s/unsafe_name.txt',name:'Sanitized upload'}]);
        assert.match(enriched,/report_1.pdf, \/s\/unsafe_name.txt/);
        const html = _steerRowInnerHtml('choose B',live,'','sid');
        assert.match(html,/path=report_1.pdf/);
        assert.match(html,/path=unsafe_name.txt/);
        assert.match(html,/>Original report<|>Sanitized upload</);
        assert.doesNotMatch(html,/path=Original|path=Sanitized|path=report.pdf|path=unsafe%20name.txt/);
        const retry = await _steerTextWithPendingFiles('choose B','sid',staged,p=>{live=p;});
        assert.equal(retry,enriched);
        assert.equal(uploadedCalls,1,'retry must use the same upload destinations');
        assert.deepEqual(live,[{path:'/s/report_1.pdf',name:'Original report'},
                               {path:'/s/unsafe_name.txt',name:'Sanitized upload'}]);

        _steerUploadCache=null;
        uploads=[{path:'/s/report_2.pdf'},{name:'unsafe name.txt'}]; // partial upload: a pathless result cannot supply a link
        const partial = await _steerTextWithPendingFiles('choose B','sid',staged,p=>{live=p;});
        assert.deepEqual(live,[{path:'/s/report_2.pdf',name:'report_2.pdf'}]);
        assert.doesNotMatch(partial,/unsafe_name/);
        assert.doesNotMatch(_steerRowInnerHtml('choose B',live,'','sid'),/unsafe_name/);
        _steerUploadCache=null;
        uploads=[];
        live=null;
        const noFiles = await _steerTextWithPendingFiles('choose B','sid',staged,p=>{live=p;});
        assert.equal(noFiles,'choose B');
        assert.equal(live,null);
      })().catch(err=>{console.error(err);process.exitCode=1;});
    """]))


@pytest.mark.parametrize("content,expected_text,expected_files", [
    ("[OUT-OF-BAND USER MESSAGE]\nchoose B\n[/OUT-OF-BAND USER MESSAGE]", "choose B", []),
    ("[OUT-OF-BAND USER MESSAGE]\nouter\n[OUT-OF-BAND USER MESSAGE]\ninner\n[/OUT-OF-BAND USER MESSAGE]", None, []),
    ("[OUT-OF-BAND USER MESSAGE]\na\n[/OUT-OF-BAND USER MESSAGE]\n[/OUT-OF-BAND USER MESSAGE]", None, []),
    ("[OUT-OF-BAND USER MESSAGE]\na\n[/OUT-OF-BAND USER MESSAGE]\ntrailing", None, []),
    ("ordinary\n[/OUT-OF-BAND USER MESSAGE]", None, []),
    ("choose B\n\n[Attached files for this steer: /s/report_1.pdf]\nUse the file tools/read_file to inspect these documents if needed.",
     "choose B", ["/s/report_1.pdf"]),
])
def test_stored_frame_unwrap_only_exactly_one_complete_outer_pair(content, expected_text, expected_files):
    _, _, _, display = _sources()
    expected = content if expected_text is None else expected_text
    _node("\n".join([display, "const assert=require('node:assert/strict');", f"const content={json.dumps(content)};",
                    f"assert.deepEqual(_steerDisplayParts(content),{{text:{json.dumps(expected)},files:{json.dumps(expected_files)}}});",
                    "assert.equal(_isSteerDisplayMessage({role:'user',display_kind:'steer'}),true);",
                    "assert.equal(_isSteerDisplayMessage({role:'user',content}),false);"]))
