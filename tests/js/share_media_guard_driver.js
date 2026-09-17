/**
 * Drive the REAL _inlineMediaHtmlForRef() from static/ui.js, using the same
 * brace-depth extraction the repo's own test_renderer_js_behaviour.py uses.
 * Proves the share-page guard blocks the smuggled-target shapes while leaving
 * a legitimate loopback asset rewrite intact.
 */
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://localhost:8787/app/' };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
function _mediaKindForName(n){
  const s=String(n||'');
  if(_IMAGE_EXTS.test(s)) return 'image';
  if(_AUDIO_EXTS.test(s)) return 'audio';
  if(_VIDEO_EXTS.test(s)) return 'video';
  return 'file';
}
function _mediaPlayerHtml(kind,src,name){ return `<${kind} src="${esc(src)}"></${kind}>`; }
function _dataImageHtml(){ return ''; }
function t(k){ return k; }

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

eval(extractFunc('_unquoteMediaRef'));
eval(extractFunc('_isExternalMediaUrl'));
eval(extractFunc('_localTargetMarkers'));
eval(extractFunc('_decodeUrlComponentBounded'));
eval(extractFunc('_externalMediaUrlHidesLocalTarget'));
eval(extractFunc('_inlineMediaHtmlForRef'));

const cases = [
  // [label, ref, mustBeInert]
  ['plain loopback image still rewrites to origin', 'http://127.0.0.1:8787/img/shot.png', false],
  ['plain localhost image still rewrites to origin', 'http://localhost:8787/img/shot.png', false],
  ['public cdn image renders', 'https://cdn.test/a.png', false],
  ['public cdn with harmless query renders', 'https://cdn.test/a.png?w=800&fmt=webp', false],
  ['loopback /api/media is inert', 'http://127.0.0.1:8787/api/media?path=/etc/shadow', true],
  ['loopback nested MEDIA: is inert', 'http://127.0.0.1:8787/i.png?src=MEDIA:/etc/shadow.png', true],
  ['public cdn nested MEDIA: is inert', 'https://cdn.test/a.png?src=MEDIA:/etc/shadow.png', true],
  ['public cdn /api/media is inert', 'https://cdn.test/api/media?path=/etc/shadow', true],
  ['nested MEDIA: in fragment is inert', 'https://cdn.test/a.png#MEDIA:/etc/shadow.png', true],
  ['file:// in query is inert', 'https://cdn.test/a.png?src=file:///etc/shadow.png', true],
  ['percent-encoded MEDIA: is inert', 'https://cdn.test/a.png?src=%4dEDIA:/etc/shadow.png', true],
  ['double-encoded MEDIA: is inert', 'https://cdn.test/a.png?src=%254dEDIA:/etc/shadow.png', true],
  // Private HOST alone is deliberately NOT inert client-side: the normal app
  // legitimately serves assets from a dev server. The server-side twin in
  // api/helpers.py rejects these for published snapshots.
  ['RFC1918 plain image renders (server-side rejects for shares)', 'http://192.168.1.5/x.png', false],
  ['RFC1918 host reaching /api/media is inert', 'http://192.168.1.5/api/media?path=/etc/shadow', true],
  ['10/8 host nested MEDIA: is inert', 'http://10.0.0.7/i.png?src=MEDIA:/etc/shadow.png', true],
];

let failures = 0;
for (const [label, ref, mustBeInert] of cases) {
  let html;
  try { html = _inlineMediaHtmlForRef(ref); }
  catch (e) { html = 'THREW: ' + e.message; }
  const isInert = /^<code>/.test(html);
  const ok = isInert === mustBeInert;
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'} | ${mustBeInert ? 'inert ' : 'render'} | ${label}`);
  if (!ok) console.log(`       got: ${String(html).slice(0, 200)}`);
  // A rendered loopback ref must actually have been retargeted at the origin.
  if (ok && !mustBeInert && /127\.0\.0\.1|localhost/.test(ref)) {
    const rewritten = html.includes('localhost:8787/app');
    if (!rewritten) { failures++; console.log(`FAIL | loopback rewrite lost for ${ref}: ${html.slice(0,160)}`); }
    else console.log('       (rewrite to origin preserved)');
  }
  // No inert output may leak the smuggled path as a LIVE attribute. The inert
  // <code> body legitimately contains the escaped URL text (which can include
  // the literal characters "src="), so assert on real markup instead: no
  // element with a src/href attribute, and no unescaped angle brackets.
  if (isInert) {
    if (/<(img|a|audio|video|iframe|script)\b/i.test(html)) {
      failures++; console.log('FAIL | inert output produced a live element');
    }
    const body = html.replace(/^<code>/, '').replace(/<\/code>$/, '');
    if (/[<>]/.test(body)) {
      failures++; console.log('FAIL | inert output body is not escaped: ' + body.slice(0,120));
    }
  }
}
console.log(failures === 0 ? '\nALL OK' : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
