"""Regression tests for the Project OS injected extension.

These are intentionally source-level tests. The extension is a browser-injected,
stateful UI layer, and the stale-running-state bug is easiest to pin by keeping
the dedicated project-session lifecycle branches explicit.
"""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_JS = (REPO_ROOT / "extensions" / "project-os" / "project-os-extension.js").read_text(encoding="utf-8")
EXTENSION_CSS = (REPO_ROOT / "extensions" / "project-os" / "project-os-extension.css").read_text(encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This source-level extension test does not need the repo-wide test server."""
    yield


def test_project_os_workflow_root_seed_is_reference_only_done_anchor():
    start = EXTENSION_JS.index("const PROJECT_WORKFLOW_SEEDS = [")
    end = EXTENSION_JS.index("function getEmptySubmitState()", start)
    seeds = EXTENSION_JS[start:end]
    assert 'id: "root"' in seeds
    assert 'title: "Project OS import/resume workflow"' in seeds
    assert 'status: "done"' in seeds
    assert 'Reference-only anchor: preserve workflow continuity context for this board.' in seeds
    assert 'Do not decompose, spec, or treat this anchor as the next actionable triage card.' in seeds
    assert 'id: "first-actionable-child"' in seeds
    assert 'title: "가드레일 — decomposer가 루트 seed를 실행 태스크로 오해하지 않기"' in seeds
    assert 'status: "ready"' in seeds
    assert 'Seed: first-actionable-child' in seeds


def test_project_os_import_and_sync_refresh_existing_workflow_seed_output_before_dispatch():
    helper_start = EXTENSION_JS.index("function normalizeWorkflowSeedText(value) {")
    helper_end = EXTENSION_JS.index("function getSelectedTaskTitleFromDom() {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'function getWorkflowSeedTaskPatch(task, seed) {' in helper
    assert 'function findWorkflowSeedTask(tasks, seed) {' in helper
    assert 'body.includes(`Seed: ${seed?.id}`)' in helper
    assert 'const created = await api(`/api/kanban/tasks${boardQuery(boardSlug)}`' in helper
    assert 'createPayload.parents = [rootTask.id];' in helper
    assert 'created_by: "project-os-extension"' in helper
    assert 'patch.body = seed.body;' in helper
    assert 'patch.status = seed.status;' in helper
    assert 'await api(`/api/kanban/tasks/${encodeURIComponent(seedTask.id)}${boardQuery(boardSlug)}`' in helper
    assert 'state.boardData = await api(`/api/kanban/board${boardQuery(boardSlug)}`);' in helper
    assert 'async function resolveAuthoritativeControlPlaneBoard(currentBoardSlug = "", options = {}) {' in EXTENSION_JS
    assert 'const canonicalCurrentBoard = String(currentBoardSlug || "").trim();' in EXTENSION_JS
    assert 'const localCurrentBoard = String(state.currentBoard || "").trim();' in EXTENSION_JS
    assert 'const allowSessionCandidates = options.allowLinkedSessionBoard !== false;' in EXTENSION_JS
    assert 'const sessionCandidates = [state.projectSession, { session_id: state.projectSessionId }, { session_id: state.submit?.sessionId }]' in EXTENSION_JS
    assert 'if (!boardSlug || !boardExists(boardSlug) || isEmptyIdleProjectSession(session)) continue;' in EXTENSION_JS
    assert 'if (canonicalCurrentBoard && boardExists(canonicalCurrentBoard)) {' in EXTENSION_JS
    assert 'return localCurrentBoard && boardExists(localCurrentBoard) ? localCurrentBoard : null;' in EXTENSION_JS
    assert 'async function prepareControlPlaneBoardContext(options = {}) {' in EXTENSION_JS
    assert 'const boardsPayload = await api("/api/kanban/boards");' in EXTENSION_JS
    assert 'state.boards = boardsPayload.boards || [];' in EXTENSION_JS
    assert 'const authoritativeBoardSlug = await resolveAuthoritativeControlPlaneBoard(boardsPayload.current, options);' in EXTENSION_JS
    assert 'await reconcileVisibleKanbanBoard(authoritativeBoardSlug);' in EXTENSION_JS

    dispatch_start = EXTENSION_JS.index("async function dispatchProjectControlPlanePrompt(kind) {")
    dispatch_end = EXTENSION_JS.index("function fillNativeComposer(text) {", dispatch_start)
    dispatch = EXTENSION_JS[dispatch_start:dispatch_end]
    assert 'if (kind === "import" || kind === "sync") {' in dispatch
    assert 'const controlPlaneBoard = await prepareControlPlaneBoardContext({ allowLinkedSessionBoard: false });' in dispatch
    assert 'await syncWorkflowSeedTasks(controlPlaneBoard.boardSlug, controlPlaneBoard.boardData);' in dispatch
    assert 'startData = await sendProjectSessionPrompt(promptText);' in dispatch


def test_project_os_intake_surface_copy_matches_shipped_dedicated_session_affordance():
    helper_start = EXTENSION_JS.index("function getControlPlaneIntakeCopy(meta) {")
    helper_end = EXTENSION_JS.index("function loadPersisted()", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'Shipped intake uses dedicated project-session doc actions, not a multi-step wizard.' in helper
    assert 'createLabel: "Start blank docs"' in helper
    assert 'importLabel: "Recover current repo"' in helper
    assert 'resumeLabel: "Resume linked project"' in helper
    assert 'syncLabel: "Refresh docs"' in helper
    assert 'Choose a fresh import, reopen the linked project session, or refresh docs only.' in helper
    assert 'const primaryAction = linkedProjectSessionId ? "resume" : status === "active" ? "import" : "create";' in helper
    assert 'hasLinkedSession: Boolean(linkedProjectSessionId),' in helper
    assert 'primaryAction,' in helper



def test_project_os_import_prompt_rejects_stale_missing_recover_evidence_narrative():
    prompt_start = EXTENSION_JS.index("function buildImportProjectPrompt() {")
    prompt_end = EXTENSION_JS.index("function buildSyncControlPlanePrompt() {", prompt_start)
    prompt = EXTENSION_JS[prompt_start:prompt_end]
    assert (
        'If repo-local continuity or live runtime evidence already proves a real Recover current repo run happened, treat that proof as current truth and correct any older doc text that still says the evidence is missing.'
        in prompt
    )
    assert 'Do not copy stale pre-proof narrative forward just because an older PROJECT/PLAN/STATUS draft still contains it.' in prompt
    assert 'Live phase-1 topology is default/ops/builder only; do not introduce a live reviewer profile in repo-side continuity, assignment rules, or phase-boundary notes.' in prompt
    assert 'Treat builder as the active manual implementation lane rather than a reserve-only lane, but keep recurring cron/background ownership on the existing default/ops automation surfaces instead of builder.' in prompt
    assert 'Route code review through default or builder using repo-side review contracts and skills; route UX/UI/design review through Claude Design first, then hand implementation-facing findings back through DESIGN.md and docs/UIUX-GUIDE.md.' in prompt
    assert 'If the current board is still empty, seed the board itself — not only the docs.' in prompt
    assert 'Create a reference-only root workflow seed in done/archive and a separate first actionable child in ready on this same board.' in prompt
    assert 'Do not stop after doc refresh if board-local task output is still missing.' in prompt


def test_project_os_sync_prompt_pins_builder_and_review_routing_contracts():
    prompt_start = EXTENSION_JS.index("function buildSyncControlPlanePrompt() {")
    prompt_end = EXTENSION_JS.index("function getActiveProfileName() {", prompt_start)
    prompt = EXTENSION_JS[prompt_start:prompt_end]
    assert 'Start by re-reading .ax/handoff/current.json, .ax/handoff/current.md, docs/project-os/STATUS.md, and the live kanban board for this board; when they disagree, treat the live board closure state as canonical truth.' in prompt
    assert 'If the board advanced or the current acceptance card just closed, rewrite .ax/handoff/current.json, .ax/handoff/current.md, and docs/project-os/STATUS.md to that new live truth before proposing any later follow-up.' in prompt
    assert 'Live phase-1 topology is default/ops/builder only; do not drift repo-side docs back toward a live reviewer profile or reserve-only builder wording.' in prompt
    assert 'Builder is an active manual implementation lane, but recurring cron/background ownership stays off builder unless an explicit future contract changes that rule.' in prompt
    assert 'Route code review through default or builder plus repo review guidance, and route UX/UI/design review through Claude Design first before repo-side DESIGN.md / docs/UIUX-GUIDE.md implementation follow-through.' in prompt
    assert 'Do not thaw or narrate the next queue-frozen follow-up from this continuity refresh unless the live board already shows that card released; continuity repair comes before any new thaw.' in prompt



def test_project_os_workflow_runtime_payload_only_assigns_ready_seed_cards():
    start = EXTENSION_JS.index("function getWorkflowSeedRuntimePayload(seed) {")
    end = EXTENSION_JS.index("function getWorkspaceLabel()", start)
    helper = EXTENSION_JS[start:end]
    assert 'if (String(seed?.status || "").toLowerCase() !== "ready") return {};' in helper
    assert 'assignee: getActiveProfileName(),' in helper
    assert 'payload.workspace_kind = "dir";' in helper


def test_project_os_auto_loop_prefers_control_plane_import_for_new_empty_active_board():
    start = EXTENSION_JS.index("function getAutoIntakeAction(summary, meta) {")
    end = EXTENSION_JS.index("function buildLoopPromptFromSummary(summary, meta) {", start)
    helper = EXTENSION_JS[start:end]
    assert 'if (status !== "active") return "";' in helper
    assert 'if (summary.blockedItems.length || summary.counts.ready > 0 || summary.next.length > 0) return "";' in helper
    assert 'if (summary.counts.total !== 0) return "";' in helper
    assert 'const missingControlPlaneRefs = !meta?.projectRef && !meta?.planRef && !meta?.statusRef;' in helper
    assert 'const linkedProjectSessionId = String(state.projectSessionId || state.projectSession?.session_id || "").trim();' in helper
    assert 'return "import";' in helper

    loop_start = EXTENSION_JS.index("function deriveLoopState(summary, meta) {")
    loop_end = EXTENSION_JS.index("function getSummaryForRender() {", loop_start)
    loop = EXTENSION_JS[loop_start:loop_end]
    assert 'const autoIntakeAction = getAutoIntakeAction(summary, meta);' in loop
    assert 'if (!state.session?.id && !state.session?.title && getNativeComposer() && !autoIntakeAction) {' in loop
    assert 'category: "project_intake"' in loop
    assert 'controlPlaneAction: autoIntakeAction,' in loop
    assert 'buildImportProjectPrompt()' in loop
    assert 'Dispatch the shipped Recover current repo intake through the dedicated project session before generic loop planning.' in loop


def test_project_os_auto_loop_dispatches_control_plane_action_before_generic_prompt_route():
    start = EXTENSION_JS.index("async function maybeRunAutoLoop(force) {")
    end = EXTENSION_JS.index("function maybeRefreshForLoop() {", start)
    helper = EXTENSION_JS[start:end]
    assert 'if (!state.loop.canAutoRun || (!state.loop.prompt && !state.loop.controlPlaneAction) || !state.loop.signature) return false;' in helper
    assert 'if (state.loop.controlPlaneAction) {' in helper
    assert 'await dispatchProjectControlPlanePrompt(state.loop.controlPlaneAction);' in helper
    assert 'detail: `control-plane:${state.loop.controlPlaneAction}`,' in helper
    assert 'const route = routePromptToHermes(state.loop.prompt, true);' in helper


def test_project_os_selected_task_title_prefers_visible_kanban_detail_preview_over_body_fallback():
    start = EXTENSION_JS.index("function getSelectedTaskTitleFromDom() {")
    end = EXTENSION_JS.index("function deriveSelectedTask() {", start)
    helper = EXTENSION_JS[start:end]
    assert 'const preview = document.querySelector("#kanbanTaskPreview");' in helper
    assert 'if (preview && preview.style.display !== "none") {' in helper
    assert 'const previewTitle = preview.querySelector(".kanban-task-preview-title")?.textContent || "";' in helper
    assert 'if (previewTitle.trim()) return previewTitle.trim();' in helper
    assert 'const markerMatch = bodyText.match(/^Task:\\s+(.+)$/m);' in helper


def test_project_os_import_and_sync_dispatch_open_dedicated_project_session():
    start = EXTENSION_JS.index("async function dispatchProjectControlPlanePrompt(kind) {")
    end = EXTENSION_JS.index("function fillNativeComposer(text) {", start)
    dispatch = EXTENSION_JS[start:end]
    assert "await refreshProjectSession({ renderNow: false });" in dispatch
    assert "const guard = getControlPlaneActionGuard();" in dispatch
    assert "let startData;" in dispatch
    assert "startData = await sendProjectSessionPrompt(promptText);" in dispatch
    assert 'beginSubmitState("control-plane", rawLabel, "project-session", startData?.stream_id || "");' in dispatch
    assert 'if (kind === "import" || kind === "sync") {\n      await openProjectSession();\n    }' in dispatch


def test_project_os_board_switch_ignores_url_session_when_it_targets_another_board():
    start = EXTENSION_JS.index("function chooseRepresentativeSessionId(meta, explicitSessionId, sessions, boardSlug = state.currentBoard) {")
    end = EXTENSION_JS.index("function getProjectSessionBoardTitle(boardSlug = state.currentBoard) {", start)
    helper = EXTENSION_JS[start:end]
    assert 'const explicit = sessionRows.find((entry) => entry.session_id === explicitSessionId);' in helper
    assert 'if (explicit && projectSessionMatchesBoard(explicit, boardSlug)) {' in helper
    assert 'return { sessionId: explicitSessionId, mode: "url" };' in helper
    assert 'const metaSession = sessionRows.find((entry) => entry.session_id === meta.representativeSessionId);' in helper
    assert 'if (metaSession && projectSessionMatchesBoard(metaSession, boardSlug)) {' in helper
    assert 'return { sessionId: meta.representativeSessionId, mode: "project-meta" };' in helper
    assert '.filter((entry) => !entry.archived && projectSessionMatchesBoard(entry, boardSlug))' in helper
    assert 'const scoreDelta = getProjectSessionEvidenceScore(b) - getProjectSessionEvidenceScore(a);' in helper
    assert 'return boardMatched?.session_id ? { sessionId: boardMatched.session_id, mode: "project-board" } : { sessionId: null, mode: "none" };' in helper
    assert 'recent-visible' not in helper


def test_project_os_visible_board_slug_prefers_exact_match_before_prefix_ambiguity():
    start = EXTENSION_JS.index("function getVisibleKanbanBoardSlug() {")
    end = EXTENSION_JS.index("function getLayoutElements() {", start)
    helper = EXTENSION_JS[start:end]
    assert 'const exact = state.boards.find((board) => {' in helper
    assert 'return Boolean((name && label === name) || (slug && label === slug));' in helper
    assert 'if (exact) return exact.slug || null;' in helper
    assert '.sort((a, b) => String(b?.name || "").length - String(a?.name || "").length)[0];' in helper


def test_project_os_host_gate_detection_ignores_hidden_overlays():
    helper_start = EXTENSION_JS.index("function getApprovalGateDetails() {")
    helper_end = EXTENSION_JS.index("function getAutoIntakeAction(summary, meta) {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'function isElementVisible(node) {' in helper
    assert 'if (!isElementVisible(scope)) return null;' in helper
    assert 'if (node.hidden) return false;' in helper
    assert 'style.display === "none"' in helper
    assert 'style.visibility === "hidden"' in helper


def test_project_os_current_board_meta_helper_drives_loop_rendering():
    helper_start = EXTENSION_JS.index("function getCurrentProjectMeta() {")
    helper_end = EXTENSION_JS.index("function setProjectMeta(boardSlug, patch, options) {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'return getProjectMeta(state.currentBoard || "default");' in helper
    assert 'const meta = getCurrentProjectMeta();' in EXTENSION_JS
    assert 'label: getCurrentProjectMeta().autoLoopEnabled ? "Turn project loop off" : "Turn project loop on"' in EXTENSION_JS


def test_project_os_visible_recover_action_is_restored_in_drawer_palette_and_binding():
    commands_start = EXTENSION_JS.index("function getCommands() {")
    commands_end = EXTENSION_JS.index("function filterCommands() {", commands_start)
    commands = EXTENSION_JS[commands_start:commands_end]
    assert 'id: "control-plane:create"' in commands
    assert 'label: intakeCopy.createLabel' in commands
    assert 'void dispatchProjectControlPlanePrompt("create");' in commands
    assert 'id: "control-plane:import"' in commands
    assert 'label: intakeCopy.importLabel' in commands
    assert 'Dispatch a fresh dedicated Recover current repo action for the current board.' in commands
    assert 'void dispatchProjectControlPlanePrompt("import");' in commands
    assert 'id: "control-plane:resume"' in commands
    assert 'label: intakeCopy.resumeLabel' in commands
    assert 'Reopen the linked dedicated project session without dispatching a fresh import.' in commands
    assert 'void resumeLinkedProjectSession();' in commands
    assert 'id: "control-plane:sync"' in commands
    assert 'label: intakeCopy.syncLabel' in commands
    assert 'Refresh the existing PROJECT/PLAN/STATUS docs from live board and runtime truth.' in commands
    assert 'void dispatchProjectControlPlanePrompt("sync");' in commands

    drawer_start = EXTENSION_JS.index("function renderDrawer(meta) {")
    drawer_end = EXTENSION_JS.index("function renderLoginSurface() {", drawer_start)
    drawer = EXTENSION_JS[drawer_start:drawer_end]
    assert 'const intakeCopy = getControlPlaneIntakeCopy(meta);' in drawer
    assert 'const intakePrimaryActionClass = (action) =>' in drawer
    assert 'const intakeDisabledAttr = (enabled) => (enabled ? "" : \'disabled aria-disabled="true"\');' in drawer
    assert '<button class="${intakePrimaryActionClass("create")}" data-action="start-blank-docs">${esc(intakeCopy.createLabel)}</button>' in drawer
    assert '<button class="${intakePrimaryActionClass("import")}" data-action="recover-current-repo">${esc(intakeCopy.importLabel)}</button>' in drawer
    assert '<button class="${intakePrimaryActionClass("resume")}" data-action="resume-linked-project" ${intakeDisabledAttr(intakeCopy.hasLinkedSession)}>${esc(intakeCopy.resumeLabel)}</button>' in drawer
    assert '<button class="${intakePrimaryActionClass("sync")}" data-action="refresh-project-docs">${esc(intakeCopy.syncLabel)}</button>' in drawer

    bind_start = EXTENSION_JS.index("function bindEvents() {")
    bind_end = EXTENSION_JS.index("function onDocumentClick(event) {", bind_start)
    bind = EXTENSION_JS[bind_start:bind_end]
    assert 'dom.root.querySelectorAll("[data-action=\'start-blank-docs\']").forEach((button) => {' in bind
    assert 'dom.root.querySelectorAll("[data-action=\'recover-current-repo\']").forEach((button) => {' in bind
    assert 'dom.root.querySelectorAll("[data-action=\'resume-linked-project\']").forEach((button) => {' in bind
    assert 'dom.root.querySelectorAll("[data-action=\'refresh-project-docs\']").forEach((button) => {' in bind
    assert 'void dispatchProjectControlPlanePrompt("create");' in bind
    assert 'void dispatchProjectControlPlanePrompt("import");' in bind
    assert 'void resumeLinkedProjectSession();' in bind
    assert 'void dispatchProjectControlPlanePrompt("sync");' in bind


def test_project_os_resume_linked_project_reopens_existing_session_without_dispatching_import_prompt():
    start = EXTENSION_JS.index("async function resumeLinkedProjectSession() {")
    end = EXTENSION_JS.index("function fillNativeComposer(text) {", start)
    helper = EXTENSION_JS[start:end]

    assert 'await refreshProjectSession({ renderNow: false });' in helper
    assert 'const linkedSessionId = getLinkedProjectSessionId();' in helper
    assert 'setToast("No linked project session", "Run Recover current repo first, then use Resume linked project.");' in helper
    assert 'return openProjectSession();' in helper


def test_project_os_send_prompt_refuses_only_live_project_session_truth():
    start = EXTENSION_JS.index("async function sendProjectSessionPrompt(promptText) {")
    end = EXTENSION_JS.index("function getPrimaryNavButtons() {", start)
    helper = EXTENSION_JS[start:end]

    assert "const projectSession = await ensureProjectSession();" in helper
    assert "const refreshed = await refreshProjectSession({ renderNow: false });" in helper
    assert "if (getProjectSessionRunningState(Date.now(), refreshed).running) {" in helper
    assert (
        'throw new Error("Project OS composer session is still running. Wait for the current reply or open the session.");'
        in helper
    )
    assert "const baselineMessageCount = getProjectSessionMessageCount(refreshed || projectSession);" in helper
    assert "const sessionId = String(state.projectSessionId || projectSession?.session_id || \"\").trim();" in helper
    assert "const ack = await awaitProjectSessionTransportAck(sessionId, baselineMessageCount);" in helper
    assert "session_id: String(ack?.session?.session_id || sessionId).trim()," in helper
    assert "stream_id: String(ack?.stream_id || \"\").trim()," in helper
    assert "pending_started_at: ack?.pending_started_at || Date.now()" in helper
    ack_start = EXTENSION_JS.index("async function awaitProjectSessionTransportAck(sessionId, baselineMessageCount) {")
    ack_end = EXTENSION_JS.index("async function openProjectSession() {", ack_start)
    ack_helper = EXTENSION_JS[ack_start:ack_end]
    assert "Dedicated project session send did not start transport" in ack_helper


def test_project_os_dispatch_surfaces_project_session_send_failures():
    start = EXTENSION_JS.index("async function dispatchProjectControlPlanePrompt(kind) {")
    end = EXTENSION_JS.index("function fillNativeComposer(text) {", start)
    block = EXTENSION_JS[start:end]

    assert "try {" in block
    assert "startData = await sendProjectSessionPrompt(promptText);" in block
    assert 'status: "timed_out"' in block
    assert 'setToast("Project session dispatch failed", detail);' in block


def test_project_os_open_session_prefers_linked_evidence_session_when_project_session_id_drifted():
    start = EXTENSION_JS.index("async function openProjectSession() {")
    end = EXTENSION_JS.index("async function awaitProjectSessionComposerReady(sessionId) {", start)
    helper = EXTENSION_JS[start:end]
    assert 'const currentProjectSession = await ensureProjectSession();' in helper
    assert 'const linkedSessionId = String(state.submit?.sessionId || "").trim();' in helper
    assert 'const linkedPayload = await api(`/api/session?session_id=${encodeURIComponent(linkedSessionId)}`);' in helper
    assert 'projectSessionMatchesBoard(linkedProjectSession, state.currentBoard)' in helper
    assert 'const currentSessionId = String(currentProjectSession?.session_id || state.projectSessionId || "").trim();' in helper
    assert 'const sessionId = preferredLinkedSessionId || currentSessionId || state.projectSessionId;' in helper
    assert 'const resolvedSessionId = String(preferredLinkedSessionId || currentSessionId || state.projectSessionId || "").trim();' in helper
    assert 'if (resolvedSessionId && resolvedSessionId !== state.projectSessionId) {' in helper
    assert 'state.projectSessionId = resolvedSessionId;' in helper
    assert 'if (resolvedSessionId && resolvedSessionId !== linkedSessionId && linkedProjectSession && isEmptyIdleProjectSession(linkedProjectSession)) {' in helper
    assert 'sessionId: resolvedSessionId,' in helper
    assert 'await window.loadSession(resolvedSessionId);' in helper
    assert 'openPanel("Chat");' in helper
    assert 'focusNativeComposer();' in helper
    assert 'history.pushState(null, "", `/session/${encodeURIComponent(resolvedSessionId)}`);' in helper



def test_project_os_send_project_session_prompt_waits_for_ready_composer_and_uses_route_fallback():
    start = EXTENSION_JS.index("async function awaitProjectSessionComposerReady(sessionId) {")
    end = EXTENSION_JS.index("function getPrimaryNavButtons() {", start)
    helper = EXTENSION_JS[start:end]
    assert 'const expectedSessionId = String(sessionId || state.projectSessionId || "").trim();' in helper
    assert 'const currentPath = String(location.pathname || "").trim();' in helper
    assert 'const native = getNativeComposer();' in helper
    assert 'const textareaReady = Boolean(native?.textarea && isElementFocusable(native.textarea));' in helper
    assert 'const sendReady = Boolean(native?.sendButton);' in helper
    assert 'await new Promise((resolve) => window.setTimeout(resolve, 80));' in helper
    assert 'throw new Error("Dedicated project session composer did not become ready. Open the session and retry.");' in helper
    assert 'const opened = await openProjectSession();' in helper
    assert 'await awaitProjectSessionComposerReady(opened?.sessionId || state.projectSessionId || projectSession?.session_id || "");' in helper
    assert 'const routed = routePromptToHermes(promptText, true);' in helper
    assert 'if (!routed?.ok) {' in helper



def test_project_os_open_session_reconciles_wrong_visible_host_board_before_loading_session():
    helper_start = EXTENSION_JS.index("function getVisibleKanbanBoardSlug() {")
    helper_end = EXTENSION_JS.index("function getLayoutElements() {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'async function reconcileVisibleKanbanBoard(boardSlug = state.currentBoard) {' in helper
    assert 'const visibleBoardSlug = getVisibleKanbanBoardSlug();' in helper
    assert 'if (visibleBoardSlug === targetBoard) return false;' in helper
    assert 'if (typeof window.switchKanbanBoard === "function") {' in helper
    assert 'await window.switchKanbanBoard(targetBoard);' in helper
    assert 'state.currentBoard = targetBoard;' in helper
    assert 'state.lastBoardSlug = targetBoard;' in helper


def test_project_os_load_persisted_restores_linked_project_session_from_submit_continuity():
    normalize_start = EXTENSION_JS.index("function normalizePersistedSubmit(rawSubmit, persistedProjectSessionId) {")
    normalize_end = EXTENSION_JS.index("function isLoginSurface() {", normalize_start)
    normalize = EXTENSION_JS[normalize_start:normalize_end]
    assert 'const linkedSessionId = String(persistedProjectSessionId || merged.sessionId || "").trim();' in normalize
    assert 'if (linkedSessionId && merged.status === "linked") {' in normalize
    assert 'const sessionMissing = !linkedSessionId;' in normalize

    load_start = EXTENSION_JS.index("function loadPersisted() {")
    load_end = EXTENSION_JS.index("function savePersisted() {", load_start)
    load = EXTENSION_JS[load_start:load_end]
    assert 'state.submit = normalizePersistedSubmit(parsed.submit, parsed.projectSessionId || "");' in load
    assert 'if (!state.projectSessionId && state.submit.sessionId) {' in load
    assert 'state.projectSessionId = state.submit.sessionId;' in load


def test_project_os_best_candidate_helper_prefers_meaningful_same_board_session_over_empty_idle_stub():
    helper_start = EXTENSION_JS.index("function getProjectSessionBoardTitle(boardSlug = state.currentBoard) {")
    helper_end = EXTENSION_JS.index("async function ensureProjectSession() {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'function getLinkedProjectSessionId() {' in EXTENSION_JS
    assert 'return String(state.projectSession?.session_id || state.projectSessionId || state.submit?.sessionId || "").trim();' in EXTENSION_JS
    assert 'function projectSessionMatchesBoard(session, boardSlug = state.currentBoard) {' in helper
    assert 'function getProjectSessionEvidenceScore(session) {' in helper
    assert 'function isEmptyIdleProjectSession(session) {' in helper
    assert 'const boardTitle = getProjectSessionBoardTitle(boardSlug);' in helper
    assert 'sessionsPayload = await api("/api/sessions");' in helper
    assert 'sessionsPayload.sessions.filter((session) => String(session?.title || "") === boardTitle)' in helper
    assert 'const currentMatchesBoard = projectSessionMatchesBoard(currentSession, boardSlug);' in helper
    assert 'const candidates = [currentMatchesBoard ? currentSession : null, ...matchingSessions].filter(Boolean);' in helper
    assert 'const scoreDelta = getProjectSessionEvidenceScore(b) - getProjectSessionEvidenceScore(a);' in helper
    assert 'state.projectSession = preferred;' in helper
    assert 'state.projectSessionId = preferred.session_id;' in helper


def test_project_os_ensure_project_session_creates_board_local_session_when_persisted_id_drifted():
    start = EXTENSION_JS.index("async function ensureProjectSession() {")
    end = EXTENSION_JS.index("function requestRender() {", start)
    helper = EXTENSION_JS[start:end]
    assert 'const fallbackPayload = await api(`/api/session?session_id=${encodeURIComponent(fallbackId)}`);' in helper
    assert 'if (projectSessionMatchesBoard(fallbackSession, state.currentBoard)) {' in helper
    assert 'return await createProjectSessionForBoard(state.currentBoard);' in helper

    create_start = EXTENSION_JS.index("function buildProjectSessionCreatePayload() {")
    create_end = EXTENSION_JS.index("async function ensureProjectSession() {", create_start)
    create_helper = EXTENSION_JS[create_start:create_end]
    assert 'profile: getActiveProfileName(),' in create_helper
    assert 'const workspace = String(state.session?.workspace || state.workspaces?.[0]?.path || "").trim();' in create_helper
    assert 'const createPayload = await api("/api/session/new", {' in create_helper
    assert 'const renamePayload = await api("/api/session/rename", {' in create_helper
    assert 'title: boardTitle,' in create_helper


def test_project_os_submit_lifecycle_does_not_timeout_while_project_session_is_running():
    assert "function getProjectSessionRunningState(now = Date.now(), sourceSession = state.projectSession) {" in EXTENSION_JS
    assert "const running = Boolean(hasActiveStream || hasPendingUserMessage);" in EXTENSION_JS
    lifecycle_start = EXTENSION_JS.index("function updateSubmitLifecycle() {")
    lifecycle_end = EXTENSION_JS.index("function clearComposerAfterDispatch()", lifecycle_start)
    lifecycle = EXTENSION_JS[lifecycle_start:lifecycle_end]
    running_branch_start = lifecycle.index("if (runningState.running) {")
    running_branch_end = lifecycle.index("if (!runningState.running && now - state.submit.sentAt >= 1500) {", running_branch_start)
    running_branch = lifecycle[running_branch_start:running_branch_end]
    assert 'detail: "Dedicated project session is still running."' in running_branch
    assert 'status: "timed_out"' not in running_branch


def test_project_os_submit_lifecycle_does_not_treat_pending_user_message_only_state_as_stopped():
    lifecycle_start = EXTENSION_JS.index("function updateSubmitLifecycle() {")
    lifecycle_end = EXTENSION_JS.index("function clearComposerAfterDispatch()", lifecycle_start)
    lifecycle = EXTENSION_JS[lifecycle_start:lifecycle_end]
    assert "const hasPendingUserMessage = Boolean(session.pending_user_message);" in EXTENSION_JS
    assert "const running = Boolean(hasActiveStream || hasPendingUserMessage);" in EXTENSION_JS
    assert "if (!runningState.running && now - state.submit.sentAt >= 1500) {" in lifecycle
    assert "if (!state.projectSession?.active_stream_id && now - state.submit.sentAt >= 1500) {" not in lifecycle


def test_project_os_submit_lifecycle_marks_zero_message_active_stream_as_stalled_running():
    assert "const RUNNING_STALLED_MS = 45 * 1000;" in EXTENSION_JS
    assert "function getProjectSessionMessageCount(sourceSession = state.projectSession) {" in EXTENSION_JS
    assert "const zeroMessageDispatch = running && messageCount === 0;" in EXTENSION_JS
    assert "suspiciouslyStalled: zeroMessageDispatch && ageMs >= RUNNING_STALLED_MS" in EXTENSION_JS
    assert 'status: "stalled_running"' in EXTENSION_JS
    assert 'detail: "Project session is running with no visible messages yet."' in EXTENSION_JS
    assert "Active stream is still present, but the project session still reports message_count=0." in EXTENSION_JS
    assert 'if (status === "stalled_running") return { label: "Project may be stuck", className: "is-timeout" };' in EXTENSION_JS


def test_project_os_mobile_shortcuts_restore_first_slash_and_question_mark_instead_of_only_focusing():
    focus_start = EXTENSION_JS.index("function insertTextAtCursor(input, text) {")
    focus_end = EXTENSION_JS.index("function buildContextualPrompt(userText) {", focus_start)
    focus_helper = EXTENSION_JS[focus_start:focus_end]
    assert 'function focusFloatingComposer(options) {' in focus_helper
    assert 'const insertText = typeof options === "string" ? options : options?.insertText || "";' in focus_helper
    assert 'if (success && insertText && !textInserted) {' in focus_helper
    assert 'textInserted = insertTextAtCursor(composer, insertText) || textInserted;' in focus_helper

    keydown_start = EXTENSION_JS.index("function onGlobalKeydown(event) {")
    keydown_end = EXTENSION_JS.index("if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === \"k\") {", keydown_start)
    keydown = EXTENSION_JS[keydown_start:keydown_end]
    assert 'const questionPressed =' in keydown
    assert 'insertTextAtCursor(composer, "?");' in keydown
    assert 'focusFloatingComposer({ insertText: "?" });' in keydown
    assert 'focusFloatingComposer({ insertText: "/" });' in keydown
    assert 'if (slashPressed && (isComposerEditable(target) || isComposerEditable(activeElement))) {' in keydown
    assert 'insertTextAtCursor(composer, "/");' in keydown
    assert '!inEditable' in keydown
    assert '(!inEditable || !insideExtension)' not in keydown


def test_project_os_ctrl_period_toggles_project_context_drawer():
    keydown_start = EXTENSION_JS.index("function onGlobalKeydown(event) {")
    keydown_end = EXTENSION_JS.index("if (event.key === \"Escape\" && state.paletteOpen) {", keydown_start)
    keydown = EXTENSION_JS[keydown_start:keydown_end]
    assert 'if ((event.metaKey || event.ctrlKey) && event.key === ".") {' in keydown
    assert 'state.summaryOpen = !state.summaryOpen;' in keydown
    assert 'render();' in keydown
    assert 'return;' in keydown


def test_project_os_floating_context_wrapper_includes_execution_contract_for_hui_commands():
    start = EXTENSION_JS.index("function buildContextualPrompt(userText) {")
    end = EXTENSION_JS.index("function getControlPlaneActionGuard() {", start)
    helper = EXTENSION_JS[start:end]
    assert '"[Project Context]"' in helper
    assert '`Board: ${state.currentBoard || "default"}`' in helper
    assert '`Project state: ${meta.status}`' in helper
    assert '"[Execution Contract]"' in helper
    assert '"Treat this as a Project OS operating command inside Hermes WebUI, not a generic chat message."' in helper
    assert '"Use the Project Context above as the default scope of truth unless the user explicitly overrides it."' in helper
    assert '"Prefer taking the next concrete action over giving broad advice."' in helper
    assert '"If the request implies board/task/doc/session work, use the current board and current canonical repo context first."' in helper
    assert '"When the request is actionable, do the work or move it forward materially; do not answer with planning-only text."' in helper
    assert '"If you cannot act safely, explain the exact blocker and the next safe operator action in concrete terms."' in helper
    assert '"Keep Project OS thin: do not invent a shadow tracker, duplicate goal engine, or separate source of truth."' in helper


def test_project_os_composer_context_moves_to_right_edge_without_status_preview_block():
    render_start = EXTENSION_JS.index("function renderComposer(meta) {")
    render_end = EXTENSION_JS.index("function renderPalette() {", render_start)
    render_composer = EXTENSION_JS[render_start:render_end]
    assert 'const showPreview = expanded && (hasDraft || hasSubmitActivity);' not in render_composer
    assert 'const hasLoopSignal =' not in render_composer
    assert 'class="pux-composer-context-rail"' in render_composer
    assert 'class="pux-composer-actions pux-composer-actions--context-end"' in render_composer
    assert 'class="pux-composer-meta pux-composer-meta--context-end"' in render_composer
    assert '<span class="pux-overline">Project status</span>' not in render_composer
    assert '<span class="pux-overline">Extension status</span>' not in render_composer
    assert 'data-action="open-chat-preview"' not in render_composer
    assert 'data-action="run-loop-now"' not in render_composer

    assert '.pux-composer-context-rail {' in EXTENSION_CSS
    assert 'justify-content: flex-end;' in EXTENSION_CSS
    assert '.pux-composer-actions--context-end {' in EXTENSION_CSS
    assert '.pux-composer-meta--context-end {' in EXTENSION_CSS


def test_project_os_focus_listener_does_not_rerender_composer_on_focus():
    listeners_start = EXTENSION_JS.index('const composer = dom.root.querySelector("[data-field=\'composer-text\']");')
    listeners_end = EXTENSION_JS.index('dom.root.querySelector("[data-action=\'send-hermes\']")?.addEventListener("click", () => {', listeners_start)
    listeners = EXTENSION_JS[listeners_start:listeners_end]
    focus_start = listeners.index('composer?.addEventListener("focus", () => {')
    blur_start = listeners.index('composer?.addEventListener("blur", () => {', focus_start)
    focus_block = listeners[focus_start:blur_start]
    assert 'markComposerInteraction();' in focus_block
    assert 'savePersisted();' in focus_block
    assert 'render();' not in focus_block
    assert 'composer?.addEventListener("beforeinput", (event) => {' in listeners
    assert 'if ((text === "/" || text === "?") && event.inputType === "insertText") {' in listeners
    assert 'insertTextAtCursor(event.target, text);' in listeners
    assert 'composer?.addEventListener("paste", () => {' in listeners
    assert 'composer?.addEventListener("compositionstart", () => {' in listeners
    assert 'composer?.addEventListener("compositionend", () => {' in listeners


def test_project_os_route_timer_skips_rerender_during_active_mobile_composer_interaction():
    assert 'function isComposerInteractionActive() {' in EXTENSION_JS
    mount_start = EXTENSION_JS.index("function mount() {")
    mount_end = EXTENSION_JS.index("function unmount() {", mount_start)
    mount = EXTENSION_JS[mount_start:mount_end]
    assert 'if (isComposerInteractionActive()) {' in mount
    assert 'Avoid re-rendering the floating composer while mobile typing/paste/IME is active.' in mount
    assert '} else if (submitLifecycleChanged) {' in mount


def test_project_os_host_timeout_banner_is_not_project_session_truth():
    lifecycle_start = EXTENSION_JS.index("function updateSubmitLifecycle() {")
    lifecycle_end = EXTENSION_JS.index("function clearComposerAfterDispatch()", lifecycle_start)
    lifecycle = EXTENSION_JS[lifecycle_start:lifecycle_end]
    assert "const timeoutVisible = Boolean(state.projectSession?.active_stream_id) && isHostTimeoutVisible();" in lifecycle
    assert (
        'hostNote: timeoutVisible ? "A timeout banner is visible on the host surface, but Project OS is using its own session." : ""'
        in lifecycle
    )
    timeout_branch_start = lifecycle.index("if (now - state.submit.sentAt >= SUBMIT_TIMEOUT_MS)")
    timeout_branch = lifecycle[timeout_branch_start:]
    assert '"The project session may still be running — open session to inspect."' in timeout_branch


def test_project_os_closeout_rules_define_readiness_and_actions():
    assert "function deriveCloseoutSnapshot(meta, flattened, byColumn) {" in EXTENSION_JS
    start = EXTENSION_JS.index("function deriveCloseoutSnapshot(meta, flattened, byColumn) {")
    end = EXTENSION_JS.index("function inferProjectSignals(meta, flattened, byColumn) {", start)
    closeout = EXTENSION_JS[start:end]
    assert 'const readyTasks = byColumn.ready || [];' in closeout
    assert 'const runningTasks = byColumn.running || [];' in closeout
    assert 'const blockedTasks = byColumn.blocked || [];' in closeout
    assert 'const missingGoal = !String(meta.goalSummary || "").trim();' in closeout
    assert 'const projectSessionMessageCount = getProjectSessionMessageCount(state.projectSession);' in closeout
    assert 'const linkedProjectSessionId = getLinkedProjectSessionId();' in closeout
    assert 'const snapshotEvidenceSignals = Array.isArray(meta.summarySnapshot?.closeout?.evidenceGate?.evidenceSignals)' in closeout
    assert 'const refs = getEffectiveProjectRefs(meta);' in closeout
    assert 'const finalReportRef = refs.statusRef || refs.projectRef || refs.planRef || "";' in closeout
    assert 'const evidenceCriteria = [' in closeout
    assert 'if (!finalReportRef) evidenceBlockers.push("Missing final report ref");' in closeout
    assert 'const readyToClose = blockers.length === 0;' in closeout
    assert 'label: readyToClose ? "Ready to close" : "Closeout blocked"' in closeout
    assert 'actionLabel: readyToClose ? "Close project" : "Review closeout blockers"' in closeout
    assert 'archiveProject: readyToClose' in closeout
    assert 'finalReportRef,' in closeout
    assert 'if (projectSessionMessageCount > 0) evidenceSignals.push("Dedicated project session history linked");' in closeout
    assert 'if (!projectSessionMessageCount && linkedProjectSessionId && snapshotEvidenceSignals.length && !evidenceCollecting) {' in closeout
    assert 'evidenceSignals.push("Dedicated project session continuity still linked");' in closeout
    assert 'if (!evidenceSignals.length && snapshotEvidenceSignals.length && !evidenceCollecting) {' in closeout
    assert 'evidenceSignals.push("Previously captured browser/runtime proof is still linked");' in closeout
    assert 'label: evidenceReady ? "Evidence ready" : evidenceCollecting ? "Evidence collecting" : "Evidence blocked"' in closeout


def test_project_os_archived_state_uses_closeout_snapshot_in_summary():
    start = EXTENSION_JS.index("function inferProjectSignals(meta, flattened, byColumn) {")
    end = EXTENSION_JS.index("function deriveSummary(options) {", start)
    signals = EXTENSION_JS[start:end]
    assert "const closeout = deriveCloseoutSnapshot(meta, flattened, byColumn);" in signals
    assert 'matters.push(closeout.summaryLine);' in signals
    assert 'return { linkedCrons, nextCron, matters, closeout };' in signals

    summary_start = EXTENSION_JS.index("function deriveSummary(options) {")
    summary_end = EXTENSION_JS.index("function getSummaryForRender() {", summary_start)
    summary = EXTENSION_JS[summary_start:summary_end]
    assert "const { linkedCrons, nextCron, matters, closeout } = inferProjectSignals(meta, flattened, byColumn);" in summary
    assert "closeout," in summary


def test_project_os_report_ref_defaults_and_open_report_action_use_control_plane_docs():
    refs_start = EXTENSION_JS.index("function getDefaultProjectControlPlaneRefs() {")
    refs_end = EXTENSION_JS.index("function getControlPlaneIntakeCopy(meta) {", refs_start)
    refs = EXTENSION_JS[refs_start:refs_end]
    assert 'projectRef: "docs/project-os/PROJECT.md"' in refs
    assert 'planRef: "docs/project-os/PLAN.md"' in refs
    assert 'statusRef: "docs/project-os/STATUS.md"' in refs
    assert 'function getEffectiveProjectRefs(meta) {' in refs
    assert 'function getCurrentProjectReportRef() {' in refs
    assert 'const closeoutRef = String(state.summary?.closeout?.finalReportRef || "").trim();' in refs
    assert 'return refs.statusRef || refs.projectRef || refs.planRef || "";' in refs

    bindings_start = EXTENSION_JS.index('dom.root.querySelectorAll("[data-action=\'open-project-session\']").forEach((button) => {')
    bindings_end = EXTENSION_JS.index('dom.root.querySelectorAll("[data-action=\'open-project-artifact-path\']").forEach((button) => {', bindings_start)
    bindings = EXTENSION_JS[bindings_start:bindings_end]
    assert 'dom.root.querySelectorAll("[data-action=\'open-project-artifact\']").forEach((button) => {' in bindings
    assert 'const artifactPath = getCurrentProjectReportRef();' in bindings
    assert 'if (!artifactPath || typeof window.openArtifactPath !== "function") return;' in bindings
    assert 'void window.openArtifactPath(artifactPath);' in bindings


def test_project_os_summary_renders_workflow_card_with_evidence_actions_and_close_action():
    workflow_start = EXTENSION_JS.index("function renderWorkflowEvidenceActions(evidenceGate, closeout) {")
    workflow_end = EXTENSION_JS.index("function renderComposer(meta) {", workflow_start)
    workflow_render = EXTENSION_JS[workflow_start:workflow_end]
    assert 'const evidenceSignals = Array.isArray(evidenceGate?.evidenceSignals) ? evidenceGate.evidenceSignals : [];' in workflow_render
    assert 'const criteria = Array.isArray(evidenceGate?.criteria) ? evidenceGate.criteria : [];' in workflow_render
    assert 'data-action="open-project-session"' in workflow_render
    assert 'Open evidence thread' in workflow_render
    assert 'closeout?.finalReportRef' in workflow_render
    assert 'Open report' in workflow_render
    assert 'data-action="open-project-artifact"' in workflow_render
    assert 'function renderWorkflowCard(summary) {' in workflow_render
    assert 'const refs = summary.refs || "";' in workflow_render
    assert 'const actionGuardHtml = summary.actionGuardHtml || "";' in workflow_render
    assert 'const actionGuardAttrs = summary.actionGuardAttrs || "";' in workflow_render
    assert 'const blockedItems = summary.blockedItemsHtml' in workflow_render
    assert 'const nextItems = summary.nextItemsHtml' in workflow_render
    assert 'const stageSource = Array.isArray(summary.workflowStages) ? summary.workflowStages : [];' in workflow_render
    assert 'const workflowStages = stageSource.length ? stageSource : fallbackStages;' in workflow_render
    assert 'class="pux-workflow-ladder"' in workflow_render
    assert 'Browser/runtime evidence gate' in workflow_render
    assert 'Goal: ${esc(meta.goalSummary || "Not set")}' in workflow_render
    assert 'Next step: ${esc(meta.nextStepSummary || "No explicit next-step summary yet.")}' in workflow_render
    assert 'Top blocker: ${esc(meta.blockerSummary || summary.closeout?.blockers?.[0] || "None")}' in workflow_render

    render_start = EXTENSION_JS.index("function renderSummary() {")
    render_end = EXTENSION_JS.index("function renderComposer(meta) {", render_start)
    render_summary = EXTENSION_JS[render_start:render_end]
    assert 'const closeout = summary.closeout || deriveCloseoutSnapshot(meta, [], {});' in render_summary
    assert 'const currentReportRef = closeout.finalReportRef || getCurrentProjectReportRef();' in render_summary
    assert 'const renderedCloseout = currentReportRef && !closeout.finalReportRef ? { ...closeout, finalReportRef: currentReportRef } : closeout;' in render_summary
    assert 'const workflowSummary = {' in render_summary
    assert 'workflowStages: Array.isArray(summary.workflowStages) ? summary.workflowStages : [],' in render_summary
    assert 'actionGuardHtml,' in render_summary
    assert 'actionGuardAttrs,' in render_summary
    assert 'blockedItemsHtml: blockedItems,' in render_summary
    assert 'nextItemsHtml: nextItems,' in render_summary
    assert 'refs: summary.refs || renderedCloseout.finalReportRef,' in render_summary
    assert 'evidenceGate: renderedCloseout.evidenceGate,' in render_summary
    assert 'closeout: renderedCloseout,' in render_summary
    assert '${renderWorkflowCard(workflowSummary)}' in render_summary
    assert 'data-action="close-project"' in render_summary
    assert 'renderedCloseout.finalReportRef' in render_summary
    assert 'renderedCloseout.archiveProject && !state.stateChanging ? "" : "disabled aria-disabled=\\"true\\""' in render_summary

    assert '.pux-workflow-ladder {' in EXTENSION_CSS
    assert '.pux-workflow-stage {' in EXTENSION_CSS
    assert '.pux-workflow-columns {' in EXTENSION_CSS
    assert '.pux-chip--status.is-done {' in EXTENSION_CSS

    activity_start = EXTENSION_JS.index("function renderComposerActivity() {")
    activity_end = EXTENSION_JS.index("function renderProjectThreadDetail() {", activity_start)
    activity = EXTENSION_JS[activity_start:activity_end]
    assert 'const linkedSessionId = getLinkedProjectSessionId();' in activity
    assert 'const sessionLabel = linkedSessionId ? compactLabel(getProjectSessionLabel(), 20) : "No session";' in activity

    detail_start = EXTENSION_JS.index("function renderProjectThreadDetail() {")
    detail_end = EXTENSION_JS.index("function isHostTimeoutVisible() {", detail_start)
    detail = EXTENSION_JS[detail_start:detail_end]
    assert 'const linkedSessionId = getLinkedProjectSessionId();' in detail
    assert 'const feedItems = getProjectSessionExecutionFeed(session);' in detail
    assert 'const artifactItems = getProjectSessionArtifactList(session);' in detail
    assert 'Linked dedicated project session is available. Open session to inspect the settled proof thread.' in detail
    assert '<div class="pux-card-title">Execution feed</div>' in detail
    assert '<div class="pux-card-title">Recent execution items</div>' in detail
    assert '<div class="pux-card-title">Recent artifacts</div>' in detail
    assert 'No recent execution items captured on the linked dedicated project session yet.' in detail
    assert 'Artifact mini-list is still empty for this linked session. Use Open session for the full thread when deeper drill-down is needed.' in detail
    assert 'data-action="open-project-artifact-path"' in detail
    assert 'data-action="open-project-session"' in detail


def test_project_os_active_import_resume_state_refreshes_before_lifecycle_checks():
    route_start = EXTENSION_JS.index("state.routeTimer = window.setInterval(() => {")
    route_end = EXTENSION_JS.index("}, ROUTE_POLL_MS);", route_start)
    route_poll = EXTENSION_JS[route_start:route_end]
    assert (
        '(state.submit.status === "sent" || state.submit.status === "waiting" || state.submit.status === "stalled_running")'
        in route_poll
    )
    assert "Date.now() - Number(state.projectSessionLastFetchedAt || 0) >= 1200" in route_poll
    assert "void refreshProjectSession({ renderNow: false });" in route_poll
    assert "const submitLifecycleChanged = updateSubmitLifecycle();" in route_poll


def test_project_os_refresh_data_adopts_same_board_evidence_session_before_restoring_continuity():
    refresh_data_start = EXTENSION_JS.index("async function refreshData(options) {")
    refresh_data_end = EXTENSION_JS.index("async function switchBoard(boardSlug) {", refresh_data_start)
    refresh_data = EXTENSION_JS[refresh_data_start:refresh_data_end]
    assert "await refreshProjectSession({ renderNow: false });" in refresh_data
    assert "await adoptBestProjectSessionCandidate(resolvedBoard, state.projectSession);" in refresh_data
    assert "restoreProjectSessionContinuity();" in refresh_data


def test_project_os_refresh_data_prefers_only_corroborated_visible_board_over_stale_continuity():
    helper_start = EXTENSION_JS.index("function normalizeVisibleBoardSwitcherLabel(raw) {")
    helper_end = EXTENSION_JS.index("function getLayoutElements() {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'document.querySelector(".kanban-board-switcher-toggle")' in helper
    assert 'const label = normalizeVisibleBoardSwitcherLabel(toggle?.textContent || toggle?.innerText || "");' in helper
    assert 'state.boards.find((board) => {' in helper
    assert 'return matched?.slug || null;' in helper

    refresh_data_start = EXTENSION_JS.index("async function refreshData(options) {")
    refresh_data_end = EXTENSION_JS.index("async function switchBoard(boardSlug) {", refresh_data_start)
    refresh_data = EXTENSION_JS[refresh_data_start:refresh_data_end]
    assert 'const persistedBoardSlug =' in refresh_data
    assert 'const visibleBoardSlug = getVisibleKanbanBoardSlug();' in refresh_data
    assert 'const trustedVisibleBoardSlug =' in refresh_data
    assert 'visibleBoardSlug === boardsPayload.current' in refresh_data
    assert '(!persistedBoardSlug && !state.projectSessionId)' in refresh_data
    assert 'const resolvedBoard =\n        trustedVisibleBoardSlug ||\n        persistedBoardSlug ||' in refresh_data


def test_project_os_refresh_data_reconciles_live_project_session_board_when_visible_board_drifted():
    helper_start = EXTENSION_JS.index("function getProjectSessionBoardTitle(boardSlug = state.currentBoard) {")
    helper_end = EXTENSION_JS.index("function getLinkedProjectSessionId() {", helper_start)
    helper = EXTENSION_JS[helper_start:helper_end]
    assert 'function getBoardSlugFromProjectSession(session) {' in helper
    assert 'const prefix = "Project OS · ";' in helper
    assert 'return String(title.slice(prefix.length).trim() || "default");' in helper

    refresh_data_start = EXTENSION_JS.index("async function refreshData(options) {")
    refresh_data_end = EXTENSION_JS.index("async function switchBoard(boardSlug) {", refresh_data_start)
    refresh_data = EXTENSION_JS[refresh_data_start:refresh_data_end]
    assert 'const linkedProjectBoardSlug = getBoardSlugFromProjectSession(state.projectSession);' in refresh_data
    assert 'linkedProjectBoardSlug !== state.currentBoard' in refresh_data
    assert 'state.boards.some((board) => board.slug === linkedProjectBoardSlug)' in refresh_data
    assert '!isEmptyIdleProjectSession(state.projectSession)' in refresh_data
    assert 'await reconcileVisibleKanbanBoard(linkedProjectBoardSlug);' in refresh_data
    assert 'api(`/api/kanban/board${boardQuery(linkedProjectBoardSlug)}`)' in refresh_data
    assert 'api(`/api/kanban/stats${boardQuery(linkedProjectBoardSlug)}`)' in refresh_data
    assert 'state.boardData = linkedBoardData;' in refresh_data
    assert 'state.boardStats = linkedStats;' in refresh_data


def test_project_os_refresh_awaits_inflight_session_fetch_before_returning_cached_truth():
    refresh_start = EXTENSION_JS.index("async function refreshProjectSession(options = {}) {")
    refresh_end = EXTENSION_JS.index("async function openProjectSession()", refresh_start)
    refresh = EXTENSION_JS[refresh_start:refresh_end]
    assert "projectSessionRefreshPromise: null" in EXTENSION_JS
    assert "return state.projectSessionRefreshPromise || state.projectSession;" in refresh
    assert "state.projectSessionRefreshPromise = (async () => {" in refresh
    assert "return await state.projectSessionRefreshPromise;" in refresh
    assert "state.projectSessionRefreshPromise = null;" in refresh
    assert "const submitLifecycleChanged = isControlPlaneSubmitActive() ? updateSubmitLifecycle() : false;" in refresh
    assert "if (submitLifecycleChanged && !options.renderNow) {\n        requestRender();\n      }" in refresh

    restore_start = EXTENSION_JS.index("function restoreProjectSessionContinuity() {")
    restore_end = EXTENSION_JS.index("function getSubmitDocRefreshKey(submit = state.submit) {", restore_start)
    restore = EXTENSION_JS[restore_start:restore_end]
    assert 'if (isEmptyIdleProjectSession(state.projectSession)) return false;' in restore
    assert 'sessionId: state.projectSession.session_id || state.projectSessionId || "",' in restore

    begin_start = EXTENSION_JS.index("function beginSubmitState(source, command, routeMode, streamId = \"\") {")
    begin_end = EXTENSION_JS.index("function updateSubmitLifecycle() {", begin_start)
    begin_submit = EXTENSION_JS[begin_start:begin_end]
    assert 'sessionId: state.projectSession?.session_id || state.projectSessionId || "",' in begin_submit


def test_project_os_lifecycle_defers_timeout_until_dedicated_session_refresh_is_fresh():
    lifecycle_start = EXTENSION_JS.index("function updateSubmitLifecycle() {")
    lifecycle_end = EXTENSION_JS.index("function clearComposerAfterDispatch()", lifecycle_start)
    lifecycle = EXTENSION_JS[lifecycle_start:lifecycle_end]
    refresh_branch_start = lifecycle.index("const refreshAgeMs = now - Number(state.projectSessionLastFetchedAt || 0);")
    refresh_branch_end = lifecycle.index("if (!runningState.running && now - state.submit.sentAt >= 1500) {", refresh_branch_start)
    refresh_branch = lifecycle[refresh_branch_start:refresh_branch_end]
    assert "const needsFreshProjectSession = Boolean(" in refresh_branch
    assert "state.projectSessionSyncing || refreshAgeMs >= 1200" in refresh_branch
    assert "void refreshProjectSession({ renderNow: false });" in refresh_branch
    assert 'status: "waiting"' in refresh_branch
    assert 'detail: "Refreshing dedicated project session state before deciding timeout."' in refresh_branch
    assert 'status: "timed_out"' not in refresh_branch
