# Hermes Co-Work Web UI: Browser Testing Plan

> This document is for manual browser testing by Nathan or by a Claude browser agent.
> It covers every user-facing feature of the UI through Sprint 2.
> Each section is written as a step-by-step test procedure with expected outcomes.
> A browser agent (e.g. Claude with Chrome access) can execute this plan directly.
>
> Prerequisites: SSH tunnel is active on port 8787. Open http://localhost:8787 in browser.
> Server health check: curl http://127.0.0.1:8787/health should return {"status":"ok"}.

---

## How to Use This Document

Each test has:
- SETUP: what to do before the test
- STEPS: numbered actions to perform
- EXPECT: what you should see (pass criteria)
- FAIL: what a failure looks like

Work through sections in order. Each section builds on the previous.

---

## Section 1: Initial Load and Empty State

### T1.1: Fresh Load Shows Empty State
SETUP: Clear localStorage (DevTools > Application > Local Storage > delete hermes-webui-session) or open in incognito.
STEPS:
  1. Navigate to http://localhost:8787
EXPECT:
  - Dark background, Hermes logo in sidebar header
  - Center area shows "What can I help with?" heading with suggestion buttons
  - Session list in sidebar is empty or shows existing sessions
  - No session is highlighted active
  - Send button is present but there is no input focus by default
FAIL: Page shows error, blank white screen, or auto-creates a new session without user action.

### T1.2: Suggestion Buttons Work
SETUP: T1.1 complete, no active session.
STEPS:
  1. Click "What files are in this workspace?" suggestion button
EXPECT:
  - A new session is created automatically (since none existed)
  - The text "What files are in this workspace?" appears as the user message
  - Thinking dots appear below the user message
  - After a few seconds, Hermes responds
FAIL: Button does nothing, error appears, or page crashes.

---

## Section 2: Session Management

### T2.1: Create New Session via + Button
SETUP: Any state.
STEPS:
  1. Click the "+ New conversation" button in the sidebar
EXPECT:
  - A new session named "Untitled" appears highlighted in the session list
  - The center area shows the empty state ("What can I help with?")
  - The + button is the ONLY way to create a session (no auto-create on load)
FAIL: Multiple sessions created, error thrown, or empty state not shown.

### T2.2: Send a Message and See Response
SETUP: Active session exists.
STEPS:
  1. Click in the message input at the bottom
  2. Type: "Say hello in exactly three words"
  3. Press Enter (not Shift+Enter)
EXPECT:
  - User message appears immediately in chat
  - Thinking dots (three animated dots) appear below
  - Status bar shows "Hermes is thinking..."
  - Send button becomes disabled (grayed out)
  - Within 10-30 seconds, Hermes responds with a three-word greeting
  - Thinking dots disappear
  - Send button re-enables
  - Session title in sidebar updates to reflect the first message
FAIL: Message never appears, thinking dots never go away, Send button stays disabled forever.

### T2.3: Shift+Enter Creates Newline (Does Not Send)
SETUP: Active session, input focused.
STEPS:
  1. Click the message input
  2. Type "Line one"
  3. Press Shift+Enter
  4. Type "Line two"
EXPECT:
  - Two lines of text appear in the input box
  - No message is sent (no user message appears in chat)
FAIL: Message sent on Shift+Enter.

### T2.4: Reload Restores Session
SETUP: A session exists with at least one exchange (user + assistant).
STEPS:
  1. Note the session title and last message content
  2. Reload the page (Cmd+R or F5)
EXPECT:
  - The same session loads automatically (no empty state)
  - All messages from before the reload are visible
  - Session title in topbar and sidebar matches what it was before
FAIL: Session lost on reload, empty state shown, or messages missing.

### T2.5: Delete Active Session
SETUP: At least two sessions exist. One is active.
STEPS:
  1. Hover over the active session in the sidebar (trash icon appears)
  2. Click the trash icon on the active session
EXPECT:
  - A "Conversation deleted" toast appears at the bottom for ~3 seconds
  - The deleted session disappears from the sidebar list
  - The next most recent session automatically loads (or empty state if none remain)
  - NO new session is auto-created
FAIL: Session not removed, new session auto-created, error shown, or wrong session loaded.

### T2.6: Delete Non-Active Session
SETUP: At least two sessions exist. Session B is not active.
STEPS:
  1. Hover over a session that is NOT currently active
  2. Click its trash icon
EXPECT:
  - Toast "Conversation deleted" appears
  - That session disappears from list
  - Currently active session remains active and unchanged
FAIL: Active session changes, multiple sessions deleted, or error.

### T2.7: Delete Last Session Shows Empty State
SETUP: Exactly one session exists.
STEPS:
  1. Delete that session via the trash icon
EXPECT:
  - Session list is empty
  - Center area shows "What can I help with?" empty state
  - No session is auto-created
FAIL: New session created, error thrown, or UI breaks.

---

## Section 3: Model Selection

### T3.1: Model Dropdown Shows All Options
SETUP: Any active session.
STEPS:
  1. Look at the sidebar bottom: "Model" label and a dropdown
  2. Click the dropdown to expand it
EXPECT:
  - Provider groups visible: OpenAI, Anthropic, Other
  - OpenAI group: GPT-5.4 Mini, GPT-4o, o3, o4-mini
  - Anthropic group: Claude Sonnet 4.6, Claude Sonnet 4.5, Claude Haiku 3.5
  - Other group: Gemini 2.5 Pro, DeepSeek V3, Llama 4 Scout
FAIL: Only 2 options visible, no groups, or missing models.

### T3.2: Model Chip Reflects Selection
SETUP: Active session.
STEPS:
  1. Change model dropdown to "Claude Sonnet 4.6"
EXPECT:
  - The blue chip in the topbar right updates to "Sonnet 4.6" immediately
  - NOT "GPT-5.4 Mini" (this was Bug B3, now fixed)
STEPS (continued):
  2. Change model to "Gemini 2.5 Pro"
EXPECT:
  - Chip updates to "Gemini 2.5 Pro" (not "GPT-5.4 Mini")
FAIL: Chip shows wrong model name for any non-Sonnet selection.

---

## Section 4: File Upload

### T4.1: Click-to-Attach Opens File Picker
SETUP: Active session.
STEPS:
  1. Click the paperclip icon in the composer footer
EXPECT:
  - OS file picker dialog opens
  - Accepted types filter visible (images, text, PDF, common code files)
FAIL: Nothing happens, error thrown.

### T4.2: Attach a Text File and Send
SETUP: Have a small .txt or .py file ready to upload.
STEPS:
  1. Click the paperclip, select the file
  2. File chip appears in the composer tray above the input
  3. Type "What is in this file?" in the input
  4. Press Enter
EXPECT:
  - Upload progress bar briefly appears
  - User message shows the message text plus a file badge with the filename
  - Hermes responds describing or reading the file content
FAIL: Upload fails, file badge never appears, Hermes does not mention the file.

### T4.3: Drag and Drop a File
SETUP: Active session, a file ready on your desktop.
STEPS:
  1. Drag a file from Finder/Explorer over the composer area
EXPECT:
  - Blue dashed border and "Drop files to upload to workspace" overlay appear
STEPS (continued):
  2. Drop the file
EXPECT:
  - File chip appears in the tray
  - Overlay disappears
FAIL: No drag visual feedback, file not accepted, error on drop.

### T4.4: Paste Screenshot from Clipboard
SETUP: Take a screenshot (Cmd+Shift+4 on Mac, saves to clipboard).
STEPS:
  1. Click in the message input
  2. Press Cmd+V (paste)
EXPECT:
  - An image file chip appears in the tray: "screenshot-{timestamp}.png"
  - Status bar briefly shows "Image pasted: screenshot-..."
FAIL: Nothing pasted, error, or raw binary data appears in input.

### T4.5: Remove a File from Tray
SETUP: At least one file in the attach tray.
STEPS:
  1. Click the X button on a file chip in the tray
EXPECT:
  - That file is removed from the tray
  - If it was the only file, tray collapses
FAIL: File not removed, error.

---

## Section 5: Workspace File Browser

### T5.1: File Tree Loads on Session Start
SETUP: Active session with workspace set.
EXPECT:
  - Right panel shows "WORKSPACE" header
  - File tree lists files and directories in the workspace
  - Directories have folder icons
  - Files have type-appropriate icons (camera for images, notepad for markdown, etc.)
FAIL: Right panel is blank, error, or all files show generic icon regardless of type.

### T5.2: Navigate Into a Directory
SETUP: Workspace has at least one subdirectory.
STEPS:
  1. Click a directory name in the file tree
EXPECT:
  - File tree updates to show contents of that directory
  - A ".." or breadcrumb is NOT shown (current behavior: flat navigation)
FAIL: Click does nothing, error, or entire page breaks.

### T5.3: Preview a Code File
SETUP: Workspace has a .py or .js or .txt file.
STEPS:
  1. Click the file name in the tree
EXPECT:
  - Right panel switches from file tree to preview area
  - File path shown at top with file extension badge (gray)
  - File contents shown as monospace text (raw code)
  - File tree is hidden, preview is visible
FAIL: Nothing happens, binary gibberish shown, crash.

### T5.4: Close Preview Returns to File Tree
SETUP: T5.3 complete, preview is showing.
STEPS:
  1. Click the X button in the panel header
EXPECT:
  - Preview closes
  - File tree is visible again
  - Preview area is hidden
  - Reopening the same file shows fresh content (no stale cached text)
FAIL: X button does nothing, tree does not reappear.

### T5.5: Preview an Image File (Sprint 2)
SETUP: Upload a PNG, JPG, or any image file to the workspace, OR the workspace already contains one.
STEPS:
  1. Click an image file (e.g. .png or .jpg) in the file tree
EXPECT:
  - Preview area shows the actual image rendered inline (NOT a blob of bytes or placeholder)
  - Image is centered, fits within the panel width
  - Path bar shows "image" badge in blue
  - Image maintains aspect ratio
FAIL: Raw binary text displayed, broken image icon, error message, or nothing happens.

### T5.6: Preview a Markdown File (Sprint 2)
SETUP: Workspace has a .md file (or create one: upload a file named README.md with some markdown content).
STEPS:
  1. Click the .md file in the file tree
EXPECT:
  - Preview shows formatted, rendered markdown (NOT raw text with asterisks)
  - Headings render as large bold text
  - **bold** renders as bold, *italic* as italic
  - Bullet lists render as actual list items
  - Code blocks render in monospace with dark background
  - Path bar shows "md" badge in gold
FAIL: Raw markdown text with asterisks/hashes shown, or no preview at all.

### T5.7: Markdown Preview Renders Tables (Sprint 2)
SETUP: Upload or create a .md file with a table like:
  | Name | Value |
  |------|-------|
  | foo  | bar   |
STEPS:
  1. Click the file in the file tree
EXPECT:
  - Table renders as an actual HTML table with borders
  - Column headers (Name, Value) are bold/highlighted
  - Data rows alternate subtle background
FAIL: Table displayed as raw pipe-separated text.

### T5.8: Refresh Files Button
SETUP: Active session, workspace has files.
STEPS:
  1. Click the "Files" refresh button in the sidebar bottom actions
EXPECT:
  - File tree reloads
  - If a file was added externally, it now appears
FAIL: Error, spinner never stops, tree clears without reloading.

---

## Section 6: Workspace Path

### T6.1: Change Workspace Path
SETUP: Active session.
STEPS:
  1. Click the workspace path display in the sidebar bottom (shows current path)
  2. A prompt dialog appears
  3. Enter a new valid path (e.g. /tmp)
  4. Click OK
EXPECT:
  - Workspace chip in topbar updates to show the last segment of the new path
  - File tree refreshes to show files at the new path
  - Next message sent uses the new workspace
FAIL: Dialog does not appear, path not saved, error on invalid path.

---

## Section 7: Tool Approval

### T7.1: Dangerous Command Shows Approval Card
SETUP: Active session with a test-workspace (NOT a production directory).
STEPS:
  1. Type: "Run the command: rm -rf /tmp/hermes_test_delete_me"
  2. Send the message
EXPECT:
  - Thinking dots appear
  - An orange/red approval card appears above the composer:
    "Dangerous command - approval required"
  - The card shows the command text
  - The card shows the pattern description (e.g. "recursive delete [recursive_delete]")
  - Four buttons: Allow once, Allow this session, Always allow, Deny
FAIL: No card appears, agent executes without asking, page crashes.

### T7.2: Deny Approval Blocks the Command
SETUP: T7.1 complete, card is showing.
STEPS:
  1. Click "Deny"
EXPECT:
  - Approval card disappears
  - Agent responds with a message indicating the command was denied/blocked
  - No file was deleted
FAIL: Command executes despite deny, card stays up, error.

### T7.3: Allow Once Executes the Command
SETUP: Create a safe test: type "Run: touch /tmp/hermes_approval_test.txt"
STEPS:
  1. Send the message
  2. When approval card appears, click "Allow once"
EXPECT:
  - Approval card disappears
  - Agent continues and reports the command ran successfully
  - Verify: open a terminal and run: ls /tmp/hermes_approval_test.txt
FAIL: Command blocked after Allow once, card stays, error.

---

## Section 8: Transcript Download

### T8.1: Download Conversation as Markdown
SETUP: A session with at least 2 messages (1 user + 1 assistant).
STEPS:
  1. Click the "Transcript" download button in the sidebar bottom
EXPECT:
  - Browser downloads a .md file named hermes-{session_id}.md
  - Opening the file shows the conversation in markdown format:
    ## user
    (message text)
    ## assistant
    (response text)
FAIL: No download triggered, file is empty, file is corrupted JSON instead of markdown.

---

## Section 9: Reconnect Banner (Sprint 1 - B4/B5)

### T9.1: Reconnect Banner After Mid-Stream Reload
NOTE: This test requires deliberate timing. Best done with a slow/long agent request.
SETUP: Active session.
STEPS:
  1. Send a message that will take a while (e.g. "Write me a 500-word short story")
  2. While thinking dots are showing (within the first 5 seconds), reload the page (Cmd+R)
EXPECT:
  - Page reloads and restores the session
  - A gold/amber banner appears near the top: "A response may have been in progress..."
  - Two buttons on the banner: "Dismiss" and "Reload"
  - Clicking "Reload" fetches fresh messages from server
  - Clicking "Dismiss" removes the banner
FAIL: No banner shown, page crashes, banner appears on normal reloads with no in-flight request.

---

## Section 10: Multi-Session and Concurrent Behavior

### T10.1: Switch Sessions While Response Is Loading
SETUP: Active session, agent running (thinking dots visible from a previous message).
STEPS:
  1. While thinking dots are showing, click a DIFFERENT session in the sidebar
EXPECT:
  - The new session loads cleanly (its messages show)
  - The Send button for the NEW session is NOT disabled (it's not busy)
  - The original session's response is still being generated in the background
  - Clicking back to the original session shows the thinking dots still running
  - When the original request finishes, its messages update correctly
FAIL: New session shows busy state, switching breaks messages, response lands in wrong session.

### T10.2: Multiple Sessions in List (Up to 30)
SETUP: Create enough sessions to have at least 5 in the sidebar.
EXPECT:
  - Sessions listed most-recently-updated first
  - Long titles truncate with "..." and do not overflow the sidebar width
  - Hover shows the trash icon on any session
FAIL: Titles overflow sidebar, order is wrong, trash icon never appears.

---

## Section 11: Visual and Layout Checks

### T11.1: Right Panel Hidden on Small Screens
STEPS:
  1. Resize browser window to below 900px width
EXPECT:
  - Right panel (workspace) disappears
  - Chat area expands to fill the full width
FAIL: Right panel overlaps chat or causes horizontal scroll.

### T11.2: Sidebar Hidden on Very Small Screens
STEPS:
  1. Resize browser window to below 640px width
EXPECT:
  - Left sidebar disappears
  - Chat area takes full width
FAIL: Sidebar causes layout overflow or blocks chat.

### T11.3: Structured Log Output
SETUP: SSH access to VPS.
STEPS:
  1. In a terminal: tail -f /tmp/webui-mvp.log
  2. In browser: perform any action (load page, send message, click file)
EXPECT:
  - Log entries appear in terminal as JSON: {"ts":"...","method":"GET","path":"/health","status":200,"ms":0.1}
  - Every request produces one log line
  - Status codes are correct (200 for success, 400 for bad requests)
FAIL: No log output, log shows Apache-style text instead of JSON, log file not created.

---

## Section 12: Error Handling

### T12.1: Send Button Disabled When Busy
SETUP: Message is sending (thinking dots visible).
EXPECT:
  - Send button is visually grayed out
  - Pressing Enter does NOT send another message
  - Clicking Send button does nothing
FAIL: Multiple messages sent while one is in flight.

### T12.2: Upload Failure Shows Status
SETUP: Active session.
STEPS:
  1. Try to attach a file larger than 20MB (if available)
EXPECT:
  - Status bar shows an error message about file size or the upload is rejected
  - The chat is not broken (can still send messages)
FAIL: Uncaught error, page crashes, or no feedback given.

### T12.3: File Preview for Binary Non-Image
SETUP: Workspace has a .zip or .bin file.
STEPS:
  1. Click the binary file in the file tree
EXPECT:
  - Code preview shows some text (may be replacement characters for binary content)
  - OR a "File too large" or "Could not open file" error in the status bar
  - Page does NOT crash
FAIL: Browser freezes, crash, or security issue.

---

## Automated Test Coverage Reference

These behaviors are verified by pytest (run: venv/bin/python -m pytest webui-mvp/tests/ -v):

Sprint 1 tests (test_sprint1.py):
  - Server health, session CRUD (create/load/update/delete/sort)
  - B11 footgun fix (/api/session 400 on missing ID)
  - Multipart parser: text file, binary PNG
  - HTTP upload: success, too large, no file field, bad session
  - Approval API: pending, inject+deny, inject+session-approve
  - Stream status endpoint
  - File listing, path traversal block

Sprint 2 tests (test_sprint2.py):
  - GET /api/file/raw: PNG, JPEG, SVG content-types
  - GET /api/file/raw: path traversal blocked, missing file 404
  - GET /api/file: markdown content returned as text
  - GET /api/file: markdown with table content preserved
  - GET /api/list: image and markdown files appear in directory listing

Manual-only tests (not covered by automation):
  - All browser rendering tests (T5.5, T5.6, T5.7 -- visual verification)
  - T7.1-T7.3 -- tool approval card (requires live agent execution)
  - T9.1 -- reconnect banner timing
  - T10.1 -- concurrent session switching
  - T11.1, T11.2 -- responsive layout
  - T11.3 -- log output verification
  - All visual/CSS checks

---

## Claude Browser Agent Instructions

If you are a Claude agent with browser access, follow these instructions:

1. Navigate to http://localhost:8787 (assumes SSH tunnel is active from your Mac).
2. Execute tests in order. For each test:
   a. Follow SETUP instructions.
   b. Perform each STEP using browser tools (click, type, navigate).
   c. Use browser_vision or browser_snapshot to verify EXPECT conditions.
   d. Record PASS or FAIL with a brief note on what you observed.
3. For tests requiring file uploads: use the browser's file picker; you may need to
   create test files in /tmp first via terminal.
4. For T7.x (approval tests): the agent running inside Hermes needs to detect a
   dangerous command. Ask Hermes to "run: rm -rf /tmp/test_hermes_approval" and watch
   for the card. The actual rm will not run in a safe test workspace.
5. Skip T9.1 (reconnect banner) unless you can precisely time a page reload during an
   active SSE stream.
6. Report results as a checklist at the end.

---

*Last updated: Sprint 2, March 30, 2026*
*Server version: v0.4 (server.py in webui-mvp/)*
*Run automated tests: cd /home/hermes/.hermes/hermes-agent && venv/bin/python -m pytest webui-mvp/tests/ -v*

---

## Section 13: Sidebar Panel Navigation (Sprint 3)

### T13.1: Four Tabs Visible in Sidebar
EXPECT:
  - Four tabs at top of sidebar: Chat, Tasks, Skills, Memory (with icons)
  - Chat tab is active/highlighted by default on load
FAIL: Only one section visible, no tabs shown.

### T13.2: Tab Switching Preserves Chat State
STEPS:
  1. Send a message, get a response (so chat has content)
  2. Click the "Tasks" tab
  3. Click the "Chat" tab
EXPECT:
  - When on Tasks tab: session list disappears, cron list appears
  - When back on Chat: full conversation is still there, session list returns
  - Send button still works after switching back
FAIL: Messages lost, session list blank, Send button broken.

---

## Section 14: Tasks Panel (Cron Viewer) (Sprint 3)

### T14.1: Tasks Tab Shows Cron Jobs
STEPS:
  1. Click the "Tasks" tab in the sidebar
EXPECT:
  - A list of scheduled jobs appears
  - Each job shows its name and a status badge (active/paused/error/off)
  - At least one job visible ("Morning Crypto Update" or similar)
FAIL: Empty list, loading spinner forever, error message.

### T14.2: Expand a Job Row
STEPS:
  1. Click a job name/row in the Tasks panel
EXPECT:
  - Row expands to show: schedule string, prompt text (truncated), action buttons
  - "Run now" button (blue), "Pause" or "Resume" button
  - Last output section showing timestamp and content from the last run
FAIL: Click does nothing, no expansion, buttons missing.

### T14.3: Pause and Resume a Job
STEPS:
  1. Expand an active job
  2. Click "Pause"
EXPECT:
  - Toast: job paused
  - Job list reloads, status badge changes to "paused"
  - Button changes to "Resume"
STEPS (continued):
  3. Click "Resume"
EXPECT:
  - Toast: job resumed
  - Status badge returns to "active"
FAIL: Status doesn't change, error toast.

### T14.4: Last Output Shows Real Content
SETUP: At least one job has run at least once.
STEPS:
  1. Expand a job that has run
EXPECT:
  - Last output section shows a timestamp (e.g. "2026-03-30_16-00-37")
  - Content preview of the last run output (first ~600 chars)
FAIL: "(no runs yet)" shown even though job has run, or content is garbled.

---

## Section 15: Skills Panel (Sprint 3)

### T15.1: Skills Tab Shows Categorized List
STEPS:
  1. Click the "Skills" tab
EXPECT:
  - A search box at the top
  - Skills grouped by category with folder icons and count (e.g. "autonomous-ai-agents (4)")
  - Individual skill items with name and short description excerpt
FAIL: Loading forever, empty list, uncategorized blob.

### T15.2: Search Filters Skills
STEPS:
  1. Click the Skills tab
  2. Type "github" in the search box
EXPECT:
  - List narrows to only skills matching "github" in name, description, or category
  - Categories with no matches are hidden
STEPS (continued):
  3. Clear the search box
EXPECT:
  - Full list returns
FAIL: Search does nothing, list disappears entirely.

### T15.3: Click Skill Opens SKILL.md in Right Panel
STEPS:
  1. Click the Skills tab
  2. Click any skill name (e.g. "dogfood")
EXPECT:
  - The right workspace panel switches to preview mode
  - The skill's SKILL.md content renders as formatted markdown
  - Path bar shows the skill name with gold "skill" badge
  - Headings, bold, lists all render correctly (not raw asterisks)
FAIL: Nothing happens in right panel, raw markdown text shown, error.

---

## Section 16: Memory Panel (Sprint 3)

### T16.1: Memory Tab Shows Personal Notes
STEPS:
  1. Click the "Memory" tab
EXPECT:
  - Two sections: "My Notes" and "User Profile"
  - Each section has a last-modified timestamp in the header
  - Content renders as formatted markdown (not raw text)
FAIL: Blank, loading forever, error, raw text with § symbols.

### T16.2: Memory Content is Accurate
STEPS:
  1. Open Memory tab
  2. Check a fact you know is in memory (e.g. your Pacific Time preference)
EXPECT:
  - The fact appears in the correct section (User Profile or My Notes)
FAIL: Wrong content shown, different user's data, empty even though memory exists.

---

## Section 17: Bug Fix Verification (Sprint 3)

### T17.1: B6 - New Session Inherits Workspace
SETUP: Active session with workspace changed to a non-default path (e.g. /tmp).
STEPS:
  1. Click + New conversation while workspace is set to /tmp
EXPECT:
  - New session shows /tmp in the workspace chip and path display
  - File tree shows files from /tmp
FAIL: New session resets to default test-workspace.

### T17.2: B10 - Tool Events Replace Thinking Dots
SETUP: Send a message that will trigger tool use (e.g. "What files are in /tmp?").
STEPS:
  1. Watch the chat area carefully after sending
EXPECT:
  - Thinking dots appear immediately
  - When first tool fires: dots disappear, replaced by a compact "Running terminal..." row
  - When first token arrives: tool row disappears, streaming text begins
  - NOT: thinking dots AND "Running..." AND streaming text all visible at once
FAIL: Thinking dots stay while tool runs, or multiple rows stacked confusingly.

### T17.3: B14 - Cmd/Ctrl+K Creates New Chat
STEPS:
  1. Press Cmd+K (Mac) or Ctrl+K (Windows/Linux) while on the Chat tab
EXPECT:
  - A new "Untitled" session is created
  - Empty state shown, input focused
  - Does NOT work if a request is in-flight (Send is disabled)
FAIL: Nothing happens, browser intercepts shortcut for its own purpose, crash.

---

## Automated Test Coverage (Updated Sprint 3)

Sprint 3 tests (test_sprint3.py) - 21 tests:
  - Cron API: list, required fields, output, pause/resume validation, run nonexistent
  - Skills API: list, required fields, content for known skill, name required
  - Memory API: both files returned, string types, mtime keys present
  - Input validation: session/update, session/delete, chat/start require fields;
    chat/start requires message; session/update unknown ID returns 404
  - B6: new session with workspace param sets it correctly

Manual-only (not covered by automation):
  - T13.x - T16.x: all visual panel rendering tests
  - T17.1-T17.3: bug fix verification (B6/B10/B14)
  - All previous manual tests from Sections 1-12

---

*Last updated: Sprint 3, March 30, 2026*
*Total automated tests: 48/48*
*Run: cd /home/hermes/.hermes/hermes-agent && venv/bin/python -m pytest webui-mvp/tests/ -v*

---

## Section 18: Source Relocation Verification (Sprint 4)

### T18.1: UI Loads and CSS Renders Correctly After Relocation
STEPS:
  1. Open http://localhost:8787
EXPECT:
  - Dark background, correct colors, sidebar visible
  - Open DevTools Network tab: verify style.css loads from /static/style.css (not inline)
  - No CSS errors in console
FAIL: Plain unstyled HTML, white background, console errors about missing stylesheet.

---

## Section 19: Session Rename (Sprint 4)

### T19.1: Double-Click to Rename a Session
SETUP: At least one session in the sidebar.
STEPS:
  1. Double-click on a session title in the sidebar
EXPECT:
  - The title text is replaced by an editable input field, pre-selected
  - Input has a blue border style
  - Sidebar click does NOT navigate away (click is stopped)
FAIL: Nothing happens, navigation fires, input not focused.

### T19.2: Enter Commits the Rename
STEPS (continued from T19.1):
  1. Clear the input and type "My Renamed Session"
  2. Press Enter
EXPECT:
  - Input disappears, replaced by the new title text
  - Sidebar shows "My Renamed Session"
  - If this was the active session, topbar title also updates immediately
  - Page reload: title persists
FAIL: Title reverts, topbar doesn't update, enter not handled.

### T19.3: Escape Cancels the Rename
STEPS:
  1. Double-click a session to start editing
  2. Type some changes
  3. Press Escape
EXPECT:
  - Input disappears, original title restored (no change)
FAIL: Title changed despite Escape, crash.

### T19.4: Blur (Click Away) Commits the Rename
STEPS:
  1. Double-click a session, type a new name
  2. Click somewhere else on the page (not the input)
EXPECT:
  - New title is committed (same as pressing Enter)
FAIL: Title reverts on blur.

---

## Section 20: Session Search (Sprint 4)

### T20.1: Search Box Filters Sessions
SETUP: Multiple sessions with different titles (including at least one you renamed in T19).
STEPS:
  1. Verify the Chat tab is active in the sidebar
  2. Type part of a known session title in the "Filter conversations..." box
EXPECT:
  - List updates live as you type, showing only matching sessions
  - Non-matching sessions disappear
  - No network request (filtering is instant, client-side)
FAIL: All sessions shown regardless, search causes page reload, error.

### T20.2: Clear Search Restores Full List
STEPS (continued):
  1. Clear the search input
EXPECT:
  - Full session list returns immediately
FAIL: List stays filtered after clearing.

### T20.3: No Match Shows Empty List
STEPS:
  1. Type "zzznomatch9999" in the search box
EXPECT:
  - Session list is empty (no items)
  - No error message, just empty
FAIL: Error shown, crash, or unfiltered list still displayed.

---

## Section 21: Workspace File Operations (Sprint 4)

### T21.1: Delete Button Appears on File Hover
SETUP: Workspace has at least one file.
STEPS:
  1. Hover over a file in the right panel file tree
EXPECT:
  - A small trash icon appears on the right side of the file row
  - Hovering away hides it again
FAIL: No icon ever appears, icon always visible (not hover-only).

### T21.2: Delete a File with Confirmation
STEPS:
  1. Hover over a file and click its trash icon
  2. A browser confirm dialog appears: "Delete [filename]?"
  3. Click OK
EXPECT:
  - Toast: "Deleted [filename]"
  - File disappears from the tree
  - If the file was open in the preview panel, preview closes
FAIL: File not deleted, no confirmation dialog, error.

### T21.3: Cancel Delete Does Nothing
STEPS:
  1. Hover over a file and click its trash icon
  2. Click Cancel on the confirm dialog
EXPECT:
  - File remains in the tree
  - No toast, no error
FAIL: File deleted despite cancel.

### T21.4: Create a New File
STEPS:
  1. Click the + button in the workspace panel header
  2. A prompt dialog appears: "New file name (e.g. notes.md):"
  3. Type "test-sprint4.md" and click OK
EXPECT:
  - Toast: "Created test-sprint4.md"
  - File appears in the tree
  - File opens immediately in the preview panel
  - File is empty (no content)
FAIL: Nothing happens, error, file not created.

### T21.5: Create File Validates Name
STEPS:
  1. Click + for a new file
  2. Press Cancel or leave name empty
EXPECT:
  - Nothing created, no error
FAIL: Empty-named file created, crash.

---

## Automated Test Coverage (Updated Sprint 4)

Sprint 4 tests (test_sprint4.py) - 20 tests:
  - Relocation: server health, static CSS served, unknown static 404
  - Session rename: success, persistence, truncation, missing fields, unknown ID
  - Session search: matches found, empty query, no results
  - File create: success, missing fields, duplicate rejected
  - File delete: success, missing file 404, path traversal blocked
  - Validation: /api/list and /api/file require session_id and path

Total automated: 68/68 passing.

Manual-only for Sprint 4:
  - T18.1: visual CSS verification
  - T19.1-T19.4: inline rename UX
  - T20.1-T20.3: live search UX
  - T21.1-T21.5: file operations UX

---

*Last updated: Sprint 4, March 30, 2026*
*Total automated tests: 68/68*
*Run: cd /home/hermes/.hermes/hermes-agent && venv/bin/python -m pytest /home/hermes/webui-mvp/tests/ -v*
*Source: /home/hermes/webui-mvp/*

---

## Section 22: Workspace Management (Sprint 5)

### T22.1: Spaces Tab Shows Configured Workspaces
STEPS:
  1. Click the "Spaces" tab in the sidebar (folder icon)
EXPECT:
  - A list of workspaces with name and path
  - At least the default workspace (test-workspace) listed
  - An "Add workspace path" input at the bottom
  - Each workspace has "Use" and X (remove) buttons
FAIL: Empty panel, loading forever, error.

### T22.2: Add a Valid Workspace Path
STEPS:
  1. Type "/tmp" in the add input and click + Add
EXPECT:
  - "/tmp" appears in the list with name "tmp"
  - Toast: "Workspace added"
FAIL: Error shown, path not saved, no feedback.

### T22.3: Add an Invalid Path is Rejected
STEPS:
  1. Type "/this/path/does/not/exist" in the add input and click + Add
EXPECT:
  - Status bar shows an error (e.g. "Path does not exist")
  - Path is NOT added to the list
FAIL: Invalid path added, no error.

### T22.4: Remove a Workspace
STEPS:
  1. Click the X button next to any non-default workspace
  2. Confirm the dialog
EXPECT:
  - Workspace disappears from the list
  - Toast: "Workspace removed"
FAIL: Workspace stays, no confirmation, error.

### T22.5: Topbar Workspace Chip is a Dropdown
STEPS:
  1. In any active chat session, look at the topbar right side
  2. Click the workspace chip (shows folder icon + workspace name + arrow)
EXPECT:
  - A dropdown appears listing all configured workspaces
  - Current workspace is highlighted
  - A "Manage workspaces" link at the bottom
FAIL: Nothing happens, no dropdown, error.

### T22.6: Quick-Switch Workspace via Topbar
SETUP: At least two workspaces configured (add /tmp if needed via Spaces tab).
STEPS:
  1. Click the workspace chip to open the dropdown
  2. Click a different workspace than the current one
EXPECT:
  - Dropdown closes
  - Toast: "Switched to [name]"
  - Workspace chip in topbar updates to the new workspace name
  - File tree in right panel refreshes to show files in the new workspace
  - Current session is now using the new workspace
FAIL: Workspace chip doesn't update, file tree unchanged, error.

### T22.7: New Session Inherits Last Used Workspace
SETUP: Switch to a non-default workspace (e.g. /tmp) via the topbar dropdown.
STEPS:
  1. Click + New conversation
EXPECT:
  - New session's workspace chip shows the same workspace you last switched to (/tmp)
  - File tree shows files from /tmp
FAIL: New session defaults back to test-workspace.

---

## Section 23: Copy Message to Clipboard (Sprint 5)

### T23.1: Copy Icon Appears on Hover
SETUP: A chat session with at least one message.
STEPS:
  1. Hover over any chat message (user or assistant)
EXPECT:
  - A small clipboard icon appears in the message header row (right side of "You" or "Hermes")
  - Icon is not visible when not hovering
FAIL: No icon ever appears, always visible.

### T23.2: Copy Puts Text in Clipboard
STEPS:
  1. Hover over an assistant message
  2. Click the clipboard icon
EXPECT:
  - Icon briefly shows a checkmark (✓) then reverts to clipboard
  - Paste (Cmd+V) elsewhere shows the full text of that message
FAIL: No visual feedback, clipboard empty or wrong content.

---

## Section 24: Inline File Editor (Sprint 5)

### T24.1: Code File Opens in Read-Only Mode
STEPS:
  1. Click any .py, .js, or .txt file in the workspace file tree
EXPECT:
  - File content shows in read-only monospace view
  - An "✎ Edit" button is visible in the preview path bar
  - Content is NOT editable (clicking in it does nothing)
FAIL: Content immediately editable, no Edit button.

### T24.2: Edit Button Enters Edit Mode
STEPS:
  1. Click "✎ Edit" on a code file preview
EXPECT:
  - Read-only view replaced by an editable textarea
  - Content of the file is pre-populated in the textarea
  - Button changes to "💾 Save"
FAIL: Nothing changes, button doesn't change.

### T24.3: Save Writes Changes to Disk
STEPS:
  1. In edit mode, change some text
  2. Click "💾 Save"
EXPECT:
  - Read-only view returns, showing the updated content
  - Toast: "Saved"
  - Verify: refreshing the file tree and reopening the file shows the new content
FAIL: Save does nothing, content reverts, error.

### T24.4: Dirty Indicator Shows Unsaved Changes
STEPS:
  1. Enter edit mode on a file
  2. Make any change (type a character)
EXPECT:
  - Button shows "💾 Save*" (asterisk indicates unsaved changes)
FAIL: No asterisk, button stays as "💾 Save".

### T24.5: Markdown File Edit-Save Roundtrip
STEPS:
  1. Click a .md file in the workspace
  2. Click Edit
  3. Add a new line: "## Added by Sprint 5 test"
  4. Click Save
EXPECT:
  - Save succeeds (toast)
  - Markdown view re-renders showing the new heading
FAIL: Save fails, markdown not re-rendered.

---

## Automated Test Coverage (Updated Sprint 5)

Sprint 5 tests (test_sprint5.py) - 18 tests:
  - Phase A: app.js served correctly
  - Workspace CRUD: list, add valid, add invalid path, add invalid dir, add duplicate, requires path, remove, rename, rename unknown
  - Last workspace tracking: updates on session/update, new session inherits last
  - File save: success, missing fields, nonexistent file 404, path traversal blocked
  - Session index: created after save, sessions sorted correctly

Total automated: 86/86 passing.

Manual-only for Sprint 5:
  - T22.1-T22.7: workspace UI (tabs, dropdown, switching, inheritance)
  - T23.1-T23.2: copy to clipboard UX
  - T24.1-T24.5: inline file editor UX

---

*Last updated: Sprint 5, March 30, 2026*
*Total automated tests: 86/86*
*Run: cd /home/hermes/.hermes/hermes-agent && venv/bin/python -m pytest /home/hermes/webui-mvp/tests/ -v*
*Source: /home/hermes/webui-mvp/ | Static: static/style.css + static/app.js*

---

## Section 25: UI Polish Pass (Post-Sprint 5 Visual Audit)

These are visual regression checks. Take a screenshot after loading the UI and compare
against each criterion below. A Claude browser agent can verify these with browser_vision.

### T25.1: Sidebar Nav Tabs are Icon-Only
EXPECT:
  - Five icon-only tabs in the sidebar nav row: 💬 ⏱️ 📚 🧠 📁
  - No text labels visible by default (text removed to prevent overflow)
  - Hovering a tab shows a tooltip with the label (Chat/Tasks/Skills/Memory/Spaces)
  - Active tab has a blue underline, icon brighter blue
  - All five icons fit in the sidebar width without clipping
FAIL: Text labels showing alongside icons causing overflow, "Spaces" tab cut off.

### T25.2: Message Role Labels are Softer
EXPECT:
  - "You" and "Hermes" labels appear in slightly muted blue/gold (not full-brightness)
  - Labels use Title Case not ALL CAPS
  - Role icons are circles (not squares) with a subtle border
  - The role label area does not visually overpower the message content below
FAIL: Bright gold "HERMES" and blue "YOU" in caps drawing eye away from content.

### T25.3: Code Blocks Have a Connected Language Header
EXPECT:
  - When a code block has a language, the header bar is connected (no gap) to the code
  - Header has a small colored dot on the left and the language name
  - Background: slightly lighter than the code body to distinguish it
FAIL: Header floats above the code block with visible gap.

### T25.4: Send Button Has Depth
EXPECT:
  - Send button has a subtle gradient (lighter at top, slightly darker at bottom)
  - Hover: button shifts very slightly upward (1px transform)
  - Visual distinction from the blue link/chip color
FAIL: Send button is same flat blue as all other blue elements.

### T25.5: Session List Shows Date Groups
EXPECT:
  - Sessions grouped under "Today", "Yesterday", "Earlier" headers
  - Headers are small, all-caps, muted gray
  - Active session has a blue left border accent
FAIL: No grouping, flat list, no visual hierarchy.

### T25.6: Empty State Logo is Distinct
EXPECT:
  - The "H" logo on the empty state is a frosted-glass circle outline style (blue tint)
  - NOT the same orange/red gradient as the sidebar header logo
  - They should look different so it's clear they're two different things
FAIL: Both logos look identical.

### T25.7: New Conversation Button is Blue-Tinted
EXPECT:
  - The "New conversation" button has a subtle blue background tint
  - Blue-colored text (not plain white/muted)
  - Feels clickable and primary (not the same style as the secondary sm-btn buttons)
FAIL: Button looks same as other secondary buttons.

### T25.8: Suggestion Buttons Slide on Hover
EXPECT:
  - On the empty state, hovering a suggestion button shifts it slightly right (2px)
  - Border turns blue on hover
  - Subtle background tint on hover
FAIL: No hover movement, generic hover state.

### T25.9: Toast Notification is Premium
EXPECT:
  - Toast appears at bottom center with blur/frosted-glass background
  - Subtle shadow behind the toast
  - Toast slightly floats up (translateY) when appearing
FAIL: Plain flat dark box, no blur, no movement.

### T25.10: Composer Input Has Glow on Focus
EXPECT:
  - Clicking in the composer/message input shows a blue glow ring around the box
  - (box-shadow: 0 0 0 3px rgba(124,185,255,0.08))
  - Border also brightens to a stronger blue
FAIL: Only border color change, no glow ring.

---

## Section 26: Resizable Panels (Sprint 6)

### T26.1: Sidebar Can Be Resized by Dragging
STEPS:
  1. Hover over the right edge of the left sidebar (between sidebar and chat area)
EXPECT:
  - Cursor changes to col-resize (double-headed horizontal arrow)
  - A subtle blue glow appears on the edge
STEPS (continued):
  2. Click and drag the edge to the right
EXPECT:
  - Sidebar widens in real time as you drag
  - Chat area compresses to compensate
  - Minimum width ~180px, maximum ~420px
  - Releasing the mouse commits the new width
  - Hard-refreshing the page restores the saved width
FAIL: Cursor doesn't change, panel doesn't resize, width not saved.

### T26.2: Workspace Panel Can Be Resized by Dragging
STEPS:
  1. Hover over the left edge of the right workspace panel
  2. Drag left to narrow, drag right to widen
EXPECT:
  - Panel resizes within 180-500px range
  - Width persists across page reload (stored in localStorage)
FAIL: Panel not resizable.

---

## Section 27: Cron Job Create (Sprint 6)

### T27.1: New Job Button Opens Form
STEPS:
  1. Click the Tasks tab in the sidebar
  2. Click the "+ New job" button at the top of the Tasks panel
EXPECT:
  - A form slides in below the header with:
    - Name input (optional)
    - Schedule input (cron expression or natural language)
    - Prompt textarea
    - Delivery target dropdown
    - Create job and Cancel buttons
FAIL: Nothing happens, no form appears.

### T27.2: Create a Job Successfully
STEPS:
  1. Open the create form (T27.1)
  2. Fill in: Name "Test Job", Schedule "every 999h", Prompt "Say hello"
  3. Click Create job
EXPECT:
  - Form closes
  - Toast: "Job created ✓"
  - New job appears in the cron list with status "active"
FAIL: Error shown, job not created, form stays open.

### T27.3: Invalid Schedule Shows Error
STEPS:
  1. Open the create form
  2. Leave schedule empty or type "not_a_schedule"
  3. Click Create job
EXPECT:
  - Error message appears below the form: "Schedule is required" or parse error
  - Form stays open (not dismissed)
FAIL: Form closes without feedback, generic error.

### T27.4: Cancel Closes Form Without Creating
STEPS:
  1. Open the create form, fill in some fields
  2. Click Cancel
EXPECT:
  - Form closes
  - No new job created
  - No toast
FAIL: Job created, form doesn't close.

---

## Section 28: Session JSON Export (Sprint 6)

### T28.1: JSON Export Button Downloads File
SETUP: Active session with at least a few messages.
STEPS:
  1. Click the "JSON" button in the sidebar footer (next to Transcript)
EXPECT:
  - Browser downloads a file named hermes-{session_id}.json
  - Opening the file shows valid JSON with: session_id, title, messages array,
    workspace, model, created_at, updated_at
  - messages array contains objects with role and content fields
FAIL: No download triggered, file is empty, file is not valid JSON.

### T28.2: Escape Cancels File Editor
SETUP: Open a text file in the workspace right panel (click any .py or .md file).
STEPS:
  1. Click "Edit" button in the preview path bar
  2. Make some changes in the textarea
  3. Press Escape
EXPECT:
  - Textarea disappears, read-only view returns
  - Original content is shown (changes discarded)
  - File on disk is unchanged (verify by closing and reopening the file)
FAIL: Escape does nothing, changes are saved, crash.

---

## Automated Test Coverage (Updated Sprint 6)

Sprint 6 tests (test_sprint6.py) - 16 tests:
  - Phase E: HTML served from static/index.html, server.py has no inline HTML
  - Phase D: approval/respond requires session_id and valid choice; file/raw validates session
  - Cron create: requires prompt, requires schedule, invalid schedule returns 400, success
  - Session export: requires session_id, unknown session 404, valid JSON with session_id
  - Resize: static files contain resize handles and resize JS logic

Total automated: 106/106 passing.

Manual-only for Sprint 6:
  - T26.1-T26.2: resize drag UX
  - T27.1-T27.4: cron create form UX
  - T28.1: JSON export download
  - T28.2: Escape from file editor

---

*Last updated: Sprint 7, March 31, 2026*
*Total automated tests: 125/125*
*Run: cd /home/hermes/.hermes/hermes-agent && venv/bin/python -m pytest /home/hermes/webui-mvp/tests/ -v*
*Source: /home/hermes/webui-mvp/*
*Static: static/index.html + static/style.css + static/app.js*


---

## Section 29: Cron Edit and Delete (Sprint 7)

### T29.1: Edit Button Opens Inline Form
SETUP: At least one cron job exists. Tasks tab open.
STEPS:
  1. Click a cron job to expand it
  2. Click the "Edit" (pencil) button
EXPECT:
  - An inline form appears inside the expanded cron body
  - Name, schedule, and prompt fields are pre-filled with current values
FAIL: Nothing happens, new form not shown.

### T29.2: Save Edit Updates the Job
STEPS (continued from T29.1):
  1. Change the name field to "Renamed Job"
  2. Click Save
EXPECT:
  - Form closes, toast "Job updated ✓"
  - Job header shows new name
FAIL: Save fails, name unchanged.

### T29.3: Delete Button Removes the Job
SETUP: A cron job you can safely delete (or a test job created for this).
STEPS:
  1. Expand the job, click "Delete"
  2. Confirm the dialog
EXPECT:
  - Toast: "Job deleted"
  - Job disappears from the list
FAIL: Job stays, no confirmation dialog.

---

## Section 30: Skill Create and Edit (Sprint 7)

### T30.1: New Skill Button Opens Form
STEPS:
  1. Click the Skills tab
  2. Click "+ New skill" button
EXPECT:
  - A form appears with name, category, and content textarea fields
FAIL: Nothing happens.

### T30.2: Create a Skill and See it in List
STEPS:
  1. Fill in: Name "test-ui-skill", Content "---
name: test-ui-skill
description: UI test.
tags: [test]
---

# Test"
  2. Click Save skill
EXPECT:
  - Toast "Skill created ✓", form closes
  - Skill appears in the skills list
FAIL: Error, skill not in list.

### T30.3: Cancel Closes Form Without Creating
STEPS:
  1. Open new skill form, fill some fields, click Cancel
EXPECT:
  - Form closes, no skill created
FAIL: Skill created, form stays.

---

## Section 31: Memory Inline Edit (Sprint 7)

### T31.1: Edit Button Opens Memory Edit Form
STEPS:
  1. Click the Memory tab
  2. Click the "Edit" (pencil) button in the header
EXPECT:
  - An edit form appears below the memory panel with a textarea pre-filled with MEMORY.md content
FAIL: Nothing happens, no form.

### T31.2: Save Writes Changes
STEPS:
  1. In edit mode, add a line to the textarea
  2. Click Save
EXPECT:
  - Toast "Memory saved ✓", form closes
  - Memory panel reloads showing the updated content
FAIL: Save fails, content unchanged.

### T31.3: Cancel Discards Changes
STEPS:
  1. Open edit form, change content, click Cancel
EXPECT:
  - Form closes, original content unchanged
FAIL: Changes saved despite cancel.

---

## Automated Test Coverage (Updated Sprint 7)

Sprint 7 tests (test_sprint7.py) - 19 tests:
  - Health: active_streams field, uptime_seconds field
  - Session search: empty query, content+depth params accepted, count returned
  - Cron update: requires job_id, unknown job 404
  - Cron delete: requires job_id, unknown job 404
  - Skill save: requires name, requires content, invalid name rejected, create+delete roundtrip
  - Skill delete: requires name, unknown skill 404
  - Memory write: requires section, requires content, invalid section, write+read roundtrip

Total automated: 125/125 passing.

Manual-only for Sprint 7:
  - T29.1-T29.3: cron edit/delete UX
  - T30.1-T30.3: skill create UX
  - T31.1-T31.3: memory edit UX

---

*Last updated: Sprint 7, March 31, 2026*
*Total automated tests: 125/125*
*Run: cd /home/hermes/.hermes/hermes-agent && venv/bin/python -m pytest /home/hermes/webui-mvp/tests/ -v*
*Source: /home/hermes/webui-mvp/*