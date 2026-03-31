     1|# Hermes Co-Work Web UI: Developer and Architecture Guide
     2|
     3|> This document is the canonical reference for anyone (human or agent) working on the
     4|> Hermes Web UI. It covers the exact current state of the code, every design decision and
     5|> quirk discovered during development, and a phased architecture improvement roadmap that
     6|> runs in parallel with the feature roadmap in ROADMAP.md.
     7|>
     8|> Keep this document updated as architecture changes are made.
     9|
    10|---
    11|
    12|## 1. Overview and Purpose
    13|
    14|The Hermes Co-Work Web UI is a lightweight, single-file web application that gives you
    15|a browser-based interface to the Hermes agent that is functionally equivalent to the CLI.
    16|It is modeled on the Claude Co-Work interface: a three-panel layout with a sidebar for
    17|session management, a central chat area, and a right panel for workspace file browsing.
    18|
    19|The design philosophy is deliberately minimal. There is no build step, no bundler, no
    20|frontend framework. Everything ships from a single Python file. This makes the code easy
    21|to modify from a terminal or by an agent, but it creates architectural debt that grows as
    22|the feature set expands.
    23|
    24|---
    25|
    26|## 2. File Inventory
    27|
    28|    <agent-dir>/webui-mvp/
    29|    server.py          Main server file. ~1150 lines. Pure Python.
    30|                       HTTP server, all API handlers, Session model, SSE engine,
    31|                       approval wiring, file upload parser. No inline HTML/CSS/JS.
    32|                       (Phase A+E complete: HTML/CSS/JS all extracted to static/)
    33|    server.py.bak      Backup from a prior iteration. Kept for reference.
    34|    server_new.py      Intermediate ~900-line draft. Superseded by server.py.
    35|                       Safe to delete once Wave 1 begins.
    36|    start.sh           Convenience script: kills running instance, starts server.py
    37|                       via nohup, writes stdout/stderr to /tmp/webui-mvp.log
    38|    AGENTS.md          Instruction file for agents working in this directory.
    39|    ROADMAP.md         Feature and product roadmap document.
    40|    ARCHITECTURE.md    THIS FILE.
    41|
    42|State directory (runtime data, separate from source):
    43|
    44|    ~/.hermes/webui-mvp/
    45|    sessions/          One JSON file per session: {session_id}.json
    46|    test-workspace/    Default empty workspace used during development
    47|
    48|Log file:
    49|
    50|    /tmp/webui-mvp.log   stdout/stderr from the background server process
    51|
    52|---
    53|
    54|## 3. Runtime Environment
    55|
    56|- Python interpreter: <agent-dir>/venv/bin/python
    57|- The venv has all Hermes agent dependencies (run_agent, tools/*, cron/*)
    58|- Server binds to 127.0.0.1:8787 (localhost only, not public internet)
    59|- Access from Mac: SSH tunnel: ssh -N -L 8787:127.0.0.1:8787 <user>@<your-server>
    60|- The server imports Hermes modules via sys.path.insert(0, parent_dir)
    61|
    62|Environment variables controlling behavior:
    63|
    64|    HERMES_WEBUI_HOST              Bind address (default: 127.0.0.1)
    65|    HERMES_WEBUI_PORT              Port (default: 8787)
    66|    HERMES_WEBUI_DEFAULT_WORKSPACE Default workspace path for new sessions
    67|    HERMES_WEBUI_STATE_DIR         Where sessions/ folder lives
    68|    HERMES_CONFIG_PATH             Path to ~/.hermes/config.yaml
    69|    HERMES_WEBUI_DEFAULT_MODEL     Default LLM model string
    70|
    71|Test isolation environment variables (set by conftest.py):
    72|
    73|    HERMES_WEBUI_PORT=8788                           Isolated test port
    74|    HERMES_WEBUI_STATE_DIR=~/.hermes/webui-mvp-test  Isolated test state
    75|    HERMES_WEBUI_DEFAULT_WORKSPACE=.../test-workspace Isolated test workspace
    76|
    77|Tests NEVER talk to the production server (port 8787).
    78|The test state dir is wiped before each test session and deleted after.
    79|See: <repo>/tests/conftest.py
    80|
    81|Per-request environment variables (set by chat handler, restored after):
    82|
    83|    TERMINAL_CWD         Set to session.workspace before running agent.
    84|                         The terminal tool reads this to default cwd.
    85|    HERMES_EXEC_ASK      Set to "1" to enable approval gate for dangerous commands.
    86|    HERMES_SESSION_KEY   Set to session_id. The approval tool keys pending entries
    87|                         by this value, enabling per-session approval state.
    88|
    89|WARNING: These env vars are process-global. Two concurrent chat requests will clobber
    90|each other. This is safe only for single-user, single-concurrent-request use.
    91|See Architecture Phase B for the fix.
    92|
    93|---
    94|
    95|## 4. Server Architecture: Current State
    96|
    97|### 4.1 HTTP Server Layer
    98|
    99|Python stdlib ThreadingHTTPServer (from http.server). Each HTTP request runs in its own
   100|thread. The Handler class subclasses BaseHTTPRequestHandler with two methods:
   101|
   102|    do_GET    Routes: /, /health, /api/session, /api/sessions, /api/list,
   103|                      /api/chat/stream, /api/file, /api/approval/pending
   104|    do_POST   Routes: /api/upload, /api/session/new, /api/session/update,
   105|                      /api/session/delete, /api/chat/start, /api/chat,
   106|                      /api/approval/respond
   107|
   108|Routing is a flat if/elif chain inside each method. No routing framework.
   109|
   110|Helper functions used by all handlers:
   111|
   112|    j(handler, payload, status=200)     Sends JSON response with correct headers
   113|    t(handler, payload, status=200, ct) Sends plain text or HTML response
   114|    read_body(handler)                  Reads and JSON-parses the POST body
   115|
   116|CRITICAL ORDERING RULE in do_POST:
   117|The /api/upload check MUST appear BEFORE calling read_body(). read_body() calls
   118|handler.rfile.read() which consumes the HTTP body stream. The upload handler also
   119|needs rfile (to read the multipart payload). If read_body() runs first on a multipart
   120|request, the upload handler receives an empty body and the upload silently fails.
   121|
   122|### 4.2 Session Model
   123|
   124|Session is a plain Python class (not a dataclass, not SQLAlchemy):
   125|
   126|    Fields:
   127|      session_id    hex string, 12 chars (uuid4().hex[:12])
   128|      title         string, auto-set from first user message
   129|      workspace     absolute path string, resolved at creation
   130|      model         OpenRouter model ID string (e.g. "anthropic/claude-sonnet-4.6")
   131|      messages      list of OpenAI-format message dicts
   132|      created_at    float Unix timestamp
   133|      updated_at    float Unix timestamp, updated on every save()
   134|
   135|    Key methods:
   136|      path (property)  Returns SESSION_DIR/{session_id}.json
   137|      save()           Writes __dict__ as pretty JSON to path, updates updated_at
   138|      load(cls, sid)   Class method: reads JSON from disk, returns Session or None
   139|      compact()        Returns metadata-only dict (no messages) for the session list
   140|
   141|    In-memory cache:
   142|      SESSIONS = {}    dict: session_id -> Session object
   143|      LOCK = threading.Lock()   defined but NOT currently used around SESSIONS access
   144|
   145|    get_session(sid): checks SESSIONS cache, loads from disk on miss, raises KeyError
   146|    new_session(workspace, model): creates Session, caches in SESSIONS, saves, returns
   147|    all_sessions(): scans SESSION_DIR/*.json + SESSIONS, deduplicates, sorts by updated_at,
   148|                    returns list of compact() dicts
   149|
   150|    all_sessions() does a full directory scan on every call.
   151|    With 10 sessions: negligible. With 1000+: will be slow.
   152|    See Architecture Phase C for the index file fix.
   153|
   154|title_from(): takes messages list, finds first user message, returns first 64 chars.
   155|Called after run_conversation() completes to set the session title retroactively.
   156|
   157|### 4.3 SSE Streaming Engine
   158|
   159|This is the most architecturally interesting part. Two endpoints cooperate:
   160|
   161|    POST /api/chat/start     Receives the user message. Creates a queue.Queue, stores it
   162|                             in STREAMS[stream_id], spawns a daemon thread running
   163|                             _run_agent_streaming(), returns {stream_id} immediately.
   164|
   165|    GET  /api/chat/stream    Long-lived SSE connection. Reads from STREAMS[stream_id]
   166|                             and forwards events to the browser until 'done' or 'error'.
   167|
   168|Queue registry:
   169|
   170|    STREAMS = {}               dict: stream_id -> queue.Queue
   171|    STREAMS_LOCK = threading.Lock()
   172|
   173|SSE event types and their data shapes:
   174|
   175|    token       {"text": "..."}                         LLM token delta
   176|    tool        {"name": "...", "preview": "..."}       Tool invocation started
   177|    approval    {"command": "...", "description": "...", "pattern_keys": [...]}
   178|    done        {"session": {compact_fields + messages}} Agent finished successfully
   179|    error       {"message": "...", "trace": "..."}       Agent threw exception
   180|
   181|The SSE handler loop:
   182|    - Blocks on queue.get(timeout=30)
   183|    - On timeout (no events in 30s): sends a heartbeat comment (": heartbeat
   184|
   185|")
   186|      to keep the connection alive through proxies and firewalls
   187|    - On 'done' or 'error' event: breaks the loop and returns
   188|    - Catches BrokenPipeError and ConnectionResetError silently (browser disconnected)
   189|
   190|Stream cleanup: _run_agent_streaming() pops its stream_id from STREAMS in a finally
   191|block. If the browser disconnects mid-stream, the daemon thread runs to completion and
   192|then cleans up. The queue fills and the put_nowait() calls fail silently (queue.Full
   193|is caught).
   194|
   195|Fallback sync endpoint: POST /api/chat still exists and holds the connection open until
   196|the agent finishes. The frontend never uses it but it can be useful for debugging.
   197|
   198|### 4.4 Agent Invocation (_run_agent_streaming)
   199|
   200|    def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id):
   201|
   202|1. Fetches session from SESSIONS (not from disk -- session was just updated by /api/chat/start)
   203|2. Sets TERMINAL_CWD, HERMES_EXEC_ASK, HERMES_SESSION_KEY env vars
   204|3. Creates AIAgent with:
   205|   - model=model, platform='cli', quiet_mode=True
   206|   - enabled_toolsets=CLI_TOOLSETS (from config.yaml or hardcoded default)
   207|   - session_id=session_id
   208|   - stream_delta_callback=on_token (fires per token)
   209|   - tool_progress_callback=on_tool (fires per tool invocation)
   210|4. Calls agent.run_conversation(user_message=msg_text, conversation_history=s.messages,
   211|                                 task_id=session_id)
   212|   NOTE: keyword is task_id NOT session_id (common mistake, documented in skill)
   213|5. On return: updates s.messages, calls title_from(), saves session
   214|6. Puts ('done', {session: ...}) into queue
   215|7. Finally block: restores env vars, pops stream_id from STREAMS
   216|
   217|on_token callback:
   218|    if text is None: return  # end-of-stream sentinel from AIAgent
   219|    put('token', {'text': text})
   220|
   221|on_tool callback:
   222|    put('tool', {'name': name, 'preview': preview})
   223|    # Also immediately surface any pending approval:
   224|    if has_pending(session_id):
   225|        with _lock: p = dict(_pending.get(session_id, {}))
   226|        if p: put('approval', p)
   227|
   228|The approval surface-on-tool logic means approvals appear immediately after the tool
   229|fires (within the same SSE stream), without waiting for the next poll cycle.
   230|
   231|### 4.5 Approval System Integration
   232|
   233|The approval system uses the existing Hermes gateway module at tools/approval.py.
   234|All state lives in module-level variables in that file:
   235|
   236|    _pending = {}        dict: session_key -> pending_entry_dict
   237|    _lock = Lock()       protects _pending
   238|    _permanent_approved  set of permanently approved pattern keys
   239|
   240|Because server.py imports tools.approval at module load time and everything runs in the
   241|same process, this state IS shared between HTTP threads and agent daemon threads.
   242|
   243|Important: this only works because Python imports are cached (sys.modules). The same
   244|module object is used everywhere. If the approval module were ever imported in a subprocess
   245|or via importlib.reload(), this would break.
   246|
   247|GET /api/approval/pending:
   248|    - Peeks at _pending[sid] without removing it
   249|    - Returns {pending: entry} or {pending: null}
   250|    - Called by the browser every 1500ms while S.busy is true (polling fallback)
   251|
   252|POST /api/approval/respond:
   253|    - Pops _pending[sid] (removes it)
   254|    - For choice "once" or "session": calls approve_session(sid, pattern_key) for each key
   255|    - For choice "always": calls approve_session + approve_permanent + save_permanent_allowlist
   256|    - For choice "deny": just pops, does nothing (agent gets denied result)
   257|    - Returns {ok: true, choice: choice}
   258|
   259|### 4.6 File Upload Parser
   260|
   261|parse_multipart(rfile, content_type, content_length):
   262|    - Reads all content_length bytes from rfile into memory (up to MAX_UPLOAD_BYTES = 20MB)
   263|    - Extracts boundary from Content-Type header
   264|    - Splits raw bytes on b'--' + boundary
   265|    - For each part: parses MIME headers via email.parser.HeaderParser
   266|    - Returns (fields, files) where fields is {name: value} and files is {name: (filename, bytes)}
   267|
   268|handle_upload(handler):
   269|    - Calls parse_multipart()
   270|    - Validates: file field present, filename present, session exists
   271|    - Sanitizes filename: replaces non-word chars with _, truncates to 200 chars
   272|    - Writes bytes to session.workspace / safe_name
   273|    - Returns {filename, path, size}
   274|
   275|Why not cgi.FieldStorage:
   276|    - Deprecated in Python 3.11+
   277|    - Broken for binary files (silently corrupts or throws)
   278|    - The manual parser handles all file types correctly
   279|
   280|### 4.7 File System Operations
   281|
   282|safe_resolve(root, requested):
   283|    - Resolves requested path relative to root
   284|    - Calls .relative_to(root) to assert the result is inside root
   285|    - Raises ValueError on path traversal (../../etc/passwd)
   286|
   287|list_dir(workspace, rel='.'):
   288|    - Calls safe_resolve, then iterdir()
   289|    - Sorts: directories first, then files, case-insensitive alpha within each group
   290|    - Returns up to 200 entries with {name, path, type, size}
   291|
   292|read_file_content(workspace, rel):
   293|    - Calls safe_resolve
   294|    - Enforces MAX_FILE_BYTES = 200KB size limit
   295|    - Reads as UTF-8 with errors='replace' (binary files show replacement chars)
   296|    - Returns {path, content, size, lines}
   297|
   298|---
   299|
   300|## 5. Frontend Architecture: Current State
   301|
   302|### 5.1 Structure
   303|
   304|The entire frontend is ~750 lines inside the HTML Python raw string.
   305|Structure: <head> with CSS only (no external stylesheets), <body> with three-panel layout,
   306|<script> with all JavaScript (no external libraries).
   307|
   308|Three-panel layout:
   309|
   310|    <aside class="sidebar">    Left panel: session list, model selector, workspace path
   311|    <main class="main">        Center: topbar, messages area, approval card, composer
   312|    <aside class="rightpanel"> Right panel: workspace file tree and file preview
   313|
   314|### 5.2 Global State
   315|
   316|    const S = {
   317|      session:      null,   // current Session compact dict (includes model, workspace, title)
   318|      messages:     [],     // full messages array for current session
   319|      entries:      [],     // current directory listing
   320|      busy:         false,  // true while agent is running (disables Send button)
   321|      pendingFiles: []      // File objects queued for upload with next message
   322|    }
   323|
   324|    const INFLIGHT = {}
   325|    // keyed by session_id while a request is in-flight for that session
   326|    // value: {messages: [...snapshot...], uploaded: [...filenames...]}
   327|    // Purpose: if user switches sessions while a request is pending,
   328|    //   switching back shows the in-progress state instead of the saved state
   329|
   330|### 5.3 Key Functions Reference
   331|
   332|Session management:
   333|    newSession()          POST /api/session/new, update S.session, save to localStorage
   334|    loadSession(sid)      GET /api/session?session_id=X, check INFLIGHT first, update S
   335|    deleteSession(sid)    POST /api/session/delete, handle active/inactive cases correctly
   336|    renderSessionList()   GET /api/sessions, rebuild #sessionList DOM
   337|
   338|Chat:
   339|    send()                Main action: upload files, POST /api/chat/start, open EventSource
   340|    uploadPendingFiles()  Upload each file in S.pendingFiles, return filenames array
   341|    appendThinking()      Adds three-dot animation to message list
   342|    removeThinking()      Removes thinking dots (called on first token or on error)
   343|
   344|Rendering:
   345|    renderMessages()      Full rebuild of #msgInner from S.messages
   346|    renderMd(raw)         Homegrown markdown renderer (see 5.4 for known gaps)
   347|    syncTopbar()          Updates topbar title, meta, model chip, workspace chip
   348|    renderTray()          Updates attach tray showing pending files
   349|
   350|Approval:
   351|    showApprovalCard(p)   Shows the approval card with command/description text
   352|    hideApprovalCard()    Hides approval card, clears text
   353|    respondApproval(ch)   POST /api/approval/respond, hide card
   354|    startApprovalPolling  setInterval 1500ms GET /api/approval/pending
   355|    stopApprovalPolling   clearInterval
   356|
   357|UI helpers:
   358|    setStatus(t)          Updates #statusText in composer footer
   359|    setBusy(v)            Sets S.busy, disables/enables Send button, clears status on false
   360|    showToast(msg, ms)    Bottom-center fade toast (default 2800ms)
   361|    autoResize()          Auto-resize #msg textarea up to 200px
   362|
   363|Files:
   364|    loadDir(path)         GET /api/list, rebuild #fileTree
   365|    openFile(path)        GET /api/file, show in #previewArea
   366|
   367|Transcript:
   368|    transcript()          Builds markdown string from S.messages for download
   369|
   370|Boot IIFE:
   371|    localStorage key 'hermes-webui-session' stores last session_id
   372|    On load: try to loadSession(saved), fall back to empty state if missing or fails
   373|    NEVER auto-creates a session on boot
   374|
   375|### 5.4 Markdown Renderer (renderMd)
   376|
   377|A hand-rolled regex chain. Processes in this order:
   378|1. Code blocks (``` lang ... ```) -> <pre><code> with language header
   379|2. Inline code (`...`) -> <code>
   380|3. Bold+italic (***..***) -> <strong><em>
   381|4. Bold (**...**) -> <strong>
   382|5. Italic (*...*) -> <em>
   383|6. Headings (# ## ###) -> <h1> <h2> <h3>
   384|7. Horizontal rules (---+) -> <hr>
   385|8. Blockquotes (> ...) -> <blockquote>
   386|9. Unordered lists (- or * or + at line start) -> <ul><li>
   387|10. Ordered lists (N. at line start) -> <ol><li>
   388|11. Links ([text](https://...)) -> <a href target=_blank>
   389|12. Paragraph wrapping: remaining double-newline-separated blocks -> <p>
   390|
   391|Known gaps:
   392|- Tables: not supported, render as plain text
   393|- Nested lists: single regex pass, multi-level indentation not handled
   394|- Mixed bold+link in same line: may produce garbled output
   395|- Inline HTML: not sanitized (esc() only runs on code content)
   396|
   397|### 5.5 Model Chip Label (Fixed in Sprint 1)
   398|
   399|B3 was resolved in Sprint 1. Current code uses a MODEL_LABELS dict:
   400|
   401|    const MODEL_LABELS = {
   402|      'openai/gpt-5.4-mini': 'GPT-5.4 Mini', 'openai/gpt-4o': 'GPT-4o',
   403|      'openai/o3': 'o3', 'openai/o4-mini': 'o4-mini',
   404|      'anthropic/claude-sonnet-4.6': 'Sonnet 4.6', 'anthropic/claude-sonnet-4-5': 'Sonnet 4.5',
   405|      'anthropic/claude-haiku-3-5': 'Haiku 3.5', 'google/gemini-2.5-pro': 'Gemini 2.5 Pro',
   406|      'deepseek/deepseek-chat-v3-0324': 'DeepSeek V3', 'meta-llama/llama-4-scout': 'Llama 4 Scout',
   407|    };
   408|    $('modelChip').textContent = MODEL_LABELS[m] || (m.split('/').pop() || 'Unknown');
   409|
   410|Fallback: any unlisted model shows its short ID (after the last /) rather than a wrong label.
   411|To add a new model: add an entry to MODEL_LABELS and add an <option> to the <select>.
   412|
   413|### 5.6 Session Delete Rules (from skill)
   414|
   415|These rules are critical. GPT-5.4-mini has repeatedly re-introduced broken versions.
   416|
   417|1. deleteSession() NEVER calls newSession(). Deleting does not create.
   418|2. If deleted session was active AND other sessions exist: load sessions[0] (most recent).
   419|3. If deleted session was active AND no sessions remain: show empty state.
   420|4. If deleted session was not active: just re-render the list.
   421|5. Always show toast("Conversation deleted") after any delete.
   422|
   423|### 5.7 Send() Session Guard
   424|
   425|Before any async operations in send():
   426|    const activeSid = S.session.session_id;
   427|
   428|After the agent completes:
   429|    if (S.session && S.session.session_id === activeSid) {
   430|      // apply result, re-render
   431|      setBusy(false);
   432|    } else {
   433|      // user switched sessions mid-flight
   434|      // only refresh sidebar, do NOT call setBusy(false) on the new session
   435|      await renderSessionList();
   436|    }
   437|
   438|This prevents a session switch mid-flight from either clobbering the new session's state
   439|or unlocking the Send button on the wrong session.
   440|
   441|---
   442|
   443|## 6. Data Flow: Full Chat Round Trip
   444|
   445|Step-by-step trace of what happens when you type a message and press Send:
   446|
   447|1.  User types, presses Enter. send() is called.
   448|2.  Guard: return if (!text && !pendingFiles) || S.busy
   449|3.  If S.session is null: await newSession(), await renderSessionList()
   450|4.  Capture activeSid = S.session.session_id (before any awaits)
   451|5.  uploadPendingFiles(): POST each file in S.pendingFiles to /api/upload
   452|    - Shows upload progress bar
   453|    - Clears S.pendingFiles on completion
   454|    - Returns array of uploaded filenames
   455|6.  Build msgText from text + file note
   456|7.  Build userMsg {role:'user', content: displayText, attachments?: filenames}
   457|8.  Push userMsg to S.messages, call renderMessages(), appendThinking()
   458|9.  setBusy(true), setStatus('Hermes is thinking...')
   459|10. INFLIGHT[activeSid] = {messages: [...S.messages], uploaded}
   460|11. startApprovalPolling(activeSid)
   461|12. POST /api/chat/start {session_id, message, model, workspace}
   462|    Server: saves session, creates queue.Queue, starts daemon thread, returns {stream_id}
   463|13. Browser opens EventSource('/api/chat/stream?stream_id=X')
   464|14. In the SSE loop:
   465|    - 'token': assistantText += d.text, ensureAssistantRow(), render markdown
   466|    - 'tool': setStatus('tool name...')
   467|    - 'approval': showApprovalCard(d)
   468|    - 'done': sync S from d.session, renderMessages(), loadDir, renderSessionList,
   469|               setBusy(false), delete INFLIGHT[activeSid]
   470|    - 'error': show error message, setBusy(false)
   471|    - es.onerror: handle network drops (show error, setBusy(false))
   472|15. If approval needed: user clicks a button, respondApproval() fires
   473|    POST /api/approval/respond -> server pops _pending, calls approve_*
   474|    Agent retries the command (now is_approved() returns True) and continues
   475|
   476|---
   477|
   478|## 7. Dependency Map
   479|
   480|Direct imports in server.py:
   481|
   482|    run_agent.AIAgent              Main agent class. Wraps LLM + tool execution.
   483|    tools.approval.*               Module-level approval state.
   484|    yaml                           Config loading.
   485|    Standard library: json, os, re, sys, threading, time, traceback, uuid,
   486|                      http.server, pathlib, urllib.parse, email.parser, queue
   487|
   488|AIAgent constructor parameters used:
   489|
   490|    model=               OpenRouter model ID string
   491|    platform='cli'       Sets the platform context for tool selection
   492|    quiet_mode=True      Suppresses agent's own stdout output
   493|    enabled_toolsets=    List of toolset names from config.yaml
   494|    session_id=          Used for tool state keying (memory, todos, etc.)
   495|    stream_delta_callback=   Called per token delta (or None as sentinel)
   496|    tool_progress_callback=  Called per tool invocation (name, preview, args)
   497|
   498|AIAgent.run_conversation() parameters:
   499|
   500|    user_message=           The human turn text
   501|