#!/usr/bin/env python3
"""Browser gate: the public share page shows a message as plain text when it
would hide part of it, and never shows a formatted message with hidden text.

``static/share.js`` renders each message with ``renderMd`` and then compares the
words of the published text with the words the rendered page shows. If any word
is missing, it shows the whole message as plain text. This gate boots the real
``server.py`` with isolated state, imports conversations, shares them, opens the
real share page in Chromium and checks:

1. every message in ``HIDING`` (markup the renderer hides) is shown as plain
   text, with its marker word visible;
2. every message in ``ORDINARY`` (realistic Markdown, code, math, tables,
   images, links) keeps its formatting;
3. a differential run over random messages built from the constructs renderMd
   treats specially: for every message shown formatted, each marker word is
   visible on the page. "Visible" is the browser's own layout-aware
   ``innerText``, plus link and image addresses and image alt text, which are
   out of scope (a word glued to an address becomes part of it).

Run:
    python tests/browser_share_hidden_text.py

Exits 0 on success, 1 on a failed check, 2 on a setup problem.
"""
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MARK = "zqmark"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = int(os.environ.get("SHARE_GATE_PORT") or _free_port())
BASE = f"http://127.0.0.1:{PORT}"

# Markup the renderer hides. Each must be shown as plain text.
HIDING = [
    f"before <!-- {MARK} --> after",
    f"before &lt;!-- {MARK} --&gt; after",
    f"before &amp;lt;!-- {MARK} --&amp;gt; after",
    f'<span data-note="{MARK}">hello</span>',
    f'<span title="{MARK}">hello</span>',
    f'<a href="https://example.com/&quot;title=&quot;{MARK}">docs</a>',
    f'<a href="https://example.com/${MARK}$">docs</a>',
    f"<custom-{MARK}>text</custom-{MARK}>",
    f"see <https://example.com/x> and <{MARK}> here",
    f"```js some {MARK} words\ncode\n```",
    f"> ```js {MARK} words\n> x\n> ```",
    f"$x `{MARK}` y$",
    f"\\( see `{MARK}` \\)",
    f"a $b $${MARK}$$ c$ d",
    f"<pre>a `{MARK}` b</pre>",
    f"- see\u2028{MARK} here",
    f"visible word {MARK} and a hidden copy <!-- {MARK} -->",
    # A bare link shows its address once; a hidden copy of an address word still counts.
    f"see https://example.com/{MARK} <!-- {MARK} -->",
    # A code language class is free text, so it never counts as shown.
    f'<code class="language-{MARK}">x</code>',
    # Free-text attributes the renderer keeps from raw HTML never count as shown.
    f'<div class="mermaid-block" data-mermaid-id="{MARK}">x</div>',
    f'<a href="https://example.com/f" download="{MARK}">file</a>',
    # Tag syntax inside a comment is hidden text, not markup (only standard names,
    # so nothing but the comment rule can catch these).
    "before <!-- <mark> <select> <option> --> after",
    "before <!-- &#60;mark&#62; --> after",
    "before <!-- &#122;&#113;&#109;&#97;&#114;&#107; --> after",
    # A fence language the renderer drops (not a word-led single word) is hidden.
    f"```+the-{MARK}-is-here\ncode\n```",
    f"```-wire-{MARK}\ncode\n```",
    f"<!--\n```{MARK}-pay-now\n-->",
    # A literal entity or %-escape can't credit a hidden copy of a word.
    f"Use `&{MARK};` literally; <!-- {MARK} -->",
    f"[docs](https://example.com/%7A{MARK[1:]}) <!-- {MARK} -->",
    f"see https://example.com/{MARK}/%41 <!-- {MARK} -->",
    # A list item's number shows only inside a numbered list, and only up to the browser's limit.
    '<ul><li value="731337">item</li></ul>',
    '<li value="731338">item</li>',
    '- one\n<li value="731339">two</li>',
    '<ol><li value="99999999999">item</li></ol>',
    # ...and not in full past four digits, or inside a block that clips the margin.
    "12345. item",
    '<div class="katex-block"><ol><li value="42">x</li></ol></div>',
    '<div class="mermaid-block"><ol><li value="123">x</li></ol></div>',
    '<pre><ol><li value="4321">x</li></ol></pre>',
    # A raw anchor whose address the renderer drops, with an encoded label; other attributes of an
    # address-labelled anchor.
    '<a href="secret-plan">%73%65%63%72%65%74-%70%6C%61%6E</a>',
    '<a href="https://e.com/zqd3x" title="zqd4x">https://e.com/zqd3x</a>',
    '<a href="https://e.com/zqd9x"/title="zqe1x">https://e.com/zqd9x</a>',
    # A word shown inside code can't pay for the same word hidden in a comment, even where the
    # marker, task-box, mermaid, link and anchor rules apply outside code.
    "`MEDIA: hello` <!-- media -->",
    "`file:///tmp/report.txt` <!-- file -->",
    "```markdown\n- [x] done\n```\n<!-- x -->",
    "````markdown\n```mermaid\ngraph TD\n```\n````\n<!-- mermaid -->",
    f'`<a href="https://example.com/{MARK}">https://example.com/{MARK}</a>` <!-- {MARK} -->',
    f"`[https://example.com/{MARK}](https://example.com/{MARK})` <!-- {MARK} -->",
    # A marker word the page still shows can't pay for a hidden copy of itself: a mermaid fence's
    # language, MEDIA: the renderer leaves as text, and file:// outside a bare address or link.
    "```mermaid\ngraph TD\n  A-->B\n```\n<!-- mermaid -->",
    "```mermaid\nnot a diagram\n```\n<!-- mermaid -->",
    "<!-- mermaid -->\n```mermaid\ngraph TD",
    "MEDIA: hello <!-- media -->",
    "**MEDIA:** <!-- media -->",
    "See file:// here <!-- file -->",
    "Saved (file:///tmp/r.txt) <!-- file -->",
    "<pre>see file:///tmp/x.txt</pre>\n<!-- file -->",
    "`a.`MEDIA:` <!-- media -->",
    # (A remote address: publishing rewrites local MEDIA: paths, so the published text would differ.)
    "`MEDIA:https://example.com/a.png` <!-- media -->",
]

# Realistic content that must keep its formatting.
ORDINARY = [
    "Plain prose with **bold**, _italic_, ~~struck~~ and `inline code`.",
    "## Setup\n\n1. Install the package\n2. Run `make test`\n   - on CI too\n3. Done\n\n> Note: this takes a minute.",
    "| Name | Value |\n|---|:---:|\n| alpha | 1 |\n| beta | 2 |",
    "```python\ndef add(a, b):\n    return a + b\n```\n\nThen call `add(1, 2)`.",
    "```mermaid\ngraph TD\n  A --> B\n```",
    'Use `Vec<String>` and `<div class="row">` in the example.',
    "Inline math $a^2 + b^2 = c^2$ and display math:\n\n$$\\int_0^1 x\\,dx = \\frac{1}{2}$$",
    "Costs $5 and $10; set $HOME first.",
    "See [the docs](https://example.com/docs/intro) and https://example.com/raw for details.",
    "![diagram](https://example.com/diagram.png)",
    "- [x] write the code\n- [ ] write the tests",
    "Line one<br>line two, E = mc<sup>2</sup>, press <kbd>Ctrl</kbd>.",
    "AT&T &amp; friends &gt; others, 3 < 4.",
    "中文**粗体**文字，还有 `代码`。 Émoji 🎉 and naïve café.",
    "un*believ*able and snake_case_name",
    "Shell:\n\n```bash\nexport PATH=\"$HOME/bin:$PATH\"\necho \"done\"\n```",
    # The renderer doesn't support link titles, so this stays visible text.
    '[docs](https://example.com/p "a title")',
    # Character references the browser decodes as text, in prose and in a table.
    "Price&nbsp;list &mdash; &copy; 2026 &rarr; 20&#176;C &#8594; &#x41;BC caf&eacute;",
    "| Item | Note |\n|---|---|\n| a&nbsp;b | &copy;&nbsp;Acme |",
    # Encoded tags show as text, so they must not be read as markup.
    "Write &#60;b&#62;bo&#60;/b&#62;ld to show the tags.",
    # A link labelled with its own address, Markdown and HTML forms.
    "[https://example.com/docs](https://example.com/docs)",
    '<a href="https://example.com/docs">https://example.com/docs</a>',
    # Inline math between CJK characters, and entities next to a link.
    "这是$x$的值。See [details](https://example.com/math).",
    "Copyright &copy; 2026. See [manual](https://example.com/manual).",
    # An entity shown as written inside code; a local file link.
    "Write `&copy;` to get the symbol.",
    "Saved to [report.txt](file:///tmp/report.txt) and file:///tmp/notes.txt",
    "```python\nprint('hi')\n```",
    # A label that differs from its address only by a trailing slash, an address-labelled anchor with
    # extra attributes, and a numbered list that starts later (its numbers are shown).
    "[https://example.com](https://example.com/)",
    '<a href="https://example.com/docs" target="_blank" rel="noopener">https://example.com/docs</a>',
    "3. third step\n4. fourth step",
    # HTML examples in code keep their formatting and neighbouring links; year lists keep theirs.
    "HTML: `<span>Hello</span><span>world</span>`. See [documentation](https://example.org/html).",
    "```html\n<b>bold</b><i>it</i>\n```",
    "2023. Launched\n2024. Grew",
    # Markers, task boxes, a mermaid fence and links written inside code are shown as written; a
    # link labelled with a code span of its own address shows the address once.
    "Use `MEDIA: path/to/file.png` in a reply to attach a file.",
    "Open `file:///tmp/report.txt` in a browser.",
    "```markdown\n- [x] done\n- [ ] todo\n```",
    "````markdown\n```mermaid\ngraph TD\n```\n````",
    'Write `<a href="https://example.com/docs">https://example.com/docs</a>` for a link.',
    "[`https://example.com/docs`](https://example.com/docs)",
    # Marker words shown on the page, and markers the renderer turns into media.
    "```mermaid\nnot a diagram\n```",
    "```mermaid\ngraph TD",
    "MEDIA: hello",
    "**MEDIA:**",
    "See file:// here",
    "Saved (file:///tmp/r.txt)",
    "<pre>see file:///tmp/x.txt</pre>",
    "Here: MEDIA:/tmp/chart.png",
    "Report: [report](file:///tmp/report.txt)",
    "`a.`MEDIA:`",
]

# Random messages: tokens renderMd treats specially, with numbered marker words.
# Address tokens end with a space so a marker never joins an address.
FUZZ_TOKENS = [
    "<", ">", "`", "``", "```", "````", "\n", "\n\n", " ", "&lt;", "&gt;", "&amp;", "&quot;", "&#39;",
    "&amp;lt;", "<!--", "-->", "<b>", "</b>", "<br>", "<span>", "</span>", "<x>", "<span title=\"",
    '<a href="https://e.com/p" ', '">', '<img src="https://e.com/i.png" ', '"', "'", "> ", "- ", "1. ",
    "# ", "| ", " |", "|---|", "**", "*", "_", "~~", "$", "\\(", "\\)", "```js", "```js x", "```c++",
    "[", "]", "(", ")", "$$", "\\[", "\\]", "$x", "x$", "\n```\n", "\n```py\n", "- [x] ", "\u2028",
    "MEDIA:https://e.com/m.png ", "https://e.com/p ", "<https://e.com/ ", "](https://e.com/l ",
    "\t", "   ", ":", "!", "?", "/", "=", "#",
    "&nbsp;", "&copy;", "&mdash;", "&#65;", "&#x3c;", "&#60;", "&lt;!--", "<mark>", "<select>",
    "\n```+a-", "\n```-b", "\n```c#", "%7A", "%41", "%20", "这是", "\\(", "[https://e.com/s](https://e.com/s) ",
    "file:///tmp/f ", "`&copy;`",
]


def _fuzz_messages(seed, count):
    rng = random.Random(seed)
    out = []
    for i in range(count):
        parts, words = [], 0
        for _ in range(rng.randint(1, 18)):
            if rng.random() < 0.35:
                parts.append(f"zq{i}w{words}x")
                words += 1
            else:
                parts.append(rng.choice(FUZZ_TOKENS))
        if not words:  # the importer drops a blank message, which would shift every later row
            parts.append(f"zq{i}w0x")
            words = 1
        out.append(("".join(parts), [f"zq{i}w{k}x" for k in range(words)]))
    return out


SEED_JS = """async (conversations) => {
  const post = async (path, body) => {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await r.json();
    if (!r.ok) throw new Error(path + ' failed: ' + JSON.stringify(data));
    return data;
  };
  const tokens = [];
  for (const conv of conversations) {
    const imported = await post('/api/session/import', {title: conv.title, messages: conv.messages});
    const shared = await post('/api/share/create', {session_id: imported.session.session_id});
    const token = (shared.session && shared.session.share_token) || shared.share_token || shared.token;
    if (!token) throw new Error('no share token: ' + JSON.stringify(shared));
    tokens.push(token);
  }
  return tokens;
}"""

READ_JS = """() => Array.from(document.querySelectorAll('.share-message')).map(row => {
  const body = row.querySelector('.share-message-body');
  const addresses = Array.from(body.querySelectorAll('a[href], img'))
    .map(el => [el.getAttribute('href'), el.getAttribute('src'), el.getAttribute('alt'), el.getAttribute('title')].filter(Boolean).join(' '))
    .join(' ');
  return {plain: body.classList.contains('share-plain'), text: body.innerText, addresses};
})"""


def _wait_for_health(timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _read_share(browser, token, expected):
    ctx = browser.new_context(base_url=BASE)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"/share/{token}", wait_until="domcontentloaded")
    deadline = time.time() + 60
    rows = []
    while time.time() < deadline:
        rows = page.evaluate(READ_JS)
        if len(rows) >= expected:
            break
        page.wait_for_timeout(100)
    ctx.close()
    return rows, errors


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-share-gate-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })
    seeds = [int(s) for s in os.environ.get("SHARE_GATE_SEEDS", "1,2").split(",") if s]
    per_seed = int(os.environ.get("SHARE_GATE_FUZZ", "600"))
    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen([sys.executable, server_py], cwd=repo_root, env=env, stdout=log, stderr=subprocess.STDOUT)
    failures = []  # row-count mismatches are reported, so the zips below need not be strict
    try:
        if not _wait_for_health():
            print("SETUP FAIL: server did not become healthy", file=sys.stderr)
            return 2
        convs = [
            {"title": "hiding", "messages": [{"role": "assistant", "content": c} for c in HIDING]},
            {"title": "ordinary", "messages": [{"role": "assistant", "content": c} for c in ORDINARY]},
        ]
        fuzz = []
        for seed in seeds:
            cases = _fuzz_messages(seed, per_seed)
            fuzz.append(cases)
            convs.append({"title": f"fuzz {seed}", "messages": [{"role": "assistant", "content": c} for c, _ in cases]})
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(base_url=BASE)
            page = ctx.new_page()
            page.goto("/", wait_until="domcontentloaded")
            tokens = page.evaluate(SEED_JS, convs)
            ctx.close()

            rows, errors = _read_share(browser, tokens[0], len(HIDING))
            failures += [f"hiding page error: {e}" for e in errors]
            if len(rows) != len(HIDING):
                failures.append(f"hiding: {len(rows)} rows, expected {len(HIDING)}")
            for content, row in zip(HIDING, rows, strict=False):
                if not row["plain"]:
                    failures.append(f"hiding shown formatted: {content!r}")
                elif content not in row["text"]:
                    failures.append(f"hiding fallback doesn't show the exact published text: {content!r}")
            print(f"hiding: {sum(r['plain'] for r in rows)}/{len(HIDING)} shown as plain text")

            rows, errors = _read_share(browser, tokens[1], len(ORDINARY))
            failures += [f"ordinary page error: {e}" for e in errors]
            for content, row in zip(ORDINARY, rows, strict=False):
                if row["plain"]:
                    failures.append(f"ordinary shown as plain text: {content!r}")
            print(f"ordinary: {sum(not r['plain'] for r in rows)}/{len(ORDINARY)} keep their formatting")

            for seed, cases, token in zip(seeds, fuzz, tokens[2:], strict=True):
                rows, errors = _read_share(browser, token, len(cases))
                failures += [f"fuzz {seed} page error: {e}" for e in errors]
                if len(rows) != len(cases):
                    failures.append(f"fuzz {seed}: {len(rows)} rows, expected {len(cases)}")
                formatted = misses = 0
                for (content, marks), row in zip(cases, rows, strict=False):
                    seen = row["text"] + " " + row["addresses"]
                    missing = [m for m in marks if m not in seen]
                    if not row["plain"]:
                        formatted += 1
                    if missing:
                        misses += 1
                        if misses <= 5:
                            failures.append(f"fuzz {seed}: hidden {missing} in {'formatted' if not row['plain'] else 'plain'} {content!r}")
                print(f"fuzz {seed}: {len(cases)} messages, {formatted} formatted, {misses} with hidden words")
                if misses:
                    failures.append(f"fuzz {seed}: {misses} messages with hidden words")
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    if failures:
        print("\nSHARE HIDDEN-TEXT GATE FAILED:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("\nSHARE HIDDEN-TEXT GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
