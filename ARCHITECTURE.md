# Hermes Co-Work Web UI: Developer and Architecture Guide

> This document is the canonical reference for anyone (human or agent) working on the
> Hermes Web UI. It covers the exact current state of the code, every design decision and
> quirk discovered during development, and a phased architecture improvement roadmap that
> runs in parallel with the feature roadmap in ROADMAP.md.
>
> Keep this document updated as architecture changes are made.

---

## 1. Overview and Purpose

The Hermes Co-Work Web UI is a lightweight, single-file web application that gives you
a browser-based interface to the Hermes agent that is functionally equivalent to the CLI.
It is modeled on the Claude Co-Work interface: a three-panel layout with a sidebar for
session management, a central chat area, and a right panel for workspace file browsing.

The design philosophy is deliberately minimal. There is no build step, no bundler, no
frontend framework. Everything ships from a single Python file. This makes the code easy
to modify from a terminal or by an agent, but it creates architectural debt that grows as
the feature set expands.

---

## 2. File Inventory

    <agent-dir>/webui-mvp/
    server.py          Main server file. ~1150 lines. Pure Python.
                       HTTP server, all API handlers, Session model, SSE engine,
                       approval wiring, file upload parser. No inline HTML/CSS/JS.
                       (Phase A+E complete: HTML/CSS/JS all extracted to static/)
    server.py.bak      Backup from a prior iteration. Kept for reference.
    server_new.py      Intermediate ~900-line draft. Superseded by server.py.
                       Safe to delete once Wave 1 begins.
    start.sh           Convenience script: kills running instance, starts server.py
                       via nohup, writes stdout/stderr to /tmp/webui-mvp.log
    AGENTS.md          Instruction file for agents working in this directory.
    ROADMAP.md         Feature and product roadmap document.
    ARCHITECTURE.md    THIS FILE.

State directory (runtime data, separate from source):

    ~/.hermes/webui-mvp/
    sessions/          One JSON file per session: {session_id}.json
    test-workspace/    Default empty workspace used during development

Log file:

    /tmp/webui-mvp.log   stdout/stderr from the background server process

---

## 3. Runtime Environment

- Python interpreter: <agent-dir>/venv/bin/python
- The venv has all Hermes agent dependencies (run_agent, tools/*, cron/*)
- Server binds to 127.0.0.1:8787 (localhost only, not public internet)
- Access from Mac: SSH tunnel: ssh -N -L 8787:127.0.0.1:8787 <user>@<your-server>
- The server imports Hermes modules via sys.path.insert(0, parent_dir)

Environment variables controlling behavior:

    HERMES_WEBUI_HOST              Bind address (default: 127.0.0.1)
    HERMES_WEBUI_PORT              Port (default: 8787)
    HERMES_WEBUI_DEFAULT_WORKSPACE Default workspace path for new sessions
    HERMES_WEBUI_STATE_DIR         Where sessions/ folder lives
    HERMES_CONFIG_PATH             Path to ~/.hermes/config.yaml
    HERMES_WEBUI_DEFAULT_MODEL     Default LLM model string

Test isolation environment variables (set by conftest.py):

    HERMES_WEBUI_PORT=8788                           Isolated test port
    HERMES_WEBUI_STATE_DIR=~/.hermes/webui-mvp-test  Isolated test state
    HERMES_WEBUI_DEFAULT_WORKSPACE=.../test-workspace Isolated test workspace

Tests NEVER talk to the production server (port 8787).
The test state dir is wiped before each test session and deleted after.
See: <repo>/tests/conftest.py

Per-request environment variables (set by chat handler, restored after):

    TERMINAL_CWD         Set to session.workspace before running agent.
                         The terminal tool reads this to default cwd.
    HERMES_EXEC_ASK      Set to "1" to enable approval gate for dangerous commands.
    HERMES_SESSION_KEY   Set to session_id. The approval tool keys pending entries
                         by this value, enabling per-session approval state.

WARNING: These env vars are process-global. Two concurrent chat requests will clobber
each other. This is safe only for single-user, single-concurrent-request use.
See Architecture Phase B for the fix.

---

## 4. Server Architecture: Current State

### 4.1 HTTP Server Layer

Python stdlib ThreadingHTTPServer (from http.server). Each HTTP request runs in its own
thread. The Handler class subclasses BaseHTTPRequestHandler with two methods:

    do_GET    Routes: /, /health, /api/session, /api/sessions, /api/list,
                      /api/chat/stream, /api/file, /api/approval/pending
    do_POST   Routes: /api/upload, /api/session/new, /api/session/update,
                      /api/session/delete, /api/chat/start, /api/chat,
                      /api/approval/respond

Routing is a flat if/elif chain inside each method. No routing framework.

Helper functions used by all handlers:

    j(handler, payload, status=200)     Sends JSON response with correct headers
    t(handler, payload, status=200, ct) Sends plain text or HTML response
    read_body(handler)                  Reads and JSON-parses the POST body

CRITICAL ORDERING RULE in do_POST:
The /api/upload check MUST appear BEFORE calling read_body(). read_body() calls
handler.rfile.read() which consumes the HTTP body stream. The upload handler also
needs rfile (to read the multipart payload). If read_body() runs first on a multipart
request, the upload handler receives an empty body and the upload silently fails.

### 4.2 Session Model

Session is a plain Python class (not a dataclass, not SQLAlchemy):

    Fields:
      session_id    hex string, 12 chars (uuid4().hex[:12])
      title         string, auto-set from first user message
      workspace     absolute path string, resolved at creation
      model         OpenRouter model ID string (e.g. "anthropic/claude-sonnet-4.6")
      messages      list of OpenAI-format message dicts
      created_at    float Unix timestamp
      updated_at    float Unix timestamp, updated on every save()

    Key methods:
      path (property)  Returns SESSION_DIR/{session_id}.json
      save()           Writes __dict__ as pretty JSON to path, updates updated_at
      load(cls, sid)   Class method: reads JSON from disk, returns Session or None
      compact()        Returns metadata-only dict (no messages) for the session list

    In-memory cache:
      SESSIONS = {}    dict: session_id -> Session object
      LOCK = threading.Lock()   defined but NOT currently used around SESSIONS access

    get_session(sid): checks SESSIONS cache, loads from disk on miss, raises KeyError
    new_session(workspace, model): creates Session, caches in SESSIONS, saves, returns
    all_sessions(): scans SESSION_DIR/*.json + SESSIONS, deduplicates, sorts by updated_at,
                    returns list of compact() dicts

    all_sessions() does a full directory scan on every call.
    With 10 sessions: negligible. With 1000+: will be slow.
    See Architecture Phase C for the index file fix.

title_from(): takes messages list, finds first user message, returns first 64 chars.
Called after run_conversation() completes to set the session title retroactively.

### 4.3 SSE Streaming Engine

This is the most architecturally interesting part. Two endpoints cooperate:

    POST /api/chat/start     Receives the user message. Creates a queue.Queue, stores it
                             in STREAMS[stream_id], spawns a daemon thread running
                             _run_agent_streaming(), returns {stream_id} immediately.

    GET  /api/chat/stream    Long-lived SSE connection. Reads from STREAMS[stream_id]
                             and forwards events to the browser until 'done' or 'error'.

Queue registry:

    STREAMS = {}               dict: stream_id -> queue.Queue
    STREAMS_LOCK = threading.Lock()

SSE event types and their data shapes:

    token       {"text": "..."}                         LLM token delta
    tool        {"name": "...", "preview": "..."}       Tool invocation started
    approval    {"command": "...", "description": "...", "pattern_keys": [...]}
    done        {"session": {compact_fields + messages}} Agent finished successfully
    error       {"message": "...", "trace": "..."}       Agent threw exception

The SSE handler loop:
    - Blocks on queue.get(timeout=30)
    - On timeout (no events in 30s): sends a heartbeat comment (": heartbeat

")
      to keep the connection alive through proxies and firewalls
    - On 'done' or 'error' event: breaks the loop and returns
    - Catches BrokenPipeError and ConnectionResetError silently (browser disconnected)

Stream cleanup: _run_agent_streaming() pops its stream_id from STREAMS in a finally
block. If the browser disconnects mid-stream, the daemon thread runs to completion and
then cleans up. The queue fills and the put_nowait() calls fail silently (queue.Full
is caught).

Fallback sync endpoint: POST /api/chat still exists and holds the connection open until
the agent finishes. The frontend never uses it but it can be useful for debugging.

### 4.4 Agent Invocation (_run_agent_streaming)

    def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id):

1. Fetches session from SESSIONS (not from disk -- session was just updated by /api/chat/start)
2. Sets TERMINAL_CWD, HERMES_EXEC_ASK, HERMES_SESSION_KEY env vars
3. Creates AIAgent with:
   - model=model, platform='cli', quiet_mode=True
   - enabled_toolsets=CLI_TOOLSETS (from config.yaml or hardcoded default)
   - session_id=session_id
   - stream_delta_callback=on_token (fires per token)
   - tool_progress_callback=on_tool (fires per tool invocation)
4. Calls agent.run_conversation(user_message=msg_text, conversation_history=s.messages,
                                 task_id=session_id)
   NOTE: keyword is task_id NOT session_id (common mistake, documented in skill)
5. On return: updates s.messages, calls title_from(), saves session
6. Puts ('done', {session: ...}) into queue
7. Finally block: restores env vars, pops stream_id from STREAMS

on_token callback:
    if text is None: return  # end-of-stream sentinel from AIAgent
    put('token', {'text': text})

on_tool callback:
    put('tool', {'name': name, 'preview': preview})
    # Also immediately surface any pending approval:
    if has_pending(session_id):
        with _lock: p = dict(_pending.get(session_id, {}))
        if p: put('approval', p)

The approval surface-on-tool logic means approvals appear immediately after the tool
fires (within the same SSE stream), without waiting for the next poll cycle.

### 4.5 Approval System Integration

The approval system uses the existing Hermes gateway module at tools/approval.py.
All state lives in module-level variables in that file:

    _pending = {}        dict: session_key -> pending_entry_dict
    _lock = Lock()       protects _pending
    _permanent_approved  set of permanently approved pattern keys

Because server.py imports tools.approval at module load time and everything runs in the
same process, this state IS shared between HTTP threads and agent daemon threads.

Important: this only works because Python imports are cached (sys.modules). The same
module object is used everywhere. If the approval module were ever imported in a subprocess
or via importlib.reload(), this would break.

GET /api/approval/pending:
    - Peeks at _pending[sid] without removing it
    - Returns {pending: entry} or {pending: null}
    - Called by the browser every 1500ms while S.busy is true (polling fallback)

POST /api/approval/respond:
    - Pops _pending[sid] (removes it)
    - For choice "once" or "session": calls approve_session(sid, pattern_key) for each key
    - For choice "always": calls approve_session + approve_permanent + save_permanent_allowlist
    - For choice "deny": just pops, does nothing (agent gets denied result)
    - Returns {ok: true, choice: choice}

### 4.6 File Upload Parser

parse_multipart(rfile, content_type, content_length):
    - Reads all content_length bytes from rfile into memory (up to MAX_UPLOAD_BYTES = 20MB)
    - Extracts boundary from Content-Type header
    - Splits raw bytes on b'--' + boundary
    - For each part: parses MIME headers via email.parser.HeaderParser
    - Returns (fields, files) where fields is {name: value} and files is {name: (filename, bytes)}

handle_upload(handler):
    - Calls parse_multipart()
    - Validates: file field present, filename present, session exists
    - Sanitizes filename: replaces non-word chars with _, truncates to 200 chars
    - Writes bytes to session.workspace / safe_name
    - Returns {filename, path, size}

Why not cgi.FieldStorage:
    - Deprecated in Python 3.11+
    - Broken for binary files (silently corrupts or throws)
    - The manual parser handles all file types correctly

### 4.7 File System Operations

safe_resolve(root, requested):
    - Resolves requested path relative to root
    - Calls .relative_to(root) to assert the result is inside root
    - Raises ValueError on path traversal (../../etc/passwd)

list_dir(workspace, rel='.'):
    - Calls safe_resolve, then iterdir()
    - Sorts: directories first, then files, case-insensitive alpha within each group
    - Returns up to 200 entries with {name, path, type, size}

read_file_content(workspace, rel):
    - Calls safe_resolve
    - Enforces MAX_FILE_BYTES = 200KB size limit
    - Reads as UTF-8 with errors='replace' (binary files show replacement chars)
    - Returns {path, content, size, lines}

---

## 5. Frontend Architecture: Current State

### 5.1 Structure

The entire frontend is ~750 lines inside the HTML Python raw string.
Structure: <head> with CSS only (no external stylesheets), <body> with three-panel layout,
<script> with all JavaScript (no external libraries).

Three-panel layout:

    <aside class="sidebar">    Left panel: session list, model selector, workspace path
    <main class="main">        Center: topbar, messages area, approval card, composer
    <aside class="rightpanel"> Right panel: workspace file tree and file preview

### 5.2 Global State

    const S = {
      session:      null,   // current Session compact dict (includes model, workspace, title)
      messages:     [],     // full messages array for current session
      entries:      [],     // current directory listing
      busy:         false,  // true while agent is running (disables Send button)
      pendingFiles: []      // File objects queued for upload with next message
    }

    const INFLIGHT = {}
    // keyed by session_id while a request is in-flight for that session
    // value: {messages: [...snapshot...], uploaded: [...filenames...]}
    // Purpose: if user switches sessions while a request is pending,
    //   switching back shows the in-progress state instead of the saved state

### 5.3 Key Functions Reference

Session management:
    newSession()          POST /api/session/new, update S.session, save to localStorage
    loadSession(sid)      GET /api/session?session_id=X, check INFLIGHT first, update S
    deleteSession(sid)    POST /api/session/delete, handle active/inactive cases correctly
    renderSessionList()   GET /api/sessions, rebuild #sessionList DOM

Chat:
    send()                Main action: upload files, POST /api/chat/start, open EventSource
    uploadPendingFiles()  Upload each file in S.pendingFiles, return filenames array
    appendThinking()      Adds three-dot animation to message list
    removeThinking()      Removes thinking dots (called on first token or on error)

Rendering:
    renderMessages()      Full rebuild of #msgInner from S.messages
    renderMd(raw)         Homegrown markdown renderer (see 5.4 for known gaps)
    syncTopbar()          Updates topbar title, meta, model chip, workspace chip
    renderTray()          Updates attach tray showing pending files

Approval:
    showApprovalCard(p)   Shows the approval card with command/description text
    hideApprovalCard()    Hides approval card, clears text
    respondApproval(ch)   POST /api/approval/respond, hide card
    startApprovalPolling  setInterval 1500ms GET /api/approval/pending
    stopApprovalPolling   clearInterval

UI helpers:
    setStatus(t)          Updates #statusText in composer footer
    setBusy(v)            Sets S.busy, disables/enables Send button, clears status on false
    showToast(msg, ms)    Bottom-center fade toast (default 2800ms)
    autoResize()          Auto-resize #msg textarea up to 200px

Files:
    loadDir(path)         GET /api/list, rebuild #fileTree
    openFile(path)        GET /api/file, show in #previewArea

Transcript:
    transcript()          Builds markdown string from S.messages for download

Boot IIFE:
    localStorage key 'hermes-webui-session' stores last session_id
    On load: try to loadSession(saved), fall back to empty state if missing or fails
    NEVER auto-creates a session on boot

### 5.4 Markdown Renderer (renderMd)

A hand-rolled regex chain. Processes in this order:
1. Code blocks (``` lang ... ```) -> <pre><code> with language header
2. Inline code (`...`) -> <code>
3. Bold+italic (***..***) -> <strong><em>
4. Bold (**...**) -> <strong>
5. Italic (*...*) -> <em>
6. Headings (# ## ###) -> <h1> <h2> <h3>
7. Horizontal rules (---+) -> <hr>
8. Blockquotes (> ...) -> <blockquote>
9. Unordered lists (- or * or + at line start) -> <ul><li>
10. Ordered lists (N. at line start) -> <ol><li>
11. Links ([text](https://...)) -> <a href target=_blank>
12. Paragraph wrapping: remaining double-newline-separated blocks -> <p>

Known gaps:
- Tables: not supported, render as plain text
- Nested lists: single regex pass, multi-level indentation not handled
- Mixed bold+link in same line: may produce garbled output
- Inline HTML: not sanitized (esc() only runs on code content)

### 5.5 Model Chip Label (Fixed in Sprint 1)

B3 was resolved in Sprint 1. Current code uses a MODEL_LABELS dict:

    const MODEL_LABELS = {
      'openai/gpt-5.4-mini': 'GPT-5.4 Mini', 'openai/gpt-4o': 'GPT-4o',
      'openai/o3': 'o3', 'openai/o4-mini': 'o4-mini',
      'anthropic/claude-sonnet-4.6': 'Sonnet 4.6', 'anthropic/claude-sonnet-4-5': 'Sonnet 4.5',
      'anthropic/claude-haiku-3-5': 'Haiku 3.5', 'google/gemini-2.5-pro': 'Gemini 2.5 Pro',
      'deepseek/deepseek-chat-v3-0324': 'DeepSeek V3', 'meta-llama/llama-4-scout': 'Llama 4 Scout',
    };
    $('modelChip').textContent = MODEL_LABELS[m] || (m.split('/').pop() || 'Unknown');

Fallback: any unlisted model shows its short ID (after the last /) rather than a wrong label.
To add a new model: add an entry to MODEL_LABELS and add an <option> to the <select>.

### 5.6 Session Delete Rules (from skill)

These rules are critical. GPT-5.4-mini has repeatedly re-introduced broken versions.

1. deleteSession() NEVER calls newSession(). Deleting does not create.
2. If deleted session was active AND other sessions exist: load sessions[0] (most recent).
3. If deleted session was active AND no sessions remain: show empty state.
4. If deleted session was not active: just re-render the list.
5. Always show toast("Conversation deleted") after any delete.

### 5.7 Send() Session Guard

Before any async operations in send():
    const activeSid = S.session.session_id;

After the agent completes:
    if (S.session && S.session.session_id === activeSid) {
      // apply result, re-render
      setBusy(false);
    } else {
      // user switched sessions mid-flight
      // only refresh sidebar, do NOT call setBusy(false) on the new session
      await renderSessionList();
    }

This prevents a session switch mid-flight from either clobbering the new session's state
or unlocking the Send button on the wrong session.

---

## 6. Data Flow: Full Chat Round Trip

Step-by-step trace of what happens when you type a message and press Send:

1.  User types, presses Enter. sen

... [OUTPUT TRUNCATED - 25716 chars omitted out of 75716 total] ...

nerate last response, clear conversation,
                       Prism.js syntax highlighting, message queue (MSG_QUEUE + drain on idle),
                       INFLIGHT-first loadSession (message persists on switch-away/back)
            Bug fixes: A1 (reconnect banner false positive), A2 (session list scroll clip)
            New endpoints: POST /api/session/clear, POST /api/session/truncate
            Tests: 14 new, 139/139 total
            JS: MSG_QUEUE global, updateQueueBadge(), setBusy drain logic, send() queues when busy,
                loadSession checks INFLIGHT before server fetch
    v1.2.2 Concurrency sweeps (March 31, 2026):
            R10-R15: approval cross-session, activity bar per-session, live card
            restore on switch-back, settled cards after done, model source,
            newSession card clear. 190/190 tests.
    v1.2  Sprint 10 (March 31, 2026):
            Arch: server.py split into api/ modules (config, helpers, models, workspace, upload, streaming)
            Features: background task cancel, cron run history, tool card UX polish
            Post-sprint fixes: SSE cancel event breaks loop, Cancel button always hidden on setBusy(false),
              S.activeStreamId initialized, tool-card show-more uses data attributes, version label v1.2,
              Session.__init__ **kwargs forward-compat, test cron isolation via HERMES_HOME,
              last_workspace reset in conftest between tests, tool cards grouped by assistant turn
            Tests: 18 new, 167/167 total
            Regressions fixed: uuid, AIAgent, has_pending, SSE cancel loop, Session.__init__ tool_calls
            test_regressions.py: 10 tests -- one per introduced bug, permanent regression gate
            Total after fixes: 177/177
    v1.1  Sprint 9 (March 31, 2026):
            Arch: app.js deleted; replaced by ui.js, workspace.js, sessions.js, messages.js, panels.js, boot.js
            Features: tool call cards (inline collapsible, live + history), attachment persistence,
                       todo list panel (parses tool results from session history)
            Tests: 10 new, 149/149 total
    v0.9  Sprint 7 (March 31, 2026):
            Features: cron edit+delete, skill create/edit/delete, memory write, session content search
            Arch: Phase G partial (active_streams+uptime in /health), git init
            Bug fixes: A1 (activity bar min-height), A2 (model chip sync), A3 (cron output overflow)
            New endpoints: /api/crons/update, /api/crons/delete, /api/skills/save, /api/skills/delete,
                           /api/memory/write, /api/sessions/search (extended)
            Tests: 19 new, 125/125 total


---

## 15. Sprint Log

This section records what was actually built and changed in each sprint. It is the
permanent history of the codebase. Update it at the end of every sprint.

### Sprint 1 (March 30, 2026): Bug Fixes, Arch Foundations, First Tests

**Tracks:** Bug fixes (7), Architecture (3), Tests (1)
**Test result:** 19/19 passing
**Backup:** server.py.sprint1.bak

#### Bug Fixes Applied

| ID  | Description                          | Change                                                                 |
|-----|--------------------------------------|------------------------------------------------------------------------|
| B3  | Model chip label wrong for new models | Replaced substring check with MODEL_LABELS dict; 10 models supported  |
| B7  | Sidebar title overflow                | Added min-width:0 to .session-item                                     |
| B11 | /api/session GET creates session silently | Returns 400 with error message when session_id is missing          |
| B2  | File input no accept attribute        | Added accept= with image/*, text/*, pdf, json, common code extensions  |
| B9  | Empty assistant messages render       | loadSession() filters out empty-text assistant messages before render  |
| B1  | Approval card missing pattern context | showApprovalCard() now appends pattern_keys to description text        |
| B4/B5 | Reload mid-stream loses context     | markInflight/clearInflight in localStorage; checkInflightOnBoot() shows gold reconnect banner; GET /api/chat/stream/status endpoint added |

Model dropdown also expanded from 2 options to 10, grouped by provider in <optgroup>.

#### Architecture Improvements Applied

| Item   | Description                          | Change                                                                    |
|--------|--------------------------------------|---------------------------------------------------------------------------|
| Arch-1 | Section headers                      | 8 clear # === SECTION === banners dividing server.py into logical zones   |
| Arch-2 | LOCK around SESSIONS dict            | get_session, new_session, delete now hold LOCK; eliminates race condition |
| Arch-3 | Structured request logging           | log_request() override emits JSON per request to /tmp/webui-mvp.log      |

Request log format:
    {"ts": "2026-03-30T17:30:08Z", "method": "GET", "path": "/health", "status": 200, "ms": 0.1}

#### Test Suite Added

File: webui-mvp/tests/test_sprint1.py (19 tests)
File: webui-mvp/tests/__init__.py

Test categories:
    Health check (1)
    Session CRUD: create, load, update, delete, sort, B11 footgun (6)
    Multipart parser unit tests: text file, binary/PNG (2)
    HTTP upload: success, too large, no file, bad session (4)
    Approval API: pending/none, inject+deny, inject+session-approve (3)
    Stream status endpoint (1)
    File browser: list dir, path traversal block (2)

Run tests:
    cd <agent-dir>
    venv/bin/python -m pytest webui-mvp/tests/test_sprint1.py -v

#### Section 5.5 Update (B3 resolved)

The model chip label bug is now fixed. The MODEL_LABELS object in syncTopbar():

    const MODEL_LABELS = {
      'openai/gpt-5.4-mini':             'GPT-5.4 Mini',
      'openai/gpt-4o':                   'GPT-4o',
      'openai/o3':                       'o3',
      'openai/o4-mini':                  'o4-mini',
      'anthropic/claude-sonnet-4.6':     'Sonnet 4.6',
      'anthropic/claude-sonnet-4-5':     'Sonnet 4.5',
      'anthropic/claude-haiku-3-5':      'Haiku 3.5',
      'google/gemini-2.5-pro':           'Gemini 2.5 Pro',
      'deepseek/deepseek-chat-v3-0324':  'DeepSeek V3',
      'meta-llama/llama-4-scout':        'Llama 4 Scout',
    };
    $('modelChip').textContent = MODEL_LABELS[m] || (m.split('/').pop() || 'Unknown');

Fallback: splits on '/' and uses the last segment, so any unlisted model shows its
short identifier rather than a wrong hardcoded label.

#### Version History Update

    v0.3  Sprint 1: B3/B7/B11/B2/B9/B1/B4/B5 bug fixes
    v0.3  Sprint 1: Model dropdown expanded to 10 models in provider groups
    v0.3  Sprint 1: LOCK added around SESSIONS dict (thread safety)
    v0.3  Sprint 1: Section headers added throughout server.py
    v0.3  Sprint 1: Structured JSON request logging via log_request() override
    v0.3  Sprint 1: GET /api/chat/stream/status endpoint
    v0.3  Sprint 1: Reconnect banner (markInflight/clearInflight/checkInflightOnBoot)
    v0.3  Sprint 1: GET /api/approval/inject_test endpoint (test-only)
    v0.3  Sprint 1: First pytest suite, 19 tests, all passing

---

## 16. Architecture Phase Priority Matrix

Quick-reference table for prioritizing architecture work. Phases are from Section 10.

| Phase | Name                        | Priority | Effort | Blocks         | Status     |
|-------|-----------------------------|----------|--------|----------------|------------|
| A+E   | File Separation + Frontend  | High     | Medium | F              | COMPLETE Sprint 6+9 (HTML->index.html, JS->6 modules, app.js deleted; server.py pure Python ~1150 lines) |
| B     | Thread-Safe Request Context | Critical | Medium | nothing        | PARTIAL (Sprint 4: per-session lock added; global env vars still used) |
| C     | Session Store Improvements  | Medium   | Medium | J              | PARTIAL Sprint 5 (index file + LRU cache; LRU eviction policy and pagination still open) |
| D     | Input Validation            | Medium   | Low    | nothing        | COMPLETE Sprint 6 (approval/respond + file/raw hardened; all endpoints validated) |
| E     | Frontend Modularization     | Medium   | High   | requires A     | Pending    |
| F     | API Design Cleanup          | Low      | Medium | requires A     | Pending    |
| G     | Observability               | Low      | Low    | nothing        | Partial (Sprint 7: active_streams+uptime added to /health; log rotation still pending) |
| H     | Authentication              | Low      | Medium | nothing        | Pending    |
| I     | Test Infrastructure         | High     | High   | requires A,D   | Partial(*) |
| J     | Performance                 | Low      | High   | requires C     | Pending    |

(*) Phase G is partial: structured request logging done in Sprint 1. Full observability
    (health detail, debug/stats endpoint, log rotation) remains.
(*) Phase I is partial: HTTP integration test suite started in Sprint 1. Unit tests for
    isolated modules require Phase A file split first.

Recommended execution order:
    1. Phase B (thread safety): critical, low risk, no file changes needed
    2. Phase D (input validation): low effort, improves error messages immediately
    3. Phase A (file split): enables E, F, and full Phase I
    4. Phase G remainder (health detail, debug endpoint): 1-2 hours
    5. Phase C (session index): needed as session count grows
    6. Phase E (frontend modules + marked.js): biggest UX improvement
    7. Phase I (full test suite): after A gives us importable modules
    8. Phase F, H, J: lower priority, tackle when needed

---

## 17. Working Conventions for Agent Contributors

This section is specifically for agents (Hermes instances, subagents, Codex, etc.) that
will be working on this codebase. Read this before touching any file.

### Before Making Any Change

1. Read this document (ARCHITECTURE.md) fully. Especially sections 4, 5, and the ADRs.
2. Read the relevant section of server.py by searching for the SECTION header.
3. Check the Sprint Log (Section 15) to understand what was recently changed.
4. Run the test suite first to confirm baseline: cd <agent-dir> &&
   venv/bin/python -m pytest webui-mvp/tests/test_sprint1.py -v
5. Check server health: curl -s http://127.0.0.1:8787/health

### Making Changes

Always back up server.py before a non-trivial change:
    cp server.py server.py.$(date +%Y%m%d_%H%M).bak

Use exact string matching when patching. The pitfalls are documented in the
hermes-webui-mvp skill. Key ones:
- Never use sed on this file from the shell. Use execute_code with Python string replace.
- Always assert the old string is found before replacing (prevents silent no-op patches).
- Unicode escape sequences in JS (\u2026) exist as literal backslash-u in the file.
  Match the file's raw content, not interpreted Python strings.
- The HTML block is a Python raw string (r"""..."""). Standard triple-quote escaping
  rules do not apply inside it, but Python escape sequences \n etc. work in JS strings
  inside it as literal two-character sequences.

After any change:
    venv/bin/python -m py_compile webui-mvp/server.py   # syntax check
    curl -s http://127.0.0.1:8787/health                # server still alive
    venv/bin/python -m pytest webui-mvp/tests/ -v       # tests still pass

### Critical Rules (do NOT regress these)

These patterns have been broken and fixed multiple times. Do not re-introduce them.

RULE-1: deleteSession() must NEVER call newSession().
    Deleting does not create. If the deleted session was active and others remain,
    load sessions[0]. If none remain, show empty state. See Section 5.6.

RULE-2: /api/upload must be checked BEFORE read_body() in do_POST.
    read_body() consumes the request body. Upload parsing also needs the body.
    Order matters. See Section 4.1.

RULE-3: run_conversation() takes task_id=, NOT session_id=.
    task_id is the correct keyword argument. session_id= raises TypeError silently.

RULE-4: stream_delta_callback receives None as end-of-stream sentinel.
    The on_token callback must guard: if text is None: return

RULE-5: send() must capture activeSid BEFORE any await.
    The active session can change while awaits are pending. Capture first, guard on return.

RULE-6: Boot IIFE must never auto-create a session.
    Only two places create sessions: the + button and send() when S.session is null.

RULE-7: All SESSIONS dict accesses must hold LOCK.
    LOCK is a module-level threading.Lock(). Use: with LOCK: ...

RULE-8: do NOT expose tracebacks to API clients.
    500 responses should return {"error": "Internal server error"}, not the full traceback.
    (Currently traceback is exposed; fix in Phase D. Do not make it worse.)

RULE-9: Pattern_keys, not pattern_key, for multi-pattern approvals.
    The approval module may include both pattern_key (singular, legacy) and pattern_keys
    (plural, all matched patterns). Always iterate pattern_keys when approving.

### Adding New API Endpoints

See Section 11 for the exact code pattern. Short version:
- GET: add before the 404 fallback in do_GET
- POST: add after /api/upload check and after read_body(), before 404 fallback in do_POST
- Always validate required fields, return 400 for missing/invalid input
- Always use get_session(sid) with try/except KeyError -> 400 or 404
- Add a test in test_sprint1.py or a new test file

### Updating This Document

Update ARCHITECTURE.md whenever you:
- Fix a bug listed in Section 9 (update its row, mark resolved)
- Complete an architecture phase (update Section 16 matrix)
- Add a new endpoint (add to Section 4.1 routing table)
- Discover a new pitfall or rule (add to Section 17)
- Complete a sprint (add a new entry to Section 15)

This document is the memory of the codebase. If it is not updated, future agents will
make the same mistakes again.

---

## 18. Endpoint Reference (Current)

Complete list of all HTTP endpoints as of Sprint 1 (v0.3).

### GET Endpoints

    /                          Returns full HTML app (index page)
    /index.html                Same as /
    /health                    {"status":"ok","sessions":N}
    /api/session               ?session_id=X -> full session + messages. 400 if no ID.
    /api/sessions              List of all session compact() dicts, sorted by updated_at
    /api/list                  ?session_id=X&path=. -> directory listing for session workspace
    /api/file                  ?session_id=X&path=rel -> file content (text, 200KB limit)
    /api/chat/stream           ?stream_id=X -> SSE stream. Long-lived. Emits token/tool/
                               approval/done/error events.
    /api/chat/stream/status    ?stream_id=X -> {"active": true/false, "stream_id": X}
    /api/approval/pending      ?session_id=X -> {"pending": entry_or_null}
    /api/approval/inject_test  ?session_id=X&pattern_key=K&command=C -> test-only endpoint.
                               Injects a pending approval entry into the server process.
    /api/file/raw              ?session_id=X&path=P -> raw file bytes with correct MIME type.
                               Used for image preview. Path traversal protected via safe_resolve.
                               Returns 404 JSON if file not found.

### POST Endpoints

    /api/upload                multipart/form-data. Fields: session_id, file. Returns filename.
    /api/session/new           {"model"?, "workspace"?} -> new session
    /api/session/update        {"session_id", "workspace"?, "model"?} -> updated session
    /api/session/delete        {"session_id"} -> {"ok": true}
    /api/chat/start            {"session_id", "message", "model"?, "workspace"?}
                               -> {"stream_id", "session_id"}. Starts agent daemon thread.
    /api/chat                  (fallback, sync) {"session_id", "message", "model"?, "workspace"?}
                               -> blocks until agent finishes. Returns full result.
    /api/approval/respond      {"session_id", "choice": once|session|always|deny}
                               -> {"ok": true, "choice": choice}

### GET Endpoints Added in Sprint 3

    /api/crons                 All cron jobs. Returns {jobs: [...]}.
    /api/crons/output          ?job_id=X&limit=N -> {outputs: [{filename, content}]}
    /api/skills                All skills. Returns {skills: [{name, description, category}]}
    /api/skills/content        ?name=X -> full skill data including SKILL.md content
    /api/memory                MEMORY.md + USER.md. Returns {memory, user, *_path, *_mtime}

### POST Endpoints Added in Sprint 3

    /api/crons/run             {job_id} -> triggers run in daemon thread. Returns {ok, status}.
    /api/crons/pause           {job_id} -> {ok, job} or 404.
    /api/crons/resume          {job_id} -> {ok, job} or 404.

---

## Sprint 2 Log Entry (March 30, 2026)

Added to Section 15 Sprint Log.

### Sprint 2: Rich File Preview (March 30, 2026)

**Tracks:** Features (4 sub-features), Tests (8 new)
**Test result:** 27/27 passing (19 Sprint 1 + 8 Sprint 2)
**Backup:** server.py.sprint1.bak (Sprint 1 backup; Sprint 2 is incremental)

#### Features Implemented

**Image Preview (GET /api/file/raw)**

New endpoint in do_GET:

    GET /api/file/raw?session_id=X&path=relative/path

- Reads raw bytes from workspace file via safe_resolve() (path traversal protected)
- Looks up MIME type from MIME_MAP constant keyed by lowercase extension
- Falls back to 'application/octet-stream' for unknown types
- Serves bytes directly with correct Content-Type header
- No MAX_FILE_BYTES size limit (images can be large; the browser handles progressive load)
- Returns JSON 404 if file not found or not a file

Frontend: openFile() checks IMAGE_EXTS set. If image, sets <img src="/api/file/raw?...">
and calls showPreview('image'). The browser loads the image natively. onerror handler
shows a status message if load fails.

**Rendered Markdown Preview**

Frontend only -- uses existing GET /api/file endpoint for text content.
openFile() checks MD_EXTS set. If markdown, fetches text then calls:

    $('previewMd').innerHTML = renderMd(data.content);

Preview renders in .preview-md container with full typography CSS separate from the
chat bubble .msg-body CSS (allows different sizing/spacing for the narrower side panel).

**Table Support in renderMd()**

Added a regex pass before paragraph wrapping:
- Detects blocks of pipe-delimited rows where row[1] is a separator (|---|---|)
- Converts to <table><thead><tbody> HTML
- Handles any number of columns
- This partially resolves B8 (renderMd missing tables)

**Smart File Icons in renderFileTree()**

New fileIcon(name, type) function maps extensions to emoji icons:
- Directories: folder icon
- Images: camera icon
- Markdown: notepad icon
- Python: snake icon
- JS/TS/JSX/TSX: circuit icon
- JSON/YAML/TOML: gear icon
- Shell scripts: terminal icon
- Everything else: document icon

**Preview Path Bar with Type Badge**

previewPath bar now has two elements:
- #previewPathText: the relative file path
- #previewBadge: colored badge with type label (image/md/extension)
  Blue for images, gold for markdown, gray for code

#### New Constants Added

    IMAGE_EXTS   set of image extensions: .png .jpg .jpeg .gif .svg .webp .ico .bmp
    MD_EXTS      set of markdown extensions: .md .markdown .mdown
    CODE_EXTS    set of code/text extensions for reference
    MIME_MAP     dict: extension -> MIME type string

#### New HTML Elements

    #previewPathText   span inside preview path bar (was direct textContent on #previewPath)
    #previewBadge      colored type badge span
    #previewImgWrap    div centering the preview image
    #previewImg        <img> element for image preview
    #previewMd         div for rendered markdown HTML

#### Endpoint Reference Update

Added to Section 18:

    GET /api/file/raw   ?session_id=X&path=P -> raw file bytes with correct MIME type.
                        Path traversal protected. 404 JSON if not found.

#### B8 Status Update (Section 9)

B8 (renderMd missing tables) is now PARTIAL: table parsing added in Sprint 2.
Nested lists and complex inline HTML still not handled. Full fix remains Phase E
(replace renderMd with marked.js).


### Sprint 3 (March 30, 2026): Panel Navigation + Feature Viewers

**Tracks:** Bug fixes (3), Features (3 panels + 8 API endpoints), Arch Phase D (partial)
**Tests:** 48/48 passing
**Backup:** server.py.sprint2.bak

#### New Sidebar Navigation

Four tabs at the top of the sidebar: Chat (default), Tasks, Skills, Memory.
Implemented via `.nav-tab` / `.panel-view` CSS classes. `switchPanel(name)` activates
the correct tab and panel-view, then lazy-loads panel data on first open.

#### Tasks Panel (Cron viewer)

`loadCrons()` fetches GET /api/crons, renders each job as a collapsible `.cron-item`.
`toggleCron(id)` expands/collapses the body. `loadCronOutput(jobId)` auto-loads the last
output file from GET /api/crons/output for each job.

Run Now: POST /api/crons/run starts the job in a daemon thread, returns immediately.
Pause/Resume: POST /api/crons/pause and /api/crons/resume call the cron.jobs functions.

#### Skills Panel

`loadSkills()` fetches GET /api/skills, caches in `_skillsData`. `renderSkills()` groups
by category, filters by search input. Clicking a skill calls `openSkill(name)` which
fetches GET /api/skills/content and renders in the right panel using `showPreview('md')`.

#### Memory Panel

`loadMemory()` fetches GET /api/memory (reads MEMORY.md + USER.md from
~/.hermes/memories/), renders both as markdown via renderMd() with timestamps.

#### New API Endpoints (Section 18 update)

    GET  /api/crons              All jobs from cron.jobs.list_jobs(include_disabled=True)
    GET  /api/crons/output       ?job_id=X&limit=N -> last N output .md files for a job
    POST /api/crons/run          {job_id} -> triggers run_job() in daemon thread
    POST /api/crons/pause        {job_id} -> pause_job(job_id)
    POST /api/crons/resume       {job_id} -> resume_job(job_id)
    GET  /api/skills             All skills via tools.skills_tool.skills_list()
    GET  /api/skills/content     ?name=X -> full skill data via skill_view(name)
    GET  /api/memory             MEMORY.md + USER.md content and mtimes

#### Phase D Input Validation Applied

    require(body, *fields)   raises ValueError with clean message on missing fields
    bad(handler, msg, status=400)  returns clean JSON error response

Endpoints hardened: /api/session/update, /api/session/delete, /api/chat/start.
Unknown session ID on /api/session/update now returns 404 instead of 500.

#### Bug Fix Details

B6: `newSession()` now passes `inheritWs = S.session?.workspace` to /api/session/new.
    Backend already accepted `workspace` param in session/new but it was never sent.

B10: `es.addEventListener('tool', ...)` now calls `removeThinking()` before updating
     status and shows a compact `.msg-role + .msg-body` tool-running row. `ensureAssistantRow()`
     also removes `#toolRunningRow` when first token arrives.

B14: `document.addEventListener('keydown', ...)` at global scope catches Cmd/Ctrl+K
     and calls `newSession()` if not busy.


### Sprint 4 (March 30, 2026): Relocation + Session Power Features + Phase A/B

**Tracks:** Bugs (B12, B8, TD5), Features (rename, search, file ops), Arch (Phase A/B start), Relocation
**Tests:** 68/68 passing
**Backup:** server.py.sprint2.bak (last full backup; Sprint 3 and 4 are incremental)

#### Source Relocation

Moved <agent-dir>/webui-mvp/ to <repo>/.
Symlink: <agent-dir>/webui-mvp -> <repo>
The symlink means all existing import paths (sys.path.insert for hermes-agent modules)
continue working unchanged. start.sh updated to reference new canonical path.

Safe from: git pull, git reset --hard, git stash on hermes-agent repo.
NOT safe from: git clean -fd (would delete symlink but not the target).
Disk failure: still a single-copy risk. Use git init + push when ready.

#### Phase A: CSS Extracted

<repo>/static/style.css: the 23KB CSS block from the Python raw string.
server.py no longer contains any CSS. GET /static/* handler serves disk files.
server.py shrunk by ~200 lines.

#### Phase B: Per-Session Agent Lock

SESSION_AGENT_LOCKS = {} keyed by session_id, each value is a threading.Lock().
_get_session_agent_lock(sid) returns the lock, creating it if needed.
_run_agent_streaming() wraps the env var block with: with _agent_lock: ...
This prevents two concurrent requests for the same session from overwriting env vars
mid-execution. Two concurrent requests for DIFFERENT sessions are still unsafe (env vars
are process-global). Full fix requires removing env var usage entirely (Phase B complete).

#### New Endpoints

    GET  /static/*             Serves files from <repo>/static/ with
                               correct Content-Type. Currently serves style.css.
    POST /api/session/rename   {session_id, title} -> {session: compact}. Truncates to 80 chars.
    GET  /api/sessions/search  ?q=X -> sessions whose title contains q (case-insensitive).
                               Empty q returns all sessions (same as /api/sessions).
    POST /api/file/delete      {session_id, path} -> {ok: true}. Path traversal protected.
    POST /api/file/create      {session_id, path, content?} -> {ok, path}. Errors if exists.


### Sprint 5 (March 30, 2026): Phase A Complete + Workspace + Edit + Copy

**Tracks:** Arch (Phase A complete, TD1/TD2/TD6/Phase C), Features (3), Tests (18)
**Tests:** 86/86 passing

#### Phase A Complete: static/app.js

Extracted 902-line JavaScript from server.py HTML string to <repo>/static/app.js.
server.py now: Python code + thin HTML skeleton (~875 lines, down from 1778).
Layout: server.py imports nothing from static/; the HTML just has <link> and <script src>.
Served via GET /static/* handler added in Sprint 4.
node --check validates app.js on every sprint.

#### TD2: LRU SESSIONS Cache

SESSIONS changed to collections.OrderedDict.
get_session(): SESSIONS.move_to_end(sid) on hit; on miss: load from disk, add, move_to_end, evict if over SESSIONS_MAX=100.
new_session(): same eviction logic on insert.
Result: memory usage capped regardless of session count.

#### TD1: Thread-Local Env Context

_thread_ctx = threading.local() added to Server Globals.
_set_thread_env(**kwargs) and _clear_thread_env() set/clear _thread_ctx.env.
_run_agent_streaming() calls _set_thread_env() before env var writes, _clear_thread_env() in outer finally.
Process-level os.environ writes still exist as fallback (needed until terminal tool reads thread-local).

#### Phase C: Session Index File

SESSION_INDEX_FILE = SESSION_DIR / '_index.json'.
_write_session_index(): builds compact() list from SESSIONS + disk files, writes JSON.
Called in Session.save() -- keeps index always current.
all_sessions(): reads index JSON first (one file read); overlays in-memory SESSIONS; falls back to full glob scan on error.
Index files starting with '_' are skipped during full scan to avoid recursion.

#### New Workspace Infrastructure

WORKSPACES_FILE = ~/.hermes/webui-mvp/workspaces.json
LAST_WORKSPACE_FILE = ~/.hermes/webui-mvp/last_workspace.txt
load_workspaces() / save_workspaces() / get_last_workspace() / set_last_workspace() helpers.
new_session() now calls get_last_workspace() as default instead of DEFAULT_WORKSPACE.
set_last_workspace() called in /api/session/update and /api/chat/start.

#### New Endpoints (Sprint 5)

    GET  /api/workspaces           {workspaces: [...], last: path}
    POST /api/workspaces/add       {path, name?} -- validates exists+dir, no duplicates
    POST /api/workspaces/remove    {path} -- removes from list, ok even if not present
    POST /api/workspaces/rename    {path, name} -- updates display name, 404 if not found
    POST /api/file/save            {session_id, path, content} -- write text to existing file


### Sprint 6 (March 31, 2026): Polish + Resize + Cron Create + Phase E

**Tests:** 106/106 passing
**Backup:** server.py.sprint5.bak

#### Phase E Complete: static/index.html

The HTML = r triple-quoted string (197 lines, 12682 chars) was extracted to
<repo>/static/index.html and served via disk read on each request.
server.py is now pure Python: zero HTML/CSS/JS inline. All static content is in static/.

Static file layout (final):
  static/index.html  (Sprint 6)  -- HTML template
  static/style.css   (Sprint 4)  -- all CSS
  static/app.js      (Sprint 5)  -- all JavaScript

server.py line count progression: 1778 (S1) -> 1042 (S5) -> 903 (S6)

#### Phase D Complete

/api/approval/respond: validates session_id present; choice must be one of
(once, session, always, deny); returns 400 on invalid.
/api/file/raw: validates session_id present; try/except KeyError returns 404.

#### New Endpoints

    POST /api/crons/create   {prompt, schedule, name?, deliver?, skills?, model?}
                             -> {ok: true, job: {...}} or 400 on invalid schedule/missing fields.
                             Uses cron.jobs.create_job() directly.
    GET  /api/session/export ?session_id=X
                             -> full session JSON with Content-Disposition: attachment header.
                             Includes all messages, workspace, model, timestamps.

#### Resizable Panels

_initResizePanels() called from boot IIFE. Creates mousedown listeners on #sidebarResize
and #rightpanelResize. On mousemove: computes delta and clamps to min/max. On mouseup:
saves width to localStorage. Widths restored at boot via localStorage.getItem().
CSS: .resize-handle with position:absolute, width:5px, cursor:col-resize.
body.resizing added during drag to suppress text selection.