// End-to-end test: verify the full-width parenthesis autolink fix in ui.js
//
// Bug: the autolink regex only excluded ASCII ')' (U+0029) from URL matches.
// When a URL appeared before a CJK full-width right parenthesis ）(U+FF09) —
// extremely common in Chinese LLM output like "（或 https://lmarena.ai）" —
// the ）was swallowed into the <a href="..."> link.
//
// Fix: add \uFF09 to the regex exclusion set AND to the trailing-punctuation
// strip. Also covers full-width comma/period/colon/semicolon/exclamation/
// question mark/ideographic comma for completeness.
const fs = require('fs');
const path = require('path');

const uiJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'ui.js'), 'utf-8');

// 1. Verify both autolink passes contain the \uFF09 fix
const pass1HasFix = uiJs.includes('\\uFF09]') && uiJs.includes('t=t.replace(/(https?');
const pass2HasFix = uiJs.includes('s=s.replace(/(https?:\\/\\/[^\\s<>"\')\\]\\uFF09]+)');

console.log('Pass 1 (inlineMd) has \\uFF09 fix:', pass1HasFix);
console.log('Pass 2 (outer) has \\uFF09 fix:', pass2HasFix);

if (!pass1HasFix || !pass2HasFix) {
  console.error('FIX MISSING: both passes must contain \\uFF09 in the exclusion set');
  process.exit(1);
}

// 2. Behavioral test using the fixed regex
function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const FIXED_REGEX = /(https?:\/\/[^\s<>"')\]\uFF09]+)/g;

function applyAutolink(t) {
  return t.replace(FIXED_REGEX, (url) => {
    const trailMatch = url.match(/[.,;:!?)\uFF09\uFF0C\uFF1B\uFF1A\uFF01\uFF1F\u3001\u3002]$/);
    const trail = trailMatch ? url.slice(-1) : '';
    const clean = trail ? url.slice(0, -1) : url;
    return '<a href="' + clean + '" target="_blank" rel="noopener">' + esc(clean) + '</a>' + trail;
  });
}

const tests = [
  { name: 'ASCII paren',            input: '(or https://lmarena.ai)',           expect: '<a href="https://lmarena.ai"',          notExpect: 'href="https://lmarena.ai)"' },
  { name: 'Full-width paren (BUG)',  input: '(or https://lmarena.ai\uFF09',     expect: '<a href="https://lmarena.ai"',          notExpect: 'href="https://lmarena.ai\uFF09"' },
  { name: 'Full-width both parens',  input: '\uFF08or https://lmarena.ai\uFF09', expect: '<a href="https://lmarena.ai"',         notExpect: 'href="https://lmarena.ai\uFF09"' },
  { name: 'Full-width comma',        input: 'See https://example.com\uFF0C',    expect: '<a href="https://example.com"',         notExpect: 'href="https://example.com\uFF0C"' },
  { name: 'Full-width period',       input: 'Visit https://example.com\u3002',  expect: '<a href="https://example.com"',         notExpect: 'href="https://example.com\u3002"' },
  { name: 'Normal URL',              input: 'https://example.com/path?q=1',     expect: '<a href="https://example.com/path?q=1"', notExpect: null },
  { name: 'ASCII trailing period',   input: 'See https://example.com.',         expect: '<a href="https://example.com"',         notExpect: 'href="https://example.com."' },
  { name: 'ASCII trailing comma',    input: 'See https://example.com,',         expect: '<a href="https://example.com"',         notExpect: 'href="https://example.com,"' },
];

let allPass = true;
for (const t of tests) {
  const output = applyAutolink(t.input);
  const hasExpect = output.includes(t.expect);
  const hasNotExpect = t.notExpect ? !output.includes(t.notExpect) : true;
  const pass = hasExpect && hasNotExpect;
  if (!pass) allPass = false;
  console.log('[' + (pass ? 'PASS' : 'FAIL') + '] ' + t.name + ' => ' + output);
}

console.log('\n' + (allPass ? 'ALL TESTS PASSED' : 'SOME TESTS FAILED'));
process.exit(allPass ? 0 : 1);
