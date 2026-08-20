"""Focused served-composer coverage for issue #6927."""

import pytest

from tests.conftest import TEST_BASE


def _page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.goto(TEST_BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg")
    return pw, browser, page


def _close(pw, browser):
    browser.close()
    pw.stop()


def test_issue6927_served_composer_automatically_continues_canonical_cron_reply():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = [];
          const noop = () => {};
          ['renderSessionList','renderSessionListFromCache','renderMessages','renderTray',
           'startApprovalPolling','startClarifyPolling','_fetchYoloState','ensureLiveWorklogShell',
           'clearLiveToolCards','updateSendBtn','autoResize','hideCmdDropdown','removeThinking',
           'clearOptimisticSessionStreaming','clearInflightState','saveInflightState',
           'upsertActiveSessionForLocalTurn','applySessionTitleUpdate'].forEach(k => window[k] = noop);
          window.setBusy = value => { S.busy = !!value; };
          window.uploadPendingFiles = async () => [];
          window.attachLiveStream = noop;
          S.session = {session_id:'cron-source', raw_source:'cron', read_only:true,
            model:'m', model_provider:'p', profile:'prof', workspace:'/w',
            messages:[{role:'assistant', content:'completed'}], context:{lineage:'source'}};
          const sourceSession = S.session;
          const sourceBefore = JSON.stringify(sourceSession);
          S.messages = [{role:'assistant', content:'completed'}];
          $('msg').value = 'continue this';
          window.api = async (url, opts) => {
            calls.push({url, opts});
            if (url === '/api/session/branch') return {session_id:'child-6927'};
            if (url === '/api/session/draft') return {};
            if (url === '/api/chat/start') return {stream_id:'stream-6927'};
            throw new Error('unexpected request ' + url);
          };
          window.loadSession = async sid => {
            _loadSessionGeneration += 1;
            S.session = {session_id:sid, session_source:'fork', read_only:false,
              parent_session_id:'cron-source', model:'m', model_provider:'p', profile:'prof', workspace:'/w',
              messages:[{role:'assistant', content:'completed'}], context:{lineage:'source'},
              composer_draft:{text:'continue this', files:[]}};
            _loadingSessionId = null;
          };
          await send();
          const startCall = calls.find(c => c.url === '/api/chat/start');
          const startBody = JSON.parse(startCall.opts.body);
          return {urls:calls.map(c => c.url), child:S.session.session_id,
            start:calls.find(c => c.url === '/api/chat/start')?.opts,
            startBody, branchBody:JSON.parse(calls.find(c => c.url === '/api/session/branch').opts.body),
            sourceUnchanged:JSON.stringify(sourceSession) === sourceBefore,
            userCount:S.messages.filter(m => m.role === 'user').length, text:$('msg').value};
        }""")
        assert result["urls"] == ["/api/session/branch", "/api/session/draft", "/api/chat/start", "/api/session/draft"]
        assert result["child"] == "child-6927"
        assert result["start"].get("retries") == 0
        assert result["branchBody"] == {"session_id": "cron-source"}
        assert result["startBody"]["session_id"] == "child-6927"
        assert result["startBody"]["message"] == "continue this"
        assert result["startBody"]["workspace"] == "/w"
        assert result["sourceUnchanged"] is True
        assert result["userCount"] == 1
        assert result["text"] == ""
    finally:
        _close(pw, browser)


def test_non_cron_source_and_id_shape_remain_refused():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = [];
          S.session = {session_id:'cron-looking-id', raw_source:'messaging', session_source:'cron', read_only:true};
          $('msg').value = 'raw source is non-cron';
          window.api = async url => { calls.push(url); return {}; };
          await send();
          return {calls, text:$('msg').value};
        }""")
        assert result == {"calls": [], "text": "raw source is non-cron"}
    finally:
        _close(pw, browser)


def test_branch_source_uses_first_present_server_field():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""() => [
          _isBranchableReadOnlySession({read_only:true, source_tag:'messaging', raw_source:'cron'}),
          _isBranchableReadOnlySession({read_only:true, raw_source:'cron'}),
          _isBranchableReadOnlySession({read_only:true, raw_source:'messaging', source:'cron'}),
          _isBranchableReadOnlySession({read_only:true, source:'cron'}),
          _isBranchableReadOnlySession({read_only:true, session_source:'cron', source:'messaging'})
        ]""")
        assert result == [False, True, False, True, False]
    finally:
        _close(pw, browser)


@pytest.mark.parametrize("command", ["/compress", "/retry", "/undo"])
def test_mutating_commands_remain_refused_on_read_only_cron(command):
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async command => {
          const calls = []; const toasts = [];
          S.session = {session_id:'cron-command', raw_source:'cron', read_only:true};
          $('msg').value = command;
          window.queueSessionMessage = () => calls.push('queue');
          window.showToast = message => toasts.push(message);
          window.api = async url => { calls.push(url); return {}; };
          await send();
          return {calls, text:$('msg').value, toast:toasts[0]};
        }""", command)
        assert result["calls"] == []
        assert result["text"] == command
        assert result["toast"] == "Read-only imported sessions cannot be modified."
    finally:
        _close(pw, browser)


def test_child_draft_failure_keeps_complete_payload():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = [];
          const file = new File(['bytes'], 'live.txt');
          S.session = {session_id:'cron-draft-failure', raw_source:'cron', read_only:true};
          S.pendingFiles = [file]; $('msg').value = 'child draft acknowledgement failed';
          window.api = async (url) => { calls.push(url); if (url === '/api/session/branch') return {session_id:'child-failure'}; throw new Error('child draft acknowledgement failed'); };
          await send();
          return {calls, text:$('msg').value, files:S.pendingFiles.length,
            file:S.pendingFiles[0] ? S.pendingFiles[0].name : null};
        }""")
        assert result["calls"] == ["/api/session/branch", "/api/session/draft"]
        assert result["text"] == "child draft acknowledgement failed"
        assert result["file"] == "live.txt"
    finally:
        _close(pw, browser)


def test_handoff_preserves_text_and_live_file_through_child_load():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = []; const uploads = [];
          const file = new File(['bytes'], 'live.txt', {type:'text/plain'});
          const noop = () => {};
          ['renderSessionList','renderSessionListFromCache','renderMessages','renderTray','startApprovalPolling',
           'startClarifyPolling','_fetchYoloState','ensureLiveWorklogShell','clearLiveToolCards','updateSendBtn',
           'autoResize','hideCmdDropdown','removeThinking','clearOptimisticSessionStreaming','clearInflightState',
           'saveInflightState','upsertActiveSessionForLocalTurn','applySessionTitleUpdate'].forEach(k => window[k] = noop);
          window.setBusy = value => { S.busy = !!value; };
          window.uploadPendingFiles = async ({files}) => { uploads.push(files[0].name); return [{name:'live.txt', path:'/live.txt'}]; };
          window.attachLiveStream = noop;
          S.session = {session_id:'cron-files', raw_source:'cron', read_only:true, model:'m', model_provider:'p', workspace:'/w'};
          S.pendingFiles = [file]; $('msg').value = 'reply with file';
          window.api = async (url, opts) => { calls.push(url); if(url === '/api/session/branch') return {session_id:'child-files'}; if(url === '/api/session/draft') return {}; if(url === '/api/chat/start') return {stream_id:'stream-files'}; throw new Error(url); };
          window.loadSession = async sid => { _loadSessionGeneration += 1; S.session = {session_id:sid, read_only:false, composer_draft:{text:'reply with file', files:[]}}; _loadingSessionId=null; };
          await send();
          return {calls, uploads, text:$('msg').value, map:_readOnlyForkPayloads.size};
        }""")
        assert result["calls"] == ["/api/session/branch", "/api/session/draft", "/api/chat/start", "/api/session/draft"]
        assert result["uploads"] == ["live.txt"]
        assert result["map"] == 0
    finally:
        _close(pw, browser)


def test_branch_failure_restores_exact_source_without_sidecar_or_queue():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = []; S.session = {session_id:'cron-branch-failure', raw_source:'cron', read_only:true};
          $('msg').value = 'branch failure before child SID';
          window.queueSessionMessage = () => calls.push('queue');
          window.api = async url => { calls.push(url); throw new Error('branch failure before child SID'); };
          await send();
          return {calls, text:$('msg').value, map:_readOnlyForkPayloads.size};
        }""")
        assert result == {"calls":["/api/session/branch"], "text":"branch failure before child SID", "map":0}
    finally:
        _close(pw, browser)


def test_child_load_failure_keeps_one_child_draft_and_live_file_owner():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = []; const file = new File(['bytes'], 'live.txt');
          S.session = {session_id:'cron-load-failure', raw_source:'cron', read_only:true};
          S.pendingFiles = [file]; $('msg').value = 'child metadata or messages unavailable';
          window.api = async (url) => { calls.push(url); if(url === '/api/session/branch') return {session_id:'child-load-failure'}; if(url === '/api/session/draft') return {}; throw new Error(url); };
          window.loadSession = async sid => { _loadSessionFailureGeneration += 1; _sessionLoadFailureSid = sid; _loadSessionGeneration += 1; S.session = {session_id:sid, read_only:false}; _loadingSessionId=null; };
          await send();
          const record = _readOnlyForkPayloads.get('child-load-failure');
          return {calls, map:_readOnlyForkPayloads.size, state:record && record.state, file:record && record.files[0].name};
        }""")
        assert result == {"calls":["/api/session/branch","/api/session/draft"], "map":1, "state":"recovery", "file":"live.txt"}
    finally:
        _close(pw, browser)


def test_no_stream_id_keeps_child_recovery_and_does_not_clear_draft():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = []; S.session = {session_id:'cron-no-stream', raw_source:'cron', read_only:true}; $('msg').value='no stream commit';
          window.api = async (url) => { calls.push(url); if(url === '/api/session/branch') return {session_id:'child-no-stream'}; if(url === '/api/session/draft') return {}; if(url === '/api/chat/start') return {}; throw new Error(url); };
          window.loadSession = async sid => { _loadSessionGeneration += 1; S.session={session_id:sid, read_only:false, composer_draft:{text:'no stream commit', files:[]}}; _loadingSessionId=null; };
          await send();
          const record = _readOnlyForkPayloads.get('child-no-stream');
          return {calls:calls.filter(url => ['/api/session/branch','/api/session/draft','/api/chat/start'].includes(url)), state:record && record.state, text:$('msg').value, map:_readOnlyForkPayloads.size};
        }""")
        assert result["calls"] == ["/api/session/branch", "/api/session/draft", "/api/chat/start"]
        assert result["state"] == "recovery" and result["map"] == 1
        assert result["text"] == "no stream commit"
    finally:
        _close(pw, browser)


def test_branch_and_chat_start_are_each_attempted_once():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const attempts={branch:0,start:0}; const retries={branch:null,start:null};
          S.session={session_id:'cron-once', raw_source:'cron', read_only:true}; $('msg').value='once';
          const originalApi=window.api;
          window.api=async (url, opts) => {
            if(url==='/api/session/branch') retries.branch=opts.retries;
            if(url==='/api/chat/start') retries.start=opts.retries;
            return originalApi(url, opts);
          };
          window.fetch=async (url) => {
            if(String(url).includes('/api/session/branch')) { attempts.branch++; return new Response(JSON.stringify({session_id:'child-once'}), {status:200, headers:{'Content-Type':'application/json'}}); }
            if(String(url).includes('/api/session/draft')) return new Response('{}', {status:200, headers:{'Content-Type':'application/json'}});
            if(String(url).includes('/api/chat/start')) { attempts.start++; throw new TypeError('network'); }
            return new Response('{}', {status:200, headers:{'Content-Type':'application/json'}});
          };
          window.loadSession=async sid=>{_loadSessionGeneration+=1; S.session={session_id:sid,read_only:false,composer_draft:{text:'once',files:[]}}; _loadingSessionId=null;};
          await send();
          const record=_readOnlyForkPayloads.get('child-once');
          return {attempts,retries,state:record&&record.state};
        }""")
        assert result == {"attempts": {"branch": 1, "start": 1}, "retries": {"branch": 0, "start": 0}, "state": "recovery"}
    finally:
        _close(pw, browser)


def test_handoff_start_failure_restores_payload_and_clears_busy_without_queue_drain():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls=[]; let queueDrains=0; const noop=()=>{};
          ['renderSessionList','renderSessionListFromCache','renderMessages','renderTray','startApprovalPolling',
           'startClarifyPolling','_fetchYoloState','ensureLiveWorklogShell','clearLiveToolCards','autoResize',
           'hideCmdDropdown','removeThinking','clearOptimisticSessionStreaming','clearInflightState',
           'saveInflightState','upsertActiveSessionForLocalTurn','applySessionTitleUpdate'].forEach(k=>window[k]=noop);
          window.setBusy=value=>{S.busy=!!value;}; window.updateSendBtn=()=>{}; window.queueSessionMessage=()=>queueDrains++;
          window.uploadPendingFiles=async()=>[]; window.attachLiveStream=noop;
          S.session={session_id:'cron-start-failure', raw_source:'cron', read_only:true}; $('msg').value='handoff start failure';
          window.api=async url=>{calls.push(url); if(url==='/api/session/branch')return {session_id:'child-start-failure'}; if(url==='/api/session/draft')return {}; if(url==='/api/chat/start')throw new Error('start failed'); throw new Error(url);};
          window.loadSession=async sid=>{_loadSessionGeneration+=1; S.session={session_id:sid,read_only:false,composer_draft:{text:'handoff start failure',files:[]}}; _loadingSessionId=null;};
          await send();
          return {calls, busy:S.busy, queueDrains, text:$('msg').value, map:_readOnlyForkPayloads.size};
        }""")
        assert result["calls"] == ["/api/session/branch", "/api/session/draft", "/api/chat/start"]
        assert result["busy"] is False and result["queueDrains"] == 0
        assert result["text"] == "handoff start failure" and result["map"] == 1
    finally:
        _close(pw, browser)


def test_concurrent_served_handoff_queues_newer_source_input_during_handoff():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls=[]; let release; const gate=new Promise(r=>release=r);
          S.session={session_id:'cron-concurrent', raw_source:'cron', read_only:true}; $('msg').value='first reply';
          const originalQueueSessionMessage=window.queueSessionMessage;
          window.queueSessionMessage=(sid,payload)=>{calls.push('queue'); return originalQueueSessionMessage(sid,payload);};
          window.api=async url=>{calls.push(url); if(url==='/api/session/branch'){await gate;return {session_id:'child-concurrent'};} if(url==='/api/session/draft')return {}; throw new Error(url);};
          const first=send(); await new Promise(r=>setTimeout(r,20)); $('msg').value='newer source input'; _loadSessionGeneration += 1; _loadingSessionId='other-pane'; await send(); release(); await first;
          return {calls:calls.filter(url => ['/api/session/branch','/api/session/draft','queue'].includes(url)), text:$('msg').value, loading:_loadingSessionId, map:_readOnlyForkPayloads.size, queue:_getSessionQueue('cron-concurrent',false).map(entry=>entry.text)};
        }""")
        assert result == {"calls":["/api/session/branch","queue","/api/session/draft"], "text":"", "loading":"other-pane", "map":1, "queue":["newer source input"]}
    finally:
        _close(pw, browser)


def test_concurrent_source_submit_preserves_newer_input_without_source_queue():
    test_concurrent_served_handoff_queues_newer_source_input_during_handoff()


def test_concurrent_handoff_transfers_queued_reply_to_child_after_load():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls=[]; let release; const gate=new Promise(r=>release=r);
          const originalQueueSessionMessage=window.queueSessionMessage;
          window.queueSessionMessage=(sid,payload)=>{calls.push('queue'); return originalQueueSessionMessage(sid,payload);};
          window.setBusy=value=>{S.busy=!!value;}; window.uploadPendingFiles=async()=>[]; window.attachLiveStream=()=>{};
          S.session={session_id:'cron-transfer-source',raw_source:'cron',read_only:true,model:'m',model_provider:'p'};
          $('msg').value='first transfer reply';
          window.api=async url=>{calls.push(url); if(url==='/api/session/branch'){await gate;return {session_id:'child-transfer'};} if(url==='/api/session/draft')return {}; if(url==='/api/chat/start')return {stream_id:'stream-transfer'}; throw new Error(url);};
          window.loadSession=async sid=>{_loadSessionGeneration+=1;S.session={session_id:sid,read_only:false,model:'m',model_provider:'p',composer_draft:{text:'first transfer reply',files:[]}};_loadingSessionId=null;};
          const first=send(); await new Promise(r=>setTimeout(r,20)); $('msg').value='second transfer reply'; await send(); release(); await first;
          return {calls:calls.filter(url=>['/api/session/branch','/api/session/draft','/api/chat/start','queue'].includes(url)),source:_getSessionQueue('cron-transfer-source',false).length,child:_getSessionQueue('child-transfer',false).map(entry=>entry.text),map:_readOnlyForkPayloads.size};
        }""")
        assert result == {"calls":["/api/session/branch","queue","/api/session/draft","queue","/api/chat/start","/api/session/draft"], "source":0, "child":["second transfer reply"], "map":0}
    finally:
        _close(pw, browser)


def test_partial_batch_delete_cannot_block_later_cron_handoff():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const record={sourceSid:'cron-deleted', childSid:'child-after-delete', state:'recovery', text:'stale', files:[]};
          _readOnlyForkPayloads.set(record.sourceSid, record);
          _readOnlyForkPayloads.set(record.childSid, record);
          const batchSource = _renderBatchActionBar.toString();
          _clearHandoffStorageForSession(record.childSid);
          const clearedMap = _readOnlyForkPayloads.size;
          _readOnlyForkPayloads.set(record.childSid, record);
          S.session={session_id:record.childSid, read_only:false, model:'m', workspace:'/w'}; $('msg').value='new writable reply';
          window.uploadPendingFiles=async()=>[]; window.api=async url=>url==='/api/chat/start'?{stream_id:'after-delete'}:{};
          await send();
          return {map:_readOnlyForkPayloads.size, text:$('msg').value, clearedMap,
            batchUsesAllSettled:batchSource.includes('Promise.allSettled'),
            releasesEachSuccess:batchSource.includes('_clearHandoffStorageForSession(sid)')};
        }""")
        assert result == {"map":0, "text":"", "clearedMap":0, "batchUsesAllSettled":True, "releasesEachSuccess":True}
    finally:
        _close(pw, browser)


def test_off_pane_child_draft_save_cannot_clear_handoff_payload():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls=[];
          _readOnlyForkPayloads.set('child-off-pane', {sourceSid:'cron-off-pane', childSid:'child-off-pane', state:'child-draft-owned', sendActive:true, text:'keep me', files:[]});
          S.session={session_id:'child-off-pane', read_only:false};
          window.api=async url=>{calls.push(url); return {};};
          _saveComposerDraft('child-off-pane', '', []);
          await _saveComposerDraftNow('child-off-pane', '', [], {throwOnError:true});
          await new Promise(resolve=>setTimeout(resolve, 500));
          return {calls:calls.filter(url => url === '/api/session/draft'), map:_readOnlyForkPayloads.size};
        }""")
        assert result == {"calls": [], "map": 1}
    finally:
        _close(pw, browser)


def test_ordinary_writable_send_keeps_default_network_retry():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const attempts=[]; const seen=[]; const originalApi=window.api;
          window.api=async (url, opts) => { seen.push({url, retries:opts && opts.retries}); return originalApi(url, opts); };
          window.fetch=async (url, opts) => { if(String(url).includes('/api/chat/start')) { attempts.push(opts); if(attempts.length===1) throw new TypeError('network'); return new Response(JSON.stringify({stream_id:'ordinary-retry'}), {status:200, headers:{'Content-Type':'application/json'}}); } return new Response('{}', {status:200, headers:{'Content-Type':'application/json'}}); };
          S.session={session_id:'writable-retry', read_only:false, model:'m', workspace:'/w'}; $('msg').value='ordinary retry';
          window.uploadPendingFiles=async()=>[]; window.attachLiveStream=()=>{};
          await send();
          return {attempts:attempts.length, retries:seen.find(x=>x.url==='/api/chat/start').retries, text:$('msg').value};
        }""")
        assert result == {"attempts":2, "retries":None, "text":""}
    finally:
        _close(pw, browser)
