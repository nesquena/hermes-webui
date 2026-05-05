/**
 * NEO Sprint 5 — Projects Command Center controller.
 *
 * Owns the #mainProjects panel: fetches the v2 snapshot from /api/projects,
 * renders Kanban + List views, drives filters, status pills, modals and
 * HTML5 drag-and-drop. Persists every mutation via /api/projects and
 * /api/project-tasks routes (POST/PATCH).
 *
 * Public entry points:
 *   - loadProjectsCommandCenter()  -> called by switchPanel('projects')
 *
 * Local-first by design: never reaches Jira/GitHub/Obsidian directly.
 * external_ref metadata is kept on tasks for future sync (EP-10).
 */

(function () {
  'use strict';

  const STATUS_ORDER = ['backlog', 'em_andamento', 'em_revisao', 'concluido'];
  const PRIORITY_VALUES = ['baixa', 'media', 'alta'];
  // Ranks for semantic sorting (kanban flow + risk/urgency).
  const STATUS_RANK = { backlog: 0, em_andamento: 1, em_revisao: 2, concluido: 3 };
  const PRIORITY_RANK = { alta: 0, media: 1, baixa: 2 };
  const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];
  const DEFAULT_PAGE_SIZE = 25;
  const LIST_PREFS_KEY = 'neo-projects-list-prefs-v1';
  const CATEGORY_KEYS = {
    Design: 'projects_category_design',
    Frontend: 'projects_category_frontend',
    Backend: 'projects_category_backend',
    Database: 'projects_category_database',
    Infra: 'projects_category_infra',
    DevOps: 'projects_category_devops',
    Docs: 'projects_category_docs',
    QA: 'projects_category_qa',
    'Segurança': 'projects_category_security',
  };

  const state = {
    projects: [],
    tasks: [],
    sources: [],
    counts: { total: 0, by_status: {} },
    // Multi-select filters: each dimension is a Set of selected values.
    // Empty Set = "no constraint on this dimension".
    filters: {
      text: '',
      status: new Set(),
      project_id: new Set(),
      priority: new Set(),
      source: new Set(),
      owner: new Set(),
      due: '',
    },
    view: 'kanban',
    showArchived: false,         // HU-04.10: "Mostrar arquivados" toggle
    loaded: false,
    bound: false,
    dragTaskId: null,
    list: {
      sortKey: 'status',     // default: agrupa por fluxo (backlog → concluído)
      sortDir: 'asc',
      page: 1,
      pageSize: DEFAULT_PAGE_SIZE,
    },
  };

  // Restore persisted list prefs (sort + page size). Page index is not
  // persisted on purpose — always start from page 1 on a fresh load.
  try {
    const raw = localStorage.getItem(LIST_PREFS_KEY);
    if (raw) {
      const saved = JSON.parse(raw) || {};
      if (saved.sortKey) state.list.sortKey = String(saved.sortKey);
      if (saved.sortDir === 'asc' || saved.sortDir === 'desc') state.list.sortDir = saved.sortDir;
      if (PAGE_SIZE_OPTIONS.indexOf(Number(saved.pageSize)) >= 0) state.list.pageSize = Number(saved.pageSize);
    }
  } catch (_) { /* ignore corrupt prefs */ }

  function _persistListPrefs() {
    try {
      localStorage.setItem(LIST_PREFS_KEY, JSON.stringify({
        sortKey: state.list.sortKey,
        sortDir: state.list.sortDir,
        pageSize: state.list.pageSize,
      }));
    } catch (_) { /* quota or private mode — silently degrade */ }
  }

  // Re-export so module is exercisable from console & tests.
  window._neoProjectsState = state;

  // ── i18n shim ───────────────────────────────────────────────────────────
  function _t(key, fallback) {
    if (typeof t === 'function') {
      const v = t(key);
      if (v && v !== key) return v;
    }
    if (typeof TRANSLATIONS === 'object') {
      const lang = (localStorage.getItem('hermes-lang') || 'pt-BR');
      const dict = TRANSLATIONS[lang] || TRANSLATIONS.en || {};
      if (dict[key]) return dict[key];
    }
    return fallback != null ? fallback : key;
  }

  // ── DOM helpers ─────────────────────────────────────────────────────────
  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function $id(id) { return document.getElementById(id); }

  function _toast(msg, level) {
    if (typeof showToast === 'function') showToast(msg, 2600, level || 'info');
    else console.log('[neo-projects]', msg);
  }

  // ── Network helpers ─────────────────────────────────────────────────────
  async function _api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body is fine */ }
    if (!r.ok) {
      const msg = (data && data.error) || ('HTTP ' + r.status);
      throw new Error(msg);
    }
    return data;
  }

  // ── Data fetch ──────────────────────────────────────────────────────────
  async function fetchSnapshot() {
    // HU-04.10: when "Mostrar arquivados" is on, ask the backend to include
    // archived projects/tasks. Counts always reflect ACTIVE work regardless.
    const path = state.showArchived
      ? '/api/projects?include_archived=1'
      : '/api/projects';
    const data = await _api('GET', path);
    state.projects = Array.isArray(data.projects) ? data.projects : [];
    state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
    state.sources = Array.isArray(data.sources) ? data.sources : [];
    state.counts = data.counts || { total: 0, by_status: {} };
    return data;
  }

  // ── Filters ─────────────────────────────────────────────────────────────
  function _matchesFilters(task) {
    const f = state.filters;
    if (f.status.size && !f.status.has(task.status)) return false;
    if (f.project_id.size && !f.project_id.has(task.project_id)) return false;
    if (f.priority.size && !f.priority.has(task.priority)) return false;
    if (f.source.size && !f.source.has(_taskSource(task))) return false;
    if (f.owner.size && !f.owner.has(task.owner || '')) return false;
    if (f.due && !_matchesDueFilter(task, f.due)) return false;
    if (f.text) {
      const q = f.text.toLowerCase();
      const hay = [
        task.title, task.description, task.owner, task.category,
        task.external_ref && task.external_ref.key,
        task.external_ref && task.external_ref.url,
        _projectName(task.project_id),
      ].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function _taskSource(task) {
    const ext = task && task.external_ref;
    return ext && ext.type ? String(ext.type).trim().toLowerCase() : 'local';
  }

  function _parseDateOnly(value) {
    const s = String(value || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
    const d = new Date(s + 'T00:00:00');
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function _todayDateOnly() {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), now.getDate());
  }

  function _matchesDueFilter(task, dueFilter) {
    const due = _parseDateOnly(task && task.due_date);
    if (dueFilter === 'none') return !due;
    if (!due) return false;
    const today = _todayDateOnly();
    if (dueFilter === 'overdue') return due < today;
    if (dueFilter === 'week') {
      const weekEnd = new Date(today);
      weekEnd.setDate(today.getDate() + 7);
      return due >= today && due <= weekEnd;
    }
    return true;
  }

  function _activeFilterCount() {
    const f = state.filters;
    return (
      (f.text ? 1 : 0)
      + f.status.size
      + f.project_id.size
      + f.priority.size
      + f.source.size
      + f.owner.size
      + (f.due ? 1 : 0)
    );
  }

  function _projectName(pid) {
    const p = state.projects.find(p => p.project_id === pid);
    return p ? p.name : '';
  }

  function _projectColor(pid) {
    const p = state.projects.find(p => p.project_id === pid);
    return (p && p.color) || '#00E5FF';
  }

  // ── Card render ─────────────────────────────────────────────────────────
  function _categoryLabel(category) {
    const key = CATEGORY_KEYS[category];
    return key ? _t(key, category) : (category || '');
  }

  function _priorityLabel(p) {
    return _t('projects_priority_' + (p === 'baixa' ? 'low' : p === 'media' ? 'medium' : 'high'), p);
  }

  function _renderTaskCard(task) {
    const card = document.createElement('article');
    card.className = 'kanban-card' + (task.archived ? ' kanban-card-archived' : '');
    card.dataset.taskId = task.task_id;
    card.dataset.priority = task.priority;
    card.dataset.status = task.status;
    if (task.archived) card.dataset.archived = '1';
    // Archived cards keep visible (HU-04.10) but cannot be dragged.
    card.draggable = !task.archived;

    const projectColor = _projectColor(task.project_id);
    const projName = _projectName(task.project_id) || _t('projects_unassigned', 'Sem projeto');

    const ext = task.external_ref;
    const extLabel = ext && (ext.key || ext.type)
      ? `<span class="kanban-card-ref" data-ref-type="${_esc(ext.type || 'local')}">${_esc((ext.type || 'local').toUpperCase())}${ext.key ? ' · ' + _esc(ext.key) : ''}</span>`
      : '';

    const isDone = task.status === 'concluido';
    const progressBlock = isDone
      ? `<span class="kanban-card-done-chip" data-i18n="projects_col_completed">${_esc(_t('projects_col_completed', 'Concluído'))}</span>`
      : `<div class="kanban-card-progress" aria-label="progress">
           <div class="kanban-card-progress-bar" style="width:${Number(task.progress) || 0}%"></div>
           <span class="kanban-card-progress-label">${Number(task.progress) || 0}%</span>
         </div>`;

    const due = task.due_date ? `<span class="kanban-card-due">${_esc(task.due_date)}</span>` : '';

    card.innerHTML = `
      <header class="kanban-card-head">
        <span class="kanban-card-project" style="--project-color:${_esc(projectColor)}">
          <span class="kanban-card-project-dot" aria-hidden="true"></span>
          ${_esc(projName)}
        </span>
        ${extLabel}
      </header>
      <h3 class="kanban-card-title">${_esc(task.title)}</h3>
      <div class="kanban-card-chips">
        <span class="kanban-card-chip chip-category">${_esc(_categoryLabel(task.category))}</span>
        <span class="kanban-card-chip chip-priority chip-priority-${_esc(task.priority)}">${_esc(_priorityLabel(task.priority))}</span>
        ${task.archived ? `<span class="kanban-card-chip chip-archived" data-i18n="projects_archived">${_esc(_t('projects_archived', 'Arquivado'))}</span>` : ''}
      </div>
      ${progressBlock}
      <footer class="kanban-card-foot">
        <span class="kanban-card-owner">${_esc(task.owner || '—')}</span>
        ${due}
      </footer>
    `;

    card.addEventListener('dragstart', _onDragStart);
    card.addEventListener('dragend', _onDragEnd);
    // Click on project chip → edit project; click anywhere else → edit task.
    const projChip = card.querySelector('.kanban-card-project');
    if (projChip) {
      projChip.style.cursor = 'pointer';
      projChip.title = _t('projects_edit_project_hint', 'Editar projeto');
      projChip.addEventListener('click', ev => {
        ev.stopPropagation();
        const proj = state.projects.find(p => p.project_id === task.project_id);
        if (proj) openProjectModal(proj);
      });
    }
    card.addEventListener('click', () => openTaskModal(task));
    return card;
  }

  // ── Render: Kanban + List + pills + counts ──────────────────────────────
  function renderKanban() {
    const root = $id('projectsKanban');
    if (!root) return;
    const visible = state.tasks.filter(t => (state.showArchived || !t.archived) && _matchesFilters(t));
    const byStatus = { backlog: [], em_andamento: [], em_revisao: [], concluido: [] };
    visible.forEach(t => { (byStatus[t.status] || (byStatus[t.status] = [])).push(t); });

    STATUS_ORDER.forEach(status => {
      const list = root.querySelector(`.kanban-column-list[data-drop-target="${status}"]`);
      if (!list) return;
      list.innerHTML = '';
      (byStatus[status] || []).forEach(task => list.appendChild(_renderTaskCard(task)));
      const colCount = root.querySelector(`.kanban-column-count[data-count="${status}"]`);
      if (colCount) colCount.textContent = String((byStatus[status] || []).length);
    });
  }

  // Comparable value for a sort key. Returns either a number (numeric/rank
  // sort) or a string (locale-aware compare). Keeps null/undefined at the end
  // by mapping them to Infinity / '' before the dir flip.
  function _sortValue(task, key) {
    switch (key) {
      case 'task_id':  return String(task.task_id || '').toLowerCase();
      case 'title':    return String(task.title || '').toLowerCase();
      case 'project':  return _projectName(task.project_id).toLowerCase();
      case 'priority': return PRIORITY_RANK[task.priority] != null ? PRIORITY_RANK[task.priority] : 99;
      case 'owner':    return String(task.owner || '').toLowerCase();
      case 'status':   return STATUS_RANK[task.status] != null ? STATUS_RANK[task.status] : 99;
      case 'source': {
        const ref = task.external_ref;
        return ref && ref.type ? String(ref.type).toLowerCase() : 'local';
      }
      default: return '';
    }
  }

  function _compareTasks(a, b, key, dir) {
    const va = _sortValue(a, key);
    const vb = _sortValue(b, key);
    let cmp;
    if (typeof va === 'number' && typeof vb === 'number') {
      cmp = va - vb;
    } else {
      cmp = String(va).localeCompare(String(vb), undefined, { numeric: true, sensitivity: 'base' });
    }
    if (cmp === 0) {
      // Stable tiebreaker: created_at desc, then task_id.
      const ta = Number(a.created_at) || 0;
      const tb = Number(b.created_at) || 0;
      cmp = tb - ta;
      if (cmp === 0) cmp = String(a.task_id).localeCompare(String(b.task_id));
    }
    return dir === 'desc' ? -cmp : cmp;
  }

  function _renderListSortIndicators() {
    document.querySelectorAll('.projects-list-sortable').forEach(th => {
      const key = th.dataset.sortKey;
      const active = key === state.list.sortKey;
      th.classList.toggle('is-sorted', active);
      th.classList.toggle('is-sorted-asc', active && state.list.sortDir === 'asc');
      th.classList.toggle('is-sorted-desc', active && state.list.sortDir === 'desc');
      th.setAttribute(
        'aria-sort',
        active ? (state.list.sortDir === 'asc' ? 'ascending' : 'descending') : 'none'
      );
      const ind = th.querySelector('.projects-list-sort-indicator');
      if (ind) ind.textContent = active ? (state.list.sortDir === 'asc' ? '▲' : '▼') : '';
    });
  }

  function _renderListPager(totalRows, pageSize, page, totalPages) {
    const pager = $id('projectsListPager');
    if (!pager) return;
    pager.hidden = totalRows === 0;
    const summary = $id('projectsListPagerSummary');
    if (summary) {
      if (totalRows === 0) {
        summary.textContent = _t('projects_list_pager_empty', 'Nenhuma tarefa.');
      } else {
        const from = (page - 1) * pageSize + 1;
        const to = Math.min(page * pageSize, totalRows);
        const tpl = _t('projects_list_pager_summary', '{from}–{to} de {total}');
        summary.textContent = tpl
          .replace('{from}', String(from))
          .replace('{to}', String(to))
          .replace('{total}', String(totalRows));
      }
    }
    const pageEl = $id('projectsListPagerPage');
    if (pageEl) pageEl.textContent = `${page} / ${Math.max(1, totalPages)}`;
    const prev = $id('projectsListPagerPrev');
    const next = $id('projectsListPagerNext');
    if (prev) prev.disabled = page <= 1;
    if (next) next.disabled = page >= totalPages;
    const sizeSel = $id('projectsListPageSize');
    if (sizeSel && Number(sizeSel.value) !== pageSize) sizeSel.value = String(pageSize);
  }

  function renderList() {
    const tbody = $id('projectsListBody');
    if (!tbody) return;
    const visible = state.tasks.filter(t => (state.showArchived || !t.archived) && _matchesFilters(t));

    // Sort.
    const sortKey = state.list.sortKey;
    const sortDir = state.list.sortDir;
    visible.sort((a, b) => _compareTasks(a, b, sortKey, sortDir));

    // Paginate.
    const total = visible.length;
    const pageSize = state.list.pageSize;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (state.list.page > totalPages) state.list.page = totalPages;
    if (state.list.page < 1) state.list.page = 1;
    const page = state.list.page;
    const startIdx = (page - 1) * pageSize;
    const slice = visible.slice(startIdx, startIdx + pageSize);

    tbody.innerHTML = '';
    slice.forEach(task => {
      const tr = document.createElement('tr');
      tr.dataset.taskId = task.task_id;
      tr.addEventListener('click', () => openTaskModal(task));
      const ext = task.external_ref;
      const refLabel = ext && ext.type
        ? `${_esc((ext.type || '').toUpperCase())}${ext.key ? ' · ' + _esc(ext.key) : ''}`
        : _t('projects_source_local', 'Local');
      const statusLabel = _t('projects_col_' + (task.status === 'em_andamento' ? 'in_progress' : task.status === 'em_revisao' ? 'in_review' : task.status === 'concluido' ? 'completed' : 'backlog'), task.status);
      tr.innerHTML = `
        <td class="projects-list-id">${_esc(task.task_id.slice(0, 12))}</td>
        <td class="projects-list-task">${_esc(task.title)}</td>
        <td>${_esc(_projectName(task.project_id))}</td>
        <td><span class="chip-priority chip-priority-${_esc(task.priority)}">${_esc(_priorityLabel(task.priority))}</span></td>
        <td>${_esc(task.owner || '—')}</td>
        <td><span class="projects-list-status status-${_esc(task.status)}">${_esc(statusLabel)}</span></td>
        <td>${refLabel}</td>
      `;
      tbody.appendChild(tr);
    });

    _renderListSortIndicators();
    _renderListPager(total, pageSize, page, totalPages);
  }

  function _onListSortClick(th) {
    const key = th.dataset.sortKey;
    if (!key) return;
    if (state.list.sortKey === key) {
      state.list.sortDir = state.list.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      state.list.sortKey = key;
      state.list.sortDir = 'asc';
    }
    state.list.page = 1;
    _persistListPrefs();
    renderList();
  }

  function _onListPageSizeChange(ev) {
    const v = Number(ev.target.value);
    if (PAGE_SIZE_OPTIONS.indexOf(v) < 0) return;
    state.list.pageSize = v;
    state.list.page = 1;
    _persistListPrefs();
    renderList();
  }

  function _onListPagerPrev() {
    if (state.list.page > 1) {
      state.list.page -= 1;
      renderList();
    }
  }

  function _onListPagerNext() {
    state.list.page += 1;        // renderList clamps to totalPages
    renderList();
  }

  function renderStatusPills() {
    const visibleAll = state.tasks.filter(t => !t.archived);
    const counts = { all: visibleAll.length, backlog: 0, em_andamento: 0, em_revisao: 0, concluido: 0 };
    visibleAll.forEach(t => { if (counts[t.status] != null) counts[t.status] += 1; });
    document.querySelectorAll('#projectsStatusPills .projects-pill-count').forEach(el => {
      const k = el.dataset.count;
      if (counts[k] != null) el.textContent = String(counts[k]);
    });
    document.querySelectorAll('#projectsStatusPills .projects-pill').forEach(btn => {
      const status = btn.dataset.statusFilter;
      const active = status === 'all'
        ? state.filters.status.size === 0
        : state.filters.status.size === 1 && state.filters.status.has(status);
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function renderEmptyState() {
    const empty = $id('projectsEmptyState');
    const kanban = $id('projectsKanban');
    const list = $id('projectsList');
    const noProjects = state.projects.filter(p => !p.archived).length === 0;
    // Empty notice is now a discreet inline strip; kanban/list stay visible
    // so the user keeps the columns and can drop a task as soon as a project exists.
    if (empty) empty.hidden = !noProjects;
    if (kanban) kanban.hidden = state.view !== 'kanban';
    if (list) list.hidden = state.view !== 'list';
  }

  function renderAll() {
    renderEmptyState();
    renderStatusPills();
    if (state.view === 'kanban') renderKanban();
    else renderList();
  }

  // ── Drag and drop ───────────────────────────────────────────────────────
  function _onDragStart(ev) {
    const card = ev.currentTarget;
    state.dragTaskId = card.dataset.taskId;
    card.classList.add('kanban-card-dragging');
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = 'move';
      try { ev.dataTransfer.setData('text/plain', state.dragTaskId); } catch (_) {}
    }
  }

  function _onDragEnd(ev) {
    ev.currentTarget.classList.remove('kanban-card-dragging');
    document.querySelectorAll('.kanban-column-list.drop-active')
      .forEach(el => el.classList.remove('drop-active'));
    state.dragTaskId = null;
  }

  function _bindDropTargets() {
    document.querySelectorAll('.kanban-column-list[data-drop-target]').forEach(list => {
      list.addEventListener('dragover', ev => {
        ev.preventDefault();
        list.classList.add('drop-active');
      });
      list.addEventListener('dragleave', () => list.classList.remove('drop-active'));
      list.addEventListener('drop', async ev => {
        ev.preventDefault();
        list.classList.remove('drop-active');
        const taskId = state.dragTaskId
          || (ev.dataTransfer && ev.dataTransfer.getData('text/plain'));
        const newStatus = list.dataset.dropTarget;
        if (!taskId || !newStatus) return;
        const task = state.tasks.find(t => t.task_id === taskId);
        if (!task || task.status === newStatus) return;
        const prevStatus = task.status;
        task.status = newStatus;            // optimistic
        renderAll();
        try {
          const r = await _api('PATCH', `/api/project-tasks/${encodeURIComponent(taskId)}`, { status: newStatus });
          if (r && r.task) Object.assign(task, r.task);
          renderAll();
        } catch (err) {
          task.status = prevStatus;          // rollback
          renderAll();
          _toast(_t('projects_error_save', 'Erro ao salvar.') + ' ' + (err.message || ''), 'error');
        }
      });
    });
  }

  // ── Modals ──────────────────────────────────────────────────────────────
  function _openModal(modalEl) {
    if (!modalEl) return;
    modalEl.hidden = false;
    document.body.classList.add('projects-modal-open');
    const focusable = modalEl.querySelector('input,select,textarea,button');
    if (focusable) setTimeout(() => focusable.focus(), 30);
  }

  function _closeModal(modalEl) {
    if (!modalEl) return;
    modalEl.hidden = true;
    document.body.classList.remove('projects-modal-open');
    const errEl = modalEl.querySelector('[data-error-target]');
    if (errEl) { errEl.textContent = ''; errEl.hidden = true; }
  }

  function _showModalError(modalEl, msg) {
    const errEl = modalEl && modalEl.querySelector('[data-error-target]');
    if (!errEl) return;
    errEl.textContent = msg;
    errEl.hidden = false;
  }

  function openProjectModal(project) {
    const modal = $id('projectsProjectModal');
    if (!modal) return;
    const form = modal.querySelector('form');
    form.reset();
    const titleEl = $id('projectsProjectModalTitle');
    const submitBtn = $id('projectsProjectSubmitBtn');
    const archiveBtn = $id('projectsProjectArchiveBtn');
    if (project && project.project_id) {
      if (titleEl) titleEl.textContent = _t('projects_edit_title', 'Editar Projeto');
      if (submitBtn) submitBtn.textContent = _t('projects_save', 'Salvar');
      if (archiveBtn) {
        archiveBtn.hidden = false;
        archiveBtn.textContent = project.archived
          ? _t('projects_unarchive_project', 'Desarquivar projeto')
          : _t('projects_archive_project', 'Arquivar projeto');
        archiveBtn.dataset.archived = project.archived ? '1' : '0';
        archiveBtn.dataset.projectId = project.project_id;
      }
      form.elements['project_id'].value = project.project_id;
      form.elements['name'].value = project.name || '';
      form.elements['description'].value = project.description || '';
      form.elements['domain'].value = project.domain || 'projetos';
      form.elements['color'].value = project.color || '#00E5FF';
      form.elements['default_source_id'].value = project.default_source_id || '';
    } else {
      if (titleEl) titleEl.textContent = _t('projects_create_title', 'Novo Projeto');
      if (submitBtn) submitBtn.textContent = _t('projects_create_submit', 'Criar');
      if (archiveBtn) archiveBtn.hidden = true;
      form.elements['project_id'].value = '';
      form.elements['color'].value = '#00E5FF';
    }
    _openModal(modal);
  }

  function _refreshTaskFormProjectOptions(form) {
    const sel = form.querySelector('select[name="project_id"]');
    if (!sel) return;
    sel.innerHTML = '';
    state.projects.filter(p => !p.archived).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.project_id;
      opt.textContent = p.name;
      sel.appendChild(opt);
    });
  }

  function openTaskModal(task) {
    const modal = $id('projectsTaskModal');
    if (!modal) return;
    if (!state.projects.filter(p => !p.archived).length) {
      _toast(_t('projects_need_project_first', 'Crie um projeto antes de adicionar tarefas.'), 'warning');
      openProjectModal();
      return;
    }
    const form = modal.querySelector('form');
    form.reset();
    _refreshTaskFormProjectOptions(form);
    const titleEl = $id('projectsTaskModalTitle');
    const archiveBtn = $id('projectsTaskArchiveBtn');
    if (task && task.task_id) {
      if (titleEl) titleEl.textContent = _t('projects_task_edit_title', 'Editar Tarefa');
      form.elements['task_id'].value = task.task_id;
      form.elements['title'].value = task.title || '';
      form.elements['project_id'].value = task.project_id || '';
      form.elements['status'].value = task.status || 'backlog';
      form.elements['category'].value = task.category || 'Docs';
      form.elements['priority'].value = task.priority || 'media';
      form.elements['owner'].value = task.owner || 'jr';
      form.elements['due_date'].value = task.due_date || '';
      form.elements['progress'].value = task.progress != null ? task.progress : 0;
      form.elements['description'].value = task.description || '';
      const ext = task.external_ref || {};
      form.elements['ext_type'].value = ext.type || '';
      form.elements['ext_key'].value = ext.key || '';
      form.elements['ext_url'].value = ext.url || '';
      // HU-04.9: render read-only links (sessions/github/obsidian).
      _renderTaskRefs(task);
      // HU-04.10: show archive/unarchive button only on edit.
      if (archiveBtn) {
        archiveBtn.hidden = false;
        archiveBtn.textContent = task.archived
          ? _t('projects_unarchive', 'Desarquivar')
          : _t('projects_archive', 'Arquivar');
        archiveBtn.dataset.archived = task.archived ? '1' : '0';
        archiveBtn.dataset.taskId = task.task_id;
      }
    } else {
      if (titleEl) titleEl.textContent = _t('projects_task_create_title', 'Nova Tarefa');
      form.elements['task_id'].value = '';
      if (task && task.status) form.elements['status'].value = task.status;
      if (task && task.project_id) form.elements['project_id'].value = task.project_id;
      _renderTaskRefs(null);
      if (archiveBtn) archiveBtn.hidden = true;
    }
    _openModal(modal);
  }

  // HU-04.9: read-only render of vínculos (sessions / GitHub / Obsidian).
  // Refs are persisted on the backend (task.refs.{sessions,github,obsidian})
  // but are not editable from the modal yet — write-side will land in EP-10.
  function _renderTaskRefs(task) {
    const fs = $id('projectsTaskRefsFieldset');
    if (!fs) return;
    const refs = (task && task.refs) || {};
    const groups = ['sessions', 'github', 'obsidian'];
    let total = 0;
    groups.forEach(group => {
      const groupEl = fs.querySelector(`[data-ref-group="${group}"]`);
      const listEl = fs.querySelector(`[data-ref-list="${group}"]`);
      if (!groupEl || !listEl) return;
      const items = Array.isArray(refs[group]) ? refs[group] : [];
      listEl.innerHTML = '';
      if (items.length === 0) {
        groupEl.hidden = true;
        return;
      }
      groupEl.hidden = false;
      total += items.length;
      items.forEach(item => {
        const chip = document.createElement('span');
        chip.className = 'projects-task-refs-chip';
        chip.dataset.refType = group;
        let label = '';
        let url = '';
        if (typeof item === 'string') {
          label = item;
        } else if (item && typeof item === 'object') {
          label = item.label || item.key || item.id || item.url || JSON.stringify(item);
          url = item.url || '';
        }
        if (url) {
          const a = document.createElement('a');
          a.href = url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          a.textContent = label;
          chip.appendChild(a);
        } else {
          chip.textContent = label;
        }
        listEl.appendChild(chip);
      });
    });
    // Show fieldset only when at least one ref exists OR when editing
    // (so empty state hint remains visible during edit but not creation).
    const empty = $id('projectsTaskRefsEmpty');
    const hasTask = !!(task && task.task_id);
    fs.hidden = !hasTask;
    if (empty) empty.hidden = total > 0;
  }

  async function _submitProjectForm(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const modal = form.closest('.projects-modal');
    const fd = new FormData(form);
    const projectId = String(fd.get('project_id') || '').trim();
    const payload = {
      name: String(fd.get('name') || '').trim(),
      description: String(fd.get('description') || '').trim(),
      domain: String(fd.get('domain') || 'projetos').trim() || 'projetos',
      color: String(fd.get('color') || '#00E5FF'),
      default_source_id: String(fd.get('default_source_id') || '').trim() || null,
    };
    if (!payload.name) {
      _showModalError(modal, _t('projects_error_name_required', 'Nome é obrigatório.'));
      return;
    }
    try {
      let r;
      if (projectId) {
        r = await _api('PATCH', `/api/projects/${encodeURIComponent(projectId)}`, payload);
        if (r && r.project) {
          const idx = state.projects.findIndex(p => p.project_id === projectId);
          if (idx >= 0) state.projects[idx] = r.project;
        }
      } else {
        r = await _api('POST', '/api/projects', payload);
        if (r && r.project) state.projects.push(r.project);
      }
      try { await fetchSnapshot(); } catch (_) { /* keep optimistic state */ }
      _closeModal(modal);
      _renderFilterDimensions();
      renderAll();
      _toast(projectId
        ? _t('projects_toast_project_saved', 'Projeto salvo.')
        : _t('projects_toast_project_created', 'Projeto criado.'), 'success');
    } catch (err) {
      _showModalError(modal, err.message || _t('projects_error_save', 'Erro ao salvar.'));
    }
  }

  // HU-04.10: archive (or unarchive) a project from the edit modal.
  async function _onArchiveProjectClick(ev) {
    const btn = ev.currentTarget;
    const projectId = btn.dataset.projectId;
    const isArchived = btn.dataset.archived === '1';
    if (!projectId) return;
    const wantArchive = !isArchived;
    const confirmMsg = wantArchive
      ? _t('projects_archive_project_confirm', 'Arquivar este projeto? Ele e suas tarefas saem das contagens ativas.')
      : _t('projects_unarchive_project_confirm', 'Desarquivar este projeto?');
    if (typeof confirm === 'function' && !confirm(confirmMsg)) return;
    try {
      // Backend interprets status="arquivado" / "ativo" as archive flip.
      const body = { status: wantArchive ? 'arquivado' : 'ativo' };
      await _api('PATCH', `/api/projects/${encodeURIComponent(projectId)}`, body);
      await fetchSnapshot();
      const modal = $id('projectsProjectModal');
      if (modal) _closeModal(modal);
      _renderFilterDimensions();
      renderAll();
      _toast(wantArchive
        ? _t('projects_toast_project_archived', 'Projeto arquivado.')
        : _t('projects_toast_project_unarchived', 'Projeto desarquivado.'), 'success');
    } catch (err) {
      _toast(_t('projects_error_save', 'Erro ao salvar.') + ' ' + (err.message || ''), 'error');
    }
  }

  // HU-04.10: archive (or unarchive) a task from the edit modal.
  async function _onArchiveTaskClick(ev) {
    const btn = ev.currentTarget;
    const taskId = btn.dataset.taskId;
    const isArchived = btn.dataset.archived === '1';
    if (!taskId) return;
    const wantArchive = !isArchived;
    try {
      const body = { archived: wantArchive };
      const r = await _api('PATCH', `/api/project-tasks/${encodeURIComponent(taskId)}`, body);
      if (r && r.task) {
        const idx = state.tasks.findIndex(t => t.task_id === taskId);
        if (idx >= 0) state.tasks[idx] = r.task;
      }
      const modal = $id('projectsTaskModal');
      if (modal) _closeModal(modal);
      _renderFilterDimensions();
      renderAll();
      _toast(wantArchive
        ? _t('projects_toast_task_archived', 'Tarefa arquivada.')
        : _t('projects_toast_task_unarchived', 'Tarefa desarquivada.'), 'success');
    } catch (err) {
      _toast(_t('projects_error_save', 'Erro ao salvar.') + ' ' + (err.message || ''), 'error');
    }
  }

  async function _submitTaskForm(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const modal = form.closest('.projects-modal');
    const fd = new FormData(form);
    const taskId = String(fd.get('task_id') || '').trim();
    const extType = String(fd.get('ext_type') || '').trim();
    const extKey = String(fd.get('ext_key') || '').trim();
    const extUrl = String(fd.get('ext_url') || '').trim();
    let externalRef = null;
    if (extType || extKey || extUrl) {
      externalRef = {
        type: extType || 'local',
        key: extKey,
        url: extUrl,
        source_id: null,
        status: '',
      };
    }
    const payload = {
      project_id: String(fd.get('project_id') || '').trim(),
      title: String(fd.get('title') || '').trim(),
      status: String(fd.get('status') || 'backlog'),
      category: String(fd.get('category') || 'Docs'),
      priority: String(fd.get('priority') || 'media'),
      owner: String(fd.get('owner') || 'jr').trim(),
      due_date: String(fd.get('due_date') || '').trim(),
      progress: Math.max(0, Math.min(100, Number(fd.get('progress') || 0))),
      description: String(fd.get('description') || '').trim(),
      external_ref: externalRef,
    };
    if (!payload.title) {
      _showModalError(modal, _t('projects_error_title_required', 'Título é obrigatório.'));
      return;
    }
    if (!payload.project_id) {
      _showModalError(modal, _t('projects_error_project_required', 'Selecione um projeto.'));
      return;
    }
    try {
      let r;
      if (taskId) {
        r = await _api('PATCH', `/api/project-tasks/${encodeURIComponent(taskId)}`, payload);
        if (r && r.task) {
          const idx = state.tasks.findIndex(t => t.task_id === taskId);
          if (idx >= 0) state.tasks[idx] = r.task;
        }
      } else {
        r = await _api('POST', '/api/project-tasks', payload);
        if (r && r.task) state.tasks.push(r.task);
      }
      _closeModal(modal);
      _renderFilterDimensions();
      renderAll();
      _toast(taskId
        ? _t('projects_toast_task_saved', 'Tarefa salva.')
        : _t('projects_toast_task_created', 'Tarefa criada.'), 'success');
    } catch (err) {
      _showModalError(modal, err.message || _t('projects_error_save', 'Erro ao salvar.'));
    }
  }

  // ── View toggle, filters, pills binding ─────────────────────────────────
  function _setView(view) {
    state.view = view === 'list' ? 'list' : 'kanban';
    document.querySelectorAll('.projects-view-btn').forEach(btn => {
      const active = btn.dataset.view === state.view;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    renderAll();
  }

  // ── Filters popover ───────────────────────────────────────────────────
  function _statusLabel(status) {
    return _t('projects_col_' + (
      status === 'em_andamento' ? 'in_progress'
        : status === 'em_revisao' ? 'in_review'
        : status === 'concluido' ? 'completed'
        : 'backlog'
    ), status);
  }

  function _renderFilterCheckbox(container, dim, value, label) {
    const id = `projects-flt-${dim}-${String(value).replace(/[^a-z0-9_-]/gi, '_')}`;
    const item = document.createElement('label');
    item.className = 'projects-filters-option';
    item.htmlFor = id;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = id;
    cb.value = value;
    cb.dataset.filterDim = dim;
    cb.checked = state.filters[dim] && state.filters[dim].has(value);
    cb.addEventListener('change', () => _onFilterCheckboxChange(dim, value, cb.checked));
    const span = document.createElement('span');
    span.className = 'projects-filters-option-label';
    span.textContent = label;
    item.appendChild(cb);
    item.appendChild(span);
    container.appendChild(item);
  }

  function _renderFilterDimensions() {
    // Status (fixed values + canonical order)
    const statusBox = $id('projectsFilterDimStatus');
    if (statusBox) {
      statusBox.innerHTML = '';
      STATUS_ORDER.forEach(s => _renderFilterCheckbox(statusBox, 'status', s, _statusLabel(s)));
    }
    // Projects (sorted by name)
    const projBox = $id('projectsFilterDimProjects');
    if (projBox) {
      projBox.innerHTML = '';
      state.projects
        .filter(p => !p.archived)
        .slice()
        .sort((a, b) => String(a.name).localeCompare(String(b.name)))
        .forEach(p => _renderFilterCheckbox(projBox, 'project_id', p.project_id, p.name));
    }
    // Priorities (high → low)
    const prioBox = $id('projectsFilterDimPriorities');
    if (prioBox) {
      prioBox.innerHTML = '';
      ['alta', 'media', 'baixa'].forEach(p => _renderFilterCheckbox(prioBox, 'priority', p, _priorityLabel(p)));
    }
    // Sources (local + external_ref types discovered in tasks).
    const srcBox = $id('projectsFilterDimSources');
    if (srcBox) {
      srcBox.innerHTML = '';
      const sources = Array.from(new Set(
        ['local'].concat(state.tasks.filter(t => !t.archived).map(_taskSource).filter(Boolean))
      )).sort((a, b) => (a === 'local' ? -1 : b === 'local' ? 1 : a.localeCompare(b)));
      sources.forEach(s => _renderFilterCheckbox(srcBox, 'source', s, s === 'local' ? _t('projects_source_local', 'Local') : s.toUpperCase()));
    }
    // Owners (distinct, sorted)
    const ownBox = $id('projectsFilterDimOwners');
    if (ownBox) {
      ownBox.innerHTML = '';
      const owners = Array.from(new Set(
        state.tasks.filter(t => !t.archived).map(t => t.owner).filter(Boolean)
      )).sort();
      owners.forEach(o => _renderFilterCheckbox(ownBox, 'owner', o, o));
    }
    const dueSel = $id('projectsFilterDue');
    if (dueSel) dueSel.value = state.filters.due || '';
  }

  function _onFilterCheckboxChange(dim, value, checked) {
    const set = state.filters[dim];
    if (!(set instanceof Set)) return;
    if (checked) set.add(value); else set.delete(value);
    state.list.page = 1;       // any filter change resets pagination
    _updateFiltersBadge();
    renderAll();
  }

  function _onDueFilterChange(ev) {
    state.filters.due = String(ev.target.value || '');
    state.list.page = 1;
    _updateFiltersBadge();
    renderAll();
  }

  function _onStatusPillClick(btn) {
    const status = btn.dataset.statusFilter;
    if (!status) return;
    state.filters.status.clear();
    if (status !== 'all') state.filters.status.add(status);
    state.list.page = 1;
    _renderFilterDimensions();
    _updateFiltersBadge();
    renderAll();
  }

  function _updateFiltersBadge() {
    const total = _activeFilterCount();
    const btnBadge = $id('projectsFiltersBtnBadge');
    if (btnBadge) {
      btnBadge.textContent = String(total);
      btnBadge.hidden = total === 0;
    }
    const popCount = $id('projectsFiltersPopoverCount');
    if (popCount) {
      popCount.textContent = String(total);
      popCount.hidden = total === 0;
    }
    const clearBtn = $id('projectsFiltersClear');
    if (clearBtn) clearBtn.hidden = total === 0;
    const searchClear = $id('projectsFiltersSearchClear');
    if (searchClear) searchClear.hidden = !state.filters.text;
    // Badge for the parent button (state.bound only)
    const btn = $id('projectsFiltersBtn');
    if (btn) btn.classList.toggle('has-active-filters', total > 0);
  }

  function _clearAllFilters() {
    state.filters.text = '';
    state.filters.status.clear();
    state.filters.project_id.clear();
    state.filters.priority.clear();
    state.filters.source.clear();
    state.filters.owner.clear();
    state.filters.due = '';
    state.list.page = 1;
    const txt = $id('projectsFilterText');
    if (txt) txt.value = '';
    const due = $id('projectsFilterDue');
    if (due) due.value = '';
    document.querySelectorAll('.projects-filters-popover input[type="checkbox"]').forEach(cb => {
      cb.checked = false;
    });
    _updateFiltersBadge();
    renderAll();
  }

  function _toggleFiltersPopover(force) {
    const pop = $id('projectsFiltersPopover');
    const btn = $id('projectsFiltersBtn');
    if (!pop || !btn) return;
    const willOpen = typeof force === 'boolean' ? force : pop.hasAttribute('hidden');
    if (willOpen) {
      // Re-render so newly-created projects/owners show up immediately.
      _renderFilterDimensions();
      pop.removeAttribute('hidden');
      btn.setAttribute('aria-expanded', 'true');
      const txt = $id('projectsFilterText');
      if (txt) setTimeout(() => txt.focus(), 30);
    } else {
      pop.setAttribute('hidden', '');
      btn.setAttribute('aria-expanded', 'false');
    }
  }

  function _bindOnce() {
    if (state.bound) return;
    state.bound = true;

    // View toggle
    document.querySelectorAll('.projects-view-btn').forEach(btn => {
      btn.addEventListener('click', () => _setView(btn.dataset.view));
    });

    // Filters popover — button toggles, dialog renders multi-select dimensions.
    const fBtn = $id('projectsFiltersBtn');
    if (fBtn) fBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      _toggleFiltersPopover();
    });
    const popClose = $id('projectsFiltersPopoverClose');
    if (popClose) popClose.addEventListener('click', () => _toggleFiltersPopover(false));
    const popApply = $id('projectsFiltersApply');
    if (popApply) popApply.addEventListener('click', () => _toggleFiltersPopover(false));
    const popClear = $id('projectsFiltersClear');
    if (popClear) popClear.addEventListener('click', () => {
      _clearAllFilters();
      _renderFilterDimensions();
    });

    // Live search inside popover (any change resets list to page 1).
    const txt = $id('projectsFilterText');
    if (txt) txt.addEventListener('input', () => {
      state.filters.text = txt.value.trim();
      state.list.page = 1;
      _updateFiltersBadge();
      renderAll();
    });
    const searchClear = $id('projectsFiltersSearchClear');
    if (searchClear) searchClear.addEventListener('click', () => {
      state.filters.text = '';
      if (txt) { txt.value = ''; txt.focus(); }
      _updateFiltersBadge();
      renderAll();
    });

    const dueFilter = $id('projectsFilterDue');
    if (dueFilter) dueFilter.addEventListener('change', _onDueFilterChange);

    document.querySelectorAll('#projectsStatusPills .projects-pill[data-status-filter]').forEach(btn => {
      btn.addEventListener('click', () => _onStatusPillClick(btn));
    });

    // Click outside popover closes it.
    document.addEventListener('click', ev => {
      const pop = $id('projectsFiltersPopover');
      if (!pop || pop.hasAttribute('hidden')) return;
      const anchor = pop.closest('.projects-filters-anchor');
      if (anchor && !anchor.contains(ev.target)) _toggleFiltersPopover(false);
    });

    // List view: sortable headers + pagination controls
    document.querySelectorAll('.projects-list-sortable').forEach(th => {
      th.addEventListener('click', () => _onListSortClick(th));
      th.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          _onListSortClick(th);
        }
      });
      th.setAttribute('tabindex', '0');
      th.setAttribute('role', 'button');
    });
    const pageSizeSel = $id('projectsListPageSize');
    if (pageSizeSel) {
      pageSizeSel.value = String(state.list.pageSize);
      pageSizeSel.addEventListener('change', _onListPageSizeChange);
    }
    const pagerPrev = $id('projectsListPagerPrev');
    if (pagerPrev) pagerPrev.addEventListener('click', _onListPagerPrev);
    const pagerNext = $id('projectsListPagerNext');
    if (pagerNext) pagerNext.addEventListener('click', _onListPagerNext);

    // New project / Empty state CTA
    const newBtn = $id('projectsNewProjectBtn');
    if (newBtn) newBtn.addEventListener('click', openProjectModal);
    const emptyCta = $id('projectsEmptyCta');
    if (emptyCta) emptyCta.addEventListener('click', openProjectModal);

    // Add task buttons (per column)
    document.querySelectorAll('.kanban-add-task-btn[data-add-status]').forEach(btn => {
      btn.addEventListener('click', () => openTaskModal({ status: btn.dataset.addStatus }));
    });

    // Modal close buttons
    document.querySelectorAll('[data-projects-modal-close]').forEach(el => {
      el.addEventListener('click', ev => {
        const modal = ev.currentTarget.closest('.projects-modal');
        _closeModal(modal);
      });
    });

    // Form submit
    const projForm = $id('projectsProjectForm');
    if (projForm) projForm.addEventListener('submit', _submitProjectForm);
    const taskForm = $id('projectsTaskForm');
    if (taskForm) taskForm.addEventListener('submit', _submitTaskForm);

    // HU-04.10: archive buttons (project + task) and show-archived toggle.
    const archProj = $id('projectsProjectArchiveBtn');
    if (archProj) archProj.addEventListener('click', _onArchiveProjectClick);
    const archTask = $id('projectsTaskArchiveBtn');
    if (archTask) archTask.addEventListener('click', _onArchiveTaskClick);
    const showArchToggle = $id('projectsFilterShowArchived');
    if (showArchToggle) {
      showArchToggle.checked = !!state.showArchived;
      showArchToggle.addEventListener('change', async () => {
        state.showArchived = !!showArchToggle.checked;
        try {
          await fetchSnapshot();
          _renderFilterDimensions();
          renderAll();
        } catch (err) {
          _toast(_t('projects_error_load', 'Erro ao carregar projetos.') + ' ' + (err.message || ''), 'error');
        }
      });
    }

    // Esc closes the filters popover first, then any open modal.
    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Escape') return;
      const pop = $id('projectsFiltersPopover');
      if (pop && !pop.hasAttribute('hidden')) {
        _toggleFiltersPopover(false);
        return;
      }
      document.querySelectorAll('.projects-modal:not([hidden])').forEach(_closeModal);
    });

    _bindDropTargets();
  }

  // ── Public entry point ──────────────────────────────────────────────────
  async function loadProjectsCommandCenter() {
    try {
      _bindOnce();
      await fetchSnapshot();
      _renderFilterDimensions();
      _updateFiltersBadge();
      _setView(state.view);              // also calls renderAll()
      state.loaded = true;
    } catch (err) {
      console.error('[neo-projects] load failed', err);
      _toast(_t('projects_error_load', 'Erro ao carregar projetos.') + ' ' + (err.message || ''), 'error');
    }
  }

  window.loadProjectsCommandCenter = loadProjectsCommandCenter;
  window.openProjectModal = openProjectModal;
  window.openTaskModal = openTaskModal;
})();
