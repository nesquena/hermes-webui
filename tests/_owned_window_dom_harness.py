"""Node harness for the owned-window commit pipeline (#7617 gate follow-up).

Lets tests drive the REAL production `_commitMessageWindow` /
`_syncExtensionMessageActionSlots` / `invokeExtensionMessageAction` path — DOM
adoption, fresh-slot parsing, generated-control invocation — inside plain node,
without a browser or jsdom. Only reader/geometry machinery is stubbed, the same
boundary the sibling virtualization suites use (test_issue7591 stubs ownership
init; here `_restoreMessageWindowReader`/`_rememberMessageWindowReader` are
no-ops because scroll/reader state is not the subject under test).

The mini DOM implements exactly the surface those functions touch: ordered
children with insertBefore/remove/replaceWith/parentElement/contains, dataset
proxies over data-* attributes, classList, closest/querySelector(All) for the
compound selectors the production code uses (including `:not(:empty)`),
outerHTML serialization with stable attribute order (so raw-vs-raw stamp
comparisons are meaningful), and an innerHTML setter that PARSES markup into
real child elements — the piece the earlier plain-object harness lacked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

NODE = shutil.which("node")

# Hardened brace-matching extractor (skips string/template/regex literals and
# comments) — same style as the sibling virtualization suites.
_HARDENED_EXTRACTOR = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  str = null; inLine = false; inBlock = false; inRegex = false; prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i]; const n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) { if (c === '\\') { i += 2; continue; } if (c === str) str = null; i++; continue; }
    if (inRegex) { if (c === '/') inRegex = false; i++; continue; }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) { inRegex = true; i++; continue; }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}
"""

_MINI_DOM_JS = r"""
// ── mini DOM ────────────────────────────────────────────────────────────────
class MiniText {
  constructor(text){ this.nodeType = 3; this._text = String(text); this.parentElement = null; }
  get textContent(){ return this._text; }
}
class MiniEl {
  constructor(tag){
    this.nodeType = 1;
    this.tagName = String(tag).toLowerCase();
    this._attrs = new Map();
    this.children = [];
    this.parentElement = null;
    this.disabled = false;
    this.offsetHeight = 0;
    const prio = {};
    this.style = {
      getPropertyPriority(name){ return prio[String(name)] || ''; },
      setProperty(name, value, priority){ this[String(name)] = String(value); if (priority) prio[String(name)] = String(priority); },
    };
  }
  get id(){ return this._attrs.get('id') || ''; }
  set id(v){ if (v === '' || v == null) this._attrs.delete('id'); else this._attrs.set('id', String(v)); }
  _classes(){ return String(this._attrs.get('class') || '').split(/\s+/).filter(Boolean); }
  get classList(){
    const el = this;
    return {
      add(...cs){ const s = new Set(el._classes()); cs.forEach(c => s.add(String(c))); el._attrs.set('class', [...s].join(' ')); },
      remove(...cs){ const s = new Set(el._classes()); cs.forEach(c => s.delete(String(c))); el._attrs.set('class', [...s].join(' ')); },
      contains(c){ return el._classes().indexOf(String(c)) !== -1; },
    };
  }
  get dataset(){
    const el = this;
    const dash = prop => String(prop).replace(/[A-Z]/g, c => '-' + c.toLowerCase());
    return new Proxy({}, {
      get(_t, prop){
        const key = 'data-' + dash(prop);
        return el._attrs.has(key) ? el._attrs.get(key) : undefined;
      },
      set(_t, prop, value){
        const key = 'data-' + dash(prop);
        if (value === undefined || value === null) el._attrs.delete(key);
        else el._attrs.set(key, String(value));
        return true;
      },
    });
  }
  getAttribute(name){ return this._attrs.has(String(name)) ? this._attrs.get(String(name)) : null; }
  setAttribute(name, value){ this._attrs.set(String(name), String(value)); }
  removeAttribute(name){ this._attrs.delete(String(name)); }
  hasAttribute(name){ return this._attrs.has(String(name)); }
  _removeChild(node){ const i = this.children.indexOf(node); if (i >= 0) this.children.splice(i, 1); node.parentElement = null; }
  appendChild(node){ if (node.parentElement) node.parentElement._removeChild(node); node.parentElement = this; this.children.push(node); return node; }
  insertBefore(node, ref){
    if (node.parentElement) node.parentElement._removeChild(node);
    const i = ref ? this.children.indexOf(ref) : this.children.length;
    if (i < 0) throw new Error('insertBefore: ref is not a child');
    this.children.splice(i, 0, node); node.parentElement = this; return node;
  }
  remove(){ if (this.parentElement) this.parentElement._removeChild(this); }
  replaceWith(node){
    const p = this.parentElement; if (!p) return;
    const i = p.children.indexOf(this);
    p.children.splice(i, 1, node); node.parentElement = p; this.parentElement = null;
  }
  contains(node){ let cur = node; while (cur){ if (cur === this) return true; cur = cur.parentElement; } return false; }
  get isConnected(){ let cur = this; while (cur.parentElement) cur = cur.parentElement; return cur === MINI_ROOTS.messages || cur === MINI_ROOTS.msgInner; }
  get textContent(){
    return this.children.map(c => c.nodeType === 3 ? c._text : c.textContent).join('');
  }
  _escapeText(t){ return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  get innerHTML(){ return this.children.map(c => c.nodeType === 3 ? this._escapeText(c._text) : c.outerHTML).join(''); }
  set innerHTML(html){
    for (const c of this.children.slice()) c.parentElement = null;
    this.children.length = 0;
    _miniParse(this, String(html));
  }
  get outerHTML(){
    const parts = [];
    for (const [k, v] of this._attrs.entries()) parts.push(v === '' ? ` ${k}` : ` ${k}="${String(v).replace(/"/g, '&quot;')}"`);
    return `<${this.tagName}${parts.join('')}>${this.innerHTML}</${this.tagName}>`;
  }
  _tokenizeCompound(s){
    const tokens = []; let i = 0;
    while (i < s.length){
      if (s[i] === '['){ const j = s.indexOf(']', i); if (j < 0) throw new Error('bad selector ' + s); tokens.push(s.slice(i, j + 1)); i = j + 1; }
      else if (s.startsWith(':not(:empty)', i)){ tokens.push(':not(:empty)'); i += ':not(:empty)'.length; }
      else if (s[i] === ':' || s[i] === '#' || s[i] === '.'){ let j = i + 1; while (j < s.length && !'[:.#'.includes(s[j]) && s[j] !== ':') j++; tokens.push(s.slice(i, j)); i = j; }
      else { let j = i; while (j < s.length && !'[:.#:'.includes(s[j])) j++; tokens.push(s.slice(i, j)); i = j; }
    }
    return tokens.filter(Boolean);
  }
  _matchesToken(token){
    if (token === ':not(:empty)') return this.children.length > 0 || this.textContent.length > 0;
    if (token[0] === '#') return this.id === token.slice(1);
    if (token[0] === '.') return this.classList.contains(token.slice(1));
    if (token[0] === '['){
      const m = /^\[([^=\]]+)(?:="([^"]*)")?\]$/.exec(token);
      if (!m) return false;
      const v = this.getAttribute(m[1]);
      return m[2] === undefined ? v !== null : v === m[2];
    }
    return this.tagName === token.toLowerCase();
  }
  matches(selector){
    for (const compound of String(selector).split(',')){
      const tokens = this._tokenizeCompound(compound.trim());
      if (tokens.length && tokens.every(t => this._matchesToken(t))) return true;
    }
    return false;
  }
  _walk(cb){ for (const c of this.children){ if (c.nodeType === 1){ cb(c); c._walk(cb); } } }
  querySelectorAll(selector){
    const out = [];
    this._walk(el => { if (el.matches(selector)) out.push(el); });
    return out;
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  closest(selector){
    let cur = this;
    while (cur){ if (cur.nodeType === 1 && cur.matches(selector)) return cur; cur = cur.parentElement; }
    return null;
  }
}
function _miniParseAttrs(body, el){
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:]*)(?:="([^"]*)")?/g;
  let m;
  while ((m = re.exec(body)) !== null) el.setAttribute(m[1], m[2] === undefined ? '' : m[2]);
}
function _miniParse(parent, html){
  const stack = [parent];
  let i = 0;
  while (i < html.length){
    const lt = html.indexOf('<', i);
    const text = lt < 0 ? html.slice(i) : html.slice(i, lt);
    if (text) { const t = new MiniText(text); t.parentElement = stack[stack.length - 1]; stack[stack.length - 1].children.push(t); }
    if (lt < 0) break;
    if (html.startsWith('<!--', lt)){ const e = html.indexOf('-->', lt); i = e < 0 ? html.length : e + 3; continue; }
    if (html.startsWith('</', lt)){ const gt = html.indexOf('>', lt); if (stack.length > 1) stack.pop(); i = gt < 0 ? html.length : gt + 1; continue; }
    const gt = html.indexOf('>', lt);
    if (gt < 0) break;
    const tagBody = html.slice(lt + 1, gt);
    i = gt + 1;
    const nameMatch = /^[a-zA-Z0-9-]+/.exec(tagBody);
    if (!nameMatch) continue;
    const el = new MiniEl(nameMatch[0]);
    _miniParseAttrs(tagBody.slice(nameMatch[0].length), el);
    const p = stack[stack.length - 1];
    el.parentElement = p; p.children.push(el);
    if (!/\/$/.test(tagBody)) stack.push(el);
  }
}
const MINI_ROOTS = { messages: null, msgInner: null };
function miniContainer(id){
  const el = new MiniEl('div');
  el.id = id;
  MINI_ROOTS[id === 'messages' ? 'messages' : 'msgInner'] = el;
  return el;
}
"""


def run_node(source: str) -> dict:
    """Run a harness script and return its JSON stdout."""
    if NODE is None:
        raise RuntimeError("node is required for the owned-window harness")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=Path.cwd(), delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout.strip())


def build_script(ui_js: str, extracted: list[str], stubs: str, body: str) -> str:
    """Assemble a harness script: extractor + mini DOM + real eval'd functions +
    stub collaborators + the scenario body. `extracted` names the production
    functions to eval from ui.js."""
    extracted_js = "\n".join(f"eval(extractFunc('{name}'));" for name in extracted)
    return (
        "const src = " + json.dumps(ui_js) + ";\n"
        + _HARDENED_EXTRACTOR
        + _MINI_DOM_JS
        + "\n" + extracted_js + "\n"
        + stubs
        + "\n" + body + "\n"
    )


# The canonical pre-enhancement stamp statement, extracted verbatim from the
# render tail so the harness exercises the production stamp, not a re-typed one.
STAMP_LINE = (
    "for(const node of inner.children) "
    "node._messageWindowMarkup=node.outerHTML"
    ".replace(/contain-intrinsic-size: auto [\\d.]+px;/g,'');"
)


def stamp_function_source(ui_js: str) -> str:
    """Return a wrapper function around the EXACT production stamp statement."""
    count = ui_js.count(STAMP_LINE)
    if count != 1:
        raise AssertionError(
            "the render-tail pre-enhancement stamp statement must appear exactly "
            f"once in static/ui.js (found {count}); if the stamp moved or changed "
            "shape, update STAMP_LINE to track production"
        )
    return "function _stampRenderTail(inner){" + STAMP_LINE + "}"
