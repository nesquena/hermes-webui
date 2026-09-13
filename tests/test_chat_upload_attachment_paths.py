"""Regression coverage for WebUI chat upload path handoff."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
UPLOAD_PY = ROOT / "api" / "upload.py"


def test_browser_submits_raw_text_and_structured_uploads():
    """Execute formatting, admission and the production request expression in Node."""
    import json
    import subprocess

    src = MESSAGES_JS.read_text(encoding="utf-8")
    formatting = src[src.index('  const uploadedNames='):src.index('  // Composer textarea + persisted draft were already')]
    start = src.index("JSON.stringify({", src.index("const startData=await api('/api/chat/start'"))
    end = src.index('})});', start) + 2
    request = src[start:end]
    script = '''
const uploaded=[{name:'photo.jpg',path:'/uploads/photo.jpg',mime:'image/jpeg',size:12,is_image:true}];
const activeSid='session';
const S={session:{workspace:'/workspace'},activeProfile:'default'};
const _modelState={model:'model',model_provider:'provider'};
const _explicitPick=false, _pendingMoaConfig=null, _forcedSkillDirectivePending=null;
const statuses=[];
function setComposerStatus(text){statuses.push(text);}
async function build(text){
''' + formatting + '\nreturn ' + request + ';\n}\n' + '''
(async()=>{
const requests=await Promise.all(['Describe this',''].map(async text=>{
  const body=await build(text); return body ? JSON.parse(body) : null;
}));
uploaded.length=0;
const empty=await build('');
process.stdout.write(JSON.stringify({requests,statuses,emptyRejected:empty===undefined}));
})();
'''
    result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True,
                            capture_output=True, check=True)
    output = json.loads(result.stdout)
    requests = output['requests']
    assert all(body is not None for body in requests), output
    assert output['statuses'] == ['Nothing to send']
    assert output['emptyRejected'] is True
    assert [body['message'] for body in requests] == ['Describe this', '']
    for body in requests:
        assert body['attachments'] == [dict(name='photo.jpg', path='/uploads/photo.jpg',
                                           mime='image/jpeg', size=12, is_image=True)]


def test_attachment_only_text_matches_optimistic_display_and_title(tmp_path, monkeypatch):
    """The stored turn must reconcile with the filename-only optimistic row."""
    import json
    import subprocess
    from api.models import title_from
    from api.upload import build_chat_attachment_message

    monkeypatch.setenv('HERMES_WEBUI_ATTACHMENT_DIR', str(tmp_path))
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'image fixture')
    canonical = build_chat_attachment_message('', [{'name': path.name, 'path': str(path)}], str(tmp_path))
    functions = []
    for filename, name in [('ui.js', '_stripAttachedFilesMarkerForDisplay'),
                           ('sessions.js', '_stripAttachedFilesMarker'),
                           ('sessions.js', '_stripForcedSkillEnvelope'),
                           ('sessions.js', '_normalizeUserTranscriptText')]:
        src = (ROOT / 'static' / filename).read_text(encoding='utf-8')
        start = src.index(f'function {name}(')
        end = src.index('\n}', start) + 2
        functions.append(src[start:end])
    script = '\n'.join(functions) + '\nconst canonical=' + json.dumps(canonical) + ';\n' + '''
process.stdout.write(JSON.stringify({
 display:_stripAttachedFilesMarkerForDisplay(canonical),
 reconciles:_normalizeUserTranscriptText(canonical)===_normalizeUserTranscriptText('Uploaded: photo.jpg')
}));
'''
    result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True,
                            capture_output=True, check=True)
    observed = json.loads(result.stdout)
    assert observed == {'display': 'Uploaded: photo.jpg', 'reconciles': True}
    assert title_from([{'role': 'user', 'content': canonical}]) == 'Uploaded: photo.jpg'


def test_marker_punctuation_in_path_stays_hidden_from_display():
    """A valid filename must not leak the model-only suffix into the transcript."""
    import json
    import subprocess

    from api.models import title_from
    from api.streaming import _strip_title_attachment_suffix

    text = 'Describe this\n\n[Attached files: /uploads/photo [1].jpg]'
    assert title_from([{'role': 'user', 'content': text}]) == 'Describe this'
    assert _strip_title_attachment_suffix(text) == 'Describe this'
    for filename, function in [('ui.js', '_stripAttachedFilesMarkerForDisplay'),
                               ('sessions.js', '_stripAttachedFilesMarker')]:
        src = (ROOT / 'static' / filename).read_text(encoding='utf-8')
        start = src.index(f'function {function}(')
        end = src.index('\n}', start) + 2
        script = src[start:end] + f'''
const paths=['/uploads/résumé 1.jpg','/uploads/photo [1].jpg'];
process.stdout.write(JSON.stringify(paths.map(path =>
  {function}('Describe this\\n\\n[Attached files: '+path+']'))));
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True,
                                capture_output=True, check=True)
        assert json.loads(result.stdout) == ['Describe this', 'Describe this']


def test_attached_files_context_is_hidden_from_user_message_display():
    """Persist full attachment paths for the agent without showing them in chat."""
    ui_src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "function _stripAttachedFilesMarkerForDisplay" in ui_src
    assert "_stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(content))" in ui_src
    assert "const newRawText=String(displayContent).trim();" in ui_src
    assert "row.dataset.rawText=newRawText;" in ui_src


def test_attached_files_context_is_hidden_from_sidebar_titles():
    """Sidebar rows should not expose absolute uploaded image paths in titles."""
    sessions_src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "function _stripAttachedFilesMarker" in sessions_src
    assert "? _stripAttachedFilesMarker" in sessions_src


def test_server_provisional_titles_strip_attached_files_context():
    """Server-generated provisional titles must not include the path suffix."""
    from api.models import title_from

    title = title_from([
        {
            "role": "user",
            "content": "why is llm wiki not working?\n\n[Attached files: /tmp/private/Screenshot.png]",
        }
    ])

    assert title == "why is llm wiki not working?"
    assert "Attached files" not in title
    assert "/tmp/private" not in title


def test_duplicate_upload_response_reports_actual_stored_filename(tmp_path, monkeypatch):
    """Duplicate upload names should report the suffixed stored basename."""
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path))

    from api.upload import _sanitize_upload_name, _upload_destination

    safe_name = _sanitize_upload_name("photo.png")
    first = _upload_destination("session-a", safe_name)
    first.write_bytes(b"first")
    second = _upload_destination("session-a", safe_name)

    assert first.name == "photo.png"
    assert second.name == "photo-1.png"

    src = UPLOAD_PY.read_text(encoding="utf-8")
    handle_body = src[src.index("def handle_upload"):src.index("def extract_archive", src.index("def handle_upload"))]
    assert "'filename': dest.name" in handle_body
    assert "'filename': safe_name" not in handle_body
