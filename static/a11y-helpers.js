/* Shared accessibility (a11y) helpers.
 *
 * Small, dependency-free functions that give existing widgets the ARIA contract
 * and keyboard behaviour a screen reader expects, without changing how they
 * look or how mouse and touch users operate them.
 */

/* Accessible name for an element whose visible text is missing or misleading.
 * Never overrides a name the markup already provides.
 */
function a11yLabel(el, text){
  if (!el || !text) return;
  const clean = String(text).replace(/\s+/g, ' ').trim();
  if (!clean) return;
  if (!el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby')) {
    el.setAttribute('aria-label', clean);
  }
}

/* TREE (workspace files) - role=tree contract.
 *
 * Without it a screen reader read the Files tab as "▸ .cache × ▸ .cloak-venv ×":
 * rows were <div>s with no role and no tabindex (not a control, unreachable by
 * keyboard), the disclosure arrow was a bare ▸/▾ glyph with no aria-expanded,
 * the delete button was named "×", the container had no role and nothing
 * conveyed the nesting depth.
 *
 * Why role=tree instead of a list of buttons: directories COLLAPSE and rows are
 * NESTED. The tree role is the only one with vocabulary for both facts
 * (aria-expanded + aria-level), and screen readers have built-in navigation
 * for it.
 *
 * Focus pattern: ONE tabindex=0 across the whole tree (roving), arrow keys move
 * between rows. Otherwise a tree with a hundred files would cost a hundred Tab
 * presses to get past. Every row starts at tabindex=-1; a11yTreeRestoreFocus()
 * places the single tab stop after each render.
 */
function a11yTreeRow(row, opts){
  const o = opts || {};
  if (!row || typeof row.setAttribute !== 'function') return row;
  row.setAttribute('role', 'treeitem');
  if (Number(o.level) > 0) row.setAttribute('aria-level', String(Math.floor(o.level)));
  // Directory: say whether it is expanded. File: NO aria-expanded - on a leaf
  // the attribute would suggest that something can be expanded.
  if (o.expandable) row.setAttribute('aria-expanded', o.expanded ? 'true' : 'false');
  else row.removeAttribute('aria-expanded');
  if (o.disabled) row.setAttribute('aria-disabled', 'true');
  // Accessible name: the entry name plus its kind, so "folder" is audible even
  // though the icon is hidden from the screen reader.
  if (o.label) a11yLabel(row, o.label);
  if (o.description) row.setAttribute('aria-description', String(o.description));
  row.setAttribute('tabindex', '-1');
  // Stable identity for restoring the keyboard position after a rebuild. The
  // index cannot serve here: expanding a directory changes how many rows
  // precede an entry. See a11yTreeRememberFocus / a11yTreeRestoreFocus.
  if (o.path && row.dataset) row.dataset.a11yTreePath = String(o.path);
  return row;
}

/* Tree container: role, name, and Arrow/Home/End/Enter/Space navigation.
 * Idempotent - call after every render that leaves rows in the container.
 */
function a11yTree(container, opts){
  const o = opts || {};
  if (!container || typeof container.querySelectorAll !== 'function') return container;
  container.setAttribute('role', 'tree');
  // The container persists across renders, so set the name on every render
  // (after a language switch the next render renames it).
  if (o.label) container.setAttribute('aria-label', String(o.label));
  // The tree's only Tab stop is its roving row. A scrolling container is a
  // Tab stop of its own in Firefox (and in Chromium when nothing inside is
  // focusable), so the container opts out.
  container.setAttribute('tabindex', '-1');
  if (container.dataset && container.dataset.a11yTreeBound === '1') return container;
  if (container.dataset) container.dataset.a11yTreeBound = '1';
  if (typeof container.addEventListener !== 'function') return container;

  const rows = () => Array.from(container.querySelectorAll('[role="treeitem"]'));
  const moveFocus = (target) => {
    if (target && typeof target.focus === 'function') target.focus();
  };
  // Toggling a folder re-renders the tree only after its listing loads. Until
  // then the row still reads collapsed, so a second Right (or an auto-repeated
  // key) would toggle it straight back; the re-render replaces the row and
  // with it this mark.
  const toggle = (row) => {
    if (row.dataset && row.dataset.a11yTreeToggled === '1') return;
    if (row.dataset) row.dataset.a11yTreeToggled = '1';
    if (typeof row.click === 'function') row.click();
  };

  // Every focused row becomes the tab stop - by arrow keys, pointer, or the
  // screen reader's own navigation - so Tab out and back returns to it.
  container.addEventListener('focusin', (ev) => {
    const target = ev && ev.target;
    if (!target || typeof target.getAttribute !== 'function'
        || target.getAttribute('role') !== 'treeitem') return;
    _a11yTreeSetTabStop(container, target);
    // Focus that a rebuild restores is not the user's; any other focus on a
    // row (arrow keys, Tab back in, a click, the screen reader) is, and the
    // position a root refresh carries must not override it.
    if (!container._a11yTreeRestoring) container._a11yTreeCarry = null;
  });
  // Acting on the row the user is already on moves no focus, so these count
  // too: a click (also a tap, and a screen reader's activation such as NVDA
  // browse-mode Enter or a VoiceOver double-tap, which send no key; scrolling
  // sends none) or a middle click, opening the context menu (Rename, Copy...
  // act from there), starting a drag or dropping files on a row, a key the
  // tree handled (see the listener after the key handler below), and typing
  // into a control inside a row (the rename input). Other keys - a modifier
  // alone (Ctrl silences NVDA), Tab, Escape, page shortcuts - do not act on
  // the tree.
  const drop = () => { container._a11yTreeCarry = null; };
  ['click', 'auxclick', 'contextmenu', 'dragstart', 'drop'].forEach((type) => container.addEventListener(type, drop, true));

  container.addEventListener('keydown', (ev) => {
    const key = ev && ev.key;
    if (!key || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    // Focus can sit on the container itself (a click on blank space, a screen
    // reader moving there): it is click-focusable since it has tabindex=-1. A
    // navigation key there enters the tree at its tab stop - or at the
    // first/last row for Home/End - instead of doing nothing. Enter and Space
    // only enter the tree too: nothing is activated, and the key is consumed,
    // so a document-level Enter shortcut (Allow once on a pending approval)
    // cannot read the focused tree as "no control focused". Failing closed
    // costs a mouse user who clicked blank tree space one extra Enter.
    if (ev.target === container) {
      if (!['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End', 'Enter', ' '].includes(key)) return;
      ev.preventDefault();
      const items = rows();
      if (!items.length) return;
      const stop = container._a11yTreeStop;
      if (key === 'Home') moveFocus(items[0]);
      else if (key === 'End') moveFocus(items[items.length - 1]);
      else moveFocus(stop && container.contains(stop) ? stop : items[0]);
      return;
    }
    // Only keys pressed ON a row are tree navigation. Keys typed into a control
    // inside a row (the inline rename input) belong to that control.
    const current = ev.target;
    if (!current || typeof current.getAttribute !== 'function'
        || current.getAttribute('role') !== 'treeitem') return;
    const items = rows();
    const i = items.indexOf(current);
    if (i < 0) return;

    if (key === 'ArrowDown' || key === 'ArrowUp') {
      ev.preventDefault();
      const next = items[i + (key === 'ArrowDown' ? 1 : -1)];
      if (next) moveFocus(next);
      return;
    }
    if (key === 'Home' || key === 'End') {
      ev.preventDefault();
      moveFocus(key === 'Home' ? items[0] : items[items.length - 1]);
      return;
    }
    const expandable = current.hasAttribute('aria-expanded');
    const expanded = current.getAttribute('aria-expanded') === 'true';

    // Right arrow: expand a collapsed directory; on an expanded one, move to
    // its first child.
    if (key === 'ArrowRight') {
      ev.preventDefault();
      if (!expandable) return;
      if (!expanded) { toggle(current); return; }
      const child = items[i + 1];
      const level = Number(current.getAttribute('aria-level') || 0);
      if (child && Number(child.getAttribute('aria-level') || 0) > level) moveFocus(child);
      return;
    }
    // Left arrow: collapse an expanded directory; otherwise move to the parent.
    if (key === 'ArrowLeft') {
      ev.preventDefault();
      if (expandable && expanded) { toggle(current); return; }
      const level = Number(current.getAttribute('aria-level') || 0);
      for (let j = i - 1; j >= 0; j--) {
        if (Number(items[j].getAttribute('aria-level') || 0) < level) { moveFocus(items[j]); return; }
      }
      return;
    }
    if (key === 'Enter' || key === ' ') {
      ev.preventDefault();
      // A held key must not open or toggle rows over and over (nor act on the
      // row a first press on the container just moved to).
      if (ev.repeat) return;
      if (current.getAttribute('aria-disabled') === 'true') return;
      if (expandable) toggle(current);
      else if (typeof current.click === 'function') current.click();
    }
  });
  // Registered after the key handler, so it sees what it did: a key the tree
  // or a row handled is default-prevented (Delete on a row too).
  const modifierKeys = ['Shift', 'Control', 'Alt', 'AltGraph', 'Meta', 'CapsLock', 'NumLock', 'Tab', 'Escape'];
  container.addEventListener('keydown', (ev) => {
    const t = ev && ev.target;
    const inControl = !!t && t !== container
      && !(typeof t.getAttribute === 'function' && t.getAttribute('role') === 'treeitem');
    if (ev.defaultPrevented || (inControl && !modifierKeys.includes(ev.key))) drop();
  });
  return container;
}

/* Move the single tab stop. Only the previous and the new row change, so
 * holding an arrow key in a large tree stays cheap. */
function _a11yTreeSetTabStop(container, target){
  const prev = container._a11yTreeStop;
  if (prev && prev !== target) prev.setAttribute('tabindex', '-1');
  target.setAttribute('tabindex', '0');
  container._a11yTreeStop = target;
}

/* The container no longer holds rows (empty folder, skeleton, no session):
 * it is not a tree, and a generic element must not keep the tree's name.
 * Call it before wiping the rows: focus inside the tree (or opts.hadFocus,
 * for a caller that already wiped them) moves to opts.focusTo instead of
 * dropping to <body>. */
function a11yTreeReset(container, opts){
  if (!container || typeof container.removeAttribute !== 'function') return;
  const o = opts || {};
  const active = (typeof document !== 'undefined' && document.activeElement) || null;
  const hadFocus = !!o.hadFocus || (!!active && (active === container
    || (typeof container.contains === 'function' && container.contains(active))));
  if (hadFocus && o.focusTo && typeof o.focusTo.focus === 'function') o.focusTo.focus();
  container.removeAttribute('role');
  container.removeAttribute('aria-label');
  // tabindex=-1 stays: an overflowing skeleton must not become a Firefox
  // scroller Tab stop either.
  container._a11yTreeStop = null;
  container._a11yTreeCarry = null;
  container._a11yTreeScope = null;
}

/* Keep the keyboard position across a full tree rebuild.
 *
 * Toggling a directory calls click(), which re-renders the whole tree: every
 * row is replaced and the single tab stop would go back to the FIRST top-level
 * row. The row the user just toggled is detached, so focus falls to
 * document.body and the next arrow press restarts at the top of the tree.
 *
 * Identity is the entry PATH, not the index: expanding changes how many rows
 * precede an entry, so a remembered index points somewhere else after the
 * rebuild. When the remembered entry is gone (deleted, renamed, collapsed
 * away) the tab stop goes to the row that followed it, else the one before
 * it, else the first row - a tree with no tab stop is unreachable by keyboard.
 *
 * The fallback order is: the row that followed it, the one before it, the
 * nearest folder above it that is still shown, then the first row.
 *
 * A miss can be temporary: a root refresh renders once with the folder
 * listings cleared and again when they load. The refresh passes one object
 * that identifies it to both renders: {carry: refill} on the partial one and
 * {completesRefill: refill} on the completing one. Every root render also
 * passes {scope} naming the tree (session, generation, workspace): a token
 * is carried only from rows of the same tree. It is carried for that refill
 * and used by a completing render of a refill that carries it, while the tab
 * stop is still the fallback row (compared by path: other renders in the
 * gap, such as a folder listing that arrives meanwhile, replace every row
 * object but leave the token alone). It is used up once the original row is
 * back; a completing render that still misses it (its listing failed) only
 * stops carrying it, so another refill can still bring the row back.
 *
 * Refills overlap (each tool result refreshes the tree). A newer refill of
 * the same scope whose partial render finds the user still on the fallback
 * row joins the carry instead of replacing it - also when that row is gone
 * from its own partial render - so whichever completes first restores the
 * original row and the other finds nothing left to carry. Any user action in
 * the tree drops the token (see a11yTree); a refill leaves it on every other
 * exit (a11yTreeDropCarry), and it is gone once no refill carries it.
 */
function a11yTreeRememberFocus(container, opts){
  try {
    if (!container || typeof container.querySelectorAll !== 'function') return null;
    const rows = Array.from(container.querySelectorAll('[role="treeitem"]'));
    const active = (typeof document !== 'undefined' && document.activeElement) || null;
    // Focus on a control inside a row (rename input, delete button) counts too.
    const focusedRow = active ? rows.find((r) => r === active || r.contains(active)) : null;
    const hadFocus = !!focusedRow;
    // Without focus, still keep the tab stop where the user left it.
    const tracked = container._a11yTreeStop;
    const stop = focusedRow || (tracked && rows.indexOf(tracked) >= 0 ? tracked : null);
    const pathOf = (r) => (r && r.dataset && r.dataset.a11yTreePath) || null;
    const path = pathOf(stop);
    const carried = container._a11yTreeCarry;
    if (opts && opts.completesRefill && carried && carried.refills.indexOf(opts.completesRefill) >= 0
        && path && path === carried.fallbackPath) {
      return Object.assign({}, carried.token, {hadFocus: hadFocus, carried: opts.completesRefill});
    }
    if (!path) return null;
    const i = rows.indexOf(stop);
    return {path: path, next: pathOf(rows[i + 1]), prev: pathOf(rows[i - 1]), hadFocus: hadFocus};
  } catch (_e) { return null; }
}

function a11yTreeRestoreFocus(container, token, opts){
  try {
    if (!container || typeof container.querySelectorAll !== 'function') return;
    const rows = Array.from(container.querySelectorAll('[role="treeitem"]'));
    if (!rows.length) return;
    const byPath = (p) => (p && rows.find((r) => r.dataset && r.dataset.a11yTreePath === p)) || null;
    const ancestor = (p) => {
      for (let q = String(p || ''); q.lastIndexOf('/') > 0;) {
        q = q.slice(0, q.lastIndexOf('/'));
        const hit = byPath(q);
        if (hit) return hit;
      }
      return null;
    };
    const exact = token ? byPath(token.path) : null;
    const target = exact
      || (token && (byPath(token.next) || byPath(token.prev) || ancestor(token.path)))
      || rows[0];
    const targetPath = target.dataset && target.dataset.a11yTreePath;
    const refill = opts && opts.carry;
    const scope = opts && opts.scope;
    // The rows the token came from belong to this same tree.
    const sameTree = !!scope && container._a11yTreeScope === scope;
    if (scope) container._a11yTreeScope = scope;
    const carried = container._a11yTreeCarry;
    if (token && token.carried && carried) {
      if (exact) container._a11yTreeCarry = null;
      else a11yTreeDropCarry(container, token.carried);
      if (container._a11yTreeCarry) container._a11yTreeCarry.fallbackPath = targetPath;
    } else if (refill && token && sameTree && carried && carried.scope === scope
        && token.path === carried.fallbackPath) {
      carried.refills.push(refill);
      carried.fallbackPath = targetPath;
    } else if (refill && token && sameTree && !exact) {
      container._a11yTreeCarry = {token: token, refills: [refill], scope: scope, fallbackPath: targetPath};
    }
    _a11yTreeSetTabStop(container, target);
    // Only take focus when the tree had it before - otherwise a background
    // refresh would steal focus from whatever the user is doing elsewhere. The
    // scroll position was already restored by the caller.
    if (token && token.hadFocus && typeof target.focus === 'function') {
      // Marked, so the focusin it fires is not taken for the user moving.
      container._a11yTreeRestoring = true;
      try { target.focus({preventScroll: true}); } finally { container._a11yTreeRestoring = false; }
      // A carried position comes back after a shorter partial render clamped
      // the scroll offset, so bring the row into view if it ended up outside.
      if (token.carried && typeof target.scrollIntoView === 'function') target.scrollIntoView({block: 'nearest'});
    }
  } catch (_e) { /* a rebuild must never fail because of focus bookkeeping */ }
}

/* A refill is over (completed, aborted, failed, nothing left expanded): it
 * no longer carries the position, which is dropped once no refill does. */
function a11yTreeDropCarry(container, refill){
  const carried = container && container._a11yTreeCarry;
  if (!carried) return;
  carried.refills = carried.refills.filter((r) => r !== refill);
  if (!carried.refills.length) container._a11yTreeCarry = null;
}

if (typeof window !== 'undefined') {
  window.a11yLabel = a11yLabel;
  window.a11yTree = a11yTree;
  window.a11yTreeRow = a11yTreeRow;
  window.a11yTreeReset = a11yTreeReset;
  window.a11yTreeRememberFocus = a11yTreeRememberFocus;
  window.a11yTreeRestoreFocus = a11yTreeRestoreFocus;
  window.a11yTreeDropCarry = a11yTreeDropCarry;
}
