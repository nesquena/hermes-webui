     1|# Hermes Co-Work Web UI: Browser Testing Plan
     2|
     3|> This document is for manual browser testing by you or by a Claude browser agent.
     4|> It covers every user-facing feature of the UI through Sprint 2.
     5|> Each section is written as a step-by-step test procedure with expected outcomes.
     6|> A browser agent (e.g. Claude with Chrome access) can execute this plan directly.
     7|>
     8|> Prerequisites: SSH tunnel is active on port 8787. Open http://localhost:8787 in browser.
     9|> Server health check: curl http://127.0.0.1:8787/health should return {"status":"ok"}.
    10|
    11|---
    12|
    13|## How to Use This Document
    14|
    15|Each test has:
    16|- SETUP: what to do before the test
    17|- STEPS: numbered actions to perform
    18|- EXPECT: what you should see (pass criteria)
    19|- FAIL: what a failure looks like
    20|
    21|Work through sections in order. Each section builds on the previous.
    22|
    23|---
    24|
    25|## Section 1: Initial Load and Empty State
    26|
    27|### T1.1: Fresh Load Shows Empty State
    28|SETUP: Clear localStorage (DevTools > Application > Local Storage > delete hermes-webui-session) or open in incognito.
    29|STEPS:
    30|  1. Navigate to http://localhost:8787
    31|EXPECT:
    32|  - Dark background, Hermes logo in sidebar header
    33|  - Center area shows "What can I help with?" heading with suggestion buttons
    34|  - Session list in sidebar is empty or shows existing sessions
    35|  - No session is highlighted active
    36|  - Send button is present but there is no input focus by default
    37|FAIL: Page shows error, blank white screen, or auto-creates a new session without user action.
    38|
    39|### T1.2: Suggestion Buttons Work
    40|SETUP: T1.1 complete, no active session.
    41|STEPS:
    42|  1. Click "What files are in this workspace?" suggestion button
    43|EXPECT:
    44|  - A new session is created automatically (since none existed)
    45|  - The text "What files are in this workspace?" appears as the user message
    46|  - Thinking dots appear below the user message
    47|  - After a few seconds, Hermes responds
    48|FAIL: Button does nothing, error appears, or page crashes.
    49|
    50|---
    51|
    52|## Section 2: Session Management
    53|
    54|### T2.1: Create New Session via + Button
    55|SETUP: Any state.
    56|STEPS:
    57|  1. Click the "+ New conversation" button in the sidebar
    58|EXPECT:
    59|  - A new session named "Untitled" appears highlighted in the session list
    60|  - The center area shows the empty state ("What can I help with?")
    61|  - The + button is the ONLY way to create a session (no auto-create on load)
    62|FAIL: Multiple sessions created, error thrown, or empty state not shown.
    63|
    64|### T2.2: Send a Message and See Response
    65|SETUP: Active session exists.
    66|STEPS:
    67|  1. Click in the message input at the bottom
    68|  2. Type: "Say hello in exactly three words"
    69|  3. Press Enter (not Shift+Enter)
    70|EXPECT:
    71|  - User message appears immediately in chat
    72|  - Thinking dots (three animated dots) appear below
    73|  - Status bar shows "Hermes is thinking..."
    74|  - Send button becomes disabled (grayed out)
    75|  - Within 10-30 seconds, Hermes responds with a three-word greeting
    76|  - Thinking dots disappear
    77|  - Send button re-enables
    78|  - Session title in sidebar updates to reflect the first message
    79|FAIL: Message never appears, thinking dots never go away, Send button stays disabled forever.
    80|
    81|### T2.3: Shift+Enter Creates Newline (Does Not Send)
    82|SETUP: Active session, input focused.
    83|STEPS:
    84|  1. Click the message input
    85|  2. Type "Line one"
    86|  3. Press Shift+Enter
    87|  4. Type "Line two"
    88|EXPECT:
    89|  - Two lines of text appear in the input box
    90|  - No message is sent (no user message appears in chat)
    91|FAIL: Message sent on Shift+Enter.
    92|
    93|### T2.4: Reload Restores Session
    94|SETUP: A session exists with at least one exchange (user + assistant).
    95|STEPS:
    96|  1. Note the session title and last message content
    97|  2. Reload the page (Cmd+R or F5)
    98|EXPECT:
    99|  - The same session loads automatically (no empty state)
   100|  - All messages from before the reload are visible
   101|  - Session title in topbar and sidebar matches what it was before
   102|FAIL: Session lost on reload, empty state shown, or messages missing.
   103|
   104|### T2.5: Delete Active Session
   105|SETUP: At least two sessions exist. One is active.
   106|STEPS:
   107|  1. Hover over the active session in the sidebar (trash icon appears)
   108|  2. Click the trash icon on the active session
   109|EXPECT:
   110|  - A "Conversation deleted" toast appears at the bottom for ~3 seconds
   111|  - The deleted session disappears from the sidebar list
   112|  - The next most recent session automatically loads (or empty state if none remain)
   113|  - NO new session is auto-created
   114|FAIL: Session not removed, new session auto-created, error shown, or wrong session loaded.
   115|
   116|### T2.6: Delete Non-Active Session
   117|SETUP: At least two sessions exist. Session B is not active.
   118|STEPS:
   119|  1. Hover over a session that is NOT currently active
   120|  2. Click its trash icon
   121|EXPECT:
   122|  - Toast "Conversation deleted" appears
   123|  - That session disappears from list
   124|  - Currently active session remains active and unchanged
   125|FAIL: Active session changes, multiple sessions deleted, or error.
   126|
   127|### T2.7: Delete Last Session Shows Empty State
   128|SETUP: Exactly one session exists.
   129|STEPS:
   130|  1. Delete that session via the trash icon
   131|EXPECT:
   132|  - Session list is empty
   133|  - Center area shows "What can I help with?" empty state
   134|  - No session is auto-created
   135|FAIL: New session created, error thrown, or UI breaks.
   136|
   137|---
   138|
   139|## Section 3: Model Selection
   140|
   141|### T3.1: Model Dropdown Shows All Options
   142|SETUP: Any active session.
   143|STEPS:
   144|  1. Look at the sidebar bottom: "Model" label and a dropdown
   145|  2. Click the dropdown to expand it
   146|EXPECT:
   147|  - Provider groups visible: OpenAI, Anthropic, Other
   148|  - OpenAI group: GPT-5.4 Mini, GPT-4o, o3, o4-mini
   149|  - Anthropic group: Claude Sonnet 4.6, Claude Sonnet 4.5, Claude Haiku 3.5
   150|  - Other group: Gemini 2.5 Pro, DeepSeek V3, Llama 4 Scout
   151|FAIL: Only 2 options visible, no groups, or missing models.
   152|
   153|### T3.2: Model Chip Reflects Selection
   154|SETUP: Active session.
   155|STEPS:
   156|  1. Change model dropdown to "Claude Sonnet 4.6"
   157|EXPECT:
   158|  - The blue chip in the topbar right updates to "Sonnet 4.6" immediately
   159|  - NOT "GPT-5.4 Mini" (this was Bug B3, now fixed)
   160|STEPS (continued):
   161|  2. Change model to "Gemini 2.5 Pro"
   162|EXPECT:
   163|  - Chip updates to "Gemini 2.5 Pro" (not "GPT-5.4 Mini")
   164|FAIL: Chip shows wrong model name for any non-Sonnet selection.
   165|
   166|---
   167|
   168|## Section 4: File Upload
   169|
   170|### T4.1: Click-to-Attach Opens File Picker
   171|SETUP: Active session.
   172|STEPS:
   173|  1. Click the paperclip icon in the composer footer
   174|EXPECT:
   175|  - OS file picker dialog opens
   176|  - Accepted types filter visible (images, text, PDF, common code files)
   177|FAIL: Nothing happens, error thrown.
   178|
   179|### T4.2: Attach a Text File and Send
   180|SETUP: Have a small .txt or .py file ready to upload.
   181|STEPS:
   182|  1. Click the paperclip, select the file
   183|  2. File chip appears in the composer tray above the input
   184|  3. Type "What is in this file?" in the input
   185|  4. Press Enter
   186|EXPECT:
   187|  - Upload progress bar briefly appears
   188|  - User message shows the message text plus a file badge with the filename
   189|  - Hermes responds describing or reading the file content
   190|FAIL: Upload fails, file badge never appears, Hermes does not mention the file.
   191|
   192|### T4.3: Drag and Drop a File
   193|SETUP: Active session, a file ready on your desktop.
   194|STEPS:
   195|  1. Drag a file from Finder/Explorer over the composer area
   196|EXPECT:
   197|  - Blue dashed border and "Drop files to upload to workspace" overlay appear
   198|STEPS (continued):
   199|  2. Drop the file
   200|EXPECT:
   201|  - File chip appears in the tray
   202|  - Overlay disappears
   203|FAIL: No drag visual feedback, file not accepted, error on drop.
   204|
   205|### T4.4: Paste Screenshot from Clipboard
   206|SETUP: Take a screenshot (Cmd+Shift+4 on Mac, saves to clipboard).
   207|STEPS:
   208|  1. Click in the message input
   209|  2. Press Cmd+V (paste)
   210|EXPECT:
   211|  - An image file chip appears in the tray: "screenshot-{timestamp}.png"
   212|  - Status bar briefly shows "Image pasted: screenshot-..."
   213|FAIL: Nothing pasted, error, or raw binary data appears in input.
   214|
   215|### T4.5: Remove a File from Tray
   216|SETUP: At least one file in the attach tray.
   217|STEPS:
   218|  1. Click the X button on a file chip in the tray
   219|EXPECT:
   220|  - That file is removed from the tray
   221|  - If it was the only file, tray collapses
   222|FAIL: File not removed, error.
   223|
   224|---
   225|
   226|## Section 5: Workspace File Browser
   227|
   228|### T5.1: File Tree Loads on Session Start
   229|SETUP: Active session with workspace set.
   230|EXPECT:
   231|  - Right panel shows "WORKSPACE" header
   232|  - File tree lists files and directories in the workspace
   233|  - Directories have folder icons
   234|  - Files have type-appropriate icons (camera for images, notepad for markdown, etc.)
   235|FAIL: Right panel is blank, error, or all files show generic icon regardless of type.
   236|
   237|### T5.2: Navigate Into a Directory
   238|SETUP: Workspace has at least one subdirectory.
   239|STEPS:
   240|  1. Click a directory name in the file tree
   241|EXPECT:
   242|  - File tree updates to show contents of that directory
   243|  - A ".." or breadcrumb is NOT shown (current behavior: flat navigation)
   244|FAIL: Click does nothing, error, or entire page breaks.
   245|
   246|### T5.3: Preview a Code File
   247|SETUP: Workspace has a .py or .js or .txt file.
   248|STEPS:
   249|  1. Click the file name in the tree
   250|EXPECT:
   251|  - Right panel switches from file tree to preview area
   252|  - File path shown at top with file extension badge (gray)
   253|  - File contents shown as monospace text (raw code)
   254|  - File tree is hidden, preview is visible
   255|FAIL: Nothing happens, binary gibberish shown, crash.
   256|
   257|### T5.4: Close Preview Returns to File Tree
   258|SETUP: T5.3 complete, preview is showing.
   259|STEPS:
   260|  1. Click the X button in the panel header
   261|EXPECT:
   262|  - Preview closes
   263|  - File tree is visible again
   264|  - Preview area is hidden
   265|  - Reopening the same file shows fresh content (no stale cached text)
   266|FAIL: X button does nothing, tree does not reappear.
   267|
   268|### T5.5: Preview an Image File (Sprint 2)
   269|SETUP: Upload a PNG, JPG, or any image file to the workspace, OR the workspace already contains one.
   270|STEPS:
   271|  1. Click an image file (e.g. .png or .jpg) in the file tree
   272|EXPECT:
   273|  - Preview area shows the actual image rendered inline (NOT a blob of bytes or placeholder)
   274|  - Image is centered, fits within the panel width
   275|  - Path bar shows "image" badge in blue
   276|  - Image maintains aspect ratio
   277|FAIL: Raw binary text displayed, broken image icon, error message, or nothing happens.
   278|
   279|### T5.6: Preview a Markdown File (Sprint 2)
   280|SETUP: Workspace has a .md file (or create one: upload a file named README.md with some markdown content).
   281|STEPS:
   282|  1. Click the .md file in the file tree
   283|EXPECT:
   284|  - Preview shows formatted, rendered markdown (NOT raw text with asterisks)
   285|  - Headings render as large bold text
   286|  - **bold** renders as bold, *italic* as italic
   287|  - Bullet lists render as actual list items
   288|  - Code blocks render in monospace with dark background
   289|  - Path bar shows "md" badge in gold
   290|FAIL: Raw markdown text with asterisks/hashes shown, or no preview at all.
   291|
   292|### T5.7: Markdown Preview Renders Tables (Sprint 2)
   293|SETUP: Upload or create a .md file with a table like:
   294|  | Name | Value |
   295|  |------|-------|
   296|  | foo  | bar   |
   297|STEPS:
   298|  1. Click the file in the file tree
   299|EXPECT:
   300|  - Table renders as an actual HTML table with borders
   301|  - Column headers (Name, Value) are bold/highlighted
   302|  - Data rows alternate subtle background
   303|FAIL: Table displayed as raw pipe-separated text.
   304|
   305|### T5.8: Refresh Files Button
   306|SETUP: Active session, workspace has files.
   307|STEPS:
   308|  1. Click the "Files" refresh button in the sidebar bottom actions
   309|EXPECT:
   310|  - File tree reloads
   311|  - If a file was added externally, it now appears
   312|FAIL: Error, spinner never stops, tree clears without reloading.
   313|
   314|---
   315|
   316|## Section 6: Workspace Path
   317|
   318|### T6.1: Change Workspace Path
   319|SETUP: Active session.
   320|STEPS:
   321|  1. Click the workspace path display in the sidebar bottom (shows current path)
   322|  2. A prompt dialog appears
   323|  3. Enter a new valid path (e.g. /tmp)
   324|  4. Click OK
   325|EXPECT:
   326|  - Workspace chip in topbar updates to show the last segment of the new path
   327|  - File tree refreshes to show files at the new path
   328|  - Next message sent uses the new workspace
   329|FAIL: Dialog does not appear, path not saved, error on invalid path.
   330|
   331|---
   332|
   333|## Section 7: Tool Approval
   334|
   335|### T7.1: Dangerous Command Shows Approval Card
   336|SETUP: Active session with a test-workspace (NOT a production directory).
   337|STEPS:
   338|  1. Type: "Run the command: rm -rf /tmp/hermes_test_delete_me"
   339|  2. Send the message
   340|EXPECT:
   341|  - Thinking dots appear
   342|  - An orange/red approval card appears above the composer:
   343|    "Dangerous command - approval required"
   344|  - The card shows the command text
   345|  - The card shows the pattern description (e.g. "recursive delete [recursive_delete]")
   346|  - Four buttons: Allow once, Allow this session, Always allow, Deny
   347|FAIL: No card appears, agent executes without asking, page crashes.
   348|
   349|### T7.2: Deny Approval Blocks the Command
   350|SETUP: T7.1 complete, card is showing.
   351|STEPS:
   352|  1. Click "Deny"
   353|EXPECT:
   354|  - Approval card disappears
   355|  - Agent responds with a message indicating the command was denied/blocked
   356|  - No file was deleted
   357|FAIL: Command executes despite deny, card stays up, error.
   358|
   359|### T7.3: Allow Once Executes the Command
   360|SETUP: Create a safe test: type "Run: touch /tmp/hermes_approval_test.txt"
   361|STEPS:
   362|  1. Send the message
   363|  2. When approval card appears, click "Allow once"
   364|EXPECT:
   365|  - Approval card disappears
   366|  - Agent continues and reports the command ran successfully
   367|  - Verify: open a terminal and run: ls /tmp/hermes_approval_test.txt
   368|FAIL: Command blocked after Allow once, card stays, error.
   369|
   370|---
   371|
   372|## Section 8: Transcript Download
   373|
   374|### T8.1: Download Conversation as Markdown
   375|SETUP: A session with at least 2 messages (1 user + 1 assistant).
   376|STEPS:
   377|  1. Click the "Transcript" download button in the sidebar bottom
   378|EXPECT:
   379|  - Browser downloads a .md file named hermes-{session_id}.md
   380|  - Opening the file shows the conversation in markdown format:
   381|    ## user
   382|    (message text)
   383|    ## assistant
   384|    (response text)
   385|FAIL: No download triggered, file is empty, file is corrupted JSON instead of markdown.
   386|
   387|---
   388|
   389|## Section 9: Reconnect Banner (Sprint 1 - B4/B5)
   390|
   391|### T9.1: Reconnect Banner After Mid-Stream Reload
   392|NOTE: This test requires deliberate timing. Best done with a slow/long agent request.
   393|SETUP: Active session.
   394|STEPS:
   395|  1. Send a message that will take a while (e.g. "Write me a 500-word short story")
   396|  2. While thinking dots are showing (within the first 5 seconds), reload the page (Cmd+R)
   397|EXPECT:
   398|  - Page reloads and restores the session
   399|  - A gold/amber banner appears near the top: "A response may have been in progress..."
   400|  - Two buttons on the banner: "Dismiss" and "Reload"
   401|  - Clicking "Reload" fetches fresh messages from server
   402|  - Clicking "Dismiss" removes the banner
   403|FAIL: No banner shown, page crashes, banner appears on normal reloads with no in-flight request.
   404|
   405|---
   406|
   407|## Section 10: Multi-Session and Concurrent Behavior
   408|
   409|### T10.1: Switch Sessions While Response Is Loading
   410|SETUP: Active session, agent running (thinking dots visible from a previous message).
   411|STEPS:
   412|  1. While thinking dots are showing, click a DIFFERENT session in the sidebar
   413|EXPECT:
   414|  - The new session loads cleanly (its messages show)
   415|  - The Send button for the NEW session is NOT disabled (it's not busy)
   416|  - The original session's response is still being generated in the background
   417|  - Clicking back to the original session shows the thinking dots still running
   418|  - When the original request finishes, its messages update correctly
   419|FAIL: New session shows busy state, switching breaks messages, response lands in wrong session.
   420|
   421|### T10.2: Multiple Sessions in List (Up to 30)
   422|SETUP: Create enough sessions to have at least 5 in the sidebar.
   423|EXPECT:
   424|  - Sessions listed most-recently-updated first
   425|  - Long titles truncate with "..." and do not overflow the sidebar width
   426|  - Hover shows the trash icon on any session
   427|FAIL: Titles overflow sidebar, order is wrong, trash icon never appears.
   428|
   429|---
   430|
   431|## Section 11: Visual and Layout Checks
   432|
   433|### T11.1: Right Panel Hidden on Small Screens
   434|STEPS:
   435|  1. Resize browser window to below 900px width
   436|EXPECT:
   437|  - Right panel (workspace) disappears
   438|  - Chat area expands to fill the full width
   439|FAIL: Right panel overlaps chat or causes horizontal scroll.
   440|
   441|### T11.2: Sidebar Hidden on Very Small Screens
   442|STEPS:
   443|  1. Resize browser window to below 640px width
   444|EXPECT:
   445|  - Left sidebar disappears
   446|  - Chat area takes full width
   447|FAIL: Sidebar causes layout overflow or blocks chat.
   448|
   449|### T11.3: Structured Log Output
   450|SETUP: SSH access to VPS.
   451|STEPS:
   452|  1. In a terminal: tail -f /tmp/webui-mvp.log
   453|  2. In browser: perform any action (load page, send message, click file)
   454|EXPECT:
   455|  - Log entries appear in terminal as JSON: {"ts":"...","method":"GET","path":"/health","status":200,"ms":0.1}
   456|  - Every request produces one log line
   457|  - Status codes are correct (200 for success, 400 for bad requests)
   458|FAIL: No log output, log shows Apache-style text instead of JSON, log file not created.
   459|
   460|---
   461|
   462|## Section 12: Error Handling
   463|
   464|### T12.1: Send Button Disabled When Busy
   465|SETUP: Message is sending (thinking dots visible).
   466|EXPECT:
   467|  - Send button is visually grayed out
   468|  - Pressing Enter does NOT send another message
   469|  - Clicking Send button does nothing
   470|FAIL: Multiple messages sent while one is in flight.
   471|
   472|### T12.2: Upload Failure Shows Status
   473|SETUP: Active session.
   474|STEPS:
   475|  1. Try to attach a file larger than 20MB (if available)
   476|EXPECT:
   477|  - Status bar shows an error message about file size or the upload is rejected
   478|  - The chat is not broken (can still send messages)
   479|FAIL: Uncaught error, page crashes, or no feedback given.
   480|
   481|### T12.3: File Preview for Binary Non-Image
   482|SETUP: Workspace has a .zip or .bin file.
   483|STEPS:
   484|  1. Click the binary file in the file tree
   485|EXPECT:
   486|  - Code preview shows some text (may be replacement characters for binary content)
   487|  - OR a "File too large" or "Could not open file" error in the status bar
   488|  - Page does NOT crash
   489|FAIL: Browser freezes, crash, or security issue.
   490|
   491|---
   492|
   493|## Automated Test Coverage Reference
   494|
   495|These behaviors are verified by pytest (run: venv/bin/python -m pytest webui-mvp/tests/ -v):
   496|
   497|Sprint 1 tests (test_sprint1.py):
   498|  - Server health, session CRUD (create/load/update/delete/sort)
   499|  - B11 footgun fix (/api/session 400 on missing ID)
   500|  - Multipart parser: text file, binary PNG
   501|