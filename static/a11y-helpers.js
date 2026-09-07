/* Shared accessibility (a11y) utilities.
 *
 * Created while fixing accessibility audit findings (WCAG 2.2 / EN 301 549).
 * Deliberately a single module, so the same pattern is not copied in several places
 * — a duplicated pattern caused some defects (one dialog had
 * focus trapping, its twin did not).
 */

/* Focus trapping in a modal dialog + isolating the background from screen readers.
 *
 * Returns a cleanup function: removes the listener, restores the background, and returns focus
 * to the element that had it before the dialog opened (WCAG 2.4.3).
 */
function a11yTrapFocus(modalEl, opts){
  if (!modalEl) return () => {};
  const options = opts || {};
  const previouslyFocused = (document.activeElement instanceof HTMLElement)
    ? document.activeElement
    : null;

  const selector = 'a[href], button, textarea, input, select, summary, [tabindex]:not([tabindex="-1"])';
  const collect = () => Array.from(modalEl.querySelectorAll(selector)).filter((el) => {
    if (el.disabled || el.hidden) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return el.tabIndex >= 0;
  });

  // Background outside the dialog: inert removes it from tab order AND from the accessibility tree.
  const isolated = [];
  if (options.isolateBackground !== false) {
    Array.from(document.body.children).forEach((el) => {
      if (el === modalEl || el.contains(modalEl)) return;
      if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'TEMPLATE') return;
      if (el.hasAttribute('inert')) return;
      el.setAttribute('inert', '');
      el.setAttribute('data-a11y-inert', '1');
      isolated.push(el);
    });
  }

  const onKeyDown = (ev) => {
    if (ev.key !== 'Tab') return;
    const focusableEls = collect();
    if (!focusableEls.length) { ev.preventDefault(); return; }
    const current = document.activeElement;
    let idx = focusableEls.indexOf(current);
    if (idx === -1) {
      ev.preventDefault();
      focusableEls[0].focus();
      return;
    }
    idx = ev.shiftKey ? idx - 1 : idx + 1;
    idx = (idx + focusableEls.length) % focusableEls.length;
    ev.preventDefault();
    focusableEls[idx].focus();
  };
  modalEl.addEventListener('keydown', onKeyDown);

  // Initial focus: the indicated element, the first field, or the first control.
  if (options.autofocus !== false) {
    setTimeout(() => {
      let target = null;
      if (options.initialFocus) {
        target = (typeof options.initialFocus === 'string')
          ? modalEl.querySelector(options.initialFocus)
          : options.initialFocus;
      }
      if (!target) target = collect()[0] || null;
      if (target && typeof target.focus === 'function') target.focus();
    }, 0);
  }

  return () => {
    modalEl.removeEventListener('keydown', onKeyDown);
    isolated.forEach((el) => {
      el.removeAttribute('inert');
      el.removeAttribute('data-a11y-inert');
    });
    if (options.restoreFocus !== false && previouslyFocused
        && document.contains(previouslyFocused)
        && typeof previouslyFocused.focus === 'function') {
      try { previouslyFocused.focus(); } catch (_e) { /* element disappeared */ }
    }
  };
}

/* Selected state in a button group that visually marks the choice with a CSS class.
 *
 * Fixes the "CSS-class-only state" pattern — for a screen reader the class does not
 * exist, so without aria-pressed the user does not know which option is active.
 */
function a11ySyncPressedState(container, itemSelector, activeClass){
  if (!container) return;
  const cls = activeClass || 'active';
  const items = Array.from(container.querySelectorAll(itemSelector || 'button'));
  items.forEach((el) => {
    el.setAttribute('aria-pressed', el.classList.contains(cls) ? 'true' : 'false');
  });
}

/* Accessible name for a form field that has its description only next to it (in a div),
 * or only placeholder text. Does not change the appearance.
 */
function a11yLabel(el, text){
  if (!el || !text) return;
  const clean = String(text).replace(/\s+/g, ' ').trim();
  if (!clean) return;
  if (!el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby')) {
    el.setAttribute('aria-label', clean);
  }
}

/* One-time message for a screen reader (the #a11yAnnouncer live region). */
function a11yAnnounce(text){
  const region = document.getElementById('a11yAnnouncer');
  if (!region || !text) return;
  region.textContent = '';
  setTimeout(() => { region.textContent = String(text); }, 50);
}

/* Clickable element that is NOT a control — give it a role and keyboard support.
 *
 * The "div/span with onclick" pattern tells a screen reader nothing: the user does not
 * know that anything can be done here, and Tab cannot reach it (WCAG 4.1.2
 * name/role/value and 2.1.1 keyboard access).
 *
 * Deliberately ONE helper for the whole panel: handling Enter/Space separately on
 * each element diverges after the first fix (some get only
 * Enter, some get nothing), and that copy drift was exactly the source of the defects.
 */
function a11yAsButton(el, opts){
  if (!el || typeof el.setAttribute !== 'function') return el;
  const options = opts || {};
  el.setAttribute('role', 'button');
  if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
  if (options.label) a11yLabel(el, options.label);
  if (options.expanded !== undefined && options.expanded !== null) {
    el.setAttribute('aria-expanded', options.expanded ? 'true' : 'false');
  }
  if (options.pressed !== undefined && options.pressed !== null) {
    el.setAttribute('aria-pressed', options.pressed ? 'true' : 'false');
  }
  if (el.dataset && el.dataset.a11yKeyActivated === '1') return el;
  if (el.dataset) el.dataset.a11yKeyActivated = '1';
  el.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
    // Space on a surrogate element scrolls the page — this must be prevented,
    // otherwise keyboard activation shifts the view under the user.
    ev.preventDefault();
    ev.stopPropagation();
    if (typeof options.onActivate === 'function') { options.onActivate(ev); return; }
    if (typeof el.click === 'function') el.click();
  });
  return el;
}

/* Row that TAKES the user to another view (conversation, document) — a real link.
 *
 * The "link" role instead of "button" matters here: a screen reader has separate
 * navigation for links (K in NVDA, links list), and the user hears that this is
 * navigation, not an in-place action. Condition: `href` must be an address that
 * REALLY opens that view, otherwise the link is fake — opening in a new tab
 * and the browser context menu must lead to the same destination screen.
 *
 * This also gives us behaviors for free that the app does not need to implement:
 * Ctrl+click / middle button (new tab) and "copy link address".
 */
function a11yAsLink(el, href, opts){
  if (!el || typeof el.setAttribute !== 'function') return el;
  const options = opts || {};
  el.setAttribute('role', 'link');
  if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
  if (href) el.setAttribute('data-href', href);
  if (options.label) a11yLabel(el, options.label);
  if (options.current) el.setAttribute('aria-current', 'page');
  else if (el.hasAttribute('aria-current')) el.removeAttribute('aria-current');
  if (el.dataset && el.dataset.a11yKeyActivated === '1') return el;
  if (el.dataset) el.dataset.a11yKeyActivated = '1';
  el.addEventListener('keydown', (ev) => {
    // A link responds to Enter; Space belongs to a button, so we do not intercept it here,
    // so the behavior matches native <a href>.
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    ev.stopPropagation();
    if (typeof options.onActivate === 'function') { options.onActivate(ev); return; }
    if (typeof el.click === 'function') el.click();
  });
  return el;
}

/* Tab set (tablist) — ONE mechanism for all tab bars.
 *
 * Measured defect (18.08.2026): the Settings > Extensions bar had role="tablist"
 * and three role="tab", but ZERO aria-selected, ZERO aria-controls, and the tablist had no
 * name. The screen reader therefore said "tab Gallery" and DID NOT SAY which one was active —
 * and the active state existed only as a CSS class (extensions-tab-active), which means
 * information available only to a sighted person. WCAG 4.1.2.
 *
 * The neighboring bar (workspace-panel-tabs) was implemented correctly in HTML, so this is
 * drift between TWO COPIES of the same pattern — exactly the class of bug we
 * already fixed for clickable panel elements. Instead of adding the missing
 * attributes in a third place, we introduce a shared helper: every bar gets
 * a name, every tab gets aria-selected/aria-controls, panels get role="tabpanel", and the whole
 * set gets arrow-key navigation with one tab in Tab order (roving tabindex).
 *
 * Arrow keys are part of the tab role CONTRACT: if we tell the screen reader "these are tabs",
 * the user will try arrow keys, and without them gets trapped (ARIA APG: Tabs).
 *
 * The call is IDEMPOTENT — it can be repeated after every switch, because
 * the state follows the provided `activeKey`, and the keyboard listener is attached once.
 */
function a11yTablist(tablist, opts){
  if (!tablist || typeof tablist.querySelectorAll !== 'function') return tablist;
  const options = opts || {};
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  if (!tabs.length) return tablist;
  if (options.label) a11yLabel(tablist, options.label);
  if (!tablist.getAttribute('role')) tablist.setAttribute('role', 'tablist');

  const kluczTabu = (el) => (typeof options.keyOf === 'function' ? options.keyOf(el) : null);
  const active = options.activeKey;

  tabs.forEach((tab) => {
    const key = kluczTabu(tab);
    // When the caller cannot provide a key, we rely on the active-state class —
    // but we NEVER leave aria-selected undeclared.
    const czyAktywny = (key !== null && active !== undefined)
      ? String(key) === String(active)
      : (typeof options.isActive === 'function' ? !!options.isActive(tab) : false);
    tab.setAttribute('aria-selected', czyAktywny ? 'true' : 'false');
    // Roving tabindex: only the active tab is in Tab order, and the set is
    // navigated with arrow keys. Without this, a keyboard user has to pass through
    // EVERY tab to leave the bar.
    tab.setAttribute('tabindex', czyAktywny ? '0' : '-1');
    const panel = (typeof options.panelFor === 'function') ? options.panelFor(tab) : null;
    if (panel) {
      if (!panel.id) panel.id = `a11yTabPanel_${Math.random().toString(36).slice(2, 9)}`;
      tab.setAttribute('aria-controls', panel.id);
      if (!panel.getAttribute('role')) panel.setAttribute('role', 'tabpanel');
      if (!tab.id) tab.id = `a11yTab_${Math.random().toString(36).slice(2, 9)}`;
      // The panel takes its name from the ACTIVE tab. This matters when several
      // tabs switch the contents of ONE container (that is how the
      // Full/Output bar in tool cards works: the "output" mode only hides the arguments
      // inside the same element). If we left the first tab's name there,
      // after moving to "Output" the screen reader would still say "Full" — so the panel
      // would lie about what it shows. With separate panels the behavior is
      // the same as before: each panel is named after its own tab.
      if (czyAktywny || !panel.hasAttribute('aria-labelledby')) {
        panel.setAttribute('aria-labelledby', tab.id);
      }
    }
  });

  if (tablist.dataset && tablist.dataset.a11yTablistKeys === '1') return tablist;
  if (tablist.dataset) tablist.dataset.a11yTablistKeys = '1';
  tablist.addEventListener('keydown', (ev) => {
    const order = Array.from(tablist.querySelectorAll('[role="tab"]'));
    const current = order.indexOf(document.activeElement);
    if (current < 0) return;
    let nextIndex = null;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') nextIndex = (current + 1) % order.length;
    else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') nextIndex = (current - 1 + order.length) % order.length;
    else if (ev.key === 'Home') nextIndex = 0;
    else if (ev.key === 'End') nextIndex = order.length - 1;
    else return;
    ev.preventDefault();
    ev.stopPropagation();
    const target = order[nextIndex];
    if (!target) return;
    // The "automatic activation" pattern: moving with an arrow key switches the panel IMMEDIATELY,
    // because that is how the rest of the tabs in this app work (click = switch).
    target.setAttribute('tabindex', '0');
    if (typeof target.focus === 'function') target.focus();
    if (typeof target.click === 'function') target.click();
  });
  return tablist;
}

/* TREE (working-directory files) - role=tree contract.
 *
 * Michal's report (18.08.2026): in the Files tab the screen reader said
 *   "▸  .cache  ×   ▸  .cloak-venv  ×"
 * that is, arrow character, name, multiplication sign. In his words: "and try to figure out what this
 * is, what it means, and how to use it".
 *
 * Measured missing pieces (all 7 at once): the row is a <div> without a role and without tabindex
 * (for the screen reader it is NOT a control and the keyboard cannot reach it),
 * the arrow is a <span> with only a character (no aria-expanded), the delete button has
 * visible text "×" and only a title (screen readers often skip title), the container
 * has no role=tree, there is no aria-level, so it is impossible to know on which nesting
 * level the row is.
 *
 * Why role=tree instead of a list of buttons: directories COLLAPSE and rows are
 * NESTED. The tree role is the only one that has vocabulary for both of these facts
 * (aria-expanded + aria-level), and screen readers have built-in navigation for it.
 *
 * Focus pattern: ONE tabindex=0 across the whole tree (roving), arrow keys move
 * between rows. Otherwise a tree with a hundred files would require hundreds of
 * Tab presses to get past it.
 */
function a11yTreeRow(row, opts){
  const o = opts || {};
  if (!row || typeof row.setAttribute !== 'function') return row;
  row.setAttribute('role', 'treeitem');
  if (Number(o.level) > 0) row.setAttribute('aria-level', String(Math.floor(o.level)));
  // Directory: we say whether it is expanded. File: we DO NOT set aria-expanded -
  // for a leaf this attribute is false (it suggests that something can be expanded).
  if (o.expandable) row.setAttribute('aria-expanded', o.expanded ? 'true' : 'false');
  else row.removeAttribute('aria-expanded');
  // Accessible name: the file name itself plus its type, so that "folder" is audible
  // even when the icon is invisible to the screen reader.
  if (o.label) a11yLabel(row, o.label);
  row.setAttribute('tabindex', o.focusable ? '0' : '-1');
  // Stable identity for restoring the keyboard position after a rebuild. The
  // index cannot serve here: expanding a directory changes how many rows precede
  // an entry. See a11yTreeRememberFocus / a11yTreeRestoreFocus.
  if (o.path && row.dataset) row.dataset.a11yTreePath = String(o.path);
  return row;
}

/* Tree container: role, name, and Arrow/Home/End navigation.
 * Idempotent - call after every re-render. */
function a11yTree(container, opts){
  const o = opts || {};
  if (!container || typeof container.querySelectorAll !== 'function') return container;
  container.setAttribute('role', 'tree');
  if (o.label) a11yLabel(container, o.label);
  if (container.dataset && container.dataset.a11yTreeBound === '1') return container;
  if (container.dataset) container.dataset.a11yTreeBound = '1';
  if (typeof container.addEventListener !== 'function') return container;

  const rows = () => Array.from(container.querySelectorAll('[role="treeitem"]'));
  const moveFocus = (target) => {
    if (!target) return;
    for (const w of rows()) w.setAttribute('tabindex', w === target ? '0' : '-1');
    if (typeof target.focus === 'function') target.focus();
  };

  container.addEventListener('keydown', (ev) => {
    const key = ev && ev.key;
    if (!key) return;
    const items = rows();
    if (!items.length) return;
    const active = (typeof document !== 'undefined' && document.activeElement) || null;
    let i = items.indexOf(active);
    if (i < 0) i = items.findIndex((w) => w.getAttribute('tabindex') === '0');

    if (key === 'ArrowDown' || key === 'ArrowUp') {
      ev.preventDefault();
      const step = key === 'ArrowDown' ? 1 : -1;
      const next = i < 0 ? 0 : (i + step + items.length) % items.length;
      moveFocus(items[next]);
      return;
    }
    if (key === 'Home' || key === 'End') {
      ev.preventDefault();
      moveFocus(key === 'Home' ? items[0] : items[items.length - 1]);
      return;
    }
    if (i < 0) return;
    const current = items[i];
    const expandable = current.hasAttribute('aria-expanded');
    const expanded = current.getAttribute('aria-expanded') === 'true';

    // Right arrow: expand the directory. If already expanded - move inside.
    if (key === 'ArrowRight') {
      ev.preventDefault();
      if (expandable && !expanded) { if (typeof current.click === 'function') current.click(); }
      else if (items[i + 1]) moveFocus(items[i + 1]);
      return;
    }
    // Left arrow: collapse. If collapsed/file - go to the parent (higher level).
    if (key === 'ArrowLeft') {
      ev.preventDefault();
      if (expandable && expanded) { if (typeof current.click === 'function') current.click(); return; }
      const level = Number(current.getAttribute('aria-level') || 0);
      for (let j = i - 1; j >= 0; j--) {
        if (Number(items[j].getAttribute('aria-level') || 0) < level) { moveFocus(items[j]); return; }
      }
      return;
    }
    if (key === 'Enter' || key === ' ') {
      ev.preventDefault();
      if (typeof current.click === 'function') current.click();
    }
  });
  return container;
}

/* Keep the keyboard position across a full tree rebuild.
 *
 * Raised in review of #7258: toggling a directory calls click(), which triggers
 * renderFileTree(); that replaces every row and hands the single tab stop to the
 * FIRST top-level row. The row the user just toggled is detached, so focus falls
 * to document.body and the next arrow press restarts at the top of the tree.
 * Expanding a directory then teleports the user away from it, silently.
 *
 * Identity is the entry PATH, not the index: expanding changes how many rows
 * precede an entry, so a remembered index points somewhere else after the
 * rebuild. When the remembered path is gone (collapsed away, deleted) we fall
 * back to the first row - a tree with no tab stop is unreachable by keyboard,
 * which would be a worse bug than the one being fixed.
 */
function a11yTreeRememberFocus(container){
  try {
    if (!container || typeof container.querySelectorAll !== 'function') return null;
    const active = (typeof document !== 'undefined' && document.activeElement) || null;
    if (!active) return null;
    const rows = Array.from(container.querySelectorAll('[role="treeitem"]'));
    if (rows.indexOf(active) < 0) return null;   // focus is not in this tree
    const path = (active.dataset && active.dataset.a11yTreePath) || null;
    return path ? {path: path, hadFocus: true} : null;
  } catch (_e) { return null; }
}

function a11yTreeRestoreFocus(container, token){
  try {
    if (!container || typeof container.querySelectorAll !== 'function') return;
    const rows = Array.from(container.querySelectorAll('[role="treeitem"]'));
    if (!rows.length) return;
    let target = null;
    if (token && token.path) {
      target = rows.find((r) => r.dataset && r.dataset.a11yTreePath === token.path) || null;
    }
    // No token, or the entry vanished: leave exactly one tab stop on the first row.
    if (!target) target = rows.find((r) => r.getAttribute('tabindex') === '0') || rows[0];
    for (const r of rows) r.setAttribute('tabindex', r === target ? '0' : '-1');
    // Only take focus when we actually had it before - otherwise a background
    // refresh would steal focus from whatever the user is doing elsewhere.
    if (token && token.hadFocus && typeof target.focus === 'function') target.focus();
  } catch (_e) { /* a rebuild must never fail because of focus bookkeeping */ }
}

/* Arrow-key-driven selection list (combobox + listbox).
 *
 * Measured defect (18.08.2026): /slash command suggestions and
 * working-directory suggestions have full arrow-key navigation, but the selected item is
 * marked ONLY with a CSS class. The screen reader therefore announces nothing while
 * moving through the list — the user hears silence and does not know what Enter will
 * confirm. WCAG 4.1.2.
 *
 * This is another variant of the same bug class (after tabs and source badges),
 * and the repo already has its correct solution for the models list (ui.js, _highlightRow:
 * role="option" + aria-selected + aria-activedescendant on the field). Instead of
 * a fourth copy of that logic, we expose it as a shared helper.
 *
 * The contract is complete — aria-selected alone is not enough, because without
 * role="listbox"/"option" the screen reader does not treat it as a selection list, and without
 * aria-activedescendant it will not announce movement, since focus stays in the text field.
 *
 * Idempotent: call after every selection change.
 */
function a11yActiveDescendantList(field, items, elements, selectedIndex, opts){
  if (!items || !elements || !elements.length) {
    // Collapsed list: the field cannot point to a non-existent element.
    if (field && typeof field.removeAttribute === 'function') {
      field.removeAttribute('aria-activedescendant');
      field.setAttribute('aria-expanded', 'false');
    }
    return;
  }
  const options = opts || {};
  const prefiks = options.idPrefix || 'a11yOpt';
  if (!items.getAttribute('role')) items.setAttribute('role', 'listbox');
  if (options.label) a11yLabel(items, options.label);

  let selected = null;
  for (let i = 0; i < elements.length; i++) {
    const el = elements[i];
    if (!el || typeof el.setAttribute !== 'function') continue;
    if (!el.getAttribute('role')) el.setAttribute('role', 'option');
    if (!el.id) el.id = `${prefiks}_${i}_${Math.random().toString(36).slice(2, 7)}`;
    const isSelected = i === selectedIndex;
    el.setAttribute('aria-selected', isSelected ? 'true' : 'false');
    if (isSelected) selected = el;
  }

  if (!field || typeof field.setAttribute !== 'function') return;
  if (!field.getAttribute('role')) field.setAttribute('role', 'combobox');
  field.setAttribute('aria-expanded', 'true');
  field.setAttribute('aria-autocomplete', 'list');
  if (!items.id) items.id = `${prefiks}_lista_${Math.random().toString(36).slice(2, 7)}`;
  field.setAttribute('aria-controls', items.id);
  if (selected) field.setAttribute('aria-activedescendant', selected.id);
  else field.removeAttribute('aria-activedescendant');
}

if (typeof window !== 'undefined') {
  window.a11yTrapFocus = a11yTrapFocus;
  window.a11ySyncPressedState = a11ySyncPressedState;
  window.a11yLabel = a11yLabel;
  window.a11yAnnounce = a11yAnnounce;
  window.a11yAsButton = a11yAsButton;
  window.a11yAsLink = a11yAsLink;
  window.a11yTablist = a11yTablist;
  window.a11yTree = a11yTree;
  window.a11yTreeRow = a11yTreeRow;
  window.a11yTreeRememberFocus = a11yTreeRememberFocus;
  window.a11yTreeRestoreFocus = a11yTreeRestoreFocus;
  window.a11yActiveDescendantList = a11yActiveDescendantList;
}

/* ── Heading navigation in the conversation transcript ────────────────────────────
 *
 * Problem: in a long conversation, it is not possible to jump quickly between
 * messages. A screen reader already has a built-in tool for this — jump by
 * headings (H in NVDA, 2/3 by level) — but the transcript did not have even a
 * single heading.
 *
 * Solution: EVERY message gets an <h2>, and elements inside an assistant
 * turn (reasoning, tool log) get <h3>. The headings are visually hidden
 * (sr-only class) — the visual layout does not change by a single pixel,
 * because roles are already shown by the icon and label.
 *
 * Why use MutationObserver instead of the rendering functions:
 * messages are created through several paths (steady render, live stream,
 * row reuse from the pool, restoring a turn after switching sessions). Decorating
 * in one place after the fact covers all those paths and cannot diverge
 * from any of them. That is also why we do not touch
 * _setLatestAssistantTurnLandmark — its contract is guarded by a repository test
 * (tests/test_a11y_transcript_landmarks.py): the turn must not contain a heading
 * added THERE or be focusable.
 */

const A11Y_HEAD_MARK = 'a11yHeading';       // dataset marker on our headings
const A11Y_HEAD_DONE = 'a11yHeadingFor';    // signature of what we labelled

/* Elements that do NOT belong to the message and must not enter the snippet:
 * role label (icon + name), time counter, action buttons, our own
 * headings, and the whole activity log. Without this, the live-turn heading
 * sounded like "HHermes Processed 1sProcessed 2s" — that is, the icon letter, the name,
 * and second counters, instead of the first words of the response (measured). */
const A11Y_SNIPPET_SKIP = [
  // Role label and its parts. `.role-icon` and `.msg-role-name` are
  // listed SEPARATELY even though they normally sit inside `.msg-role`: the role icon is
  // the first LETTER of the assistant name ("H"), so if it enters the snippet outside
  // the `.msg-role` container, the heading reads "HHermes" — exactly that symptom
  // was reported by the user. Filtering only the parent assumed a structure;
  // listing the children is robust to any markup layout.
  '.msg-role', '.role-icon', '.msg-role-name',
  '.msg-tps-inline', '.msg-foot', '.msg-actions',
  '.agent-activity-group', '.tool-call-group', '.tool-worklog',
  '.thinking-card', '.msg-files', '[data-a11y-heading]',
];

function _a11ySnippet(row, limit){
  // Prefer the message body; for assistant turns take the rendered blocks but
  // drop the activity log, which is not part of the spoken answer.
  const body = row.querySelector('.msg-body')
    || row.querySelector('.assistant-turn-blocks')
    || row;
  const clone = body.cloneNode(true);
  for (const sel of A11Y_SNIPPET_SKIP) {
    for (const el of Array.from(clone.querySelectorAll(sel))) el.remove();
  }
  const text = (clone.textContent || '').replace(/\s+/g, ' ').trim();
  const max = limit || 70;
  return text.length > max ? text.slice(0, max).replace(/\s+\S*$/, '') + '…' : text;
}

function _a11yRoleLabel(row){
  const role = (row.dataset && row.dataset.role) || '';
  if (role === 'user') {
    return (typeof t === 'function' && t('a11y_turn_you')) || 'You';
  }
  if (role === 'assistant' || row.classList.contains('assistant-turn')) {
    if (typeof assistantDisplayName === 'function') {
      try { return assistantDisplayName(); } catch (_e) { /* fall through */ }
    }
    return 'Hermes';
  }
  return (typeof t === 'function' && t('a11y_turn_system')) || 'System';
}

/* One <h2> per turn: "<n>. <role>: <opening words>".
 * The number and the opening words are what make the heading list usable —
 * a list of twenty identical "Hermes" entries would navigate no better than
 * no headings at all. */
/* Is this turn being created right now.
 *
 * Measured pitfall: the first version checked `.thinking-card:not(.done)`, and
 * reasoning cards in FINISHED turns also do not have the `done` class (5 out of 5
 * in a closed turn). Every turn therefore looked live. Reliable
 * markers are the live-turn identifier, explicit data-live, and the stream cursor —
 * all of them belong to THIS turn. The global run state used to be an extra
 * clue for the last turn and turned out harmful (see the function body).
 */
function _a11yTurnIsLive(row){
  const liveTurn = document.getElementById('liveAssistantTurn');
  if (liveTurn && (liveTurn === row || row.contains(liveTurn) || liveTurn.contains(row))) return true;
  if (row.dataset && row.dataset.live === 'true') return true;
  if (row.querySelector('.stream-cursor, .typing-indicator, .msg-streaming')) return true;
  // We DELIBERATELY do NOT check the global run state (a11yRunIsActive) here as proof
  // that the last turn is live. It was a clue that turned ONE
  // missed end signal into a permanently stuck view: the user reported
  // "the model stops writing, but I see 5. Hermes, working ... and after refresh I get the
  // message". When the run state was not cleared, every later pass of the
  // decorator treated the finished turn as live, so the heading read
  // "working", and _a11yReorderTurn did not reorder the blocks, so the response content
  // stayed AFTER the activity log.
  // The markers above belong to THIS turn and disappear with it, so they cannot
  // drift away from the global state. If none of them is present,
  // the turn is finished — even if the run counter was left open somewhere.
  //
  // EXCEPTION that does NOT suffer from the same problem: a session driven from outside
  // (TUI/Telegram). There is then no live turn in the document — the work happens
  // in another process — and the user has the right to know that something is in progress
  // (report: "the session is alive, I have no information that it is alive"). The difference from the
  // removed clue is fundamental: `_a11yForeignOwnsRun` does NOT depend on
  // a signal that can be missed — the watchdog clears it itself when it measures no
  // growth (A11Y_FOREIGN_DONE_AFTER_MS). The state therefore cannot get stuck.
  // Guard with `typeof`: `_a11yForeignOwnsRun` is declared with `let` LATER
  // in this file (temporal dead zone). In practice this function only runs after
  // the whole script is loaded, but a bare reference would throw ReferenceError if
  // the decorator were ever called earlier — and then the whole render would fail.
  if (typeof _a11yForeignOwnsRun !== 'undefined' && _a11yForeignOwnsRun && !liveTurn) {
    const allRows = document.querySelectorAll('#messages .msg-row.assistant-turn');
    if (allRows.length && allRows[allRows.length - 1] === row) return true;
  }
  return false;
}

/* Turn heading text.
 *
 * Different rules for both sides of the conversation, and this is deliberate:
 *
 * - The USER MESSAGE gets a content snippet. It serves as orientation
 *   "where did I ask about what", and prompts are short, so the snippet does not hurt.
 *
 * - The ASSISTANT RESPONSE gets only the role and time. Truncating a long
 *   response to 70 characters was irritating: the user heard a chopped-up
 *   start of the sentence, and then the same sentence again in the content. The heading is meant to
 *   be an anchor point for navigation, not a summary. The content is read
 *   IMMEDIATELY BELOW the heading (see _a11yReorderTurn).
 */
function _a11yTurnHeadingText(row, ordinal, total){
  const label = _a11yRoleLabel(row);
  const isAssistant = (row.dataset && row.dataset.role === 'assistant')
    || row.classList.contains('assistant-turn');

  // "N of M" only for the FIRST message in the window, not for every one.
  //
  // The global number already says the conversation is long (43 instead of 1), but it does not
  // say HOW MANY are ahead. Appending "of 576" to EVERY heading would be
  // too verbose: when jumping by headings, the screen reader would repeat the same
  // number dozens of times. The user needs it ONCE, when entering the window
  // — after that, the increasing number is enough.
  const numer = (Number(total) > 0 && Number(ordinal) === _a11yTurnOffset + 1)
    ? `${ordinal}/${total}`
    : `${ordinal}`;

  if (isAssistant) {
    // Time from the role-label title attribute ("17.08.2026, 11:15:25").
    const roleEl = row.querySelector('.msg-role');
    const stamp = roleEl ? (roleEl.getAttribute('title') || '') : '';
    const hhmm = (stamp.match(/(\d{1,2}:\d{2})/) || [])[1] || '';
    // A LIVE turn explicitly says that it is in progress. Without this, after jumping to the heading there was
    // no difference between a finished response and one still being created — and that was
    // exactly the user's complaint ("I don't know whether it got stuck").
    const live = _a11yTurnIsLive(row);
    if (live) {
      const working = (typeof t === 'function' && t('a11y_turn_working')) || 'working';
      return `${numer}. ${label}, ${working}`;
    }
    return `${numer}. ${label}${hhmm ? ' ' + hhmm : ''}`;
  }

  const snippet = _a11ySnippet(row);
  return `${numer}. ${label}${snippet ? ': ' + snippet : ''}`;
}

/* Order inside an assistant turn: RESPONSE FIRST, log and buttons afterward.
 *
 * Problem measured in a session with 1152 messages: in the page code the activity
 * log ("Processed") sits BEFORE the response content (indexes 5 vs 346).
 * Jumping to the message heading therefore landed on the log, not on the response —
 * the content had to be reached only afterwards.
 *
 * Solution: .assistant-turn-blocks is a column flex container (measured:
 * display:flex, flex-direction:column), and flexbox allows changing the order
 * with the order property — and, most importantly here, FOR THE SCREEN READER TOO, because
 * browsers set the order in the accessibility tree according to the flex
 * layout. So we do not move nodes in the DOM (which would break row recycling,
 * height measurements, and scroll anchoring), we only assign order.
 *
 * EXCEPTION: a LIVE turn stays unchanged. The log is then the only
 * progress information and must remain at the top; moving it during the
 * response would shift the layout under the user's fingers.
 */
function _a11yReorderTurn(row){
  if (!row.classList.contains('assistant-turn')) return;
  const blocks = row.querySelector('.assistant-turn-blocks');
  if (!blocks) return;

  // Live turn: we do not touch its order. The log is then the only
  // progress information and must stay at the top; moving it during the
  // would shift the layout under the user's fingers.
  const live = _a11yTurnIsLive(row);

  const kids = Array.from(blocks.children);
  const hasProse = kids.some(el => el.classList.contains('assistant-segment')
    && el.getClientRects().length > 0);
  if (live || !hasProse) {
    // revert any earlier reordering
    for (const el of kids) {
      if (el.dataset && el.dataset.a11yOrdered) {
        el.style.order = '';
        delete el.dataset.a11yOrdered;
      }
    }
    return;
  }

  for (const el of kids) {
    const isLog = el.classList.contains('agent-activity-group')
      || el.classList.contains('tool-call-group')
      || el.classList.contains('tool-worklog')
      || el.classList.contains('thinking-card');
    if (!isLog) continue;
    // MEASURED: CSS `order` alone is NOT enough. After setting order=2/1, the visual layout
    // changed correctly (content above the log), but in the accessibility tree
    // the log was STILL before the content (positions 1731 vs 1734) —
    // and the screen reader reads that tree, not the visual layout.
    // That is why we move the node to the end of the container. We do this only for
    // FINISHED turns, so we do not interfere with streaming.
    if (el.nextElementSibling) blocks.appendChild(el);
    if (el.style.order) el.style.order = '';
    if (el.dataset) el.dataset.a11yOrdered = '1';
  }
}

function _a11yEnsureTurnHeading(row, ordinal, total){
  const text = _a11yTurnHeadingText(row, ordinal, total);

  let h = row.firstElementChild;
  if (!(h && h.dataset && h.dataset[A11Y_HEAD_MARK] === 'turn')) {
    h = document.createElement('h2');
    h.className = 'sr-only';
    h.dataset[A11Y_HEAD_MARK] = 'turn';
    row.insertBefore(h, row.firstChild);
  }
  if (h.dataset[A11Y_HEAD_DONE] !== text) {
    h.textContent = text;
    h.dataset[A11Y_HEAD_DONE] = text;
  }
}

/* <h3> for the collapsible blocks inside an assistant turn.  These are the
 * parts users skip past most often, so being able to jump over them at level 3
 * (and land on the next turn at level 2) is the point.
 *
 * Measured lesson: the first attempt put the <h3> inside .thinking-card, but
 * those cards live inside the COLLAPSED activity log (display:none), so no
 * screen reader ever saw them — NVDA's heading list showed h2 only.  The
 * heading has to sit on the element the user can actually reach: the visible
 * summary button that expands the log.  We label the wrapper, not the card. */
const A11Y_SUBHEADS = [
  // Measured against the live DOM, not guessed from class names elsewhere in
  // the code: the visible wrapper around the activity summary button is
  // .agent-activity-group (.tool-call-group is only the collapsed body).
  ['.agent-activity-group', 'a11y_block_activity', 'Tool activity'],
  ['.tool-call-group', 'a11y_block_activity', 'Tool activity'],
  ['.tool-worklog', 'a11y_block_worklog', 'Work log'],
];

/* Heading text for a collapsible block: prefer the block's own summary text
 * ("Processed", "Reading files", …) so the heading list is informative rather
 * than nine identical entries. */
function _a11yBlockLabel(block, fallbackKey, fallbackText){
  const summary = block.querySelector(
    '.tool-call-group-summary, .tool-worklog-summary, .tool-group-head, [aria-expanded]');
  const own = summary ? (summary.textContent || '').replace(/\s+/g, ' ').trim() : '';
  if (own) return own.length > 60 ? own.slice(0, 60).replace(/\s+\S*$/, '') + '…' : own;
  return (typeof t === 'function' && t(fallbackKey)) || fallbackText;
}

function _a11yEnsureBlockHeadings(row){
  for (const [sel, key, fallback] of A11Y_SUBHEADS) {
    for (const block of Array.from(row.querySelectorAll(sel))) {
      // Only decorate blocks the user can actually reach.  A heading buried in
      // a display:none subtree is invisible to assistive tech and just noise.
      if (!block.getClientRects().length) continue;
      const text = _a11yBlockLabel(block, key, fallback);
      let h = block.firstElementChild;
      if (!(h && h.dataset && h.dataset[A11Y_HEAD_MARK] === 'block')) {
        h = document.createElement('h3');
        h.className = 'sr-only';
        h.dataset[A11Y_HEAD_MARK] = 'block';
        block.insertBefore(h, block.firstChild);
      }
      if (h.dataset[A11Y_HEAD_DONE] !== text) {
        h.textContent = text;
        h.dataset[A11Y_HEAD_DONE] = text;
      }
    }
  }
}

let _a11yHeadingObserver = null;
let _a11yHeadingPending = false;

/* Numbering offset: how many messages are HIDDEN above the loaded window.
 *
 * Michal's report (18.08.2026): "in a long session the numbers are 1, 2, 3, even though
 * the session has dozens of messages; I'd prefer them to be counted globally -
 * the user should have a clear sense that the session is growing".
 *
 * WebUI loads only the tail of the conversation (older content is loaded backward with a button), and
 * the numbering counted from the first row IN THE DOM. The same message therefore had
 * a different number depending on how much of the window was loaded, and "1." at the 576th
 * message said NOTHING about its place in the conversation.
 *
 * The server now provides _visible_turns_before / _visible_turns_total in the same
 * space as the visible rows (_messages_offset alone would not be enough:
 * measured 978 storage rows for 100 messages in the window). */
let _a11yTurnOffset = 0;
let _a11yTurnTotal = 0;

/* Called after every load/backfill of the conversation window. */
function a11ySetTurnNumbering(before, total){
  const b = Number(before);
  const t = Number(total);
  const nowyOffset = Number.isFinite(b) && b >= 0 ? Math.floor(b) : 0;
  const nowyTotal = Number.isFinite(t) && t >= 0 ? Math.floor(t) : 0;
  const zmiana = (nowyOffset !== _a11yTurnOffset) || (nowyTotal !== _a11yTurnTotal);
  _a11yTurnOffset = nowyOffset;
  _a11yTurnTotal = nowyTotal;
  // Numbers already live in the heading texts, so after the offset changes they must be
  // recomputed — otherwise loading older messages would leave the old ones behind.
  if (zmiana) { try { a11yDecorateConversationHeadings(); } catch (_e) {} }
  return zmiana;
}

function a11yTurnNumberingOffset(){ return _a11yTurnOffset; }

function a11yDecorateConversationHeadings(container){
  const root = container || document.getElementById('messages');
  if (!root) return 0;
  // Visual order is DOM order here, so a straight walk numbers turns the way
  // they are read.  Live/streaming turns are included: the heading text is
  // refreshed on every pass, so a turn that starts empty gains its opening
  // words as soon as they arrive.
  const rows = Array.from(root.querySelectorAll('.msg-row'));
  let n = 0;
  for (const row of rows) {
    if (row.classList.contains('msg-row-spacer')) continue;
    n += 1;
    try {
      // GLOBAL number: position in the whole conversation, not in the loaded window.
      _a11yEnsureTurnHeading(row, _a11yTurnOffset + n, _a11yTurnTotal);
      _a11yReorderTurn(row);
      _a11yEnsureBlockHeadings(row);
    } catch (_e) { /* never let decoration break rendering */ }
  }
  return n;
}

function a11yInstallConversationHeadings(){
  const root = document.getElementById('messages');
  if (!root || _a11yHeadingObserver) return;
  const run = () => {
    _a11yHeadingPending = false;
    // Detach while we mutate, or our own <h2> insertions retrigger the observer.
    _a11yHeadingObserver.disconnect();
    try { a11yDecorateConversationHeadings(root); }
    finally {
      _a11yHeadingObserver.observe(root, {childList: true, subtree: true, characterData: true});
    }
  };
  _a11yHeadingObserver = new MutationObserver(() => {
    if (_a11yHeadingPending) return;
    _a11yHeadingPending = true;
    // Coalesce a streaming burst into one pass; 250ms keeps the heading list
    // fresh without re-walking the transcript on every token.
    setTimeout(run, 250);
  });
  _a11yHeadingObserver.observe(root, {childList: true, subtree: true, characterData: true});
  a11yDecorateConversationHeadings(root);
}

if (typeof window !== 'undefined') {
  window.a11yDecorateConversationHeadings = a11yDecorateConversationHeadings;
  window.a11ySetTurnNumbering = a11ySetTurnNumbering;
  window.a11yTurnNumberingOffset = a11yTurnNumberingOffset;
  window.a11yInstallConversationHeadings = a11yInstallConversationHeadings;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', a11yInstallConversationHeadings);
  } else {
    setTimeout(a11yInstallConversationHeadings, 0);
  }
}

/* ── Accessible "Hermes is working" indicator ────────────────────────────────────
 *
 * User report: "the session looks like it is hung and I don't know whether Hermes
 * got stuck or something crashed, I know nothing".
 *
 * Measured cause: the app KNOWS that work is in progress (the send button is
 * disabled, the "Thinking" tab is visible), but none of these signals
 * reaches the screen reader:
 *   - #liveRunStatus     has no aria-live, and in compact log mode it is
 *                        hidden completely (el.hidden=true),
 *   - Stop button        is not in the document,
 *   - there is no typing indicator,
 *   - button disabling   is visual only.
 * Effect: silence indistinguishable from a failure. WCAG 4.1.3.
 *
 * Solution in three layers, deliberately speech-sparing:
 *  1. a ONE-TIME announcement of "Hermes is working" at the start and "done" at the end
 *     (#a11yAnnouncer live region, polite mode),
 *  2. a QUIET state to inspect on demand: role=status with aria-live=off, so
 *     the screen reader does NOT read it by itself, but the user can navigate there and
 *     read the current activity and elapsed time,
 *  3. the LIVE turn HEADING says ", working" so that after jumping it is immediately
 *     clear that the response is not finished yet.
 *
 * What we deliberately do NOT do: we do not enable aria-live on the content stream or
 * on the log. Flooding the screen reader with announcements every few hundred milliseconds is
 * worse than silence, and the contract "the transcript is not a live region"
 * is guarded by tests/test_a11y_transcript_landmarks.py.
 */

let _a11yRunActive = false;
let _a11yRunStartedAt = null;
let _a11yRunPollTimer = null;

function _a11yRunStatusHost(){
  let el = document.getElementById('a11yRunStatus');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'a11yRunStatus';
  el.className = 'sr-only';
  el.setAttribute('role', 'status');
  // aria-live="off": the screen reader does NOT read this by itself. The user goes here when they want to
  // know what is happening — without being flooded by announcements.
  el.setAttribute('aria-live', 'off');
  const anchor = document.getElementById('a11yAnnouncer');
  if (anchor && anchor.parentElement) anchor.parentElement.insertBefore(el, anchor.nextSibling);
  else document.body.appendChild(el);
  return el;
}

/* Name of the current activity, read from what the product already shows on screen
 * (reasoning card / log) — so we do not invent our own state vocabulary. */
function _a11yCurrentActivity(){
  const live = document.getElementById('liveAssistantTurn');
  const scope = live || document.getElementById('messages');
  if (!scope) return '';
  const card = scope.querySelector('.thinking-card, .agent-activity-group, .tool-call-group');
  if (!card) return '';
  const head = card.querySelector(
    '.thinking-card-header, .tool-call-group-summary, .tool-worklog-summary, [aria-expanded]');
  const raw = (head ? head.textContent : card.textContent) || '';
  const text = raw.replace(/\s+/g, ' ').trim();
  return text.length > 80 ? text.slice(0, 80).replace(/\s+\S*$/, '') + '…' : text;
}

function _a11yRunElapsedText(){
  if (!_a11yRunStartedAt) return '';
  const s = Math.max(0, Math.round((Date.now() - _a11yRunStartedAt) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${String(s % 60).padStart(2, '0')} s`;
}

/* Silence threshold: after this many milliseconds with NO growth at all, we treat the model as
 * temporarily doing nothing. 12 s, because the counter refreshes every 5 s — a shorter threshold
 * would make "Idle" flicker between ordinary stream chunks. */
const A11Y_RUN_IDLE_AFTER_MS = 12000;
let _a11yRunLastFingerprint = null;
let _a11yRunLastChangeAt = null;

/* Run PROGRESS fingerprint: response prose length + current activity text.
 * Fingerprint change = something grew. No change for A11Y_RUN_IDLE_AFTER_MS =
 * temporary silence.
 *
 * Why a fingerprint instead of "is the activity card on screen": the card STAYS on
 * screen even when the model is silent (measured — the probe showed
 * "Processed 0s" and the status stubbornly reported work even though nothing was growing).
 * The presence of an element is not proof of progress; CHANGE is proof. */
function _a11yRunProgressFingerprint(){
  const live = document.getElementById('liveAssistantTurn');
  const prose = live ? ((live.querySelector('.assistant-segment') || {}).textContent || '') : '';
  return `${prose.trim().length}|${_a11yCurrentActivity()}`;
}

function _a11yRunSilenceMs(){
  const current = Date.now();
  const odcisk = _a11yRunProgressFingerprint();
  if (odcisk !== _a11yRunLastFingerprint) {
    _a11yRunLastFingerprint = odcisk;
    _a11yRunLastChangeAt = current;
    return 0;
  }
  if (_a11yRunLastChangeAt === null) {
    _a11yRunLastChangeAt = current;
    return 0;
  }
  return current - _a11yRunLastChangeAt;
}

/* The silence-measurement baseline BELONGS TO THE RUN, not to the page.
 *
 * Measured defect (18.08.2026): the quiet status showed "Idle — 0 s" — a message
 * INTERNALLY CONTRADICTORY, because the run timer said 0 s (the run had just started), while the word
 * "Idle" requires A11Y_RUN_IDLE_AFTER_MS = 12 s WITHOUT a fingerprint change. Both cannot
 * be true at the same time.
 *
 * CAUSE: `_a11yRunLastFingerprint` / `_a11yRunLastChangeAt` are module-level and
 * reset ONLY when the fingerprint CHANGES. When the user simply looks at an
 * open conversation, the fingerprint stands still (for example "0|") and the marker ages forever.
 * A new run therefore inherited silence from before itself, and the FIRST state refresh
 * crossed the threshold -> the screen reader user got "Idle" for work that
 * had just STARTED.
 *
 * This is the same bug class that we already fixed twice in this file: state
 * measured globally even though it describes a property of ONE run. Silence during a run
 * can be counted at the earliest from the moment the run started — that is why
 * the baseline is reset by ONE shared helper called on EVERY run-boundary transition
 * (start and finish), not a copy of the condition in every place that turns the state on. */
function _a11yResetSilenceBaseline(){
  _a11yRunLastFingerprint = _a11yRunProgressFingerprint();
  _a11yRunLastChangeAt = Date.now();
}

function _a11yRefreshRunStatus(){
  if (!_a11yRunActive) return;
  const el = _a11yRunStatusHost();
  const label = (typeof t === 'function' && t('a11y_run_working')) || 'Hermes is working';
  const act = _a11yCurrentActivity();
  const elapsed = _a11yRunElapsedText();
  // The run is still active, but NOTHING has grown for a while -> this is a PAUSE during the run,
  // not active work. Then we say "Idle", in line with the user's distinction:
  // empty = finished, "Idle" = temporary silence when something is still about to
  // appear soon. We measure NO CHANGE, not the absence of an element on screen.
  if (_a11yRunSilenceMs() >= A11Y_RUN_IDLE_AFTER_MS) { a11yRunIdlePause(); return; }
  const text = `${label}${elapsed ? ' — ' + elapsed : ''}${act ? ' — ' + act : ''}`;
  if (el.textContent !== text) el.textContent = text;
}

/* RUN OUTSIDE THIS BROWSER (CLI/TUI, another tab, gateway).
 *
 * Michal's report (18.08.2026): the same session open in the terminal and in WebUI.
 * In the terminal it is clear that work is in progress; in the browser it looks as if Hermes
 * had finished. Measured: /api/session returned is_streaming=false and
 * active_stream_id=null FOR A SESSION IN WHICH THE AGENT WAS WRITING AT THAT MOMENT - the server tracks
 * only its own streams, and a turn from CLI is invisible to it.
 *
 * Our run state (a11yRunStarted/Finished) is CORRECTLY idle here: this tab
 * did not send anything. What was missing was INFORMATION that someone else is working. The server provides
 * it now in last_activity_at / last_activity_description (the agent writes them itself:
 * agent: "receiving stream response", "executing tool: terminal",
 * "terminal command running (60s elapsed)").
 *
 * Why the threshold is so loose: the measured distribution of refreshes for this signal during
 * real work showed gaps up to ~58 s (the signal updates when the STAGE changes,
 * not every second). A shorter threshold would make the message FLICKER during
 * a single long model call - and a flickering state is worse for the screen reader user
 * than no state. Therefore 90 s: with margin above the longest
 * measured gap.
 *
 * This signal is only a SUPPLEMENT: when this tab itself owns the run, priority
 * goes to the local state (more precise, refreshed every 5 s). */
const A11Y_FOREIGN_RUN_FRESH_MS = 90000;
let _a11yForeignRunActive = false;

/* Do the session data say that SOMEONE ELSE is working right now.
 * Returns an activity description or '' (no foreign run). */
function a11yForeignRunActivity(sessionData){
  if (!sessionData || typeof sessionData !== 'object') return '';
  // A finished session is not working, even if the marker is fresh.
  if (sessionData.ended_at) return '';
  const timestamp = Number(sessionData.last_activity_at || 0);
  if (!timestamp) return '';
  // The marker is in epoch seconds (that is how the agent writes it).
  const ageMs = Date.now() - timestamp * 1000;
  if (!(ageMs >= 0) || ageMs > A11Y_FOREIGN_RUN_FRESH_MS) return '';
  const description = String(sessionData.last_activity_description || '').replace(/\s+/g, ' ').trim();
  // Without a description we do not guess: "something is happening" without content is noise.
  if (!description) return '';
  return description.length > 80 ? description.slice(0, 80).replace(/\s+\S*$/, '') + '…' : description;
}

/* Called after every session-data refresh. Idempotent.
 *
 * NOTE: this is NOT a second mechanism next to the watchdog below. The watchdog decides
 * WHETHER a foreign run is active (based on marker growth), and this function adds the
 * ACTIVITY to the quiet state when the run does not belong to this tab. */
function a11ySyncForeignRunState(sessionData){
  const description = a11yForeignRunActivity(sessionData);
  // A run driven by THIS tab is more precise - we do not override it.
  if (_a11yRunActive) { _a11yForeignRunActive = false; return false; }
  const el = _a11yRunStatusHost();
  if (description) {
    const label = (typeof t === 'function' && t('a11y_run_working_elsewhere'))
      || 'Hermes is working in another session';
    const text = `${label} — ${description}`;
    if (el.textContent !== text) el.textContent = text;
    if (!_a11yForeignRunActive) {
      _a11yForeignRunActive = true;
      // One time, polite mode: the user should know that they are not looking at a
      // finished conversation. Later refreshes are QUIET.
      if (typeof a11yAnnounce === 'function') a11yAnnounce(label);
    }
    return true;
  }
  if (_a11yForeignRunActive) {
    _a11yForeignRunActive = false;
    if (el.textContent) el.textContent = '';
  }
  return false;
}

function a11yRunStarted(){
  if (_a11yRunActive) return;
  _a11yRunActive = true;
  _a11yRunStartedAt = Date.now();
  // Silence is counted FROM THIS MOMENT. Without this, a new run would inherit the marker
  // from before itself and the first refresh would print "Idle — 0 s".
  _a11yResetSilenceBaseline();
  _a11yRefreshRunStatus();
  if (typeof a11yAnnounce === 'function') {
    a11yAnnounce((typeof t === 'function' && t('a11y_run_started')) || 'Hermes is working');
  }
  if (_a11yRunPollTimer) clearInterval(_a11yRunPollTimer);
  // 5 s: it literally only refreshes the QUIET status text, it announces nothing.
  _a11yRunPollTimer = setInterval(_a11yRefreshRunStatus, 5000);
  try { a11yDecorateConversationHeadings(); } catch (_e) {}
}

function a11yRunFinished(){
  if (_a11yRunPollTimer) { clearInterval(_a11yRunPollTimer); _a11yRunPollTimer = null; }
  if (!_a11yRunActive) return;
  _a11yRunActive = false;
  _a11yRunStartedAt = null;
  // Work FINISHED -> the field stays EMPTY, not "Idle".
  // User decision (screen reader): "if it really does nothing, then
  // let it be empty; if we are waiting and something is still about to appear, and
  // the model is temporarily doing nothing, then Idle".
  // So the word "Idle" means a PAUSE DURING work, not the end of the turn —
  // otherwise after every response the user would find a misleading message there
  // suggesting that something was still happening.
  const el = document.getElementById('a11yRunStatus');
  if (el) el.textContent = '';
  // We also release the silence baseline: it belonged to THIS run. A leftover
  // marker is exactly what caused "Idle — 0 s" for the next run.
  _a11yResetSilenceBaseline();
  try { a11yDecorateConversationHeadings(); } catch (_e) {}
}

/* Pause DURING work: the model is temporarily doing nothing, but the run is still active and soon
 * another stage will appear. Here "Idle" is appropriate — it tells the user there is no
 * failure, only silence in the middle of the run. Called only for an ACTIVE run; after it
 * finishes, a11yRunFinished() clears the field. */
function a11yRunIdlePause(){
  if (!_a11yRunActive) return;
  const el = _a11yRunStatusHost();
  const label = (typeof t === 'function' && t('a11y_run_idle')) || 'Idle';
  const elapsed = _a11yRunElapsedText();
  const text = `${label}${elapsed ? ' — ' + elapsed : ''}`;
  if (el.textContent !== text) el.textContent = text;
}

function a11yRunIsActive(){ return _a11yRunActive; }

/* ── A foreign session that is STILL WORKING ─────────────────────────────────────
 *
 * User report: "if I open a session running in webui that
 * I started somewhere else, then if something is happening there, I also want to
 * know about it and I want the timer running - the session is alive, I have no information that it is alive".
 *
 * MEASURED CAUSE: a session from TUI/Telegram has neither `active_stream_id` nor
 * `is_streaming` — webui sets these fields ONLY for turns it started itself.
 * For session 20260817_192840_e7fa2f the server returned active_stream_id=null,
 * is_streaming=false, even though in state.db the last message had a timestamp
 * 4 SECONDS earlier (work was active in that second). None of the four information
 * channels was therefore on: indicator hidden, quiet status not created,
 * run state off, heading without "working".
 *
 * The RELIABLE SIGNAL is `last_message_at` from /api/sessions — the only field that
 * GROWS regardless of who owns the turn. We check GROWTH, not
 * the presence of a flag: growth is proof of work, a flag is only a declaration of the
 * stream owner.
 */
const A11Y_FOREIGN_POLL_MS = 5000;
/* A second growth must arrive in this window to treat the work as STILL IN PROGRESS.
 * One growth is also the natural end of a turn (the last message arrives),
 * so without confirmation we would light up "working" after EVERY response. */
const A11Y_FOREIGN_CONFIRM_MS = 12000;
/* After this many ms without growth we treat the foreign turn as finished. Short, because this is
 * the time for which the user sees "working" already AFTER the work finished —
 * and that was exactly the report. The silence threshold (A11Y_RUN_IDLE_AFTER_MS = 12 s)
 * is shorter, so we still manage to show "Idle" before clearing it. */
const A11Y_FOREIGN_DONE_AFTER_MS = 20000;
let _a11yForeignTimer = null;
let _a11yForeignSid = null;
let _a11yForeignLastStamp = null;
let _a11yForeignLastGrowthAt = null;
let _a11yForeignOwnsRun = false;

function _a11ySidFromLocation(){
  const m = String(location.pathname || '').match(/\/session\/([^/?#]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

/* Does THIS tab own the turn? Then we do not touch the run state — the owner
 * is the regular streaming path (showLiveRunStatus/hideLiveRunStatus). */
function _a11yThisTabOwnsTurn(){
  try {
    if (typeof S === 'undefined' || !S) return false;
    return !!(S.busy || S.activeStreamId || (S.session && S.session.active_stream_id));
  } catch (_e) { return false; }
}

async function _a11yForeignPoll(){
  const sid = _a11ySidFromLocation();
  if (sid !== _a11yForeignSid) {
    // Conversation switched — measurement starts over.
    _a11yForeignSid = sid;
    _a11yForeignLastStamp = null;
    _a11yForeignLastGrowthAt = null;
    // We clear the run state UNCONDITIONALLY, not only when it belonged to the watchdog.
    // Measured symptom (17.08.2026): after clicking another conversation, the quiet status
    // still said "Hermes is working — 5 s", because the state had been turned on by ANOTHER path
    // (the tab's own turn), and the condition on _a11yForeignOwnsRun did not touch it. This is the same
    // bug class that we fixed in hideLiveRunStatus: state assigned to
    // ONE owner stays on when someone else is responsible for clearing it. Work from
    // the previous conversation does not apply to the one the user has just opened.
    _a11yForeignOwnsRun = false;
    if (typeof a11yRunIsActive === 'function' && a11yRunIsActive()) a11yRunFinished();
  }
  if (!sid) return;
  if (_a11yThisTabOwnsTurn()) {
    // THIS tab owns the turn — the regular stream path owns the state.
    // We reset the measurement baseline so that after the tab's own turn FINISHES the watchdog does not
    // see "growth" relative to the marker from before the turn and does not turn the state
    // on again. After the reset, the first read only establishes the baseline (it does not turn it on).
    _a11yForeignLastStamp = null;
    _a11yForeignLastGrowthAt = null;
    return;
  }
  let stamp = null;
  let sessionData = null;
  try {
    const r = await fetch(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`,
                          {credentials: 'same-origin'});
    if (!r.ok) return;
    const d = await r.json();
    // OWNERSHIP RE-CHECK AFTER THE AWAIT. The guard at the top of this function
    // runs BEFORE the request, so a conversation switch while the request is in
    // flight leaves us holding the PREVIOUS session's data. Using it would set
    // the new conversation's measurement baseline from a foreign session and
    // could announce "Hermes is working" in a conversation that is idle - the
    // inverse of the defect this watchdog exists to fix, and worse for a screen
    // reader user than silence, because it is a spoken claim that is false.
    // Raised in review of #7258. See tests/test_a11y_foreign_poll_ownership.py,
    // which drives the race explicitly (the test fails without these two lines).
    if (sid !== _a11ySidFromLocation() || sid !== _a11yForeignSid) return;
    const s = (d && d.session) || d || {};
    sessionData = s;
    // Two independent proofs of progress, taken TOGETHER.
    //
    // Measured defect (18.08.2026, Michal's report: "in the terminal I can see that
    // something is happening, in WebUI it looks as if Hermes finished"): a CLI turn can
    // work for DOZENS OF SECONDS without a single new message — sampling
    // 22 times every 4 s showed the counter stuck at 1442 for the full 88 s, even though the agent
    // was working. `last_message_at` alone therefore creates dead windows in which the watchdog
    // clears the state in the middle of work.
    //
    // `last_activity_at` is written by THE AGENT ITSELF at every stage change (new
    // tool call, new stream), so it ticks even when nothing has yet
    // reached the transcript storage. The maximum of the two is monotonic, so the whole
    // logic of "growth = progress" below stays unchanged.
    stamp = Math.max(
      Number(s.last_message_at || s.updated_at || 0) || 0,
      Number(s.last_activity_at || 0) || 0,
    ) || null;
  } catch (_e) { return; }   // lack of network is not proof that the work is finished
  if (stamp === null) return;
  const current = Date.now();
  if (_a11yForeignLastStamp === null) {
    // The FIRST READ ONLY SETS THE BASELINE — it NEVER turns the state on.
    //
    // There used to be a heuristic here: "if the last message is fresher than 30 s, then
    // the session is almost certainly working" and that was WRONG, as reported by the user:
    // "I am in the session where you just answered me and I have Hermes is working,
    // but it is obviously no longer working". Right after a turn finishes, last_message_at
    // IS fresh — because the response just arrived — so the condition turned the state on
    // at exactly the moment when the work had finished.
    //
    // FRESHNESS IS NOT PROOF OF CONTINUATION. The only proof is GROWTH between
    // two reads: a marker that did NOT change means "nothing arrived",
    // regardless of how fresh it is. The cost is up to 5 s of delay when entering a
    // still-running foreign session — deliberately, because a moment of silence is
    // much less harmful than a message about work that is not happening.
    _a11yForeignLastStamp = stamp;
    _a11yForeignLastGrowthAt = current;
    return;
  }
  if (stamp > _a11yForeignLastStamp) {
    // Growth. NOTE: ONE growth does NOT prove that work is STILL IN PROGRESS — it proves that
    // SOMETHING arrived. A finished turn also ends with a growth event (the last
    // message arrives), so turning the state on after the first growth produced "working"
    // for the entire A11Y_FOREIGN_DONE_AFTER_MS after EVERY finished response.
    // That is why we require a SECOND growth in a short window: work in progress yields
    // messages one after another, while a finished turn has exactly one.
    const previous = _a11yForeignLastGrowthAt;
    _a11yForeignLastStamp = stamp;
    _a11yForeignLastGrowthAt = current;
    if (_a11yRunActive) {
      if (_a11yForeignOwnsRun) _a11yRefreshRunStatus();
      return;
    }
    const gap = previous ? (current - previous) : Infinity;
    if (gap <= A11Y_FOREIGN_CONFIRM_MS) {
      _a11yForeignOwnsRun = true;
      a11yRunStarted();
    }
    // The activity provided by the agent ("executing tool: terminal") is
    // more precise than anything that can be read from the DOM of a foreign session —
    // this tab does not render its live turn.
    a11ySyncForeignRunState(sessionData);
    return;
  }
  // no growth
  if (_a11yForeignOwnsRun) {
    if (current - (_a11yForeignLastGrowthAt || current) >= A11Y_FOREIGN_DONE_AFTER_MS) {
      _a11yForeignOwnsRun = false;
      a11yRunFinished();
      a11ySyncForeignRunState(sessionData);
    } else {
      _a11yRefreshRunStatus();   // after the silence threshold it will move to "Idle" on its own
    }
  } else {
    // The state does not belong to this tab and there is no growth: if the agent still
    // reports fresh activity (CLI can stay silent for dozens of seconds),
    // we show THAT instead of pretending that the conversation has finished.
    a11ySyncForeignRunState(sessionData);
  }
}

function a11yWatchForeignRun(){
  if (_a11yForeignTimer) return;
  _a11yForeignSid = _a11ySidFromLocation();
  _a11yForeignTimer = setInterval(() => { void _a11yForeignPoll(); }, A11Y_FOREIGN_POLL_MS);
  void _a11yForeignPoll();
}

if (typeof window !== 'undefined') {
  window.a11yWatchForeignRun = a11yWatchForeignRun;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => a11yWatchForeignRun());
    } else {
      a11yWatchForeignRun();
    }
  }
}

if (typeof window !== 'undefined') {
  window.a11yRunStarted = a11yRunStarted;
  window.a11yRunFinished = a11yRunFinished;
  window.a11yRunIdlePause = a11yRunIdlePause;
  window.a11yForeignRunActivity = a11yForeignRunActivity;
  window.a11ySyncForeignRunState = a11ySyncForeignRunState;
  window.a11yRunIsActive = a11yRunIsActive;
}
