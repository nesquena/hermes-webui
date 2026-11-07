# Markdown link boundaries (#6550)

The settled `renderMd()` scanner preserves a later valid Markdown link when a
malformed spaced destination reaches a new whitespace-separated `[label](`
opener. The malformed prefix stays visible; the next link keeps its own target.
Bracket bytes attached to a URL and link-looking text inside a valid quoted title
retain their existing destination behavior.

Raw `<code>` content is protected before inline backtick, math, labeled-link and
autolink processing. Raw `<pre>` is protected first, so moving that boundary does
not consume its nested code. Restoration still runs through the existing HTML
sanitizer. This repair changes rendered transcript HTML, not durable session,
journal, provider history, or user input.

These Chromium captures execute the actual repository `renderMd()` source and
insert its HTML into native DOM, using the repository stylesheet and a small
fixture layout. They compare original PR head `428f7c240fd1` with the repair at
1280px desktop and 390px narrow/mobile viewport widths. All page network requests
are blocked. This is renderer evidence, not a full chat/app lifecycle test.

| Width | Before | After |
| --- | --- | --- |
| Desktop 1280px | [Before](before-1280.png) | [After](after-1280.png) |
| Narrow/mobile 390px | [Before](before-390.png) | [After](after-390.png) |

[Native DOM evidence](browser-evidence.json) records link labels/targets, exact
code text and zero page errors. The three fixtures include the exact two review
inputs and a working spaced-destination control. Regression/property tests in
`tests/test_issue6550_link_scan_boundaries.py` cover paragraphs, lists, quotes,
tables, repeated malformed prefixes, supported destination schemes, raw code,
backticks, math and nested raw preformatted content. Existing sanitizer and
malformed-input growth tests remain part of the neighboring verification.
