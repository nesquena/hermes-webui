/* Projects + Spaces merge, step 3 (behind the project-folders flag).
 *
 * Adds to the existing Projects screen, only when the server reports the flag:
 *  - a private "📁 Mijn werk" entry at the top of the list (own chats without a
 *    project plus the spaces you can see; virtual, nothing is stored), and
 *  - a "📂 Mappen" card on each project with its folders and, for the owner or
 *    an admin, "Map toevoegen" (pick one of your spaces).
 * Spaces keep working unchanged; nothing is migrated.
 */
(function (root) {
  'use strict';
  let flag = null; // null = unknown, true/false after /api/projects/folders/config
  let inflight = null;
  const T = (k, fb, ...a) => (typeof root.spT === 'function' ? root.spT(k, fb, ...a) : fb);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const call = (path, opts) => root.api(path, Object.assign({redirect401: false, timeoutToast: false}, opts || {}));

  function enabled() {
    if (flag !== null) return Promise.resolve(flag);
    if (!inflight) inflight = call('/api/projects/folders/config').then(r => (flag = !!(r && r.enabled))).catch(() => (flag = false));
    return inflight;
  }

  async function afterList() {
    if (!(await enabled())) return;
    const list = root.document.getElementById('projectsPanelList');
    if (!list || list.querySelector('.proj-row-mywork')) return;
    const row = root.document.createElement('div');
    row.className = 'proj-row proj-row-mywork' + (root._spMyWorkOpen ? ' active' : '');
    row.innerHTML = '<span class="proj-dot sp-mywork-dot" aria-hidden="true">🏠</span><div class="proj-row-main">'
      + '<div class="proj-row-name">' + esc(T('projects_my_work', 'My work')) + '</div>'
      + '<div class="proj-row-meta">' + esc(T('projects_my_work_meta', 'Private · your chats without a project')) + '</div></div>';
    row.onclick = openMyWork;
    const create = list.querySelector('.proj-create');
    if (create && create.nextSibling) list.insertBefore(row, create.nextSibling);
    else list.appendChild(row);
  }

  function folderRow(f) {
    const label = f.role === 'project' ? T('projects_folder_project', 'Project folder')
      : f.role === 'space' ? (f.name || '') : (f.name || '');
    const badge = f.role === 'project' ? '<span class="proj-chip">' + esc(T('projects_folder_shared', 'Shared with members')) + '</span>' : '';
    return '<div class="proj-item" title="' + esc(f.path) + '"><span class="proj-item-name">📂 ' + esc(label) + '</span>' + badge + '</div>';
  }

  async function afterDetail(project) {
    root._spMyWorkOpen = false;
    if (!project || !project.project_id || !(await enabled())) return;
    const sections = root.document.getElementById('projSections');
    if (!sections) return;
    let data;
    try { data = await call('/api/projects/folders?project_id=' + encodeURIComponent(project.project_id)); }
    catch (_) { return; }
    const current = (typeof _projSelectedId !== 'undefined') ? String(_projSelectedId) : String(project.project_id);
    if (!data || root._spMyWorkOpen || current !== String(project.project_id)) return;
    const old = sections.querySelector('.sp-folders-card');
    if (old) old.remove();
    const template = root.document.createElement('template');
    const items = (data.folders || []).map(folderRow).join('')
      || '<div class="proj-empty">' + esc(T('projects_folders_empty', 'No folders yet. Add one of your spaces to work on it in this project.')) + '</div>';
    template.innerHTML = _projFold('folders', T('projects_folders_title', 'Files and folders'), items
      + (data.can_manage ? '<div class="proj-actions"><button type="button" class="app-dialog-btn sp-folder-add">➕ ' + esc(T('projects_folder_add', 'Add folder')) + '</button></div>' : ''), {count:data.folders?.length, open:!!data.folders?.length});
    const card = template.content.firstElementChild;
    card.classList.add('sp-folders-card');
    sections.insertBefore(card, sections.children[1] || null);
    const add = card.querySelector('.sp-folder-add');
    if (add) add.onclick = () => pickFolder(project.project_id);
  }

  async function pickFolder(projectId) {
    let spaces = [];
    try { const r = await call('/api/workspaces'); spaces = (r && r.workspaces) || []; } catch (_) {}
    if (!spaces.length) { if (root.showToast) root.showToast(T('projects_folder_none', 'You have no spaces to add.'), 3000); return; }
    const dlg = root.document.createElement('dialog');
    dlg.className = 'app-dialog sp-folder-dialog';
    dlg.innerHTML = '<form method="dialog"><h3>📂 ' + esc(T('projects_folder_add', 'Add folder')) + '</h3>'
      + '<p>' + esc(T('projects_folder_pick', 'Pick one of your spaces. Project members can then work in it too.')) + '</p>'
      + '<select class="sp-folder-select">' + spaces.map(w => '<option value="' + esc(w.path) + '">' + esc(w.name || w.path) + '</option>').join('') + '</select>'
      + '<div class="app-dialog-actions"><button value="cancel" class="app-dialog-btn">' + esc(T('cancel', 'Cancel')) + '</button>'
      + '<button value="ok" class="app-dialog-btn primary">' + esc(T('projects_folder_add', 'Add folder')) + '</button></div></form>';
    root.document.body.appendChild(dlg);
    dlg.addEventListener('close', async () => {
      const path = dlg.querySelector('.sp-folder-select').value;
      const ok = dlg.returnValue === 'ok';
      dlg.remove();
      if (!ok || !path) return;
      try {
        await call('/api/projects/folders/add', {method: 'POST', body: JSON.stringify({project_id: projectId, path})});
        if (typeof root._projOpen === 'function') await root._projOpen(projectId);
      } catch (e) {
        if (root.showToast) root.showToast(T('projects_folder_failed', 'Adding the folder did not work: ') + ((e && e.message) || ''), 4000, 'error');
      }
    });
    dlg.showModal();
  }

  async function openMyWork() {
    root._spMyWorkOpen = true;
    let data;
    try { data = await call('/api/projects/my-work'); } catch (_) { return; }
    if (!root._spMyWorkOpen) return;
    const body = root.document.getElementById('projDetailBody');
    const empty = root.document.getElementById('projDetailEmpty');
    const summary = root.document.getElementById('projSummary');
    const sections = root.document.getElementById('projSections');
    if (body) body.style.display = '';
    if (empty) empty.style.display = 'none';
    root.document.querySelectorAll('#projectsPanelList .proj-row').forEach(r => r.classList.toggle('active', r.classList.contains('proj-row-mywork')));
    if (summary) summary.innerHTML = '<div class="proj-card"><div class="proj-card-title">🏠 ' + esc(T('projects_my_work', 'My work'))
      + '<span class="proj-chip">🔒 ' + esc(T('projects_private', 'Private')) + '</span></div><div class="proj-state">'
      + esc(T('projects_my_work_sub', 'Your own chats that are not part of a project, and the folders you can work in.')) + '</div></div>';
    const convs = (data.conversations || []).map(s => '<div class="proj-item" data-project-session="' + esc(s.session_id) + '"><span class="proj-item-name">💬 ' + esc(s.title || T('untitled', 'Untitled')) + '</span></div>').join('')
      || '<div class="proj-empty">' + esc(T('projects_my_work_no_chats', 'No chats without a project yet.')) + '</div>';
    const folders = (data.folders || []).map(folderRow).join('') || '<div class="proj-empty">' + esc(T('projects_none_yet', 'Nothing here yet.')) + '</div>';
    if (sections) {
      sections.innerHTML = _projFold('conversations', T('projects_section_conversations', 'Conversations'), convs,
        {project:'my-work', count:data.conversation_count, open:true}) + _projFold('folders', T('projects_folders_title', 'Files and folders'),
        folders, {project:'my-work', count:data.folders?.length, open:!!data.folders?.length});
      sections.querySelectorAll('[data-project-session]').forEach(row => row.addEventListener('click', () => {
        if (typeof root.loadSession === 'function') root.loadSession(row.dataset.projectSession);
        if (typeof root.switchPanel === 'function') root.switchPanel('chat');
      }));
    }
  }

  // After the merge: hide the Spaces menu item (unless the classic view is on)
  // and present the composer folder chip as the project you work in.
  let config = null;
  async function applyMergedChrome() {
    try { config = await call('/api/projects/folders/config'); } catch (_) { return; }
    flag = !!(config && config.enabled);
    const html = root.document.documentElement;
    if (config && config.merged && !config.classic_spaces) html.setAttribute('data-sp-merged', '1');
    else html.removeAttribute('data-sp-merged');
    if (!(config && config.merged)) return;
    const label = root.document.getElementById('composerWorkspaceLabel');
    const chip = root.document.getElementById('composerWorkspaceChip');
    if (label) label.setAttribute('data-sp-prefix', '📁 ' + T('composer_project_prefix', 'Project: '));
    if (chip) {
      const title = T('composer_project_title', 'Choose the project you work in');
      chip.title = title; chip.setAttribute('aria-label', title);
    }
  }
  if (root.document) {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', applyMergedChrome);
    else applyMergedChrome();
  }

  root.SpProjectFolders = {afterList, afterDetail, openMyWork, applyMergedChrome, _reset: () => { flag = null; inflight = null; }};
})(typeof window !== 'undefined' ? window : globalThis);
