(function projectOsHermesWebUiExtension() {
  if (window.__PROJECT_OS_HERMES_WEBUI_EXTENSION__) {
    window.__PROJECT_OS_HERMES_WEBUI_EXTENSION__.unmount?.();
    try {
      delete window.__PROJECT_OS_HERMES_WEBUI_EXTENSION__;
    } catch (_error) {
      window.__PROJECT_OS_HERMES_WEBUI_EXTENSION__ = undefined;
    }
  }

  const STORAGE_KEY = "project-os-hermes-webui-extension:v2";
  const PENDING_KEY = "project-os-hermes-webui-extension:pending-send";
  const REFRESH_MS = 15000;
  const ROUTE_POLL_MS = 700;
  const TOAST_MS = 3200;
  const SUBMIT_TIMEOUT_MS = 12000;
  const RUNNING_STALLED_MS = 45 * 1000;
  const PROJECT_WORKFLOW_SEED_MARKER = "project-os.workflow-seed:v1";
  const PROJECT_WORKFLOW_SEEDS = [
    {
      id: "root",
      title: "Project OS import/resume workflow",
      status: "done",
      body: [
        `[${PROJECT_WORKFLOW_SEED_MARKER}]`,
        "Seed: root",
        "A-phase is complete.",
        "Reference-only anchor: preserve workflow continuity context for this board.",
        "Do not decompose, spec, or treat this anchor as the next actionable triage card.",
      ].join("\n"),
    },
    {
      id: "first-actionable-child",
      title: "가드레일 — decomposer가 루트 seed를 실행 태스크로 오해하지 않기",
      status: "ready",
      body: [
        `[${PROJECT_WORKFLOW_SEED_MARKER}]`,
        "Seed: first-actionable-child",
        "Workflow semantics slice for Project OS intake.",
        "",
        "Problem observed:",
        "- import/resume seeded root workflow card in a way that can attract decomposer/specifier behavior",
        "- root anchor should preserve continuity context, not behave like an actionable exploratory triage card",
        "",
        "Expected result:",
        "- root seed becomes reference-only / ignore-for-decompose / equivalent safe semantic",
        "- first actionable slice is the actual ready task",
        "- verify on a clean board",
      ].join("\n"),
    },
  ];

  function getEmptySubmitState() {
    return {
      source: "",
      status: "idle",
      command: "",
      detail: "",
      hostNote: "",
      sentAt: 0,
      baselineReply: "",
      resolvedReply: "",
      routeMode: "",
      sessionId: "",
      streamId: "",
      pendingStartedAt: 0,
    };
  }

  const LOOP_COOLDOWN_MS = 20000;
  const LOOP_LEDGER_LIMIT = 12;

  const state = {
    mounted: false,
    currentPath: "",
    currentPanel: "",
    boards: [],
    currentBoard: null,
    boardData: null,
    boardStats: null,
    crons: [],
    workspaces: [],
    activeProfile: null,
    session: null,
    projectSession: null,
    projectSessionId: "",
    projectSessionLastFetchedAt: 0,
    projectSessionSyncing: false,
    projectSessionRefreshPromise: null,
    sessionList: [],
    sessionMode: "",
    selectedTask: null,
    selection: "",
    selectionSource: "",
    composerText: "",
    goalDraft: "",
    projectMeta: {},
    paletteOpen: false,
    summaryOpen: false,
    composerExpanded: false,
    paletteQuery: "",
    filteredCommands: [],
    toast: null,
    loading: false,
    stateChanging: false,
    summary: null,
    lastDataRefreshAt: 0,
    refreshTimer: null,
    routeTimer: null,
    flushTimer: null,
    nativeSendPending: false,
    pendingNativePrompt: "",
    lastBoardSlug: null,
    layoutObserver: null,
    resizeHandler: null,
    submit: {
      source: "",
      status: "idle",
      command: "",
      detail: "",
      hostNote: "",
      sentAt: 0,
      baselineReply: "",
      resolvedReply: "",
      routeMode: "",
    },
    loop: {
      status: "idle",
      category: "none",
      happened: "Loop is off.",
      blocked: "",
      nextAction: "Turn loop on when you want Project OS to keep moving the board.",
      blockerHelp: "",
      operatorActionId: "",
      operatorActionLabel: "",
      controlPlaneAction: "",
      prompt: "",
      signature: "",
      lastEvaluatedAt: 0,
      lastAutoRunAt: 0,
      ledger: [],
    },
    focusDebug: null,
    composerInteractionUntil: 0,
  };

  const dom = {
    root: null,
  };

  function getComposerElement() {
    return dom.root?.querySelector("[data-field='composer-text']") || null;
  }

  function isComposerFocused() {
    const composer = getComposerElement();
    return Boolean(composer && document.activeElement === composer);
  }

  function markComposerInteraction(durationMs = 1500) {
    state.composerInteractionUntil = Date.now() + durationMs;
  }

  function isComposerInteractionActive() {
    return isComposerFocused() || Date.now() < Number(state.composerInteractionUntil || 0);
  }

  function normalizePersistedSubmit(rawSubmit, persistedProjectSessionId) {
    const merged = {
      ...getEmptySubmitState(),
      ...(rawSubmit || {}),
    };
    const linkedSessionId = String(persistedProjectSessionId || merged.sessionId || "").trim();
    if (linkedSessionId && merged.status === "linked") {
      merged.sessionId = linkedSessionId;
    }
    const sessionMissing = !linkedSessionId;
    if (merged.status === "linked" && sessionMissing) {
      merged.status = "idle";
      merged.detail = "Linked project session was not restored from continuity.";
    }
    return merged;
  }

  function isLoginSurface() {
    return location.pathname === "/login" || Boolean(document.querySelector("#login-form"));
  }

  function isReferenceModeStatus(status) {
    return status === "archived";
  }

  function summarizeCronJob(job) {
    if (!job) return null;
    return {
      id: job.id,
      name: job.name || job.id,
      state: job.state || "",
      schedule_display: job.schedule_display || "",
      next_run_at: job.next_run_at || null,
      workdir: job.workdir || "",
    };
  }

  function summarizeTaskCard(task) {
    if (!task) return null;
    return {
      label: task.label || task.title || task.id || "",
      meta: task.meta || "",
    };
  }

  function summarizeMeta(meta) {
    if (!meta) return normalizeProjectMeta({});
    return {
      status: meta.status || "active",
      goalSummary: meta.goalSummary || "",
      nextStepSummary: meta.nextStepSummary || "",
      blockerSummary: meta.blockerSummary || "",
      projectRef: meta.projectRef || "",
      planRef: meta.planRef || "",
      statusRef: meta.statusRef || "",
      linkedCronIds: Array.isArray(meta.linkedCronIds) ? meta.linkedCronIds : [],
      autoPausedCronIds: Array.isArray(meta.autoPausedCronIds) ? meta.autoPausedCronIds : [],
      autoParkedTaskIds: Array.isArray(meta.autoParkedTaskIds) ? meta.autoParkedTaskIds : [],
      autoLoopEnabled: Boolean(meta.autoLoopEnabled),
      representativeSessionId: meta.representativeSessionId || "",
      lastLoopSignature: meta.lastLoopSignature || "",
      lastLoopSentAt: meta.lastLoopSentAt || 0,
      summarySnapshot: null,
      lastStateSyncedAt: meta.lastStateSyncedAt || null,
    };
  }

  function buildSummarySnapshot(summary) {
    if (!summary) return null;
    return {
      matters: Array.isArray(summary.matters) ? summary.matters.slice(0, 6) : [],
      blockedItems: Array.isArray(summary.blockedItems) ? summary.blockedItems.map(summarizeTaskCard).filter(Boolean) : [],
      next: Array.isArray(summary.next) ? summary.next.map(summarizeTaskCard).filter(Boolean) : [],
      counts: {
        total: Number(summary.counts?.total || 0),
        ready: Number(summary.counts?.ready || 0),
        blocked: Number(summary.counts?.blocked || 0),
      },
      meta: summarizeMeta(summary.meta),
      nextCron: summarizeCronJob(summary.nextCron),
      linkedCrons: Array.isArray(summary.linkedCrons) ? summary.linkedCrons.map(summarizeCronJob).filter(Boolean) : [],
      suggestedCrons: Array.isArray(summary.suggestedCrons) ? summary.suggestedCrons.map(summarizeCronJob).filter(Boolean) : [],
      closeout: summary.closeout
        ? {
            blockers: Array.isArray(summary.closeout.blockers) ? summary.closeout.blockers.slice(0, 6) : [],
            summaryLine: summary.closeout.summaryLine || "",
            readyToClose: Boolean(summary.closeout.readyToClose),
            label: summary.closeout.label || "",
            actionLabel: summary.closeout.actionLabel || "",
            archiveProject: Boolean(summary.closeout.archiveProject),
            finalReportRef: summary.closeout.finalReportRef || "",
            evidenceGate: summary.closeout.evidenceGate
              ? {
                  evidenceSignals: Array.isArray(summary.closeout.evidenceGate.evidenceSignals)
                    ? summary.closeout.evidenceGate.evidenceSignals.slice(0, 6)
                    : [],
                  linkedProjectSessionId: summary.closeout.evidenceGate.linkedProjectSessionId || "",
                  projectSessionMessageCount: Number(summary.closeout.evidenceGate.projectSessionMessageCount || 0),
                  ready: Boolean(summary.closeout.evidenceGate.ready),
                  label: summary.closeout.evidenceGate.label || "",
                  summaryLine: summary.closeout.evidenceGate.summaryLine || "",
                  blockers: Array.isArray(summary.closeout.evidenceGate.blockers)
                    ? summary.closeout.evidenceGate.blockers.slice(0, 4)
                    : [],
                  criteria: Array.isArray(summary.closeout.evidenceGate.criteria)
                    ? summary.closeout.evidenceGate.criteria.slice(0, 4)
                    : [],
                }
              : null,
          }
        : null,
      source: {
        board: summary.source?.board || "default",
        panel: summary.source?.panel || "Chat",
      },
    };
  }

  function normalizeProjectMeta(raw) {
    const current = raw || {};
    return {
      status: current.status || "active",
      goalSummary: current.goalSummary || "",
      nextStepSummary: current.nextStepSummary || "",
      blockerSummary: current.blockerSummary || "",
      projectRef: current.projectRef || "",
      planRef: current.planRef || "",
      statusRef: current.statusRef || "",
      linkedCronIds: Array.isArray(current.linkedCronIds) ? current.linkedCronIds : [],
      autoPausedCronIds: Array.isArray(current.autoPausedCronIds) ? current.autoPausedCronIds : [],
      autoParkedTaskIds: Array.isArray(current.autoParkedTaskIds) ? current.autoParkedTaskIds : [],
      autoLoopEnabled: Boolean(current.autoLoopEnabled),
      representativeSessionId: current.representativeSessionId || "",
      lastLoopSignature: current.lastLoopSignature || "",
      lastLoopSentAt: current.lastLoopSentAt || 0,
      summarySnapshot: buildSummarySnapshot(current.summarySnapshot),
      lastStateSyncedAt: current.lastStateSyncedAt || null,
    };
  }

  function getDefaultProjectControlPlaneRefs() {
    return {
      projectRef: "docs/project-os/PROJECT.md",
      planRef: "docs/project-os/PLAN.md",
      statusRef: "docs/project-os/STATUS.md",
    };
  }

  function getEffectiveProjectRefs(meta) {
    const defaults = getDefaultProjectControlPlaneRefs();
    return {
      projectRef: String(meta?.projectRef || defaults.projectRef || "").trim(),
      planRef: String(meta?.planRef || defaults.planRef || "").trim(),
      statusRef: String(meta?.statusRef || defaults.statusRef || "").trim(),
    };
  }

  function getCurrentProjectReportRef() {
    const closeoutRef = String(state.summary?.closeout?.finalReportRef || "").trim();
    if (closeoutRef) return closeoutRef;
    const refs = getEffectiveProjectRefs(getCurrentProjectMeta());
    return refs.statusRef || refs.projectRef || refs.planRef || "";
  }

  function getControlPlaneIntakeCopy(meta) {
    const status = String(meta?.status || "active").trim() || "active";
    const linkedProjectSessionId = getLinkedProjectSessionId();
    const description =
      linkedProjectSessionId
        ? "Choose a fresh import, reopen the linked project session, or refresh docs only."
        : status === "active"
          ? "Inspect the existing workspace and recover current repo into PROJECT/PLAN/STATUS."
          : "Start a fresh PROJECT/PLAN/STATUS set for this board.";
    const primaryAction = linkedProjectSessionId ? "resume" : status === "active" ? "import" : "create";
    return {
      eyebrow: "Dedicated project-session docs",
      title: "Shipped intake uses dedicated project-session doc actions, not a multi-step wizard.",
      createLabel: "Start blank docs",
      importLabel: "Recover current repo",
      resumeLabel: "Resume linked project",
      syncLabel: "Refresh docs",
      description,
      primaryAction,
      hasLinkedSession: Boolean(linkedProjectSessionId),
    };
  }

  function buildImportProjectPrompt() {
    return [
      "Recover current repo for this board into the dedicated PROJECT/PLAN/STATUS control-plane docs.",
      "Use repo-local continuity, the live board state, and any already-settled browser/runtime proof as the canonical source of truth for this import.",
      "If repo-local continuity or live runtime evidence already proves a real Recover current repo run happened, treat that proof as current truth and correct any older doc text that still says the evidence is missing.",
      "Do not copy stale pre-proof narrative forward just because an older PROJECT/PLAN/STATUS draft still contains it.",
      "Live phase-1 topology is default/ops/builder only; do not introduce a live reviewer profile in repo-side continuity, assignment rules, or phase-boundary notes.",
      "Treat builder as the active manual implementation lane rather than a reserve-only lane, but keep recurring cron/background ownership on the existing default/ops automation surfaces instead of builder.",
      "Route code review through default or builder using repo-side review contracts and skills; route UX/UI/design review through Claude Design first, then hand implementation-facing findings back through DESIGN.md and docs/UIUX-GUIDE.md.",
      "If the current board is still empty, seed the board itself — not only the docs.",
      "Create a reference-only root workflow seed in done/archive and a separate first actionable child in ready on this same board.",
      "Do not stop after doc refresh if board-local task output is still missing.",
      "Refresh the docs conservatively: keep the frozen proof queue intact, preserve closed proof gates, and name the exact next blocker-owned source contract gap instead of broadening scope.",
      "When evidence is missing, say exactly what is missing and what the operator must do next.",
    ].join("\n");
  }

  function buildSyncControlPlanePrompt() {
    return [
      "Refresh the existing dedicated PROJECT/PLAN/STATUS control-plane docs for this board.",
      "Start by re-reading .ax/handoff/current.json, .ax/handoff/current.md, docs/project-os/STATUS.md, and the live kanban board for this board; when they disagree, treat the live board closure state as canonical truth.",
      "If the board advanced or the current acceptance card just closed, rewrite .ax/handoff/current.json, .ax/handoff/current.md, and docs/project-os/STATUS.md to that new live truth before proposing any later follow-up.",
      "Keep Hermes-native ownership intact: board, task, cron, and goal execution stay on the Hermes side while Project OS remains a thin control layer.",
      "Live phase-1 topology is default/ops/builder only; do not drift repo-side docs back toward a live reviewer profile or reserve-only builder wording.",
      "Builder is an active manual implementation lane, but recurring cron/background ownership stays off builder unless an explicit future contract changes that rule.",
      "Route code review through default or builder plus repo review guidance, and route UX/UI/design review through Claude Design first before repo-side DESIGN.md / docs/UIUX-GUIDE.md implementation follow-through.",
      "Preserve current queue-freeze and blocker ordering, and only rewrite stale narrative when continuity or live evidence proves the newer state.",
      "Do not thaw or narrate the next queue-frozen follow-up from this continuity refresh unless the live board already shows that card released; continuity repair comes before any new thaw.",
      "If the current blocker requires operator action, describe the exact blocker and the next safe action in concrete terms.",
    ].join("\n");
  }

  function getActiveProfileName() {
    return String(state.activeProfile?.name || state.activeProfile?.profile || state.activeProfile?.id || "default").trim() || "default";
  }

  function getWorkflowSeedRuntimePayload(seed) {
    if (String(seed?.status || "").toLowerCase() !== "ready") return {};
    const payload = {
      assignee: getActiveProfileName(),
      workspace_label: getWorkspaceLabel(),
    };
    if (state.session?.cwd || state.activeProfile?.cwd) {
      payload.workspace_kind = "dir";
      payload.workspace_path = String(state.session?.cwd || state.activeProfile?.cwd || "").trim();
    }
    return payload;
  }

  function getWorkspaceLabel() {
    return String(state.session?.cwd || state.activeProfile?.cwd || state.workspaces?.[0]?.path || "").trim();
  }

  function loadPersisted() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      state.projectMeta = parsed.projectMeta || {};
      state.selectedTask = parsed.selectedTask || null;
      state.composerText = parsed.composerText || "";
      state.selection = parsed.selection || "";
      state.selectionSource = parsed.selectionSource || "";
      state.lastBoardSlug = parsed.lastBoardSlug || null;
      state.composerExpanded = Boolean(parsed.composerExpanded || parsed.composerText);
      state.projectSessionId = parsed.projectSessionId || "";
      if (parsed.submit && typeof parsed.submit === "object") {
        state.submit = normalizePersistedSubmit(parsed.submit, parsed.projectSessionId || "");
      }
      if (!state.projectSessionId && state.submit.sessionId) {
        state.projectSessionId = state.submit.sessionId;
      }
    } catch (_error) {
      state.projectMeta = {};
    }
  }

  function savePersisted() {
    const payload = {
      projectMeta: state.projectMeta,
      selectedTask: state.selectedTask,
      composerText: state.composerText,
      selection: state.selection,
      selectionSource: state.selectionSource,
      lastBoardSlug: state.currentBoard,
      composerExpanded: state.composerExpanded,
      projectSessionId: state.projectSessionId,
      submit: state.submit,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  }

  function getProjectMeta(boardSlug) {
    return normalizeProjectMeta(state.projectMeta[boardSlug] || {});
  }

  function getCurrentProjectMeta() {
    return getProjectMeta(state.currentBoard || "default");
  }

  function setProjectMeta(boardSlug, patch, options) {
    const current = getProjectMeta(boardSlug);
    state.projectMeta[boardSlug] = normalizeProjectMeta({ ...current, ...patch });
    savePersisted();
    if (options?.renderNow !== false) {
      render();
    }
  }

  function setToast(title, body) {
    state.toast = { title, body, createdAt: Date.now() };
    render();
    window.clearTimeout(state.flushTimer);
    state.flushTimer = window.setTimeout(() => {
      state.toast = null;
      render();
    }, TOAST_MS);
  }

  function addLoopLedger(entry) {
    const next = {
      at: new Date().toISOString(),
      ...entry,
    };
    state.loop.ledger = [next, ...(state.loop.ledger || [])].slice(0, LOOP_LEDGER_LIMIT);
  }

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  async function api(path, options) {
    const response = await fetch(path, {
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
      ...options,
    });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    const text = await response.text();
    return text ? JSON.parse(text) : {};
  }

  function boardQuery(boardSlug, extra) {
    const params = new URLSearchParams();
    if (extra) {
      Object.entries(extra).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.set(key, String(value));
        }
      });
    }
    if (boardSlug) {
      params.set("board", boardSlug);
    }
    const query = params.toString();
    return query ? `?${query}` : "";
  }

  function getCurrentSessionId() {
    const match = location.pathname.match(/\/session\/([^/?#]+)/);
    return match ? match[1] : null;
  }

  function chooseRepresentativeSessionId(meta, explicitSessionId, sessions, boardSlug = state.currentBoard) {
    const sessionRows = Array.isArray(sessions) ? sessions : [];
    if (explicitSessionId) {
      const explicit = sessionRows.find((entry) => entry.session_id === explicitSessionId);
      if (explicit && projectSessionMatchesBoard(explicit, boardSlug)) {
        return { sessionId: explicitSessionId, mode: "url" };
      }
    }
    if (meta.representativeSessionId) {
      const metaSession = sessionRows.find((entry) => entry.session_id === meta.representativeSessionId);
      if (metaSession && projectSessionMatchesBoard(metaSession, boardSlug)) {
        return { sessionId: meta.representativeSessionId, mode: "project-meta" };
      }
    }
    const boardMatched = sessionRows
      .filter((entry) => !entry.archived && projectSessionMatchesBoard(entry, boardSlug))
      .sort((a, b) => {
        const scoreDelta = getProjectSessionEvidenceScore(b) - getProjectSessionEvidenceScore(a);
        if (scoreDelta) return scoreDelta;
        return String(b?.session_id || "").localeCompare(String(a?.session_id || ""));
      })[0];
    return boardMatched?.session_id ? { sessionId: boardMatched.session_id, mode: "project-board" } : { sessionId: null, mode: "none" };
  }

  function getProjectSessionBoardTitle(boardSlug = state.currentBoard) {
    return `Project OS · ${String(boardSlug || "default").trim() || "default"}`;
  }

  function getBoardSlugFromProjectSession(session) {
    const title = String(session?.title || "").trim();
    const prefix = "Project OS · ";
    if (!title.startsWith(prefix)) return null;
    return String(title.slice(prefix.length).trim() || "default");
  }

  function getLinkedProjectSessionId() {
    return String(state.projectSession?.session_id || state.projectSessionId || state.submit?.sessionId || "").trim();
  }

  function projectSessionMatchesBoard(session, boardSlug = state.currentBoard) {
    const boardTitle = getProjectSessionBoardTitle(boardSlug);
    return String(session?.title || "").trim() === boardTitle;
  }

  function getProjectSessionEvidenceScore(session) {
    const hasMessages = Number(session?.message_count || 0) > 0 ? 4 : 0;
    const hasStream = session?.active_stream_id ? 3 : 0;
    const hasPending = session?.pending_user_message ? 2 : 0;
    const freshness = Number(session?.updated_at || session?.last_message_at || 0) ? 1 : 0;
    return hasMessages + hasStream + hasPending + freshness;
  }

  function isEmptyIdleProjectSession(session) {
    if (!session) return true;
    return !session.active_stream_id && !session.pending_user_message && Number(session.message_count || 0) === 0;
  }

  async function adoptBestProjectSessionCandidate(boardSlug = state.currentBoard, currentSession = state.projectSession) {
    const boardTitle = getProjectSessionBoardTitle(boardSlug);
    let sessionsPayload = { sessions: [] };
    try {
      sessionsPayload = await api("/api/sessions");
    } catch (_error) {
      sessionsPayload = { sessions: [] };
    }
    const matchingSessions = Array.isArray(sessionsPayload.sessions)
      ? sessionsPayload.sessions.filter((session) => String(session?.title || "") === boardTitle)
      : [];
    const currentMatchesBoard = projectSessionMatchesBoard(currentSession, boardSlug);
    const candidates = [currentMatchesBoard ? currentSession : null, ...matchingSessions].filter(Boolean);
    const preferred =
      candidates.sort((a, b) => {
        const scoreDelta = getProjectSessionEvidenceScore(b) - getProjectSessionEvidenceScore(a);
        if (scoreDelta) return scoreDelta;
        return String(b?.session_id || "").localeCompare(String(a?.session_id || ""));
      })[0] || null;
    if (preferred) {
      state.projectSession = preferred;
      state.projectSessionId = preferred.session_id;
    }
    return preferred;
  }

  function buildProjectSessionCreatePayload() {
    const payload = {
      profile: getActiveProfileName(),
    };
    const workspace = String(state.session?.workspace || state.workspaces?.[0]?.path || "").trim();
    if (workspace) payload.workspace = workspace;
    return payload;
  }

  async function createProjectSessionForBoard(boardSlug = state.currentBoard) {
    const createPayload = await api("/api/session/new", {
      method: "POST",
      body: JSON.stringify(buildProjectSessionCreatePayload()),
    });
    const createdSession = createPayload?.session || createPayload || null;
    const sessionId = String(createdSession?.session_id || "").trim();
    if (!sessionId) return createdSession;
    const boardTitle = getProjectSessionBoardTitle(boardSlug);
    let projectSession = createdSession;
    if (!projectSessionMatchesBoard(createdSession, boardSlug)) {
      const renamePayload = await api("/api/session/rename", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId,
          title: boardTitle,
        }),
      });
      projectSession = renamePayload?.session || projectSession;
    }
    state.projectSession = {
      ...(projectSession || createdSession || {}),
      session_id: sessionId,
      title: boardTitle,
    };
    state.projectSessionId = sessionId;
    state.projectSessionLastFetchedAt = Date.now();
    return state.projectSession;
  }

  async function ensureProjectSession() {
    const preferred = await adoptBestProjectSessionCandidate(state.currentBoard, state.projectSession);
    if (preferred) return preferred;
    const fallbackId = String(state.projectSessionId || state.submit?.sessionId || "").trim();
    if (fallbackId) {
      try {
        const fallbackPayload = await api(`/api/session?session_id=${encodeURIComponent(fallbackId)}`);
        const fallbackSession = fallbackPayload?.session || fallbackPayload || null;
        if (projectSessionMatchesBoard(fallbackSession, state.currentBoard)) {
          state.projectSession = fallbackSession;
          state.projectSessionId = String(fallbackSession?.session_id || fallbackId).trim();
          return fallbackSession;
        }
      } catch (_error) {
        // Fall through to creating a board-local dedicated session.
      }
    }
    const fallback = {
      session_id: fallbackId,
      title: getProjectSessionBoardTitle(state.currentBoard),
      message_count: 0,
      active_stream_id: null,
      pending_user_message: false,
    };
    if (fallbackId && projectSessionMatchesBoard(state.projectSession, state.currentBoard)) {
      state.projectSession = fallback;
      state.projectSessionId = fallbackId;
      return fallback;
    }
    return await createProjectSessionForBoard(state.currentBoard);
  }

  function requestRender() {
    render();
  }

  function isControlPlaneSubmitActive() {
    return ["sent", "waiting", "stalled_running", "linked"].includes(String(state.submit?.status || ""));
  }

  async function refreshProjectSession(options = {}) {
    if (state.projectSessionRefreshPromise) return state.projectSessionRefreshPromise || state.projectSession;
    state.projectSessionSyncing = true;
    state.projectSessionRefreshPromise = (async () => {
      try {
        const current = await ensureProjectSession();
        const sessionId = String(current?.session_id || state.projectSessionId || "").trim();
        if (!sessionId) return current;
        const payload = await api(`/api/session?session_id=${encodeURIComponent(sessionId)}`);
        const session = payload?.session || payload || current;
        state.projectSession = session;
        state.projectSessionId = session.session_id || sessionId;
        state.projectSessionLastFetchedAt = Date.now();
        if (options.renderNow !== false) render();
        return session;
      } finally {
        state.projectSessionSyncing = false;
      }
    })();
    const submitLifecycleChanged = isControlPlaneSubmitActive() ? updateSubmitLifecycle() : false;
    if (submitLifecycleChanged && !options.renderNow) {
        requestRender();
      }
    try {
      return await state.projectSessionRefreshPromise;
    } finally {
      state.projectSessionRefreshPromise = null;
    }
  }

  function restoreProjectSessionContinuity() {
    if (isEmptyIdleProjectSession(state.projectSession)) return false;
    state.submit = normalizePersistedSubmit(
      {
        ...state.submit,
        sessionId: state.projectSession.session_id || state.projectSessionId || "",
        status: "linked",
      },
      state.projectSession.session_id || state.projectSessionId || "",
    );
    return true;
  }

  function getSubmitDocRefreshKey(submit = state.submit) {
    return `${submit?.sessionId || ""}:${submit?.streamId || ""}:${submit?.status || ""}`;
  }

  function getProjectSessionMessageCount(sourceSession = state.projectSession) {
    const session = sourceSession || {};
    return Number(session.message_count || (Array.isArray(session.messages) ? session.messages.length : 0) || 0);
  }

  function getProjectSessionRunningState(now = Date.now(), sourceSession = state.projectSession) {
    const session = sourceSession || {};
    const hasActiveStream = Boolean(session.active_stream_id);
    const hasPendingUserMessage = Boolean(session.pending_user_message);
    const running = Boolean(hasActiveStream || hasPendingUserMessage);
    const messageCount = getProjectSessionMessageCount(session);
    const ageMs = Math.max(0, now - Number(state.submit?.sentAt || 0));
    const zeroMessageDispatch = running && messageCount === 0;
    return {
      running,
      hasActiveStream,
      hasPendingUserMessage,
      messageCount,
      ageMs,
      suspiciouslyStalled: zeroMessageDispatch && ageMs >= RUNNING_STALLED_MS,
    };
  }

  async function awaitProjectSessionTransportAck(sessionId, baselineMessageCount) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < 2000) {
      const payload = await api(`/api/session?session_id=${encodeURIComponent(sessionId)}`);
      const session = payload?.session || payload || null;
      if (session) {
        state.projectSession = session;
        state.projectSessionId = String(session.session_id || sessionId).trim();
        state.projectSessionLastFetchedAt = Date.now();
        const runningState = getProjectSessionRunningState(Date.now(), session);
        if (runningState.running || runningState.messageCount > baselineMessageCount) {
          return {
            session,
            stream_id: String(session.active_stream_id || "").trim(),
            pending_started_at: startedAt,
          };
        }
      }
      await new Promise((resolve) => window.setTimeout(resolve, 180));
    }
    throw new Error("Dedicated project session send did not start transport. Open the session and retry.");
  }

  async function openProjectSession() {
    const currentProjectSession = await ensureProjectSession();
    const linkedSessionId = String(state.submit?.sessionId || "").trim();
    let linkedProjectSession = null;
    if (linkedSessionId) {
      const linkedPayload = await api(`/api/session?session_id=${encodeURIComponent(linkedSessionId)}`);
      linkedProjectSession = linkedPayload?.session || linkedPayload || null;
    }
    const preferredLinkedSessionId =
      linkedProjectSession &&
      !isEmptyIdleProjectSession(linkedProjectSession) &&
      projectSessionMatchesBoard(linkedProjectSession, state.currentBoard)
        ? linkedSessionId
        : "";
    const currentSessionId = String(currentProjectSession?.session_id || state.projectSessionId || "").trim();
    const sessionId = preferredLinkedSessionId || currentSessionId || state.projectSessionId;
    if (!sessionId) return null;
    const resolvedSessionId = String(preferredLinkedSessionId || currentSessionId || state.projectSessionId || "").trim();
    if (resolvedSessionId && resolvedSessionId !== state.projectSessionId) {
      state.projectSessionId = resolvedSessionId;
    }
    if (resolvedSessionId && resolvedSessionId !== linkedSessionId && linkedProjectSession && isEmptyIdleProjectSession(linkedProjectSession)) {
      state.submit = normalizePersistedSubmit(
        {
          ...state.submit,
          sessionId: resolvedSessionId,
          status: "linked",
        },
        resolvedSessionId,
      );
    }
    state.projectSession = {
      ...(currentProjectSession || linkedProjectSession || state.projectSession || {}),
      session_id: resolvedSessionId,
      sessionId: resolvedSessionId,
    };
    await reconcileVisibleKanbanBoard(state.currentBoard);
    await window.loadSession(resolvedSessionId);
    openPanel("Chat");
    focusNativeComposer();
    history.pushState(null, "", `/session/${encodeURIComponent(resolvedSessionId)}`);
    return {
      sessionId: resolvedSessionId,
    };
  }

  async function awaitProjectSessionComposerReady(sessionId) {
    const expectedSessionId = String(sessionId || state.projectSessionId || "").trim();
    const startedAt = Date.now();
    while (Date.now() - startedAt < 2500) {
      const currentPath = String(location.pathname || "").trim();
      const native = getNativeComposer();
      const textareaReady = Boolean(native?.textarea && isElementFocusable(native.textarea));
      const sendReady = Boolean(native?.sendButton);
      if ((!expectedSessionId || currentPath.endsWith(`/${encodeURIComponent(expectedSessionId)}`)) && textareaReady && sendReady) {
        return true;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 80));
    }
    throw new Error("Dedicated project session composer did not become ready. Open the session and retry.");
  }

  async function sendProjectSessionPrompt(promptText) {
    const projectSession = await ensureProjectSession();
    const refreshed = await refreshProjectSession({ renderNow: false });
    if (getProjectSessionRunningState(Date.now(), refreshed).running) {
      throw new Error("Project OS composer session is still running. Wait for the current reply or open the session.");
    }
    const baselineMessageCount = getProjectSessionMessageCount(refreshed || projectSession);
    const opened = await openProjectSession();
    await awaitProjectSessionComposerReady(opened?.sessionId || state.projectSessionId || projectSession?.session_id || "");
    const routed = routePromptToHermes(promptText, true);
    if (!routed?.ok) {
      throw new Error("Dedicated project session composer is unavailable.");
    }
    const sessionId = String(state.projectSessionId || projectSession?.session_id || "").trim();
    const ack = await awaitProjectSessionTransportAck(sessionId, baselineMessageCount);
    const startData = {
      session_id: String(ack?.session?.session_id || sessionId).trim(),
      stream_id: String(ack?.stream_id || "").trim(),
      pending_started_at: ack?.pending_started_at || Date.now(),
    };
    state.submit = normalizePersistedSubmit(
      {
        ...state.submit,
        status: "linked",
        sessionId: startData.session_id || state.projectSessionId || "",
        active_stream_id: startData.stream_id || null,
        pending_started_at: startData.pending_started_at || Date.now(),
      },
      startData.session_id || state.projectSessionId || "",
    );
    savePersisted();
    return startData;
  }

  function getPrimaryNavButtons() {
    const root = document.querySelector("nav[aria-label='Primary navigation'], nav.rail");
    return root ? [...root.querySelectorAll("button")] : [];
  }

  function normalizePanelLabel(raw) {
    const label = (raw || "").trim();
    if (!label) return "";
    if (label === "Agent profiles") return "Profiles";
    if (label.includes("Dashboard")) return "Hermes Dashboard";
    return label;
  }

  function getCurrentPanelName() {
    if (isLoginSurface()) return "Login";
    const buttons = getPrimaryNavButtons();
    const active =
      buttons.find((button) => button.classList.contains("active")) ||
      buttons.find((button) => button.getAttribute("aria-current") === "page") ||
      buttons.find((button) => button.getAttribute("aria-selected") === "true") ||
      buttons.find((button) => button.getAttribute("aria-pressed") === "true");
    if (active) {
      return normalizePanelLabel(active.getAttribute("aria-label") || active.innerText || active.textContent || "");
    }
    if (getNativeComposer()) return "Chat";
    return "Chat";
  }

  function normalizeVisibleBoardSwitcherLabel(raw) {
    return String(raw || "")
      .replace(/\s+/g, " ")
      .replace(/^\p{Extended_Pictographic}\s*/u, "")
      .trim()
      .toLowerCase();
  }

  function getVisibleKanbanBoardSlug() {
    const toggle = document.querySelector(".kanban-board-switcher-toggle");
    const label = normalizeVisibleBoardSwitcherLabel(toggle?.textContent || toggle?.innerText || "");
    if (!label || !Array.isArray(state.boards) || !state.boards.length) return null;
    const exact = state.boards.find((board) => {
      const name = normalizeVisibleBoardSwitcherLabel(board?.name || "");
      const slug = normalizeVisibleBoardSwitcherLabel(board?.slug || "");
      return Boolean((name && label === name) || (slug && label === slug));
    });
    if (exact) return exact.slug || null;
    const matched = state.boards
      .filter((board) => {
        const name = normalizeVisibleBoardSwitcherLabel(board?.name || "");
        return Boolean(name && (label.includes(name) || name.includes(label)));
      })
      .sort((a, b) => String(b?.name || "").length - String(a?.name || "").length)[0];
    return matched?.slug || null;
  }

  async function reconcileVisibleKanbanBoard(boardSlug = state.currentBoard) {
    const targetBoard = String(boardSlug || "").trim();
    if (!targetBoard) return false;
    const visibleBoardSlug = getVisibleKanbanBoardSlug();
    if (visibleBoardSlug === targetBoard) return false;
    if (typeof window.switchKanbanBoard === "function") {
      await window.switchKanbanBoard(targetBoard);
    } else {
      try {
        await api(`/api/kanban/boards/${encodeURIComponent(targetBoard)}/switch`, {
          method: "POST",
          body: JSON.stringify({}),
        });
      } catch (_error) {
        return false;
      }
    }
    state.currentBoard = targetBoard;
    state.lastBoardSlug = targetBoard;
    savePersisted();
    return true;
  }

  async function resolveAuthoritativeControlPlaneBoard(currentBoardSlug = "", options = {}) {
    const knownBoards = Array.isArray(state.boards) ? state.boards : [];
    const boardExists = (boardSlug) => knownBoards.some((board) => board.slug === boardSlug);
    const canonicalCurrentBoard = String(currentBoardSlug || "").trim();
    const localCurrentBoard = String(state.currentBoard || "").trim();
    const allowSessionCandidates = options.allowLinkedSessionBoard !== false;
    const sessionCandidates = [state.projectSession, { session_id: state.projectSessionId }, { session_id: state.submit?.sessionId }]
      .map((entry) => ({
        session_id: String(entry?.session_id || "").trim(),
        session: entry?.title ? entry : null,
      }))
      .filter((entry, index, array) => entry.session_id && array.findIndex((candidate) => candidate.session_id === entry.session_id) === index);

    if (allowSessionCandidates) {
      for (const candidate of sessionCandidates) {
      let session = candidate.session;
      if (!session || !session.title) {
        try {
          const payload = await api(`/api/session?session_id=${encodeURIComponent(candidate.session_id)}`);
          session = payload?.session || payload || null;
        } catch (_error) {
          session = null;
        }
      }
      const boardSlug = getBoardSlugFromProjectSession(session);
      if (!boardSlug || !boardExists(boardSlug) || isEmptyIdleProjectSession(session)) continue;
        state.projectSession = session;
        state.projectSessionId = String(session?.session_id || candidate.session_id || "").trim();
        return boardSlug;
      }
    }

    if (canonicalCurrentBoard && boardExists(canonicalCurrentBoard)) {
      return canonicalCurrentBoard;
    }
    return localCurrentBoard && boardExists(localCurrentBoard) ? localCurrentBoard : null;
  }

  async function prepareControlPlaneBoardContext(options = {}) {
    const boardsPayload = await api("/api/kanban/boards");
    state.boards = boardsPayload.boards || [];
    const authoritativeBoardSlug = await resolveAuthoritativeControlPlaneBoard(boardsPayload.current, options);
    if (!authoritativeBoardSlug) {
      return {
        boardSlug: null,
        boardData: null,
      };
    }
    const boardChanged = authoritativeBoardSlug !== state.currentBoard;
    if (boardChanged) {
      state.currentBoard = authoritativeBoardSlug;
      state.lastBoardSlug = authoritativeBoardSlug;
      await reconcileVisibleKanbanBoard(authoritativeBoardSlug);
    }
    if (boardChanged || !state.boardData) {
      const [boardData, boardStats] = await Promise.all([
        api(`/api/kanban/board${boardQuery(authoritativeBoardSlug)}`),
        api(`/api/kanban/stats${boardQuery(authoritativeBoardSlug)}`),
      ]);
      state.boardData = boardData;
      state.boardStats = boardStats;
    }
    savePersisted();
    return {
      boardSlug: authoritativeBoardSlug,
      boardData: state.boardData,
    };
  }

  function getLayoutElements() {
    return {
      rail: document.querySelector("nav.rail, nav[aria-label='Primary navigation']"),
      sidebar: document.querySelector("aside.sidebar, .sidebar"),
      main: document.querySelector("main.main, main"),
      rightpanel: document.querySelector("aside.rightpanel, .rightpanel"),
    };
  }

  function measureLayoutGeometry() {
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const { rail, sidebar, main, rightpanel } = getLayoutElements();
    const railRect = rail?.getBoundingClientRect?.();
    const sidebarRect = sidebar?.getBoundingClientRect?.();
    const mainRect = main?.getBoundingClientRect?.();
    const rightRect = rightpanel?.getBoundingClientRect?.();

    const derivedMainLeft = mainRect?.width
      ? mainRect.left
      : Math.max(railRect?.right || 0, 0) + Math.max(sidebarRect?.width || 0, 0);
    const derivedMainRight = mainRect?.width
      ? viewportWidth - mainRect.right
      : Math.max(viewportWidth - (rightRect?.left || viewportWidth), 0);
    const derivedMainWidth = mainRect?.width
      ? mainRect.width
      : Math.max(viewportWidth - derivedMainLeft - derivedMainRight, 320);

    return {
      mainLeft: Math.max(12, Math.round(derivedMainLeft || 12)),
      mainRight: Math.max(12, Math.round(derivedMainRight || 12)),
      mainWidth: Math.max(320, Math.round(derivedMainWidth || 320)),
      shellRight: Math.max(20, Math.round((derivedMainRight || 0) + 20)),
      gutter: 20,
    };
  }

  function isProjectFriendlyPanel(panelName) {
    return ["Chat", "Kanban", "Tasks"].includes(panelName || state.currentPanel || "Chat");
  }

  function isChatPanel(panelName) {
    return (panelName || state.currentPanel || "Chat") === "Chat";
  }

  function shouldUseLoginMode() {
    return isLoginSurface();
  }

  function getFlattenedTasks(boardData) {
    if (!boardData?.columns) return [];
    return boardData.columns.flatMap((column) =>
      (column.tasks || []).map((task) => ({
        ...task,
        _column: column.name,
      })),
    );
  }

  function normalizeWorkflowSeedText(value) {
    return String(value || "").replace(/\r\n/g, "\n").trim();
  }

  function getWorkflowSeedTaskPatch(task, seed) {
    const patch = {};
    if (String(task?.title || "").trim() !== String(seed?.title || "").trim()) {
      patch.title = seed.title;
    }
    if (normalizeWorkflowSeedText(task?.body) !== normalizeWorkflowSeedText(seed?.body)) {
      patch.body = seed.body;
    }
    if (String(task?.status || "").toLowerCase() !== String(seed?.status || "").toLowerCase()) {
      patch.status = seed.status;
    }
    return patch;
  }

  function findWorkflowSeedTask(tasks, seed) {
    return (tasks || []).find((task) => {
      const body = String(task?.body || "");
      const title = String(task?.title || "").trim();
      return (body.includes(`[${PROJECT_WORKFLOW_SEED_MARKER}]`) && body.includes(`Seed: ${seed?.id}`))
        || title === String(seed?.title || "").trim();
    });
  }

  async function syncWorkflowSeedTasks(boardSlug = state.currentBoard, boardData = state.boardData) {
    const flattened = getFlattenedTasks(boardData);
    const tasks = [...flattened];
    let changed = false;
    let rootTask = findWorkflowSeedTask(tasks, PROJECT_WORKFLOW_SEEDS[0]);
    for (const seed of PROJECT_WORKFLOW_SEEDS) {
      let seedTask = findWorkflowSeedTask(tasks, seed);
      if (!seedTask) {
        const createPayload = {
          title: seed.title,
          body: seed.body,
          status: seed.status,
          created_by: "project-os-extension",
          ...getWorkflowSeedRuntimePayload(seed),
        };
        if (seed.id === "first-actionable-child" && rootTask?.id) {
          createPayload.parents = [rootTask.id];
        }
        const created = await api(`/api/kanban/tasks${boardQuery(boardSlug)}`, {
          method: "POST",
          body: JSON.stringify(createPayload),
        });
        seedTask = created?.task || null;
        if (!seedTask) continue;
        tasks.push(seedTask);
        if (seed.id === "root") rootTask = seedTask;
        changed = true;
        continue;
      }
      const patch = getWorkflowSeedTaskPatch(seedTask, seed);
      if (!Object.keys(patch).length) continue;
      await api(`/api/kanban/tasks/${encodeURIComponent(seedTask.id)}${boardQuery(boardSlug)}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      changed = true;
    }
    if (changed && boardSlug === state.currentBoard) {
      state.boardData = await api(`/api/kanban/board${boardQuery(boardSlug)}`);
    }
    return changed;
  }

  function getSelectedTaskTitleFromDom() {
    const preview = document.querySelector("#kanbanTaskPreview");
    if (preview && preview.style.display !== "none") {
      const previewTitle = preview.querySelector(".kanban-task-preview-title")?.textContent || "";
      if (previewTitle.trim()) return previewTitle.trim();
    }
    const selectedCard = document.querySelector(".kanban-card.selected");
    const bodyText =
      selectedCard?.querySelector(".kanban-card-body")?.textContent ||
      selectedCard?.textContent ||
      "";
    const markerMatch = bodyText.match(/^Task:\s+(.+)$/m);
    if (markerMatch?.[1]?.trim()) return markerMatch[1].trim();
    const fallbackTitle = selectedCard?.querySelector(".kanban-card-title")?.textContent || "";
    return fallbackTitle.trim();
  }

  function deriveSelectedTask() {
    const flattened = getFlattenedTasks(state.boardData);
    const selectedCard = document.querySelector(".kanban-card.selected");
    const selectedId = selectedCard?.dataset?.kanbanTaskId || state.selectedTask?.id;
    if (!selectedId) {
      return state.selectedTask && state.selectedTask.board === state.currentBoard ? state.selectedTask : null;
    }
    const task = flattened.find((entry) => entry.id === selectedId);
    if (task) {
      return {
        id: task.id,
        title: getSelectedTaskTitleFromDom() || task.title || task.id,
        status: task.status || task._column || "",
        assignee: task.assignee || "",
        board: state.currentBoard,
      };
    }
    return state.selectedTask && state.selectedTask.id === selectedId ? state.selectedTask : null;
  }

  function getLinkedCrons(meta) {
    return state.crons.filter((job) => meta.linkedCronIds.includes(job.id));
  }

  function getSuggestedCrons(meta) {
    return state.crons.filter((job) => !meta.linkedCronIds.includes(job.id)).slice(0, 3);
  }

  function deriveCloseoutSnapshot(meta, flattened, byColumn) {
    const readyTasks = byColumn.ready || [];
    const runningTasks = byColumn.running || [];
    const blockedTasks = byColumn.blocked || [];
    const missingGoal = !String(meta.goalSummary || "").trim();
    const projectSessionMessageCount = getProjectSessionMessageCount(state.projectSession);
    const linkedProjectSessionId = getLinkedProjectSessionId();
    const snapshotEvidenceSignals = Array.isArray(meta.summarySnapshot?.closeout?.evidenceGate?.evidenceSignals)
      ? meta.summarySnapshot.closeout.evidenceGate.evidenceSignals.slice()
      : [];
    const evidenceSignals = [...snapshotEvidenceSignals];
    const evidenceCollecting = runningTasks.length > 0;
    const refs = getEffectiveProjectRefs(meta);
    const finalReportRef = refs.statusRef || refs.projectRef || refs.planRef || "";
    const evidenceCriteria = [
      "Dedicated project session linked to this board",
      "Browser/runtime proof captured in the evidence thread",
      "Final report ref ready for closeout handoff",
    ];
    if (projectSessionMessageCount > 0) evidenceSignals.push("Dedicated project session history linked");
    if (!projectSessionMessageCount && linkedProjectSessionId && snapshotEvidenceSignals.length && !evidenceCollecting) {
      evidenceSignals.push("Dedicated project session continuity still linked");
    }
    if (!evidenceSignals.length && snapshotEvidenceSignals.length && !evidenceCollecting) {
      evidenceSignals.push("Previously captured browser/runtime proof is still linked");
    }
    const evidenceBlockers = [];
    if (!linkedProjectSessionId) evidenceBlockers.push("Dedicated project session is not linked yet");
    if (!evidenceSignals.length && !evidenceCollecting) evidenceBlockers.push("No browser/runtime proof linked yet");
    if (!finalReportRef) evidenceBlockers.push("Missing final report ref");
    const evidenceReady = evidenceBlockers.length === 0;
    const blockers = [];
    if (missingGoal) blockers.push("Missing project goal summary");
    if (runningTasks.length) blockers.push("Running work still in progress");
    if (blockedTasks.length) blockers.push("Blocked tasks still unresolved");
    if (readyTasks.length) blockers.push("Ready tasks remain on the board");
    blockers.push(...evidenceBlockers);
    const readyToClose = blockers.length === 0;
    return {
      blockers,
      evidenceGate: {
        evidenceSignals,
        linkedProjectSessionId,
        projectSessionMessageCount,
        ready: evidenceReady,
        label: evidenceReady ? "Evidence ready" : evidenceCollecting ? "Evidence collecting" : "Evidence blocked",
        summaryLine: evidenceReady
          ? "Browser/runtime evidence gate passed."
          : evidenceCollecting
            ? "Browser/runtime evidence is still being collected."
            : `Browser/runtime evidence blocked: ${evidenceBlockers[0] || "proof still missing"}`,
        blockers: evidenceBlockers,
        criteria: evidenceCriteria,
      },
      summaryLine: readyToClose ? "Closeout is ready." : `Closeout blocked: ${blockers[0] || "review blockers"}`,
      readyToClose,
      label: readyToClose ? "Ready to close" : "Closeout blocked",
      actionLabel: readyToClose ? "Close project" : "Review closeout blockers",
      archiveProject: readyToClose,
      finalReportRef,
    };
  }

  function getWorkflowStageTone(status) {
    if (status === "done") return { label: "Done", className: "is-done" };
    if (status === "active") return { label: "In progress", className: "is-active" };
    if (status === "blocked") return { label: "Blocked", className: "is-blocked" };
    return { label: "Pending", className: "is-pending" };
  }

  function findReferenceWorkflowSeed(flattened) {
    return (flattened || []).find((task) => {
      const body = String(task?.body || "");
      const title = String(task?.title || "").trim();
      return body.includes(`[${PROJECT_WORKFLOW_SEED_MARKER}]`) || title === "Project OS import/resume workflow";
    }) || null;
  }

  function buildWorkflowStages(meta, flattened, byColumn, closeout, blockedItems, next) {
    const referenceSeed = findReferenceWorkflowSeed(flattened);
    const linkedProjectSessionId = getLinkedProjectSessionId();
    const runningTasks = byColumn.running || [];
    const readyTasks = byColumn.ready || [];
    const evidenceGate = closeout.evidenceGate || {};
    const nextTask = next[0] || null;
    const nextBlocked = blockedItems[0] || null;
    const refs = getEffectiveProjectRefs(meta);
    const finalReportRef = closeout.finalReportRef || refs.statusRef || refs.projectRef || refs.planRef || "";
    const goalSummary = String(meta.goalSummary || "").trim();
    const nextStepSummary = String(meta.nextStepSummary || nextTask?.label || "").trim();
    const blockerSummary = String(meta.blockerSummary || nextBlocked?.label || closeout.blockers?.[0] || "").trim();
    const stageRefs = [refs.projectRef, refs.planRef, refs.statusRef].filter(Boolean);
    return [
      {
        key: "goal",
        title: "Goal setting",
        status: goalSummary ? "done" : "blocked",
        summary: goalSummary || "Goal summary still missing.",
        detail: goalSummary ? "Project goal is pinned for downstream work." : "Save a goal summary before closeout can pass.",
      },
      {
        key: "intake",
        title: "Import/resume intake",
        status: linkedProjectSessionId ? "done" : "active",
        summary: linkedProjectSessionId
          ? "Dedicated project session linked for repo-aware intake."
          : "Dedicated project session still needs to be linked.",
        detail: linkedProjectSessionId ? getProjectSessionLabel() : "Use Recover current repo or Start blank docs.",
      },
      {
        key: "seed",
        title: "Root workflow seed",
        status: referenceSeed ? "done" : flattened.length ? "active" : "pending",
        summary: referenceSeed
          ? "Reference-only workflow anchor is present on this board."
          : flattened.length
            ? "Board has work, but the reference root seed is not visible."
            : "Seed the board to create the continuity anchor and first action.",
        detail: referenceSeed ? String(referenceSeed.title || "Project OS import/resume workflow") : "Reference-only anchor keeps continuity separate from action.",
      },
      {
        key: "development",
        title: "Continuous development",
        status: runningTasks.length ? "active" : nextTask ? "done" : flattened.length ? "active" : "pending",
        summary: blockerSummary || nextStepSummary || "No active development slice surfaced yet.",
        detail: runningTasks.length
          ? `${runningTasks.length} task running now.`
          : nextTask
            ? `Next ready slice: ${nextTask.label}`
            : "Move the next meaningful card into ready when work should continue.",
      },
      {
        key: "evidence",
        title: "Browser/runtime evidence gate",
        status: evidenceGate.ready ? "done" : runningTasks.length ? "active" : "blocked",
        summary: evidenceGate.summaryLine || "Evidence gate not evaluated yet.",
        detail: evidenceGate.ready
          ? "Evidence is linked and ready for operator review before closeout."
          : evidenceGate.blockers?.[0] || "Collect browser/runtime proof before closeout.",
        criteria: Array.isArray(evidenceGate.criteria) ? evidenceGate.criteria : [],
        signals: Array.isArray(evidenceGate.evidenceSignals) ? evidenceGate.evidenceSignals : [],
        reportRef: finalReportRef,
      },
      {
        key: "closeout",
        title: "Closeout",
        status: closeout.readyToClose ? "done" : closeout.blockers?.length ? "blocked" : readyTasks.length ? "active" : "pending",
        summary: closeout.summaryLine,
        detail: closeout.readyToClose
          ? "All blockers cleared; safe to archive when the operator chooses."
          : closeout.blockers?.[0] || "Clear the remaining blockers before archive.",
        refs: stageRefs,
      },
    ];
  }

  function inferProjectSignals(meta, flattened, byColumn) {
    const linkedCrons = getLinkedCrons(meta);
    const nextCron = linkedCrons[0] || state.crons[0] || null;
    const running = byColumn.running || [];
    const ready = byColumn.ready || [];
    const closeout = deriveCloseoutSnapshot(meta, flattened, byColumn);
    const matters = [];

    if (meta.status === "archived") {
      matters.push("Project is archived. Automation is asleep and the project is in reference mode.");
    } else if (meta.status === "paused") {
      matters.push("Project is paused. Linked automation is stopped and dispatch-ready work is parked.");
    }

    if (running.length) matters.push(`${running.length} task running now`);
    if (ready.length) matters.push(`${ready.length} task ready to dispatch`);
    if (!running.length && !ready.length && !flattened.length) matters.push("No active task on this board yet");
    if (!matters.length) matters.push("Board is quiet, but the project context is still attached");
    matters.push(closeout.summaryLine);

    return { linkedCrons, nextCron, matters, closeout };
  }

  function deriveSummary(options) {
    const meta = normalizeProjectMeta(options?.metaOverride || getProjectMeta(state.currentBoard || "default"));
    const columns = state.boardData?.columns || [];
    const flattened = getFlattenedTasks(state.boardData);
    const byColumn = Object.fromEntries(columns.map((column) => [column.name, column.tasks || []]));
    const blocked = (byColumn.blocked || []).slice(0, 3);
    const ready = (byColumn.ready || []).slice(0, 3);
    const todo = (byColumn.todo || []).slice(0, 3);
    const nextTasks = ready.length ? ready : todo;
    const { linkedCrons, nextCron, matters, closeout } = inferProjectSignals(meta, flattened, byColumn);

    const next = nextTasks.map((task) => ({
      label: task.title || task.id,
      meta: [task.assignee || "unassigned", task.priority || task.status || task._column || "todo"].filter(Boolean).join(" · "),
    }));

    const blockedItems = blocked.map((task) => ({
      label: task.title || task.id,
      meta: task.assignee || task.reason || "needs operator review",
    }));
    const workflowStages = buildWorkflowStages(meta, flattened, byColumn, closeout, blockedItems, next);

    return {
      matters,
      blockedItems,
      next,
      workflowStages,
      counts: {
        total: flattened.length,
        ready: (byColumn.ready || []).length,
        blocked: blocked.length,
      },
      meta,
      nextCron,
      linkedCrons,
      suggestedCrons: getSuggestedCrons(meta),
      closeout,
      source: {
        board: state.currentBoard || "default",
        panel: state.currentPanel || "Chat",
      },
    };
  }

  function getApprovalGateDetails() {
    const scope =
      document.querySelector('[role="alertdialog"]') ||
      Array.from(document.querySelectorAll("body *")).find((el) => {
        const text = (el.textContent || "").trim();
        return text.includes("Approval required");
      });
    if (!scope) return null;
    if (!isElementVisible(scope)) return null;
    const normalized = (scope.textContent || "").replace(/\s+/g, " ").trim();
    const title = normalized.includes("Approval required") ? "Approval required" : normalized.slice(0, 80);
    const buttons = Array.from(scope.querySelectorAll("button"))
      .map((button) => (button.textContent || "").replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .slice(0, 6);
    let bodyText = normalized;
    if (title && bodyText.startsWith(title)) {
      bodyText = bodyText.slice(title.length).trim();
    }
    buttons.forEach((label) => {
      if (!label) return;
      bodyText = bodyText.replace(label, "").trim();
    });
    bodyText = bodyText.replace(/\s+/g, " ").trim();
    return {
      title,
      text: bodyText,
      buttons,
      hasContext: Boolean(bodyText),
    };
  }

  function getClarificationGateDetails() {
    const scope =
      document.getElementById("clarifyCard") ||
      Array.from(document.querySelectorAll('[role="dialog"]')).find((el) => {
        const text = (el.textContent || "").trim();
        return text.includes("Clarification needed");
      });
    if (!scope) return null;
    if (!isElementVisible(scope)) return null;
    const titleNode = scope.querySelector("#clarifyHeading");
    const questionNode = scope.querySelector("#clarifyQuestion");
    const choicesNode = scope.querySelector("#clarifyChoices");
    const hintNode = scope.querySelector("#clarifyHint");
    const title = (titleNode?.textContent || "Clarification needed").replace(/\s+/g, " ").trim();
    const question = (questionNode?.textContent || "").replace(/\s+/g, " ").trim();
    const choices = Array.from(choicesNode?.querySelectorAll("button,[role='button']") || [])
      .map((entry) => (entry.textContent || "").replace(/\s+/g, " ").trim())
      .filter(Boolean);
    const hint = (hintNode?.textContent || "").replace(/\s+/g, " ").trim();
    return {
      title,
      question,
      choices,
      hint,
      hasContext: Boolean(question) || Boolean(choices.length),
    };
  }

  function isApprovalOverlayVisible() {
    return Boolean(getApprovalGateDetails());
  }

  function hasStalePendingPrompt(approvalGate, clarificationGate) {
    if (!approvalGate) return false;
    if (approvalGate.hasContext) return false;
    if (!clarificationGate) return false;
    return !clarificationGate.hasContext;
  }

  function isElementVisible(node) {
    if (!node || typeof node !== "object") return false;
    if (node.hidden) return false;
    const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
    if (style && (style.display === "none" || style.visibility === "hidden" || style.opacity === "0")) return false;
    const rects = typeof node.getClientRects === "function" ? node.getClientRects() : null;
    return !rects || rects.length > 0;
  }

  function getAutoIntakeAction(summary, meta) {
    const status = String(meta?.status || "active").trim() || "active";
    if (status !== "active") return "";
    if (summary.blockedItems.length || summary.counts.ready > 0 || summary.next.length > 0) return "";
    if (summary.counts.total !== 0) return "";
    const missingControlPlaneRefs = !meta?.projectRef && !meta?.planRef && !meta?.statusRef;
    const linkedProjectSessionId = String(state.projectSessionId || state.projectSession?.session_id || "").trim();
    if (missingControlPlaneRefs || !linkedProjectSessionId) {
      return "import";
    }
    return "";
  }

  function buildLoopPromptFromSummary(summary, meta) {
    const board = state.currentBoard || "default";
    const goal = (meta.goalSummary || "").trim();
    if (summary.blockedItems.length) {
      const blockedLabels = summary.blockedItems.map((item) => item.label).join(", ");
      return [
        `Project OS loop review for board ${board}.`,
        goal ? `Goal: ${goal}` : "",
        `Blocked tasks: ${blockedLabels}.`,
        "Review why these tasks are blocked, classify the blocker, and propose the single next safe action.",
        "If user action is required, say exactly what the operator must do next.",
      ]
        .filter(Boolean)
        .join("\n");
    }
    if (summary.counts.ready > 0) {
      const readyLabels = summary.next.map((item) => item.label).join(", ") || `${summary.counts.ready} ready tasks`;
      return [
        `Project OS loop continue for board ${board}.`,
        goal ? `Goal: ${goal}` : "",
        `Ready work: ${readyLabels}.`,
        "Pick the single best next task, explain why, and continue the project with the next safe action.",
        "If another blocker prevents progress, classify it clearly before asking for user action.",
      ]
        .filter(Boolean)
        .join("\n");
    }
    if (summary.counts.total === 0) {
      return [
        `Project OS bootstrap for board ${board}.`,
        goal ? `Goal: ${goal}` : "",
        "There are no tasks yet.",
        "Create the minimum viable task breakdown to start this project and identify the first safe action.",
      ]
        .filter(Boolean)
        .join("\n");
    }
    return [
      `Project OS loop review for board ${board}.`,
      goal ? `Goal: ${goal}` : "",
      "Review current board state and decide the single next action that keeps the project moving.",
      "If no automatic action is safe, explain the blocker and the exact next operator step.",
    ]
      .filter(Boolean)
      .join("\n");
  }

  function deriveLoopState(summary, meta) {
    const loopEnabled = Boolean(meta.autoLoopEnabled);
    const timeoutVisible = isHostTimeoutVisible();
    const approvalGate = getApprovalGateDetails();
    const clarificationGate = getClarificationGateDetails();
    const approvalVisible = Boolean(approvalGate);
    const stalePendingPrompt = hasStalePendingPrompt(approvalGate, clarificationGate);
    const hasSession = Boolean(state.session?.id || state.session?.title || getNativeComposer());
    const now = Date.now();
    const defaultResponse = {
      status: loopEnabled ? "observing" : "idle",
      category: "none",
      happened: loopEnabled ? "Loop is observing Hermes-native board, cron, and session state." : "Loop is off.",
      blocked: "",
      nextAction: loopEnabled
        ? "Project OS will classify the next runnable state and decide whether to send a Hermes action."
        : "Turn loop on when you want Project OS to keep a project moving automatically.",
      blockerHelp: "",
      operatorActionId: "",
      operatorActionLabel: "",
      controlPlaneAction: "",
      prompt: "",
      signature: "",
      canAutoRun: false,
      lastEvaluatedAt: now,
      lastAutoRunAt: state.loop.lastAutoRunAt || 0,
    };

    if (!loopEnabled) return defaultResponse;

    if (meta.status === "paused") {
      return {
        ...defaultResponse,
        status: "blocked_waiting_user",
        category: "project_state",
        happened: "Project loop stopped because the project is paused.",
        blocked: "Paused projects intentionally stop linked automation and park dispatch-ready work.",
        nextAction: "Resume the project when you want Project OS to continue automatic progress.",
        operatorActionId: "set-active",
        operatorActionLabel: "Set project active",
      };
    }

    if (meta.status === "archived") {
      return {
        ...defaultResponse,
        status: "blocked_waiting_user",
        category: "project_state",
        happened: "Project loop stopped because the project is archived.",
        blocked: "Archived projects are treated as reference mode, not active automation targets.",
        nextAction: "Switch the project back to Active before requesting automatic progress.",
        operatorActionId: "set-active",
        operatorActionLabel: "Set project active",
      };
    }

    if (!hasSession) {
      return {
        ...defaultResponse,
        status: "blocked_waiting_user",
        category: "host_session",
        happened: "Project OS has board data but no active Hermes session to deliver the next action into.",
        blocked: "Loop cannot continue until a visible Hermes chat session is available.",
        nextAction: "Open Chat once on this board so Project OS can route the next step into the native Hermes session.",
        blockerHelp: "This is a host-session blocker, not a project blocker. The safe recovery path is to expose a native Hermes chat composer for this board.",
        operatorActionId: "open-chat",
        operatorActionLabel: "Open Chat",
      };
    }

    const autoIntakeAction = getAutoIntakeAction(summary, meta);

    if (!state.session?.id && !state.session?.title && getNativeComposer() && !autoIntakeAction) {
      return {
        ...defaultResponse,
        status: "observing",
        category: "host_session",
        happened: "Project OS can see a native Hermes composer and is using it as a temporary session fallback.",
        blocked: "",
        nextAction: "If this board should keep running automatically, keep a representative chat session active so replies can be tracked more reliably.",
        blockerHelp: "Temporary session fallback works for now, but reply ownership is weaker until a concrete Hermes session is active for this board.",
      };
    }

    if (stalePendingPrompt) {
      return {
        ...defaultResponse,
        status: "blocked_waiting_user",
        category: "host_session",
        happened: "Hermes is showing approval or clarification cards, but the prompt body is empty.",
        blocked: "This visible session looks stuck on a stale pending prompt, so the operator cannot safely answer it here.",
        nextAction: "Start a fresh chat session for this board and continue from there. Treat the current session as stale instead of retrying the empty prompt cards.",
        blockerHelp: "The approval card has buttons but no command context, and the clarification card has no question or choices. That usually means the original pending prompt already expired or detached from this visible session.",
        operatorActionId: "open-fresh-chat",
        operatorActionLabel: "Start fresh chat",
      };
    }

    if (approvalVisible) {
      const buttons = approvalGate?.buttons?.length ? approvalGate.buttons.join(" / ") : "Approval controls visible";
      return {
        ...defaultResponse,
        status: "blocked_waiting_user",
        category: "hermes_workflow",
        happened: `Hermes reached a visible approval gate${approvalGate?.title ? `: ${approvalGate.title}` : ""}.`,
        blocked: "The host workflow is waiting for an approval decision, so Project OS should not continue automatically.",
        nextAction: `Use the visible Hermes approval controls (${buttons}) and then re-run the project loop from the updated board state.`,
        blockerHelp: approvalGate?.text
          ? `Visible approval context: ${approvalGate.text.slice(0, 240)}${approvalGate.text.length > 240 ? "…" : ""}`
          : "",
        operatorActionId: "open-chat",
        operatorActionLabel: "Open approval in Chat",
      };
    }

    if (state.submit.status === "sent" || state.submit.status === "waiting") {
      return {
        ...defaultResponse,
        status: "awaiting_result",
        category: "host_session",
        happened: "Project OS already sent the current loop action to Hermes.",
        blocked: "",
        nextAction: "Wait for a visible Hermes reply before sending another loop action.",
      };
    }

    if (state.submit.status === "timed_out") {
      return {
        ...defaultResponse,
        status: "blocked_waiting_system",
        category: timeoutVisible ? "host_session" : "hermes_workflow",
        happened: "The last loop-triggered action did not produce a visible host reply in time.",
        blocked: timeoutVisible
          ? "Hermes is showing a timeout banner, so the operator cannot trust that the command completed."
          : "The host did not show a visible completion signal for the last loop action.",
        nextAction: timeoutVisible
          ? "Open Chat and inspect the timeout banner first. Retry only after the host session is healthy again."
          : "Open Chat and confirm whether Hermes processed the last action before retrying.",
        blockerHelp: state.submit.command
          ? `Last loop command: ${state.submit.command.slice(0, 180)}${state.submit.command.length > 180 ? "…" : ""}`
          : "",
        operatorActionId: "open-chat",
        operatorActionLabel: "Inspect Chat",
      };
    }

    if (autoIntakeAction) {
      return {
        ...defaultResponse,
        status: "ready_to_dispatch",
        category: "project_intake",
        happened: "Project OS found a new or empty board that still needs dedicated import/resume intake.",
        blocked: "",
        nextAction: "Dispatch the shipped Recover current repo intake through the dedicated project session before generic loop planning.",
        controlPlaneAction: autoIntakeAction,
        prompt: autoIntakeAction === "create"
          ? "Project OS auto intake: start blank docs for the current board."
          : buildImportProjectPrompt(),
        signature: JSON.stringify({
          board: state.currentBoard || "default",
          status: meta.status,
          total: summary.counts.total,
          autoIntakeAction,
          goal: meta.goalSummary || "",
        }),
        canAutoRun: true,
      };
    }

    const prompt = buildLoopPromptFromSummary(summary, meta);
    const signature = JSON.stringify({
      board: state.currentBoard || "default",
      status: meta.status,
      total: summary.counts.total,
      ready: summary.counts.ready,
      blocked: summary.blockedItems.map((item) => item.label),
      next: summary.next.map((item) => item.label),
      goal: meta.goalSummary || "",
    });

    if (summary.blockedItems.length) {
      return {
        ...defaultResponse,
        status: "ready_to_review",
        category: "project_blocker",
        happened: "Project OS found blocked work that needs classification before progress can continue.",
        blocked: `${summary.blockedItems.length} blocked task${summary.blockedItems.length === 1 ? "" : "s"} need review.`,
        nextAction: "Ask Hermes to review blocked work and propose the single next safe action.",
        prompt,
        signature,
        canAutoRun: true,
      };
    }

    if (summary.counts.ready > 0 || summary.counts.total === 0 || summary.next.length > 0) {
      return {
        ...defaultResponse,
        status: "ready_to_dispatch",
        category: "project_progress",
        happened: summary.counts.ready > 0
          ? "Project OS found dispatch-ready work."
          : summary.counts.total === 0
            ? "Project OS found an empty board and can bootstrap the first task set."
            : "Project OS found a board that needs the next guided action.",
        blocked: "",
        nextAction: "Send one scoped Hermes action that keeps the project moving without inventing a second workflow engine.",
        prompt,
        signature,
        canAutoRun: true,
      };
    }

    return {
      ...defaultResponse,
      status: "observing",
      category: "project_progress",
      happened: "Project OS sees board activity but no obvious next automatic action yet.",
      blocked: "",
      nextAction: "Refresh summary or open Chat to inspect the latest Hermes result before sending more work.",
      prompt,
      signature,
      canAutoRun: false,
    };
  }

  function getSummaryForRender() {
    if (state.summary) return state.summary;
    return deriveSummary();
  }

  async function refreshData(options) {
    if (state.loading) return;
    const forceSummary = Boolean(options?.forceSummary);
    if (shouldUseLoginMode()) {
      state.currentPanel = "Login";
      state.loading = false;
      render();
      return;
    }
    state.loading = true;
    try {
      const boardsPayload = await api("/api/kanban/boards");
      state.boards = boardsPayload.boards || [];

      const persistedBoardSlug =
        state.lastBoardSlug && state.boards.some((board) => board.slug === state.lastBoardSlug)
          ? state.lastBoardSlug
          : null;
      const visibleBoardSlug = getVisibleKanbanBoardSlug();
      const trustedVisibleBoardSlug =
        visibleBoardSlug &&
        (visibleBoardSlug === boardsPayload.current || (!persistedBoardSlug && !state.projectSessionId))
          ? visibleBoardSlug
          : null;
      const resolvedBoard =
        trustedVisibleBoardSlug ||
        persistedBoardSlug ||
        boardsPayload.current || state.boards[0]?.slug || null;
      state.currentBoard = resolvedBoard;

      const [boardData, stats, cronsPayload, workspacesPayload, profilePayload, sessionsPayload] = await Promise.all([
        api(`/api/kanban/board${boardQuery(resolvedBoard)}`),
        api(`/api/kanban/stats${boardQuery(resolvedBoard)}`),
        api("/api/crons"),
        api("/api/workspaces"),
        api("/api/profile/active"),
        api("/api/sessions"),
      ]);

      state.boardData = boardData;
      state.boardStats = stats;
      state.crons = cronsPayload.jobs || [];
      state.workspaces = workspacesPayload.workspaces || [];
      state.activeProfile = profilePayload || null;
      state.sessionList = sessionsPayload.sessions || [];

      const currentMeta = getProjectMeta(state.currentBoard || "default");
      const sessionId = getCurrentSessionId();
      const representative = chooseRepresentativeSessionId(currentMeta, sessionId, state.sessionList, state.currentBoard);
      state.session = null;
      state.sessionMode = representative.mode;
      if (representative.sessionId) {
        try {
          const sessionPayload = await api(`/api/session?session_id=${encodeURIComponent(representative.sessionId)}`);
          state.session = sessionPayload.session || null;
          if (representative.sessionId !== currentMeta.representativeSessionId) {
            setProjectMeta(state.currentBoard || "default", { representativeSessionId: representative.sessionId }, { renderNow: false });
          }
        } catch (_error) {
          state.session = null;
        }
      }

      await refreshProjectSession({ renderNow: false });
      await adoptBestProjectSessionCandidate(resolvedBoard, state.projectSession);
      const linkedProjectBoardSlug = getBoardSlugFromProjectSession(state.projectSession);
      if (
        linkedProjectBoardSlug &&
        linkedProjectBoardSlug !== state.currentBoard &&
        state.boards.some((board) => board.slug === linkedProjectBoardSlug) &&
        !isEmptyIdleProjectSession(state.projectSession)
      ) {
        state.currentBoard = linkedProjectBoardSlug;
        await reconcileVisibleKanbanBoard(linkedProjectBoardSlug);
        const [linkedBoardData, linkedStats] = await Promise.all([
          api(`/api/kanban/board${boardQuery(linkedProjectBoardSlug)}`),
          api(`/api/kanban/stats${boardQuery(linkedProjectBoardSlug)}`),
        ]);
        state.boardData = linkedBoardData;
        state.boardStats = linkedStats;
      }
      restoreProjectSessionContinuity();
      state.selectedTask = deriveSelectedTask();

      const meta = getProjectMeta(state.currentBoard || "default");
      const derivedSummary = deriveSummary();
      if (meta.status === "active" || forceSummary || !meta.summarySnapshot) {
        state.summary = derivedSummary;
        setProjectMeta(state.currentBoard || "default", { summarySnapshot: buildSummarySnapshot(derivedSummary) }, { renderNow: false });
      } else {
        state.summary = buildSummarySnapshot(meta.summarySnapshot);
      }

      state.goalDraft = meta.goalSummary;
      state.lastBoardSlug = state.currentBoard;
      state.lastDataRefreshAt = Date.now();
      updateSubmitLifecycle();
      updateLoopLifecycle();
      savePersisted();
    } catch (error) {
      setToast("Project OS refresh failed", error.message || String(error));
    } finally {
      state.loading = false;
      render();
    }
  }

  async function switchBoard(boardSlug) {
    if (!boardSlug || boardSlug === state.currentBoard) return;
    try {
      await api(`/api/kanban/boards/${encodeURIComponent(boardSlug)}/switch`, {
        method: "POST",
        body: JSON.stringify({}),
      });
    } catch (_error) {
      // UI can still move even if the cross-process pointer update fails.
    }
    state.currentBoard = boardSlug;
    state.selectedTask = null;
    state.lastBoardSlug = boardSlug;
    savePersisted();
    await refreshData({ forceSummary: true });
    setToast("Project switched", `Now using board ${boardSlug}`);
  }

  function getNativeComposer() {
    const textarea =
      document.querySelector("textarea#msg") ||
      document.querySelector("textarea[placeholder='Message Hermes…']") ||
      document.querySelector("textarea");
    const sendButton = document.querySelector(".send-btn");
    return textarea ? { textarea, sendButton } : null;
  }

  function isElementFocusable(target) {
    if (!target) return false;
    const style = window.getComputedStyle(target);
    const visible = style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
    const enabled = !target.disabled && !target.hasAttribute("disabled");
    const rects = target.getClientRects?.();
    return visible && enabled && rects && rects.length > 0;
  }

  function focusNativeComposer() {
    const native = getNativeComposer();
    if (!native?.textarea) return false;
    const focusTarget = native.textarea;
    const placeCaretAtEnd = () => {
      focusTarget.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      focusTarget.click?.();
      focusTarget.focus();
      const length = focusTarget.value?.length || 0;
      focusTarget.setSelectionRange?.(length, length);
    };
    placeCaretAtEnd();
    window.requestAnimationFrame?.(() => placeCaretAtEnd());
    window.setTimeout(placeCaretAtEnd, 40);
    window.setTimeout(placeCaretAtEnd, 180);
    window.setTimeout(placeCaretAtEnd, 320);
    return true;
  }

  function insertTextAtCursor(input, text) {
    if (!input || typeof text !== "string" || !text) return false;
    const start = Number.isFinite(input.selectionStart) ? input.selectionStart : input.value.length;
    const end = Number.isFinite(input.selectionEnd) ? input.selectionEnd : input.value.length;
    if (typeof input.setRangeText === "function") {
      input.setRangeText(text, start, end, "end");
    } else {
      const before = input.value.slice(0, start);
      const after = input.value.slice(end);
      input.value = `${before}${text}${after}`;
      const next = start + text.length;
      input.setSelectionRange?.(next, next);
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  }

  function focusFloatingComposer(options) {
    const insertText = typeof options === "string" ? options : options?.insertText || "";
    let textInserted = false;
    const attempt = () => {
      const composer = dom.root?.querySelector("[data-field='composer-text']");
      const targetFound = Boolean(composer);
      const visible = isElementFocusable(composer);
      const enabled = Boolean(composer && !composer.disabled);
      if (!composer || !visible || !enabled) {
        state.focusDebug = {
          surface: state.currentPanel || "unknown",
          targetFound,
          visible,
          enabled,
          success: false,
          fallback: "floating-unavailable",
          at: new Date().toISOString(),
        };
        return false;
      }
      composer.focus();
      if (!composer.value) {
        composer.setSelectionRange?.(0, 0);
      } else {
        const length = composer.value.length;
        composer.setSelectionRange?.(length, length);
      }
      const success = document.activeElement === composer;
      if (success && insertText && !textInserted) {
        textInserted = insertTextAtCursor(composer, insertText) || textInserted;
      }
      markComposerInteraction();
      state.focusDebug = {
        surface: state.currentPanel || "unknown",
        targetFound,
        visible,
        enabled,
        success,
        fallback: success ? "" : "focus-failed",
        at: new Date().toISOString(),
      };
      return success;
    };

    state.composerExpanded = true;
    savePersisted();
    render();
    if (attempt()) return true;
    window.requestAnimationFrame?.(() => {
      if (!attempt()) {
        window.setTimeout(() => {
          if (!attempt()) {
            setToast("Floating shortcut unavailable", "Could not focus the floating composer on this surface. Use Project or Ctrl+K, or open Chat.");
          }
        }, 80);
      }
    });
    return false;
  }

  function buildContextualPrompt(userText) {
    const trimmed = userText.trim();
    if (trimmed.startsWith("/")) {
      return trimmed;
    }
    const meta = getProjectMeta(state.currentBoard || "default");
    const task = isReferenceModeStatus(meta.status) ? null : state.selectedTask;
    const selection = state.selection ? `Selection: ${state.selection}` : "";
    const goal = isReferenceModeStatus(meta.status) ? "" : meta.goalSummary ? `Goal: ${meta.goalSummary}` : "";
    const lines = [
      "[Project Context]",
      `Board: ${state.currentBoard || "default"}`,
      `Project state: ${meta.status}`,
      task ? `Task: ${task.id}${task.title ? ` — ${task.title}` : ""}` : isReferenceModeStatus(meta.status) ? "Task: archived project — manual context only" : "Task: none selected",
      state.activeProfile?.name ? `Profile: ${state.activeProfile.name}` : "",
      state.session?.workspace ? `Workspace: ${state.session.workspace}` : state.workspaces[0]?.path ? `Workspace: ${state.workspaces[0].path}` : "",
      goal,
      selection,
      "",
      "[Execution Contract]",
      "Treat this as a Project OS operating command inside Hermes WebUI, not a generic chat message.",
      "Use the Project Context above as the default scope of truth unless the user explicitly overrides it.",
      "Prefer taking the next concrete action over giving broad advice.",
      "If the request implies board/task/doc/session work, use the current board and current canonical repo context first.",
      "When the request is actionable, do the work or move it forward materially; do not answer with planning-only text.",
      "If you cannot act safely, explain the exact blocker and the next safe operator action in concrete terms.",
      "Keep Project OS thin: do not invent a shadow tracker, duplicate goal engine, or separate source of truth.",
      "",
      trimmed,
    ].filter(Boolean);
    return lines.join("\n");
  }

  function getControlPlaneActionGuard() {
    if (!state.currentBoard) {
      return {
        allowed: false,
        reason: "Select a board before dispatching a control-plane action.",
      };
    }
    return { allowed: true, reason: "" };
  }

  async function dispatchProjectControlPlanePrompt(kind) {
    await refreshProjectSession({ renderNow: false });
    const guard = getControlPlaneActionGuard();
    if (!guard.allowed) {
      setToast("Control-plane blocked", guard.reason);
      return null;
    }
    const intakeCopy = getControlPlaneIntakeCopy(getProjectMeta(state.currentBoard || "default"));
    const rawLabel =
      kind === "create"
        ? intakeCopy.createLabel
        : kind === "sync"
          ? intakeCopy.syncLabel
          : intakeCopy.importLabel;
    const promptText =
      kind === "create"
        ? `${intakeCopy.title}\n\nStart blank PROJECT/PLAN/STATUS docs for the current board using the dedicated project session only.`
        : kind === "sync"
          ? buildSyncControlPlanePrompt()
          : buildImportProjectPrompt();
    let startData;
    try {
      if (kind === "import" || kind === "sync") {
        const controlPlaneBoard = await prepareControlPlaneBoardContext({ allowLinkedSessionBoard: false });
        await syncWorkflowSeedTasks(controlPlaneBoard.boardSlug, controlPlaneBoard.boardData);
      }
      startData = await sendProjectSessionPrompt(promptText);
    } catch (error) {
      const detail = String(error?.message || error || "Dedicated project session dispatch failed.").trim();
      state.submit = normalizePersistedSubmit(
        {
          ...state.submit,
          status: "timed_out",
          sessionId: state.projectSessionId || "",
          detail,
        },
        state.projectSessionId || "",
      );
      setToast("Project session dispatch failed", detail);
      savePersisted();
      render();
      throw error;
    }
    beginSubmitState("control-plane", rawLabel, "project-session", startData?.stream_id || "");
    state.submit = normalizePersistedSubmit(
      {
        ...state.submit,
        status: "linked",
        sessionId: startData?.session_id || state.projectSessionId || "",
        streamId: startData?.stream_id || "",
        pendingStartedAt: startData?.pending_started_at || Date.now(),
        detail: `Dedicated project session accepted ${rawLabel}.`,
      },
      startData?.session_id || state.projectSessionId || "",
    );
    clearComposerAfterDispatch();
    if (kind === "import" || kind === "sync") {
      await openProjectSession();
    }
    savePersisted();
    render();
    return startData;
  }

  async function resumeLinkedProjectSession() {
    await refreshProjectSession({ renderNow: false });
    const linkedSessionId = getLinkedProjectSessionId();
    if (!linkedSessionId) {
      setToast("No linked project session", "Run Recover current repo first, then use Resume linked project.");
      return null;
    }
    return openProjectSession();
  }

  function fillNativeComposer(text) {
    const native = getNativeComposer();
    if (!native) return false;
    const { textarea } = native;
    const descriptor =
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement?.prototype || {}, "value") ||
      Object.getOwnPropertyDescriptor(window.HTMLInputElement?.prototype || {}, "value");
    textarea.focus();
    if (descriptor?.set) {
      descriptor.set.call(textarea, text);
    } else {
      textarea.value = text;
    }
    textarea.setSelectionRange?.(text.length, text.length);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function sendNativeComposer() {
    const native = getNativeComposer();
    if (!native?.sendButton) return false;
    native.sendButton.click();
    return true;
  }

  function fillAndSendNativeComposer(text) {
    const native = getNativeComposer();
    if (!native) return false;
    fillNativeComposer(text);
    sendNativeComposer();
    window.setTimeout(() => {
      const current = native.textarea?.value || "";
      if (current && current === text) {
        sendNativeComposer();
      }
    }, 220);
    return true;
  }

  function findPanelButton(panelName) {
    const target = normalizePanelLabel(panelName).toLowerCase();
    return getPrimaryNavButtons().find((button) => {
      const label = normalizePanelLabel(button.getAttribute("aria-label") || button.innerText || button.textContent || "").toLowerCase();
      return label === target || label.includes(target);
    });
  }

  function openPanel(panelName) {
    const button = findPanelButton(panelName);
    if (button) {
      button.click();
      return true;
    }
    return false;
  }

  function clickNewConversationButton() {
    const button = Array.from(document.querySelectorAll("button")).find((entry) => {
      const label = `${entry.getAttribute("aria-label") || ""} ${entry.getAttribute("title") || ""} ${entry.textContent || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
      return label.includes("new conversation");
    });
    if (!button) return false;
    button.click();
    return true;
  }

  function startFreshChatSession() {
    const opened = openPanel("Chat");
    const clicked = clickNewConversationButton();
    if (clicked) {
      setToast("Fresh chat started", "Use this clean Hermes chat for the current board, then run the project loop again.");
      return true;
    }
    if (opened) {
      setToast("Chat opened", "Start a new conversation from the Chat sidebar if the current session is stuck on empty approval cards.");
      return true;
    }
    setToast("Could not open fresh chat", "Open Chat manually and start a new conversation for this board.");
    return false;
  }

  function enqueuePendingSend(promptText) {
    sessionStorage.setItem(
      PENDING_KEY,
      JSON.stringify({
        text: promptText,
        createdAt: Date.now(),
      }),
    );
  }

  function routePromptToHermes(promptText, sendNow) {
    if (!promptText.trim()) return;
    if (sendNow ? fillAndSendNativeComposer(promptText) : fillNativeComposer(promptText)) {
      return { ok: true, mode: sendNow ? "native-send" : "native-fill" };
    }

    enqueuePendingSend(promptText);
    state.nativeSendPending = Boolean(sendNow);
    state.pendingNativePrompt = promptText;
    const opened = openPanel("Chat");
    if (opened) {
      setToast("Composer moved to Chat", "Message draft was carried into the Hermes chat composer without changing the current URL.");
      return { ok: true, mode: "chat-fallback" };
    }
    setToast("Command queued locally", "Could not open Chat automatically. Use View chat or the Chat tab to continue.");
    return { ok: false, mode: "chat-open-failed" };
  }

  function tryFlushPendingSend() {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (!raw) return;
    const native = getNativeComposer();
    if (!native) return;
    try {
      const payload = JSON.parse(raw);
      if (!payload.text) return;
      fillNativeComposer(payload.text);
      sessionStorage.removeItem(PENDING_KEY);
      state.pendingNativePrompt = "";
      if (state.nativeSendPending) {
        fillAndSendNativeComposer(payload.text);
        state.nativeSendPending = false;
      }
      setToast("Context attached", "Draft moved into the Hermes composer.");
    } catch (_error) {
      sessionStorage.removeItem(PENDING_KEY);
    }
  }

  function normalizeSelectionText(raw) {
    return raw
      .replace(/\s+/g, " ")
      .replace(/\s([?.!,;:])/g, "$1")
      .trim()
      .slice(0, 360);
  }

  function describeSelectionSource(node) {
    const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    if (!element) return "";
    const detail = element.closest?.("[data-kanban-task-id], article, section, main, aside, button, a, textarea, input, pre, code, p, li, h1, h2, h3, h4");
    if (!detail) return element.tagName?.toLowerCase() || "";
    if (detail.dataset?.kanbanTaskId) return `task ${detail.dataset.kanbanTaskId}`;
    const label = detail.getAttribute?.("aria-label") || detail.getAttribute?.("data-panel") || detail.getAttribute?.("placeholder");
    if (label) return label;
    const heading = detail.querySelector?.("h1, h2, h3, h4, strong");
    if (heading?.textContent?.trim()) return heading.textContent.trim().slice(0, 48);
    return detail.tagName?.toLowerCase() || "";
  }

  function captureSelection() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return false;
    if (dom.root && (dom.root.contains(selection.anchorNode) || dom.root.contains(selection.focusNode))) return false;
    const text = normalizeSelectionText(selection.toString());
    if (!text || text.length < 4) return false;
    state.selection = text;
    state.selectionSource = describeSelectionSource(selection.anchorNode) || describeSelectionSource(selection.focusNode) || "";
    savePersisted();
    render();
    return true;
  }

  function onSelectionChange() {
    captureSelection();
  }

  function onSelectionFinalize() {
    window.setTimeout(() => {
      captureSelection();
    }, 0);
  }

  function summarizeCurrentBoardIntoComposer() {
    const summary = getSummaryForRender();
    const lines = [
      `Board ${state.currentBoard || "default"} summary`,
      `- What matters now: ${summary.matters.join("; ")}`,
      `- Blocked: ${summary.blockedItems.length ? summary.blockedItems.map((item) => item.label).join(", ") : "none"}`,
      `- What next: ${summary.next.length ? summary.next.map((item) => item.label).join(", ") : "no immediate next task"}`,
    ];
    state.composerText = `${state.composerText ? `${state.composerText}\n\n` : ""}${lines.join("\n")}`;
    savePersisted();
    render();
  }

  async function refreshSummarySnapshot() {
    await refreshData({ forceSummary: true });
    setToast("Summary rebuilt", "Project interpretation refreshed from current Hermes data.");
  }

  function clearSelectedTask() {
    state.selectedTask = null;
    savePersisted();
    render();
  }

  function attachSelectionToComposer() {
    if (!state.selection) {
      setToast("No selection yet", "Select text anywhere in Hermes WebUI, then attach it to the floating composer.");
      return;
    }
    const prefix = state.composerText.trim() ? `${state.composerText.trim()}\n\n` : "";
    state.composerText = `${prefix}Please use this selected context:\n${state.selection}`;
    savePersisted();
    setToast("Selection attached", "Selected context was copied into the floating composer.");
    render();
  }

  function toggleCronLink(cronId) {
    const boardSlug = state.currentBoard || "default";
    const meta = getProjectMeta(boardSlug);
    const nextLinked = meta.linkedCronIds.includes(cronId)
      ? meta.linkedCronIds.filter((id) => id !== cronId)
      : [...meta.linkedCronIds, cronId];
    const nextAutoPaused = meta.autoPausedCronIds.filter((id) => nextLinked.includes(id));
    setProjectMeta(boardSlug, { linkedCronIds: nextLinked, autoPausedCronIds: nextAutoPaused });
    setToast(
      nextLinked.includes(cronId) ? "Cron linked" : "Cron unlinked",
      `${cronId} is now ${nextLinked.includes(cronId) ? "tracked by" : "removed from"} this project.`,
    );
  }

  async function setCronState(jobId, action) {
    const endpoint = action === "resume" ? "/api/crons/resume" : "/api/crons/pause";
    await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ job_id: jobId }),
    });
  }

  async function setTaskStatusBulk(ids, status) {
    if (!ids.length) return;
    await api(`/api/kanban/tasks/bulk${boardQuery(state.currentBoard || "default")}`, {
      method: "POST",
      body: JSON.stringify({ ids, status }),
    });
  }

  async function syncLinkedCronsForState(nextState, meta) {
    const linkedCrons = getLinkedCrons(meta);
    if (!linkedCrons.length) {
      return { changedIds: [], autoPausedCronIds: meta.autoPausedCronIds };
    }

    if (nextState === "active") {
      const resumeIds = meta.autoPausedCronIds.filter((id) => linkedCrons.some((job) => job.id === id));
      for (const id of resumeIds) {
        await setCronState(id, "resume");
      }
      return { changedIds: resumeIds, autoPausedCronIds: [] };
    }

    const pausable = linkedCrons.filter((job) => job.state !== "paused");
    const pauseIds = pausable.map((job) => job.id);
    for (const id of pauseIds) {
      await setCronState(id, "pause");
    }
    return {
      changedIds: pauseIds,
      autoPausedCronIds: [...new Set([...meta.autoPausedCronIds, ...pauseIds])],
    };
  }

  async function syncBoardQueueForState(nextState, meta) {
    const flattened = getFlattenedTasks(state.boardData);
    const readyTasks = flattened.filter((task) => (task.status || task._column || "").toLowerCase() === "ready");
    const runningTasks = flattened.filter((task) => (task.status || task._column || "").toLowerCase() === "running");

    if (nextState === "active") {
      const restorable = meta.autoParkedTaskIds.filter((id) =>
        flattened.some((task) => task.id === id && (task.status || task._column || "").toLowerCase() === "todo"),
      );
      if (restorable.length) {
        await setTaskStatusBulk(restorable, "ready");
      }
      return {
        parkedIds: [],
        changedIds: restorable,
        runningCount: runningTasks.length,
        reclaimedIds: [],
      };
    }

    const readyIds = readyTasks.map((task) => task.id);
    const runningIds = runningTasks.map((task) => task.id);
    const parkIds = [...new Set([...readyIds, ...runningIds])];
    if (parkIds.length) {
      await setTaskStatusBulk(parkIds, "todo");
    }
    return {
      parkedIds: [...new Set([...meta.autoParkedTaskIds, ...parkIds])],
      changedIds: parkIds,
      runningCount: runningTasks.length,
      reclaimedIds: runningIds,
    };
  }

  async function applyProjectState(nextState) {
    if (state.stateChanging) return;
    const boardSlug = state.currentBoard || "default";
    const previous = getProjectMeta(boardSlug);
    state.stateChanging = true;
    render();
    try {
      const cronSync = await syncLinkedCronsForState(nextState, previous);
      const queueSync = await syncBoardQueueForState(nextState, previous);
      setProjectMeta(
        boardSlug,
        {
          status: nextState,
          autoPausedCronIds: cronSync.autoPausedCronIds,
          autoParkedTaskIds: queueSync.parkedIds,
          summarySnapshot: null,
          lastStateSyncedAt: new Date().toISOString(),
        },
        { renderNow: false },
      );
      if (isReferenceModeStatus(nextState) && state.selectedTask?.board === boardSlug) {
        state.selectedTask = null;
      }
      await refreshData({ forceSummary: true });
      const actionWord = nextState === "active" ? "resumed" : nextState === "paused" ? "paused" : "archived";
      const cronText = cronSync.changedIds.length
        ? `${cronSync.changedIds.length} linked cron job${cronSync.changedIds.length === 1 ? "" : "s"} ${nextState === "active" ? "resumed" : "paused"} natively.`
        : previous.linkedCronIds.length
          ? "Linked cron jobs already matched the requested state."
          : "No linked cron jobs were linked yet.";
      const queueText = queueSync.changedIds.length
        ? `${queueSync.changedIds.length} ready task${queueSync.changedIds.length === 1 ? "" : "s"} ${nextState === "active" ? "restored to ready" : "parked back to todo"} on the board.`
        : nextState === "active"
          ? "No parked board tasks needed to be restored."
          : "No ready board tasks needed to be parked."
      const reclaimText = queueSync.reclaimedIds.length
        ? ` ${queueSync.reclaimedIds.length} running task${queueSync.reclaimedIds.length === 1 ? "" : "s"} were forced out of running by status change.`
        : "";
      const archiveText = nextState === "archived" ? " Project is now in reference mode." : "";
      setToast(`Project ${actionWord}`, `${cronText} ${queueText}${reclaimText}${archiveText}`.trim());
    } catch (error) {
      setToast("Project state change failed", error.message || String(error));
    } finally {
      state.stateChanging = false;
      render();
    }
  }

  async function pauseLinkedCrons() {
    await applyProjectState("paused");
  }

  async function resumeLinkedCrons() {
    await applyProjectState("active");
  }

  function toggleAutoLoop() {
    const boardSlug = state.currentBoard || "default";
    const meta = getCurrentProjectMeta();
    const nextValue = !meta.autoLoopEnabled;
    setProjectMeta(
      boardSlug,
      {
        autoLoopEnabled: nextValue,
        lastLoopSignature: nextValue ? meta.lastLoopSignature : "",
        lastLoopSentAt: nextValue ? meta.lastLoopSentAt : 0,
      },
      { renderNow: false },
    );
    updateLoopLifecycle();
    setToast(
      nextValue ? "Project loop on" : "Project loop off",
      nextValue
        ? "Project OS will now review board state and send one next safe Hermes action at a time."
        : "Automatic loop progression is paused. Hermes-native project state remains unchanged.",
    );
    render();
    if (nextValue) {
      void maybeRunAutoLoop(false);
      render();
    }
  }

  function runLoopNow() {
    updateLoopLifecycle();
    if (!state.loop.prompt && !state.loop.controlPlaneAction) {
      setToast("No loop action ready", state.loop.nextAction || "Project OS has no safe automatic step to send right now.");
      render();
      return;
    }
    void maybeRunAutoLoop(true);
    render();
  }

  function getCommands() {
    const intakeCopy = getControlPlaneIntakeCopy(getCurrentProjectMeta());
    const boardCommands = state.boards.map((board) => ({
      id: `board:${board.slug}`,
      label: `Switch project to ${board.name || board.slug}`,
      sub: board.description || board.slug,
      run: () => switchBoard(board.slug),
    }));

    return [
      {
        id: "summary",
        label: "Summarize current board into composer",
        sub: "Add a short operator summary before sending",
        run: summarizeCurrentBoardIntoComposer,
      },
      {
        id: "rebuild",
        label: "Rebuild project summary",
        sub: "Refresh interpretation from current Hermes data",
        run: refreshSummarySnapshot,
      },
      {
        id: "control-plane:create",
        label: intakeCopy.createLabel,
        sub: "Start a fresh PROJECT/PLAN/STATUS doc set for the current board in the dedicated project session.",
        run: () => {
          void dispatchProjectControlPlanePrompt("create");
        },
      },
      {
        id: "control-plane:import",
        label: intakeCopy.importLabel,
        sub: "Dispatch a fresh dedicated Recover current repo action for the current board.",
        run: () => {
          void dispatchProjectControlPlanePrompt("import");
        },
      },
      {
        id: "control-plane:resume",
        label: intakeCopy.resumeLabel,
        sub: intakeCopy.hasLinkedSession
          ? "Reopen the linked dedicated project session without dispatching a fresh import."
          : "Requires an already linked dedicated project session.",
        run: () => {
          void resumeLinkedProjectSession();
        },
      },
      {
        id: "control-plane:sync",
        label: intakeCopy.syncLabel,
        sub: "Refresh the existing PROJECT/PLAN/STATUS docs from live board and runtime truth.",
        run: () => {
          void dispatchProjectControlPlanePrompt("sync");
        },
      },
      {
        id: "kanban",
        label: "Open Kanban",
        sub: "Jump to the board view",
        run: () => openPanel("Kanban"),
      },
      {
        id: "tasks",
        label: "Open Tasks / cron",
        sub: "Jump to scheduled jobs and automations",
        run: () => openPanel("Tasks"),
      },
      {
        id: "spaces",
        label: "Open Spaces",
        sub: "Inspect or switch workspace context",
        run: () => openPanel("Spaces"),
      },
      {
        id: "profiles",
        label: "Open Profiles",
        sub: "Review or switch Hermes agent profiles",
        run: () => openPanel("Agent profiles"),
      },
      {
        id: "selection",
        label: "Attach current selection to composer",
        sub: state.selection ? state.selection.slice(0, 80) : "No text selected yet",
        run: attachSelectionToComposer,
      },
      {
        id: "loop-toggle",
        label: getCurrentProjectMeta().autoLoopEnabled ? "Turn project loop off" : "Turn project loop on",
        sub: "Project OS reviews blockers, chooses one next safe step, and reuses Hermes-native execution paths.",
        run: toggleAutoLoop,
      },
      {
        id: "loop-now",
        label: "Run project loop now",
        sub: state.loop.nextAction || "Evaluate board state and send the next safe Hermes action.",
        run: runLoopNow,
      },
      {
        id: "clear-task",
        label: "Clear selected task context",
        sub: state.selectedTask?.title || "No task currently attached",
        run: clearSelectedTask,
      },
      {
        id: "pause-linked",
        label: "Pause linked cron jobs",
        sub: "Uses Hermes native /api/crons/pause for linked cron ids",
        run: pauseLinkedCrons,
      },
      {
        id: "resume-linked",
        label: "Resume linked cron jobs",
        sub: "Uses Hermes native /api/crons/resume for linked cron ids",
        run: resumeLinkedCrons,
      },
      {
        id: "state:active",
        label: "Set project Active",
        sub: "Resume linked cron jobs when possible",
        run: () => applyProjectState("active"),
      },
      {
        id: "state:paused",
        label: "Set project Paused",
        sub: "Hard pause: stop linked cron jobs and park ready/running work",
        run: () => applyProjectState("paused"),
      },
      {
        id: "state:archived",
        label: "Set project Archived",
        sub: "Pause automation and switch the project into reference mode",
        run: () => applyProjectState("archived"),
      },
      ...boardCommands,
    ];
  }

  function filterCommands() {
    const query = state.paletteQuery.trim().toLowerCase();
    const commands = getCommands();
    if (!query) return commands;
    return commands.filter((command) => `${command.label} ${command.sub || ""}`.toLowerCase().includes(query));
  }

  function syncLayoutGeometry() {
    const geometry = measureLayoutGeometry();
    document.body.style.setProperty("--pux-main-left", `${geometry.mainLeft}px`);
    document.body.style.setProperty("--pux-main-right", `${geometry.mainRight}px`);
    document.body.style.setProperty("--pux-main-width", `${geometry.mainWidth}px`);
    document.body.style.setProperty("--pux-shell-right", `${geometry.shellRight}px`);
    document.body.style.setProperty("--pux-composer-gutter", `${geometry.gutter}px`);
    return geometry;
  }

  function refreshLayoutObserver() {
    state.layoutObserver?.disconnect?.();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(() => {
      syncLayoutGeometry();
    });
    const { rail, sidebar, main, rightpanel } = getLayoutElements();
    [rail, sidebar, main, rightpanel, document.body].filter(Boolean).forEach((node) => observer.observe(node));
    state.layoutObserver = observer;
  }

  function getLatestAssistantMessage() {
    const messages = Array.isArray(state.session?.messages) ? state.session.messages : [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const item = messages[index];
      if (item?.role !== "assistant") continue;
      const content = typeof item.content === "string"
        ? item.content
        : Array.isArray(item.codex_message_items)
          ? item.codex_message_items
              .flatMap((entry) => entry?.content || [])
              .filter((part) => part?.type === "output_text" && part?.text)
              .map((part) => part.text)
              .join("\n")
          : "";
      if (content && content.trim()) {
        return content.trim();
      }
    }
    return "";
  }

  function isHostTimeoutVisible() {
    return Array.from(document.querySelectorAll("body *")).some((el) =>
      (el.textContent || "").includes("Request timed out. Please try again."),
    );
  }

  function beginSubmitState(source, command, routeMode, streamId = "") {
    state.submit = {
      source,
      status: "sent",
      command,
      detail: "Extension sent command.",
      hostNote: "",
      sentAt: Date.now(),
      baselineReply: getLatestAssistantMessage(),
      resolvedReply: "",
      routeMode,
      sessionId: state.projectSession?.session_id || state.projectSessionId || "",
      streamId: streamId || "",
      pendingStartedAt: Date.now(),
    };
    savePersisted();
  }

  function updateSubmitLifecycle() {
    if (!state.submit?.status || state.submit.status === "idle" || !state.submit.sentAt) return false;
    const now = Date.now();
    const latestAssistant = getLatestAssistantMessage();
    const runningState = getProjectSessionRunningState(now, state.projectSession);
    const timeoutVisible = Boolean(state.projectSession?.active_stream_id) && isHostTimeoutVisible();
    const hasNewReply =
      latestAssistant &&
      latestAssistant.trim() &&
      latestAssistant.trim() !== (state.submit.baselineReply || "").trim();
    const refreshAgeMs = now - Number(state.projectSessionLastFetchedAt || 0);
    const needsFreshProjectSession = Boolean(
      state.projectSessionSyncing || refreshAgeMs >= 1200
    );
    if (needsFreshProjectSession && isControlPlaneSubmitActive()) {
      void refreshProjectSession({ renderNow: false });
      state.submit = {
        ...state.submit,
        status: "waiting",
        detail: "Refreshing dedicated project session state before deciding timeout.",
        hostNote: timeoutVisible ? "A timeout banner is visible on the host surface, but Project OS is using its own session." : "",
      };
      return true;
    }

    if (hasNewReply) {
      state.submit = {
        ...state.submit,
        status: "resolved",
        detail: "Host replied to the extension-triggered command.",
        hostNote: timeoutVisible ? "Host surface still shows a timeout banner from this session." : "",
        resolvedReply: latestAssistant.trim(),
      };
      return true;
    }

    if (state.submit.status === "sent") {
      state.submit = {
        ...state.submit,
        status: "waiting",
        detail:
          state.submit.routeMode === "chat-fallback"
            ? "Extension queued the command and is waiting for the Chat host path to dispatch it."
            : "Extension handed the command to the Hermes host and is waiting for a visible reply.",
        hostNote: timeoutVisible ? "Host surface currently shows a timeout banner." : "",
      };
      return true;
    }

    if (runningState.running) {
      if (runningState.suspiciouslyStalled) {
        state.submit = {
          ...state.submit,
          status: "stalled_running",
          detail: "Project session is running with no visible messages yet.",
          hostNote: timeoutVisible ? "A timeout banner is visible on the host surface, but Project OS is using its own session." : "",
          resolvedReply: "Active stream is still present, but the project session still reports message_count=0.",
        };
        return true;
      }
      state.submit = {
        ...state.submit,
        status: "waiting",
        detail: "Dedicated project session is still running.",
        hostNote: timeoutVisible ? "A timeout banner is visible on the host surface, but Project OS is using its own session." : "",
      };
      return true;
    }

    if (!runningState.running && now - state.submit.sentAt >= 1500) {
      if (timeoutVisible && now - state.submit.sentAt >= 3500) {
        state.submit = {
          ...state.submit,
          status: "timed_out",
          detail: "The project session may still be running — open session to inspect.",
          hostNote: timeoutVisible ? "A timeout banner is visible on the host surface, but Project OS is using its own session." : "",
        };
        return true;
      }
      if (now - state.submit.sentAt >= SUBMIT_TIMEOUT_MS) {
        state.submit = {
          ...state.submit,
          status: "timed_out",
          detail: "The project session may still be running — open session to inspect.",
          hostNote: timeoutVisible ? "A timeout banner is visible on the host surface, but Project OS is using its own session." : "",
        };
        return true;
      }
    }
    return false;
  }

  function updateLoopLifecycle() {
    const meta = getProjectMeta(state.currentBoard || "default");
    const summary = getSummaryForRender();
    const nextLoop = deriveLoopState(summary, meta);
    const previousSignature = state.loop.signature || "";
    const previousStatus = state.loop.status || "idle";
    state.loop = {
      ...state.loop,
      ...nextLoop,
      ledger: state.loop.ledger || [],
    };
    if (previousStatus !== state.loop.status || previousSignature !== state.loop.signature) {
      addLoopLedger({
        kind: "loop-state",
        title: state.loop.happened,
        detail: state.loop.nextAction,
      });
    }
  }

  async function maybeRunAutoLoop(force) {
    const meta = getProjectMeta(state.currentBoard || "default");
    if (!meta.autoLoopEnabled) return false;
    updateLoopLifecycle();
    if (!state.loop.canAutoRun || (!state.loop.prompt && !state.loop.controlPlaneAction) || !state.loop.signature) return false;
    const now = Date.now();
    const alreadySentSamePrompt =
      !force &&
      meta.lastLoopSignature === state.loop.signature &&
      now - Number(meta.lastLoopSentAt || 0) < LOOP_COOLDOWN_MS;
    if (alreadySentSamePrompt) {
      state.loop.nextAction = "Waiting for a meaningful board/session change before re-sending the same loop step.";
      return false;
    }

    if (state.loop.controlPlaneAction) {
      await dispatchProjectControlPlanePrompt(state.loop.controlPlaneAction);
      state.loop.status = "dispatching";
      state.loop.happened = "Project OS auto-dispatched the dedicated intake action to Hermes.";
      state.loop.blocked = "";
      state.loop.nextAction = "Wait for the dedicated project session to produce the import/resume result.";
      state.loop.lastAutoRunAt = now;
      addLoopLedger({
        kind: "loop-dispatch",
        title: "Loop dispatched",
        detail: `control-plane:${state.loop.controlPlaneAction}`,
      });
      setProjectMeta(
        state.currentBoard || "default",
        {
          lastLoopSignature: state.loop.signature,
          lastLoopSentAt: now,
        },
        { renderNow: false },
      );
      return true;
    }

    const route = routePromptToHermes(state.loop.prompt, true);
    beginSubmitState("loop-auto", state.loop.prompt, route?.mode || "");
    state.loop.status = "dispatching";
    state.loop.happened = "Project OS dispatched the next loop step to Hermes.";
    state.loop.blocked = "";
    state.loop.nextAction = "Wait for a visible Hermes reply or a classified blocked state.";
    state.loop.lastAutoRunAt = now;
    addLoopLedger({
      kind: "loop-dispatch",
      title: "Loop dispatched",
      detail: state.loop.prompt,
    });
    setProjectMeta(
      state.currentBoard || "default",
      {
        lastLoopSignature: state.loop.signature,
        lastLoopSentAt: now,
      },
      { renderNow: false },
    );
    return true;
  }

  function maybeRefreshForLoop() {
    const meta = getProjectMeta(state.currentBoard || "default");
    const now = Date.now();
    const submitActive = state.submit.status === "sent" || state.submit.status === "waiting";
    const loopActive = Boolean(meta.autoLoopEnabled);
    const stale = now - (state.lastDataRefreshAt || 0) >= 2500;
    if (!state.loading && stale && (submitActive || loopActive)) {
      refreshData();
      return true;
    }
    return false;
  }

  function clearComposerAfterDispatch() {
    state.composerText = "";
    state.selection = "";
    state.selectionSource = "";
    state.composerExpanded = true;
    savePersisted();
  }

  function autosizeComposerTextarea(textarea) {
    if (!textarea) return;
    textarea.style.height = "auto";
    const scrollHeight = Number(textarea.scrollHeight || 44);
    const nextHeight = Math.max(44, Math.min(scrollHeight, 200));
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = scrollHeight > nextHeight ? "auto" : "hidden";
  }

  function syncComposerSendButton() {
    const composer = dom.root?.querySelector("[data-field='composer-text']");
    const sendButton = dom.root?.querySelector("[data-action='send-hermes']");
    if (!sendButton) return;
    const canSend = Boolean((composer?.value || state.composerText || "").trim());
    sendButton.disabled = !canSend;
    sendButton.setAttribute("aria-disabled", canSend ? "false" : "true");
  }

  function getSubmitStatusMeta() {
    const submit = state.submit || {};
    const status = submit.status || "idle";
    if (status === "sent") return { label: "Sent", className: "is-sent" };
    if (status === "waiting") return { label: "Waiting", className: "is-waiting" };
    if (status === "resolved") return { label: "Host replied", className: "is-resolved" };
    if (status === "stalled_running") return { label: "Project may be stuck", className: "is-timeout" };
    if (status === "timed_out") return { label: "Host timed out", className: "is-timeout" };
    return { label: "Idle", className: "is-idle" };
  }

  function getLoopStatusMeta() {
    const status = state.loop.status || "idle";
    if (status === "dispatching") return { label: "Loop dispatching", className: "is-sent" };
    if (status === "awaiting_result") return { label: "Loop waiting", className: "is-waiting" };
    if (status === "ready_to_review") return { label: "Blocked review ready", className: "is-timeout" };
    if (status === "ready_to_dispatch") return { label: "Next action ready", className: "is-resolved" };
    if (status === "blocked_waiting_user") return { label: "User action needed", className: "is-timeout" };
    if (status === "blocked_waiting_system") return { label: "System blocked", className: "is-timeout" };
    if (status === "observing") return { label: "Loop observing", className: "is-idle" };
    return { label: "Loop off", className: "is-idle" };
  }

  function renderDrawer(meta) {
    if (!state.summaryOpen) return "";
    const board = state.boards.find((entry) => entry.slug === state.currentBoard);
    const intakeCopy = getControlPlaneIntakeCopy(meta);
    const summary = getSummaryForRender();
    const loopMeta = getLoopStatusMeta();
    const stateLabel = meta.status === "paused" ? "Paused" : meta.status === "archived" ? "Archived" : "Active";
    const nextCronText = summary.nextCron?.next_run_at
      ? `Next cron ${new Date(summary.nextCron.next_run_at).toLocaleString()}`
      : state.crons.length
        ? `${state.crons.length} cron job${state.crons.length === 1 ? "" : "s"} visible`
        : "No cron signal visible";
    const intakePrimaryActionClass = (action) =>
      intakeCopy.primaryAction === action ? "pux-inline-action pux-inline-action--primary" : "pux-inline-action";
    const intakeDisabledAttr = (enabled) => (enabled ? "" : 'disabled aria-disabled="true"');
    return `
      <section class="pux-drawer">
        <div class="pux-drawer-head">
          <div class="pux-project-stack">
            <div class="pux-overline">Project context</div>
            <div class="pux-title-row">
              <select class="pux-project-select" data-action="switch-board">
                ${state.boards
                  .map(
                    (entry) =>
                      `<option value="${esc(entry.slug)}" ${entry.slug === state.currentBoard ? "selected" : ""}>${esc(entry.name || entry.slug)}</option>`,
                  )
                  .join("")}
              </select>
              <span class="pux-chip"><strong>${esc(stateLabel)}</strong></span>
              <span class="pux-chip"><strong>${esc((board?.total ?? 0).toString())}</strong> task</span>
              <span class="pux-chip"><strong>${esc(summary.linkedCrons.length.toString())}</strong> linked cron</span>
            </div>
            <div class="pux-subtext">
              <span>Session: ${esc(state.session?.title || "No chat session selected")}</span>
              <span>Session source: ${esc(state.sessionMode || "none")}</span>
              <span>Workspace: ${esc(state.session?.workspace || state.workspaces[0]?.name || "Unknown")}</span>
              <span>Profile: ${esc(state.activeProfile?.name || "default")}</span>
              <span>${esc(nextCronText)}</span>
            </div>
          </div>
          <div class="pux-pill-row">
            <button class="pux-inline-action ${meta.autoLoopEnabled ? "pux-inline-action--primary" : ""}" data-action="toggle-loop">${meta.autoLoopEnabled ? "Loop on" : "Loop off"}</button>
            <button class="pux-inline-action" data-action="run-loop-now">Run loop</button>
            <button class="pux-state-btn ${meta.status === "active" ? "is-active" : ""}" data-action="set-state" data-state="active" ${state.stateChanging ? "disabled" : ""}>Active</button>
            <button class="pux-state-btn ${meta.status === "paused" ? "is-active" : ""}" data-action="set-state" data-state="paused" ${state.stateChanging ? "disabled" : ""}>Paused</button>
            <button class="pux-state-btn ${meta.status === "archived" ? "is-active" : ""}" data-action="set-state" data-state="archived" ${state.stateChanging ? "disabled" : ""}>Archived</button>
            <button class="pux-inline-action" data-action="toggle-summary">Close</button>
          </div>
        </div>
        <div class="pux-loop-banner">
          <span class="pux-chip pux-chip--status ${loopMeta.className}"><strong>${esc(loopMeta.label)}</strong></span>
          <div class="pux-loop-copy">
            <strong>${esc(state.loop.happened || "Loop is idle.")}</strong>
            <span>${esc(state.loop.blocked || state.loop.nextAction || "")}</span>
          </div>
        </div>
        ${(state.loop.blockerHelp || state.loop.operatorActionId)
          ? `<div class="pux-loop-operator">
              ${state.loop.blockerHelp ? `<p class="pux-host-note">${esc(state.loop.blockerHelp)}</p>` : ""}
              ${
                state.loop.operatorActionId
                  ? `<button class="pux-inline-action pux-inline-action--primary" data-action="${esc(state.loop.operatorActionId)}">${esc(
                      state.loop.operatorActionLabel || "Resolve blocker",
                    )}</button>`
                  : ""
              }
            </div>`
          : ""}
        <div class="pux-meta-row">
          <span class="pux-meta-chip"><strong>Current task</strong> ${state.selectedTask ? esc(state.selectedTask.title || state.selectedTask.id) : "none selected"}</span>
          <span class="pux-meta-chip"><strong>Board state</strong> ${esc(state.currentPanel || "Chat")}</span>
          <span class="pux-meta-chip"><strong>Selection</strong> ${state.selection ? `${esc(state.selection.slice(0, 46))}${state.selection.length > 46 ? "…" : ""}` : "none"}</span>
          <span class="pux-meta-chip"><strong>Source</strong> same-origin Hermes APIs + local project layer</span>
        </div>
        <div class="pux-goal-row">
          <input class="pux-goal-input" data-field="goal-summary" placeholder="Compact project goal summary" value="${esc(state.goalDraft || "")}" />
          <button class="pux-inline-action pux-inline-action--primary" data-action="save-goal">Save goal</button>
        </div>
        <div class="pux-intake-row">
          <div class="pux-empty"><strong>${esc(intakeCopy.eyebrow)}</strong> · ${esc(intakeCopy.title)} ${esc(intakeCopy.description)}</div>
          <div class="pux-card-actions">
            <button class="${intakePrimaryActionClass("create")}" data-action="start-blank-docs">${esc(intakeCopy.createLabel)}</button>
            <button class="${intakePrimaryActionClass("import")}" data-action="recover-current-repo">${esc(intakeCopy.importLabel)}</button>
            <button class="${intakePrimaryActionClass("resume")}" data-action="resume-linked-project" ${intakeDisabledAttr(intakeCopy.hasLinkedSession)}>${esc(intakeCopy.resumeLabel)}</button>
            <button class="${intakePrimaryActionClass("sync")}" data-action="refresh-project-docs">${esc(intakeCopy.syncLabel)}</button>
          </div>
        </div>
        ${renderSummary()}
      </section>
    `;
  }

  function renderLoginSurface() {
    return `
      <section class="pux-login-badge" aria-label="Project OS login status">
        <div class="pux-overline">Project OS</div>
        <strong>Extension loaded</strong>
        <p>Sign in to attach board, task, and floating controls.</p>
      </section>
    `;
  }

  function renderCronRows(summary) {
    const linked = summary.linkedCrons;
    const suggestions = summary.suggestedCrons;
    const linkedHtml = linked.length
      ? linked
          .map((job) => {
            const paused = job.state === "paused";
            return `
              <div class="pux-cron-row">
                <div class="pux-cron-main">
                  <strong>${esc(job.name || job.id)}</strong>
                  <div class="pux-empty">${esc(job.schedule_display || job.state || "scheduled")} · ${esc(job.workdir || "")}</div>
                </div>
                <div class="pux-chip-row">
                  <span class="pux-chip ${paused ? "hx-chip--paused" : ""}"><strong>${esc(paused ? "paused" : job.state || "scheduled")}</strong></span>
                  <button class="pux-inline-action" data-action="toggle-cron-link" data-cron-id="${esc(job.id)}">Unlink</button>
                </div>
              </div>
            `;
          })
          .join("")
      : `<div class="pux-empty">No linked cron jobs yet. Link the ones that should pause and resume with this project.</div>`;
    const suggestionHtml = suggestions.length
      ? `
        <div class="pux-card-subrow">
          ${suggestions
            .map(
              (job) => `
                <button class="pux-inline-action" data-action="toggle-cron-link" data-cron-id="${esc(job.id)}">
                  Link ${esc(job.name || job.id)}
                </button>`,
            )
            .join("")}
        </div>`
      : "";

    return `
      <div class="pux-cron-list">${linkedHtml}</div>
      ${suggestionHtml}
      <div class="pux-card-actions">
        <button class="pux-inline-action" data-action="pause-linked-crons">Pause linked</button>
        <button class="pux-inline-action" data-action="resume-linked-crons">Resume linked</button>
        <button class="pux-inline-action" data-action="open-tasks">Open Tasks</button>
      </div>
    `;
  }

  function compactLabel(value, max = 24) {
    const text = String(value || "").trim();
    if (text.length <= max) return text;
    return `${text.slice(0, Math.max(0, max - 1))}…`;
  }

  function iconSvg(name, size = 14) {
    try {
      return typeof window.li === "function" ? window.li(name, size) : "";
    } catch (_error) {
      return "";
    }
  }

  function nativeSendIcon(size = 16) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>`;
  }

  function getProjectSessionLabel() {
    return String(state.projectSession?.title || getProjectSessionBoardTitle(state.currentBoard)).trim();
  }

  function renderWorkflowEvidenceActions(evidenceGate, closeout) {
    const evidenceSignals = Array.isArray(evidenceGate?.evidenceSignals) ? evidenceGate.evidenceSignals : [];
    const criteria = Array.isArray(evidenceGate?.criteria) ? evidenceGate.criteria : [];
    const evidenceTone = getWorkflowStageTone(evidenceGate?.ready ? "done" : evidenceSignals.length ? "active" : "blocked");
    const criteriaHtml = criteria.length
      ? `<div class="pux-workflow-checklist">${criteria
          .map((entry) => `<div class="pux-bullet"><div>${esc(entry)}</div></div>`)
          .join("")}</div>`
      : "";
    const evidenceHtml = evidenceSignals.length
      ? `<div class="pux-workflow-signals">${evidenceSignals.map((entry) => `<span class="pux-meta-chip">${esc(entry)}</span>`).join("")}</div>`
      : `<div class="pux-empty">No evidence linked yet.</div>`;
    return `
      <div class="pux-workflow-evidence-head">
        <span class="pux-chip pux-chip--status ${evidenceTone.className}"><strong>${esc(evidenceGate?.label || evidenceTone.label)}</strong></span>
        <div class="pux-empty">${esc(evidenceGate?.summaryLine || "Evidence gate not evaluated yet.")}</div>
      </div>
      ${criteriaHtml}
      ${evidenceHtml}
      <div class="pux-card-actions">
        <button class="pux-inline-action" data-action="open-project-session">Open evidence thread</button>
        ${closeout?.finalReportRef ? `<button class="pux-inline-action" data-action="open-project-artifact">Open report</button>` : ""}
      </div>
      ${closeout?.finalReportRef ? `<div class="pux-empty">Report ref: ${esc(closeout.finalReportRef)}</div>` : ""}
    `;
  }

  function renderWorkflowCard(summary) {
    const meta = summary.meta || {};
    const refs = summary.refs || "";
    const actionGuardHtml = summary.actionGuardHtml || "";
    const actionGuardAttrs = summary.actionGuardAttrs || "";
    const blockedItems = summary.blockedItemsHtml || "";
    const nextItems = summary.nextItemsHtml || "";
    const stageSource = Array.isArray(summary.workflowStages) ? summary.workflowStages : [];
    const fallbackStages = [
      {
        key: "goal-fallback",
        title: "Goal setting",
        status: meta.goalSummary ? "done" : "blocked",
        summary: meta.goalSummary || "Goal summary still missing.",
        detail: meta.nextStepSummary || meta.blockerSummary || refs || "",
      },
      {
        key: "evidence-fallback",
        title: "Browser/runtime evidence gate",
        status: summary.evidenceGate?.ready ? "done" : "blocked",
        summary: summary.evidenceGate?.summaryLine || "Evidence gate not evaluated yet.",
        detail: summary.closeout?.summaryLine || "",
      },
    ];
    const workflowStages = stageSource.length ? stageSource : fallbackStages;
    const stagesHtml = workflowStages
      .map((stage, index) => {
        const tone = getWorkflowStageTone(stage.status);
        const refsHtml = Array.isArray(stage.refs) && stage.refs.length
          ? `<div class="pux-workflow-signals">${stage.refs.map((entry) => `<span class="pux-meta-chip">${esc(entry)}</span>`).join("")}</div>`
          : "";
        return `
          <div class="pux-workflow-stage pux-workflow-stage--${esc(stage.status || "pending")}" data-stage-key="${esc(stage.key || `stage-${index + 1}`)}">
            <div class="pux-workflow-stage-index">${index + 1}</div>
            <div class="pux-workflow-stage-copy">
              <div class="pux-workflow-stage-head">
                <strong>${esc(stage.title || `Stage ${index + 1}`)}</strong>
                <span class="pux-chip pux-chip--status ${tone.className}">${esc(tone.label)}</span>
              </div>
              <div class="pux-workflow-stage-summary">${esc(stage.summary || "")}</div>
              ${stage.detail ? `<div class="pux-empty">${esc(stage.detail)}</div>` : ""}
              ${stage.key === "evidence" ? `<div ${actionGuardAttrs}>${renderWorkflowEvidenceActions(summary.evidenceGate, summary.closeout)}</div>` : ""}
              ${refsHtml}
            </div>
          </div>
        `;
      })
      .join("");
    return `
      <article class="pux-card pux-card--workflow">
        <div class="pux-card-title">Workflow</div>
        <div class="pux-card-main">${summary.matters.map(esc).join("<br />")}</div>
        ${actionGuardHtml}
        <div class="pux-workflow-ladder">${stagesHtml}</div>
        <div class="pux-workflow-footnotes">
          ${refs ? `<div class="pux-empty">Refs: ${esc(refs)}</div>` : ""}
          <div class="pux-empty">Goal: ${esc(meta.goalSummary || "Not set")}</div>
          <div class="pux-empty">Next step: ${esc(meta.nextStepSummary || "No explicit next-step summary yet.")}</div>
          <div class="pux-empty">Top blocker: ${esc(meta.blockerSummary || summary.closeout?.blockers?.[0] || "None")}</div>
        </div>
        <div class="pux-workflow-columns">
          <div>
            <div class="pux-card-title">Blocked right now</div>
            <div class="pux-card-list">${blockedItems}</div>
          </div>
          <div>
            <div class="pux-card-title">Next up</div>
            <div class="pux-card-list">${nextItems}</div>
          </div>
        </div>
      </article>
    `;
  }

  function renderSummary() {
    const summary = getSummaryForRender();
    const meta = summary.meta || getProjectMeta(state.currentBoard || "default");
    const closeout = summary.closeout || deriveCloseoutSnapshot(meta, [], {});
    const currentReportRef = closeout.finalReportRef || getCurrentProjectReportRef();
    const renderedCloseout = currentReportRef && !closeout.finalReportRef ? { ...closeout, finalReportRef: currentReportRef } : closeout;
    const blockedItems = summary.blockedItems.length
      ? summary.blockedItems
          .map(
            (item) =>
              `<div class="pux-bullet is-blocked"><div><strong>${esc(item.label)}</strong><div class="pux-empty">${esc(item.meta)}</div></div></div>`,
          )
          .join("")
      : `<div class="pux-empty">No blocked task on this board right now.</div>`;
    const nextItems = summary.next.length
      ? summary.next
          .map(
            (item) =>
              `<div class="pux-bullet"><div><strong>${esc(item.label)}</strong><div class="pux-empty">${esc(item.meta)}</div></div></div>`,
          )
          .join("")
      : `<div class="pux-empty">No immediate task candidate. Create one or move an item to ready.</div>`;
    const actionGuardHtml = state.loop.blocked ? `<p class="pux-host-note">${esc(state.loop.blocked)}</p>` : "";
    const actionGuardAttrs = state.loop.blocked ? "" : "";
    const workflowSummary = {
      meta,
      matters: summary.matters,
      refs: summary.refs || renderedCloseout.finalReportRef,
      evidenceGate: renderedCloseout.evidenceGate,
      closeout: renderedCloseout,
      workflowStages: Array.isArray(summary.workflowStages) ? summary.workflowStages : [],
      actionGuardHtml,
      actionGuardAttrs,
      blockedItemsHtml: blockedItems,
      nextItemsHtml: nextItems,
    };
    return `
      <section class="pux-summary">
        ${renderWorkflowCard(workflowSummary)}
        ${renderProjectThreadDetail()}
        <article class="pux-card">
          <div class="pux-card-title">Linked automation</div>
          <div class="pux-card-main pux-card-main--dense">${renderCronRows(summary)}</div>
          <div class="pux-card-actions">
            <button class="pux-inline-action" data-action="close-project" ${renderedCloseout.archiveProject && !state.stateChanging ? "" : "disabled aria-disabled=\"true\""}>${esc(renderedCloseout.actionLabel)}</button>
            ${renderedCloseout.finalReportRef ? `<button class="pux-inline-action" data-action="open-project-artifact">${esc(renderedCloseout.finalReportRef)}</button>` : ""}
          </div>
        </article>
      </section>
    `;
  }

  function renderComposerActivity() {
    const linkedSessionId = getLinkedProjectSessionId();
    const sessionLabel = linkedSessionId ? compactLabel(getProjectSessionLabel(), 20) : "No session";
    return `
      <aside class="pux-composer-activity">
        <div class="pux-submit-row">
          <button class="pux-chip pux-chip--peek pux-chip--action" data-action="open-project-session" title="Open project session">
            ${iconSvg("message-square", 12)}
            <span>${esc(sessionLabel)}</span>
          </button>
        </div>
      </aside>
    `;
  }

  function normalizeProjectSessionText(value, limit = 160) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
  }

  function getProjectSessionExecutionFeed(session = state.projectSession) {
    const sourceSession = session || {};
    const toolCalls = Array.isArray(sourceSession.tool_calls) ? sourceSession.tool_calls : [];
    const messages = Array.isArray(sourceSession.messages) ? sourceSession.messages : [];
    const items = [];
    for (const tc of toolCalls) {
      const rawName = String(tc?.name || "tool").replace(/^functions\./, "");
      const preview = normalizeProjectSessionText(tc?.preview || tc?.snippet || tc?.result || tc?.output || "");
      items.push({
        kind: tc?.is_error ? "blocked" : "pending",
        label: `Tool · ${rawName}`,
        detail: preview || "Tool call recorded for the linked dedicated session.",
        ts: Number(tc?._ts || tc?.timestamp || 0),
      });
    }
    for (const msg of messages) {
      if (!msg || (msg.role !== "assistant" && msg.role !== "user" && msg.role !== "system")) continue;
      const preview = normalizeProjectSessionText(msg.content || msg.text || msg.message || "");
      if (!preview) continue;
      const roleLabel = msg.role === "assistant" ? "Assistant" : msg.role === "user" ? "User" : "System";
      items.push({
        kind: msg.role === "assistant" ? "done" : "pending",
        label: `${roleLabel} message`,
        detail: preview,
        ts: Number(msg._ts || msg.timestamp || 0),
      });
    }
    return items
      .sort((a, b) => (Number(b?.ts || 0) - Number(a?.ts || 0)))
      .slice(0, 5);
  }

  function getProjectSessionArtifactList(session = state.projectSession) {
    const sourceSession = session || {};
    const toolCalls = Array.isArray(sourceSession.tool_calls) ? sourceSession.tool_calls : [];
    const messages = Array.isArray(sourceSession.messages) ? sourceSession.messages : [];
    const items = [];
    const seen = new Set();
    const push = (path, source = "session") => {
      const normalized = String(path || "").trim().replace(/^~\//, "").replace(/^\.\//, "");
      if (!normalized || seen.has(normalized)) return;
      seen.add(normalized);
      items.push({ path: normalized, source });
    };
    for (const tc of toolCalls) {
      const toolName = String(tc?.name || "tool").replace(/^functions\./, "") || "tool";
      const args = tc?.arguments || tc?.args || tc?.input || {};
      if (args && typeof args === "object") {
        ["path", "file_path", "source", "destination"].forEach((key) => push(args[key], toolName));
        if (Array.isArray(args.paths)) args.paths.forEach((entry) => push(entry, toolName));
        if (Array.isArray(args.edits)) args.edits.forEach((entry) => push(entry?.path, toolName));
      }
      const diffText = typeof tc?.result === "string" ? tc.result : typeof tc?.output === "string" ? tc.output : "";
      const diffMatch = diffText.match(/(?:^|\n)(?:\+\+\+|---)\s+(?:[ab]\/)?([^\n\t]+)/);
      if (diffMatch) push(diffMatch[1], toolName);
    }
    for (const msg of messages) {
      const text = String(msg?.content || msg?.text || msg?.message || "");
      const diffMatch = text.match(/(?:^|\n)(?:\+\+\+|---)\s+(?:[ab]\/)?([^\n\t]+)/);
      if (diffMatch) push(diffMatch[1], "diff");
    }
    return items.slice(0, 3);
  }

  function renderProjectThreadDetail() {
    const linkedSessionId = getLinkedProjectSessionId();
    if (!linkedSessionId) return "No linked dedicated project session yet.";
    const session = state.projectSession || {};
    const runningState = getProjectSessionRunningState(Date.now(), session);
    const statusTone = runningState.running ? getWorkflowStageTone("active") : getWorkflowStageTone(getProjectSessionMessageCount(session) ? "done" : "pending");
    const feedItems = getProjectSessionExecutionFeed(session);
    const artifactItems = getProjectSessionArtifactList(session);
    const feedHtml = feedItems.length
      ? feedItems
          .map((item) => {
            const tone = getWorkflowStageTone(item.kind || "pending");
            return `<div class="pux-bullet"><div><strong>${esc(item.label || "Recent activity")}</strong><div class="pux-empty">${esc(item.detail || "Linked dedicated project session activity recorded.")}</div></div><span class="pux-chip pux-chip--status ${tone.className}">${esc(tone.label)}</span></div>`;
          })
          .join("")
      : `<div class="pux-empty">No recent execution items captured on the linked dedicated project session yet.</div>`;
    const artifactHtml = artifactItems.length
      ? `<div class="pux-card-list">${artifactItems
          .map(
            (item) => `<div class="pux-bullet"><div><strong>${esc(item.path)}</strong><div class="pux-empty">${esc(item.source || "session artifact")}</div></div><button class="pux-inline-action" data-action="open-project-artifact-path" data-artifact-path="${esc(item.path)}">Open</button></div>`,
          )
          .join("")}</div>`
      : `<div class="pux-empty">Artifact mini-list is still empty for this linked session. Use Open session for the full thread when deeper drill-down is needed.</div>`;
    return `
      <article class="pux-card">
        <div class="pux-card-title">Execution feed</div>
        <div class="pux-card-main pux-card-main--dense">Linked dedicated project session is available. Open session to inspect the settled proof thread.</div>
        <div class="pux-card-list">
          <div class="pux-bullet"><div><strong>${esc(getProjectSessionLabel())}</strong><div class="pux-empty">${esc(getProjectSessionMessageCount(session))} msgs · ${runningState.running ? "transport active" : "idle"}</div></div><span class="pux-chip pux-chip--status ${statusTone.className}">${esc(statusTone.label)}</span></div>
        </div>
        <div class="pux-card-title">Recent execution items</div>
        <div class="pux-card-list">${feedHtml}</div>
        <div class="pux-card-title">Recent artifacts</div>
        ${artifactHtml}
        <div class="pux-card-actions">
          <button class="pux-inline-action" data-action="open-project-session">Open session</button>
        </div>
      </article>
    `;
  }

  function isHostTimeoutVisible() {
    return Array.from(document.querySelectorAll("body *")).some((el) =>
      (el.textContent || "").includes("Request timed out. Please try again."),
    );
  }

  function renderComposer(meta) {
    if (isChatPanel()) return "";
    const archived = isReferenceModeStatus(meta.status);
    const submit = state.submit || {};
    const hasDraft = Boolean((state.composerText || "").trim());
    const hasSubmitActivity = ["sending", "waiting", "resolved"].includes(submit.status || "");
    const expanded = Boolean(state.composerExpanded || hasDraft || hasSubmitActivity);
    const canSend = Boolean((state.composerText || "").trim());
    const taskChip = state.selectedTask && !archived
      ? `<button class="pux-chip pux-task-pill pux-context-pill" data-action="clear-task" title="${esc(state.selectedTask.title || state.selectedTask.id)}">${iconSvg("list-todo", 12)}<span>${esc(compactLabel(state.selectedTask.title || state.selectedTask.id, 20))}</span></button>`
      : "";
    const selectionChip = state.selection
      ? `<button class="pux-chip pux-selection-pill pux-context-pill" data-action="clear-selection" title="${esc(state.selection)}">${iconSvg("file-text", 12)}<span>${esc(compactLabel(state.selection, 22))}</span></button>`
      : `<button class="pux-chip pux-chip--subtle" data-action="attach-selection">${iconSvg("file-text", 12)}<span>Selection</span></button>`;
    const boardChip = `<span class="pux-chip pux-context-pill pux-board-pill" title="${esc(state.currentBoard || "default")}">${iconSvg("folder", 12)}<span>${esc(compactLabel(state.currentBoard || "default", 16))}</span></span>`;
    return `
      <section class="pux-composer ${expanded ? "is-expanded" : "is-collapsed"} is-docked">
        <div class="pux-composer-inputrow">
          <textarea class="pux-composer-textarea" data-field="composer-text" rows="1" spellcheck="false" autocapitalize="off" placeholder="Message Hermes…">${esc(state.composerText || "")}</textarea>
        </div>
        <div class="pux-composer-lower">
          <div class="pux-composer-context-rail" aria-label="Project context controls">
            <div class="pux-chip-row pux-chip-row--composer pux-chip-row--context-control">
              <button class="pux-inline-action pux-inline-action--ghost pux-project-trigger" data-action="toggle-summary">${iconSvg("layers", 12)}<span>${state.summaryOpen ? "Hide project" : "Project"}</span></button>
            </div>
          </div>
          <div class="pux-composer-actions pux-composer-actions--context-end">
            <div class="pux-composer-meta pux-composer-meta--context-end">
              <div class="pux-chip-row pux-chip-row--composer">
                ${boardChip}
                ${taskChip}
                ${selectionChip}
              </div>
            </div>
            ${renderComposerActivity()}
            <button class="pux-send-fab" data-action="send-hermes" aria-label="Send to Hermes" title="Send to Hermes" ${canSend ? "" : "disabled aria-disabled=\"true\""}>${nativeSendIcon()}</button>
          </div>
        </div>
      </section>
    `;
  }

  function renderPalette() {
    if (!state.paletteOpen) return "";
    state.filteredCommands = filterCommands();
    const groups = [
      {
        title: "Actions",
        items: state.filteredCommands.filter((item) => !item.id.startsWith("board:")),
      },
      {
        title: "Projects",
        items: state.filteredCommands.filter((item) => item.id.startsWith("board:")),
      },
    ];

    return `
      <section class="pux-palette" data-action="close-palette-overlay">
        <div class="pux-palette-card" role="dialog" aria-modal="true" aria-label="Project OS command palette">
          <input class="pux-palette-input" data-field="palette-query" placeholder="Type a command or board name…" value="${esc(state.paletteQuery || "")}" />
          <div class="pux-palette-list">
            ${groups
              .map((group) =>
                group.items.length
                  ? `<div class="pux-palette-group">
                      <div class="pux-palette-group-title">${esc(group.title)}</div>
                      ${group.items
                        .map(
                          (item) => `
                            <button class="pux-palette-item" data-action="run-command" data-command="${esc(item.id)}">
                              <span class="pux-palette-main">
                                <span class="pux-palette-label">${esc(item.label)}</span>
                                <span class="pux-palette-sub">${esc(item.sub || "")}</span>
                              </span>
                              <span class="pux-chip">${esc(item.id.startsWith("board:") ? "switch" : "run")}</span>
                            </button>`,
                        )
                        .join("")}
                    </div>`
                  : "",
              )
              .join("")}
          </div>
        </div>
      </section>
    `;
  }

  function renderToast() {
    if (!state.toast) return "";
    return `
      <aside class="pux-toast">
        <strong>${esc(state.toast.title)}</strong>
        <p>${esc(state.toast.body)}</p>
      </aside>
    `;
  }

  function render() {
    if (!dom.root) return;
    syncLayoutGeometry();
    document.body.classList.add("project-os-extension-active");
    if (shouldUseLoginMode()) {
      dom.root.innerHTML = renderLoginSurface();
      bindEvents();
      return;
    }
    const meta = getCurrentProjectMeta();
    updateLoopLifecycle();
    dom.root.innerHTML = `
      <div class="pux-shell">${renderDrawer(meta)}</div>
      ${renderComposer(meta)}
      ${renderPalette()}
      ${renderToast()}
    `;
    bindEvents();
    autosizeComposerTextarea(dom.root.querySelector("[data-field='composer-text']"));
    syncComposerSendButton();
    tryFlushPendingSend();
  }

  function bindEvents() {
    if (shouldUseLoginMode()) {
      return;
    }
    dom.root.querySelectorAll("[data-action='set-state']").forEach((button) => {
      button.addEventListener("click", () => {
        applyProjectState(button.dataset.state);
      });
    });

    const boardSelect = dom.root.querySelector("[data-action='switch-board']");
    boardSelect?.addEventListener("change", (event) => {
      switchBoard(event.target.value);
    });

    const goalInput = dom.root.querySelector("[data-field='goal-summary']");
    goalInput?.addEventListener("input", (event) => {
      state.goalDraft = event.target.value;
    });

    dom.root.querySelector("[data-action='save-goal']")?.addEventListener("click", () => {
      setProjectMeta(state.currentBoard || "default", { goalSummary: state.goalDraft.trim() });
      setToast("Goal summary saved", "Project continuity note updated.");
      updateLoopLifecycle();
      render();
    });

    dom.root.querySelectorAll("[data-action='recover-current-repo']").forEach((button) => {
      button.addEventListener("click", () => {
        void dispatchProjectControlPlanePrompt("import");
      });
    });

    dom.root.querySelectorAll("[data-action='resume-linked-project']").forEach((button) => {
      button.addEventListener("click", () => {
        void resumeLinkedProjectSession();
      });
    });

    dom.root.querySelectorAll("[data-action='start-blank-docs']").forEach((button) => {
      button.addEventListener("click", () => {
        void dispatchProjectControlPlanePrompt("create");
      });
    });

    dom.root.querySelectorAll("[data-action='refresh-project-docs']").forEach((button) => {
      button.addEventListener("click", () => {
        void dispatchProjectControlPlanePrompt("sync");
      });
    });

    dom.root.querySelectorAll("[data-action='open-project-session']").forEach((button) => {
      button.addEventListener("click", () => {
        void openProjectSession();
      });
    });

    dom.root.querySelectorAll("[data-action='open-project-artifact']").forEach((button) => {
      button.addEventListener("click", () => {
        const artifactPath = getCurrentProjectReportRef();
        if (!artifactPath || typeof window.openArtifactPath !== "function") return;
        void window.openArtifactPath(artifactPath);
      });
    });

    dom.root.querySelectorAll("[data-action='open-project-artifact-path']").forEach((button) => {
      button.addEventListener("click", () => {
        const artifactPath = String(button.dataset.artifactPath || "").trim();
        if (!artifactPath || typeof window.openArtifactPath !== "function") return;
        void window.openArtifactPath(artifactPath);
      });
    });

    const composer = dom.root.querySelector("[data-field='composer-text']");
    composer?.addEventListener("input", (event) => {
      state.composerText = event.target.value;
      state.composerExpanded = true;
      autosizeComposerTextarea(event.target);
      syncComposerSendButton();
      markComposerInteraction();
      savePersisted();
    });
    composer?.addEventListener("focus", () => {
      markComposerInteraction();
      autosizeComposerTextarea(composer);
      if (!state.composerExpanded) {
        state.composerExpanded = true;
        savePersisted();
      }
    });
    composer?.addEventListener("beforeinput", (event) => {
      const text = typeof event.data === "string" ? event.data : "";
      if ((text === "/" || text === "?") && event.inputType === "insertText") {
        event.preventDefault();
        insertTextAtCursor(event.target, text);
        autosizeComposerTextarea(event.target);
        syncComposerSendButton();
        markComposerInteraction(1800);
        return;
      }
      markComposerInteraction(1800);
    });
    composer?.addEventListener("paste", () => {
      markComposerInteraction(2200);
    });
    composer?.addEventListener("compositionstart", () => {
      markComposerInteraction(2500);
    });
    composer?.addEventListener("compositionend", () => {
      autosizeComposerTextarea(composer);
      syncComposerSendButton();
      markComposerInteraction(1800);
    });
    composer?.addEventListener("blur", () => {
      window.setTimeout(() => {
        const stillInside = dom.root?.contains(document.activeElement);
        if (!stillInside && !state.composerText && !state.selection) {
          state.composerExpanded = false;
          savePersisted();
          render();
        }
      }, 120);
    });
    composer?.addEventListener("keydown", (event) => {
      if (!event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey && event.key === "Enter") {
        event.preventDefault();
        dom.root.querySelector("[data-action='send-hermes']")?.click();
      }
      if (event.key === "Escape" && !state.composerText) {
        event.preventDefault();
        state.composerExpanded = false;
        savePersisted();
        render();
      }
    });

    dom.root.querySelector("[data-action='send-hermes']")?.addEventListener("click", () => {
      const rawText = (state.composerText || "").trim();
      if (!rawText) {
        setToast("No command yet", "Type a command or note into the floating composer first.");
        return;
      }
      const contextualPrompt = buildContextualPrompt(rawText);
      const route = routePromptToHermes(contextualPrompt, true);
      beginSubmitState("floating", rawText, route?.mode || "");
      clearComposerAfterDispatch();
      updateSubmitLifecycle();
      render();
    });

    dom.root.querySelectorAll("[data-action='attach-selection']").forEach((button) => {
      button.addEventListener("click", () => {
        attachSelectionToComposer();
      });
    });

    dom.root.querySelector("[data-action='clear-selection']")?.addEventListener("click", () => {
      state.selection = "";
      state.selectionSource = "";
      savePersisted();
      render();
    });

    dom.root.querySelector("[data-action='clear-task']")?.addEventListener("click", () => {
      clearSelectedTask();
    });

    dom.root.querySelector("[data-action='open-tasks']")?.addEventListener("click", () => {
      openPanel("Tasks");
    });

    dom.root.querySelector("[data-action='summarize-board']")?.addEventListener("click", () => {
      summarizeCurrentBoardIntoComposer();
      render();
    });

    dom.root.querySelector("[data-action='refresh-summary']")?.addEventListener("click", () => {
      refreshSummarySnapshot();
    });

    dom.root.querySelector("[data-action='open-chat-preview']")?.addEventListener("click", () => {
      openPanel("Chat");
    });

    dom.root.querySelectorAll("[data-action='open-chat']").forEach((button) => {
      button.addEventListener("click", () => {
        openPanel("Chat");
        setToast("Chat opened", "Review the visible Hermes session or approval card, then run the project loop again.");
      });
    });

    dom.root.querySelectorAll("[data-action='open-fresh-chat']").forEach((button) => {
      button.addEventListener("click", () => {
        startFreshChatSession();
      });
    });

    dom.root.querySelectorAll("[data-action='set-active']").forEach((button) => {
      button.addEventListener("click", () => {
        applyProjectState("active");
      });
    });

    dom.root.querySelectorAll("[data-action='start-blank-docs']").forEach((button) => {
      button.addEventListener("click", () => {
        void dispatchProjectControlPlanePrompt("create");
      });
    });

    dom.root.querySelectorAll("[data-action='recover-current-repo']").forEach((button) => {
      button.addEventListener("click", () => {
        void dispatchProjectControlPlanePrompt("import");
      });
    });

    dom.root.querySelectorAll("[data-action='resume-linked-project']").forEach((button) => {
      button.addEventListener("click", () => {
        void resumeLinkedProjectSession();
      });
    });

    dom.root.querySelectorAll("[data-action='refresh-project-docs']").forEach((button) => {
      button.addEventListener("click", () => {
        void dispatchProjectControlPlanePrompt("sync");
      });
    });

    dom.root.querySelectorAll("[data-action='toggle-loop']").forEach((button) => {
      button.addEventListener("click", () => {
        toggleAutoLoop();
      });
    });

    dom.root.querySelectorAll("[data-action='run-loop-now']").forEach((button) => {
      button.addEventListener("click", () => {
        runLoopNow();
      });
    });

    dom.root.querySelectorAll("[data-action='toggle-summary']").forEach((button) => {
      button.addEventListener("click", () => {
        state.summaryOpen = !state.summaryOpen;
        render();
      });
    });

    dom.root.querySelector("[data-action='pause-linked-crons']")?.addEventListener("click", () => {
      pauseLinkedCrons();
    });

    dom.root.querySelector("[data-action='resume-linked-crons']")?.addEventListener("click", () => {
      resumeLinkedCrons();
    });

    dom.root.querySelectorAll("[data-action='toggle-cron-link']").forEach((button) => {
      button.addEventListener("click", () => {
        toggleCronLink(button.dataset.cronId);
      });
    });

    dom.root.querySelectorAll("[data-action='toggle-palette']").forEach((button) => {
      button.addEventListener("click", () => {
        state.paletteOpen = !state.paletteOpen;
        state.paletteQuery = "";
        render();
        if (state.paletteOpen) {
          window.setTimeout(() => dom.root.querySelector("[data-field='palette-query']")?.focus(), 0);
        }
      });
    });

    dom.root.querySelector("[data-action='close-palette-overlay']")?.addEventListener("click", (event) => {
      if (event.target === event.currentTarget) {
        state.paletteOpen = false;
        render();
      }
    });

    const paletteInput = dom.root.querySelector("[data-field='palette-query']");
    paletteInput?.addEventListener("input", (event) => {
      state.paletteQuery = event.target.value;
      render();
      window.setTimeout(() => dom.root.querySelector("[data-field='palette-query']")?.focus(), 0);
    });
    paletteInput?.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        state.paletteOpen = false;
        render();
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const first = filterCommands()[0];
        if (first) {
          first.run();
          state.paletteOpen = false;
          render();
        }
      }
    });

    dom.root.querySelectorAll("[data-action='run-command']").forEach((button) => {
      button.addEventListener("click", () => {
        const match = getCommands().find((command) => command.id === button.dataset.command);
        if (match) {
          match.run();
        }
        state.paletteOpen = false;
        render();
      });
    });
  }

  function onDocumentClick(event) {
    if (shouldUseLoginMode()) return;
    const navButton = event.target.closest?.("nav[aria-label='Primary navigation'] button, nav.rail button");
    if (navButton) {
      const nextPanel = normalizePanelLabel(navButton.getAttribute("aria-label") || navButton.innerText || navButton.textContent || "Chat");
      state.currentPanel = nextPanel;
      if (isChatPanel(nextPanel)) {
        state.composerExpanded = false;
      } else if (!state.composerText && !state.selection) {
        state.composerExpanded = false;
      }
      savePersisted();
      render();
      return;
    }

    const card = event.target.closest?.(".kanban-card");
    if (!card) return;
    const taskId = card.dataset?.kanbanTaskId;
    if (!taskId) return;
    const task = getFlattenedTasks(state.boardData).find((entry) => entry.id === taskId);
    if (task) {
      state.selectedTask = {
        id: task.id,
        title: task.title || task.id,
        status: task.status || task._column || "",
        assignee: task.assignee || "",
        board: state.currentBoard,
      };
      savePersisted();
      render();
    }
  }

  function isEditableElement(node) {
    if (!node || typeof node !== "object") return false;
    const tag = node.tagName?.toLowerCase?.() || "";
    return tag === "textarea" || tag === "input" || !!node.isContentEditable;
  }

  function isComposerEditable(node) {
    if (!node || typeof node !== "object") return false;
    if (node.matches?.("[data-field='composer-text']")) return true;
    return !!node.closest?.("[data-field='composer-text']");
  }

  function onGlobalKeydown(event) {
    if (shouldUseLoginMode()) return;
    state.currentPanel = getCurrentPanelName();
    const target = event.target;
    const activeElement = document.activeElement;
    const inEditable = isEditableElement(target) || isEditableElement(activeElement);
    const insideExtension = !!(dom.root?.contains(target) || dom.root?.contains(activeElement));
    const slashPressed =
      (!event.shiftKey && event.key === "/") ||
      (!event.shiftKey && (event.key === "Unidentified" || event.key === "") && event.code === "Slash") ||
      event.code === "NumpadDivide";
    const questionPressed =
      event.shiftKey &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.altKey &&
      event.code === "Slash";

    if (questionPressed && (isComposerEditable(target) || isComposerEditable(activeElement))) {
      event.preventDefault();
      const composer = getComposerElement();
      if (composer) {
        insertTextAtCursor(composer, "?");
        markComposerInteraction(1800);
      }
      return;
    }

    if (slashPressed && (isComposerEditable(target) || isComposerEditable(activeElement))) {
      event.preventDefault();
      const composer = getComposerElement();
      if (composer) {
        insertTextAtCursor(composer, "/");
        markComposerInteraction(1800);
      }
      return;
    }

    if (questionPressed && !inEditable) {
      event.preventDefault();
      if (isChatPanel()) {
        if (!focusNativeComposer()) {
          openPanel("Chat");
          window.setTimeout(() => {
            focusNativeComposer();
          }, 120);
        }
      } else {
        focusFloatingComposer({ insertText: "?" });
      }
      return;
    }

    if (
      slashPressed &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.altKey &&
      !inEditable
    ) {
      event.preventDefault();
      if (isChatPanel()) {
        if (!focusNativeComposer()) {
          openPanel("Chat");
          window.setTimeout(() => {
            focusNativeComposer();
          }, 120);
        }
      } else {
        focusFloatingComposer({ insertText: "/" });
      }
      return;
    }

    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      state.paletteOpen = !state.paletteOpen;
      if (!state.paletteOpen) {
        state.paletteQuery = "";
      }
      render();
      if (state.paletteOpen) {
        window.setTimeout(() => dom.root.querySelector("[data-field='palette-query']")?.focus(), 0);
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === ".") {
      event.preventDefault();
      state.summaryOpen = !state.summaryOpen;
      render();
      return;
    }
    if (event.key === "Escape" && state.paletteOpen) {
      state.paletteOpen = false;
      render();
    }
  }

  function mount() {
    loadPersisted();
    dom.root = document.createElement("div");
    dom.root.id = "project-os-extension-root";
    document.body.appendChild(dom.root);
    document.addEventListener("selectionchange", onSelectionChange);
    document.addEventListener("mouseup", onSelectionFinalize, true);
    document.addEventListener("keyup", onSelectionFinalize, true);
    document.addEventListener("click", onDocumentClick, true);
    document.addEventListener("keydown", onGlobalKeydown, true);
    state.resizeHandler = () => syncLayoutGeometry();
    window.addEventListener("resize", state.resizeHandler, { passive: true });
    state.currentPath = location.pathname;
    state.currentPanel = getCurrentPanelName();
    refreshLayoutObserver();
    syncLayoutGeometry();
    state.routeTimer = window.setInterval(() => {
      const nextPath = location.pathname;
      const nextPanel = getCurrentPanelName();
      if (nextPath !== state.currentPath || nextPanel !== state.currentPanel) {
        state.currentPath = nextPath;
        state.currentPanel = nextPanel;
        refreshLayoutObserver();
        if (isChatPanel(nextPanel)) {
          state.composerExpanded = false;
        } else if (!state.composerText && !state.selection) {
          state.composerExpanded = false;
        }
        savePersisted();
        refreshData();
      } else {
        syncLayoutGeometry();
        tryFlushPendingSend();
        maybeRefreshForLoop();
        if ((state.submit.status === "sent" || state.submit.status === "waiting" || state.submit.status === "stalled_running")
          && Date.now() - Number(state.projectSessionLastFetchedAt || 0) >= 1200) {
          void refreshProjectSession({ renderNow: false });
        }
        const submitLifecycleChanged = updateSubmitLifecycle();
        updateLoopLifecycle();
        maybeRunAutoLoop(false);
        if (isComposerInteractionActive()) {
          // Avoid re-rendering the floating composer while mobile typing/paste/IME is active.
        } else if (submitLifecycleChanged) {
          requestRender();
        } else {
          render();
        }
      }
    }, ROUTE_POLL_MS);
    state.refreshTimer = window.setInterval(refreshData, REFRESH_MS);
    state.mounted = true;
    refreshData({ forceSummary: true });
  }

  function unmount() {
    if (state.refreshTimer) window.clearInterval(state.refreshTimer);
    if (state.routeTimer) window.clearInterval(state.routeTimer);
    if (state.flushTimer) window.clearTimeout(state.flushTimer);
    document.removeEventListener("selectionchange", onSelectionChange);
    document.removeEventListener("mouseup", onSelectionFinalize, true);
    document.removeEventListener("keyup", onSelectionFinalize, true);
    document.removeEventListener("click", onDocumentClick, true);
    document.removeEventListener("keydown", onGlobalKeydown, true);
    if (state.resizeHandler) {
      window.removeEventListener("resize", state.resizeHandler);
      state.resizeHandler = null;
    }
    state.layoutObserver?.disconnect?.();
    state.layoutObserver = null;
    document.body.classList.remove("project-os-extension-active");
    dom.root?.remove();
    state.mounted = false;
  }

  function remount() {
    unmount();
    mount();
  }

  window.__PROJECT_OS_HERMES_WEBUI_EXTENSION__ = {
    state,
    refreshData,
    remount,
    unmount,
    debug: {
      buildContextualPrompt,
      fillNativeComposer,
    },
  };

  mount();
})();
