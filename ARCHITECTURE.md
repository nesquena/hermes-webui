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

1.  User types, presses Enter. send() is called.
2.  Guard: return if (!text && !pendingFiles) || S.busy
3.  If S.session is null: await newSession(), await renderSessionList()
4.  Capture activeSid = S.session.session_id (before any awaits)
5.  uploadPendingFiles(): POST each file in S.pendingFiles to /api/upload
    - Shows upload progress bar
    - Clears S.pendingFiles on completion
    - Returns array of uploaded filenames
6.  Build msgText from text + file note
7.  Build userMsg {role:'user', content: displayText, attachments?: filenames}
8.  Push userMsg to S.messages, call renderMessages(), appendThinking()
9.  setBusy(true), setStatus('Hermes is thinking...')
10. INFLIGHT[activeSid] = {messages: [...S.messages], uploaded}
11. startApprovalPolling(activeSid)
12. POST /api/chat/start {session_id, message, model, workspace}
    Server: saves session, creates queue.Queue, starts daemon thread, returns {stream_id}
13. Browser opens EventSource('/api/chat/stream?stream_id=X')
14. In the SSE loop:
    - 'token': assistantText += d.text, ensureAssistantRow(), render markdown
    - 'tool': setStatus('tool name...')
    - 'approval': showApprovalCard(d)
    - 'done': sync S from d.session, renderMessages(), loadDir, renderSessionList,
               setBusy(false), delete INFLIGHT[activeSid]
    - 'error': show error message, setBusy(false)
    - es.onerror: handle network drops (show error, setBusy(false))
15. If approval needed: user clicks a button, respondApproval() fires
    POST /api/approval/respond -> server pops _pending, calls approve_*
    Agent retries the command (now is_approved() returns True) and continues

---

## 7. Dependency Map

Direct imports in server.py:

    run_agent.AIAgent              Main agent class. Wraps LLM + tool execution.
    tools.approval.*               Module-level approval state.
    yaml                           Config loading.
    Standard library: json, os, re, sys, threading, time, traceback, uuid,
                      http.server, pathlib, urllib.parse, email.parser, queue

AIAgent constructor parameters used:

    model=               OpenRouter model ID string
    platform='cli'       Sets the platform context for tool selection
    quiet_mode=True      Suppresses agent's own stdout output
    enabled_toolsets=    List of toolset names from config.yaml
    session_id=          Used for tool state keying (memory, todos, etc.)
    stream_delta_callback=   Called per token delta (or None as sentinel)
    tool_progress_callback=  Called per tool invocation (name, preview, args)

AIAgent.run_conversation() parameters:

    user_message=           The human turn text
