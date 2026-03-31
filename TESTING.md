# Hermes Co-Work Web UI: Browser Testing Plan

> This document is for manual browser testing by you or by a Claude browser agent.
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
