"""Focused regressions for the PR #6836 UX re-review round 3 (2026-10-09T03:00:15Z).

Review id 5465222262 ("Re-gated ``add750760ccf`` ... Codex and the senior review
are both SAFE ... four small fixes before sign-off") asked for, in the
reviewer's order:

1 **Filter the add list.** The add-workspace dropdown still listed workspaces
  that were already bound; the comment above the fetch claimed they were
  excluded, but nothing filtered them, so picking one just toasted "already
  bound". The filter now lives in the combo's open routine, because the bound
  list changes after the fetch resolves (a row removed, a path typed in).
2 **Keep "bindings" out of user text.** The toasts now read "Project settings
  saved" / "Could not save project settings: " in every locale.
3 **Use the lucide X for the row remove control**, with an aria-label from the
  existing unbind title string (the 12px "×" glyph only reached ~2.2:1 on a 1x
  dark desktop).
4 **One noun for chats.** The checkbox label and hint said "sessions" while the
  confirmation said "chats"; every locale now uses its own chat noun.

The reviewer's optional ask (auto-add a saved workspace when it is picked) is
deliberately not implemented and not asserted here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ELLIPSIS = chr(8230)  # the U+2026 glyph used by "Type a path…"


def _read_static(name: str) -> str:
    return (REPO_ROOT / "static" / name).read_text(encoding="utf-8")


def _read_sessions_js() -> str:
    return _read_static("sessions.js")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _combo_fn() -> str:
    return _slice(
        _read_sessions_js(),
        "let _openBindingsCombo=null;",
        "\n// Modal dialog for editing a project's bindings",
    )


def _add_filter_expr() -> str:
    """The SHIPPED filterOptions arrow of the add-workspace combo."""
    line = next(
        ln
        for ln in _read_sessions_js().splitlines()
        if "filterOptions:(opts)=>opts.filter(" in ln
    )
    # Keep only the arrow function: "__FILTER__" is spliced in as the value.
    return line.strip().rstrip(",").split("filterOptions:", 1)[1].strip()


def _remove_block() -> str:
    """The SHIPPED statements that build the per-row remove button."""
    return _slice(
        _read_sessions_js(),
        "const rm=document.createElement('button');",
        "row.appendChild(rm);",
    )


def _run_node(tmp_path: Path, name: str, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the frontend behavior probe")
    script_path = tmp_path / name
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


# ---------------------------------------------------------------------------
# A shared mini-DOM: just enough for the combobox / row builder
# ---------------------------------------------------------------------------

_DOM_STUB = r"""
function assert(cond, msg) { if (!cond) throw new Error(msg); }

function _classList(node) {
  const set = new Set();
  const sync = () => { node._className = [...set].join(' '); };
  return {
    add: (...xs) => { xs.forEach((x) => x && set.add(x)); sync(); },
    remove: (...xs) => { xs.forEach((x) => set.delete(x)); sync(); },
    contains: (x) => set.has(x),
    toggle: (x, f) => { const on = f === undefined ? !set.has(x) : !!f; if (on) set.add(x); else set.delete(x); sync(); return on; },
  };
}

function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    _className: '',
    dataset: {},
    style: {},
    attrs: {},
    textContent: '',
    innerHTML: '',
    scrollHeight: 0,
    type: '',
    title: '',
    _listeners: {},
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    setAttribute(k, v) { node.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attrs, k) ? node.attrs[k] : null; },
    getBoundingClientRect() { return { top: 10, bottom: 30, left: 5, width: 120 }; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, '');
      return node.children.filter((c) => (c._className || '').split(/\s+/).includes(cls));
    },
    closest() { return node.parentNode; },
    contains(other) { let p = other; while (p) { if (p === node) return true; p = p.parentNode; } return false; },
    addEventListener() {},
  };
  Object.defineProperty(node, 'className', {
    get() { return node._className; },
    set(v) { node._className = String(v); },
  });
  Object.defineProperty(node, 'innerHTML', {
    get() { return node._innerHTML || ''; },
    set(v) { node._innerHTML = String(v); node.children = []; },
  });
  node.classList = _classList(node);
  return node;
}

const _document = {
  createElement: (tag) => makeElement(tag),
  addEventListener: () => {},
};
globalThis.document = _document;
globalThis.window = { innerHeight: 800, addEventListener: () => {} };
globalThis.t = (key) => key;
"""


# ---------------------------------------------------------------------------
# 1 — the add list hides already-bound workspaces, evaluated at OPEN time
# ---------------------------------------------------------------------------

_ADD_FILTER_PROBE = (
    _DOM_STUB
    + """
// The dialog's live bound list; the filter closes over it.
const wsList = [{ value: 'D:/alpha', name: 'alpha', sub: 'D:/alpha' }];
__COMBO__
const addCombo = _makeBindingsCombo({
  placeholder: 'pb_add_workspace_placeholder',
  value: '',
  options: [],
  filterOptions: __FILTER__,
});
const custom = { value: '__custom_path__', name: 'pb_type_path', sub: 'pb_enter_ws_path' };
addCombo.setOptions([custom,
  { value: 'D:/alpha', name: 'alpha', sub: 'D:/alpha' },
  { value: 'D:/beta', name: 'beta', sub: 'D:/beta' }]);

const trigger = addCombo.el.children[0];
const menu = addCombo.el.children[1];
const names = () => menu.children.map((row) => row.children[0].textContent);

function open() {
  if (menu.classList.contains('open')) trigger.onclick({ stopPropagation() {} });
  trigger.onclick({ stopPropagation() {} });
}

open();
assert(names().indexOf('alpha') < 0,
  'an already-bound workspace must not be offered again: ' + names().join(','));
assert(names().indexOf('beta') >= 0,
  'an unbound saved workspace must stay offered: ' + names().join(','));
assert(names().indexOf('pb_type_path') >= 0,
  'the "type a path" entry must always survive the filter: ' + names().join(','));

// The bound list moves AFTER the fetch resolved: unbinding must re-offer it.
wsList.length = 0;
open();
assert(names().indexOf('alpha') >= 0,
  'the filter must be evaluated on every open, not once at fetch time: ' + names().join(','));
console.log('ok');
"""
)


def test_the_add_workspace_list_hides_already_bound_workspaces_at_open_time(tmp_path):
    """Item 1: the comment claimed the fetch excluded bound workspaces, but
    nothing filtered them; the filter must run inside the combo's open routine."""
    src = _read_sessions_js()
    combo = _combo_fn()
    # The combo itself consults state.filterOptions when it rebuilds the menu.
    assert "filterOptions:(o&&typeof o.filterOptions==='function')?o.filterOptions:null" in combo
    open_fn = _slice(combo, "function _open(){", "  trigger.onclick=(e)=>{")
    assert "if(typeof state.filterOptions==='function'){" in open_fn, (
        "the filter must be applied by the combo's open routine"
    )
    assert "items=filtered" in open_fn
    # The add combo supplies a filter that keeps the custom entry and drops the
    # paths the project already binds.
    assert "filterOptions:(opts)=>opts.filter(o=>o&&(o.value==='__custom_path__'||!wsList.some(x=>x.value===o.value)))" in src

    script = (
        _ADD_FILTER_PROBE.replace("__COMBO__", combo)
        .replace("__FILTER__", _add_filter_expr())
    )
    assert _run_node(tmp_path, "add_filter_probe.js", script).strip() == "ok"


# ---------------------------------------------------------------------------
# 2 — user text never says "bindings"
# ---------------------------------------------------------------------------

# The pre-fix toast values (one per locale) that must never come back.
_BINDINGS_TOAST_TEXT = (
    "'Project bindings updated'",
    "'Binding update failed: '",
    "'Associazioni del progetto aggiornate'",
    "'Aggiornamento associazione non riuscito: '",
    "'プロジェクトの紐付けを更新しました'",
    "'紐付けの更新に失敗しました: '",
    "'Привязки проекта обновлены'",
    "'Не удалось обновить привязку: '",
    "'Vinculaciones del proyecto actualizadas'",
    "'Error al actualizar la vinculación: '",
    "'Projektbindungen aktualisiert'",
    "'Bindung konnte nicht aktualisiert werden: '",
    "'项目绑定已更新'",
    "'绑定更新失败：'",
    "'專案綁定已更新'",
    "'綁定更新失敗：'",
    "'Vinculações do projeto atualizadas'",
    "'Falha ao atualizar a vinculação: '",
    "'프로젝트 바인딩이 업데이트되었습니다'",
    "'바인딩 업데이트 실패: '",
    "'Liaisons du projet mises à jour'",
    "'Échec de la mise à jour de la liaison : '",
    "'Vazby projektu aktualizovány'",
    "'Aktualizace vazby se nezdařila: '",
    "'Proje bağlantıları güncellendi'",
    "'Bağlantı güncellenemedi: '",
    "'Zaktualizowano powiązania projektu'",
    "'Nie udało się zaktualizować powiązania: '",
    "'Đã cập nhật liên kết dự án'",
    "'Cập nhật liên kết thất bại: '",
)


def test_the_save_toasts_are_project_settings_not_bindings():
    """Item 2: "Use 'Project settings saved' / 'Could not save project settings'
    for the toasts" — in every locale, not just English."""
    i18n = _read_static("i18n.js")
    assert "pb_updated: 'Project settings saved'," in i18n
    assert "pb_update_failed: 'Could not save project settings: '," in i18n
    for literal in _BINDINGS_TOAST_TEXT:
        assert literal not in i18n, f"binding jargon still in the toast text: {literal}"
    # The dialog still reaches the toasts through the l10n keys.
    src = _read_sessions_js()
    assert "showToast(t('pb_updated'))" in src
    assert "showToast(t('pb_update_failed')+(e&&e.message||e))" in src


# ---------------------------------------------------------------------------
# 3 — the remove control is a lucide X with an aria-label
# ---------------------------------------------------------------------------

_REMOVE_PROBE = (
    _DOM_STUB
    + """
const row = document.createElement('div');
const idx = 0;
const wsList = [{ value: 'D:/alpha', name: 'alpha', sub: 'D:/alpha', isDefault: true }];
const _wsDefault = () => wsList.find((x) => x.isDefault) || wsList[0] || null;
const _renderWsList = () => {};
__BLOCK__
assert(rm.innerHTML.indexOf('<svg') === 0,
  'the remove control must render an SVG icon, got: ' + rm.innerHTML);
assert(rm.innerHTML.indexOf('viewBox="0 0 24 24"') >= 0,
  'the icon must be the 24x24 lucide viewBox');
assert(rm.innerHTML.indexOf('stroke="currentColor"') >= 0,
  'the icon must stroke currentColor so the skin tint applies');
assert(rm.innerHTML.indexOf('M18 6 6 18') >= 0 && rm.innerHTML.indexOf('m6 6 12 12') >= 0,
  'the lucide X draws two crossing strokes');
assert(rm.innerHTML.indexOf('aria-hidden="true"') >= 0,
  'the decorative glyph must be hidden from assistive tech');
assert(rm.textContent === '',
  'the old text glyph must be gone, got: ' + JSON.stringify(rm.textContent));
assert(rm.getAttribute('aria-label') === 'pb_unbind_ws_title',
  'the remove control needs an aria-label from the unbind title string, got: ' + rm.getAttribute('aria-label'));
assert(rm.title === 'pb_unbind_ws_title',
  'the title must stay the unbind title string, got: ' + rm.title);
console.log('ok');
"""
)


def test_the_row_remove_control_is_a_lucide_x_with_an_aria_label(tmp_path):
    """Item 3: the 12px "×" text glyph only reached ~2.2:1 on a 1x dark
    desktop; the control is now the lucide X with a real label."""
    block = _remove_block()
    assert "rm.textContent" not in block, "the row remove control still sets a text glyph"
    assert "rm.setAttribute('aria-label',t('pb_unbind_ws_title'))" in block
    script = _REMOVE_PROBE.replace("__BLOCK__", block)
    assert _run_node(tmp_path, "remove_probe.js", script).strip() == "ok"


# ---------------------------------------------------------------------------
# 4 — one noun for chats, in every locale
# ---------------------------------------------------------------------------

# locale -> (the locale's "sessions" word, the locale's "chats" word). The
# checkbox label and the hint must use the CHAT word, matching the locale's own
# confirmation ("File {0} existing chats under {1}?") and the sidebar.
_CHAT_NOUNS = {
    "en": ("sessions", "chats"),
    "it": ("sessioni", "chat"),
    "ja": (chr(0x30BB) + chr(0x30C3) + chr(0x30B7) + chr(0x30E7) + chr(0x30F3), chr(0x30C1) + chr(0x30E3) + chr(0x30C3) + chr(0x30C8)),
    "ru": ("сеансы", "чаты"),
    "es": ("sesiones", "conversaciones"),
    "de": ("Sitzungen", "Chats"),
    "zh": (chr(0x4F1A) + chr(0x8BDD), chr(0x5BF9) + chr(0x8BDD)),
    "zh-Hant": (chr(0x5DE5) + chr(0x4F5C) + chr(0x968E) + chr(0x6BB5), chr(0x5C0D) + chr(0x8A71)),
    "pt": ("sessões", "conversas"),
    "ko": ("세션", "채팅"),
    "fr": ("sessions", "conversations"),
    "cs": (None, "relace"),   # already one noun in Czech: the dialog said "relace" throughout
    "tr": ("oturum", "sohbet"),
    "pl": ("sesje", "czaty"),
    "vi": ("phiên", "hội thoại"),
}

_I18N_LOCALES = tuple(_CHAT_NOUNS)

# The default-workspace TOOLTIP is a full sentence, so a locale can need the
# inflected form of its chat noun (ru genitive "чатов", tr plural "sohbetler",
# pl genitive "czatów"). This pins the exact form the shipped title must carry;
# every other locale uses its chat noun unchanged.
_TITLE_CHAT_NOUNS = {
    "ru": "чатов",
    "tr": "sohbetler",
    "pl": "czatów",
}


def _locale_chunks(src: str):
    out = {}
    for idx, loc in enumerate(_I18N_LOCALES):
        head = ("\n  '%s': {\n" % loc) if "-" in loc else ("\n  %s: {\n" % loc)
        start = src.index(head) + len(head)
        if idx + 1 < len(_I18N_LOCALES):
            nxt = _I18N_LOCALES[idx + 1]
            nhead = ("\n  '%s': {\n" % nxt) if "-" in nxt else ("\n  %s: {\n" % nxt)
            end = src.index(nhead, start)
        else:
            end = src.index("const _I18N_TOOL_ACTION_TEXT_EN", start)
        out[loc] = src[start:end]
    return out


def _locale_value(chunk: str, key: str) -> str:
    m = re.search(r"(?m)^\s*%s: (.*)$" % re.escape(key), chunk)
    assert m, "missing i18n key %s" % key
    return m.group(1).strip()


def test_the_dialog_uses_one_noun_for_chats():
    """Item 4: the checkbox label and hint said "sessions" while the confirm
    said "chats"; every locale must speak with one noun."""
    en = _locale_chunks(_read_static("i18n.js"))["en"]
    label = _locale_value(en, "pb_auto_assign_label")
    hint = _locale_value(en, "pb_auto_assign_hint")
    title = _locale_value(en, "pb_set_default_title")
    assert "sessions" not in label, label
    assert "sessions" not in hint, hint
    assert "sessions" not in title, title
    assert "chats" in label, label
    assert "chats" in hint, hint
    assert "chats" in title, title
    assert "chats" in _locale_value(en, "pb_auto_assign_confirm")
    assert "chats" in _locale_value(en, "pb_auto_assign_confirm_btn")


def test_every_locale_speaks_the_sidebar_noun():
    chunks = _locale_chunks(_read_static("i18n.js"))
    for loc, (session_word, chat_word) in _CHAT_NOUNS.items():
        chunk = chunks[loc]
        label = _locale_value(chunk, "pb_auto_assign_label")
        hint = _locale_value(chunk, "pb_auto_assign_hint")
        # The default-workspace tooltip is user-visible text too: it kept saying
        # the locale's "sessions" word in 12 locales (maintainer LOW,
        # 2026-10-09T23:55:01Z), so it is asserted with the label and the hint.
        title = _locale_value(chunk, "pb_set_default_title")
        title_noun = _TITLE_CHAT_NOUNS.get(loc, chat_word)
        for value, key in (
            (label, "pb_auto_assign_label"),
            (hint, "pb_auto_assign_hint"),
            (title, "pb_set_default_title"),
        ):
            if session_word is not None:
                assert session_word.lower() not in value.lower(), (
                    f"locale {loc!r} {key} still uses {session_word!r}: {value}"
                )
            noun = title_noun if key == "pb_set_default_title" else chat_word
            assert noun.lower() in value.lower(), (
                f"locale {loc!r} {key} must use {noun!r}: {value}"
            )
