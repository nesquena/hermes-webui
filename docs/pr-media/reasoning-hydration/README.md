# Stored reasoning identity: browser evidence

These are synthetic persisted-session fixtures, not user conversations. They run
through `Session.save()`, the real WebUI HTTP session loader and the unmodified
browser renderer. No Agent/provider invocation is made. Public static assets load
as in the existing browser gates; this is not a network sandbox certification.

Frozen baseline: `ff26335b87610f70d74ff008608a321fb1281bd1`.
The fixture contains two same-text events with IDs A/B and one exact redelivery
of A. Before the API fix, both initial load and hard reload show only A. After the
fix they show A/B exactly once. Tests assert the DOM row identities, not merely
that two labels are visible. The Thinking cards are collapsed in these captures;
the machine-readable snapshots also check their identical retained text.

| State | Desktop (1280px) | Narrow (390px) |
| --- | --- | --- |
| Before | ![One retained event](before-1280.png) | ![One retained event](before-390.png) |
| After | ![Two distinct events retained](after-1280.png) | ![Two distinct events retained](after-390.png) |

Reproduce from the candidate checkout:

```sh
.venv/bin/python tests/browser_reasoning_identity_hydration.py --artifact-dir /tmp/reasoning-proof
```

The same driver on the baseline fails all four load/reload × desktop/narrow
identity assertions. It uses disposable Hermes/state/workspace directories and
stops its own server/browser; it does not modify production sessions. Test setup
initially lacked the seed directory and a trial's blanket external-resource block
caused console failures. Neither was counted as product RED evidence. The final
baseline/candidate comparison uses the same corrected driver.

## October 1 filtered-reasoning regression

Baseline: reviewed head `5ca72febbab3` plus normal upstream merge
`dd5451903686` (product fix absent). A persisted running Thinking row carries
its exact stream identity. Transcript reasoning either repeats visible progress
or matches the final answer; neither should consume a reconciliation slot.

Both variants fail the intended DOM identity assertion on baseline: the two saved
Thinking events are absent on load and hard reload at 1280px and 390px. After the
slot filter, both variants preserve A/B exactly once in all eight combinations.
The four images below show the visible-prose variant after hard reload; the
final-answer variant has the same missing/preserved event outcome.

| State | Desktop (1280px) | Narrow (390px) |
| --- | --- | --- |
| Before | ![Saved Thinking events missing](before-filter-1280.png) | ![Saved Thinking events missing](before-filter-390.png) |
| After | ![Both saved Thinking events retained](after-filter-1280.png) | ![Both saved Thinking events retained](after-filter-390.png) |

```sh
.venv/bin/python tests/browser_reasoning_identity_hydration.py --filtered-reasoning visible-prose --artifact-dir /tmp/reasoning-filter-prose
.venv/bin/python tests/browser_reasoning_identity_hydration.py --filtered-reasoning final-answer --artifact-dir /tmp/reasoning-filter-final
```

These remain synthetic, isolated Chromium checks; no physical-device or live
provider behavior is claimed. An initial trial lacked per-row stream ownership
and passed on baseline, so it is not negative proof. A second trial waited for
an absent worklog and timed out; the final driver captures missing rows and fails
on their identities instead of counting a timeout as evidence.


## October 1 second review: missing reasoning metadata

No-tool `thinking` content with no reasoning metadata must not allocate a
settlement slot that discards a distinct saved running event. The synthetic
persisted fixture contains `Transcript thought` in content parts and saved
`Distinct saved thought` with event ID `saved-event` and row ID `saved-row`.

The baseline is reviewed head `b2fadb981b90988138d1a34bf55b2bf415ac4471`
plus upstream `de512304b306211e71da103b5c218b17ef8d4ddc`, merge commit
`7cf6fc55b7a5244cf25960cd24fd7ab2bd153ee0`. On that baseline, actual server
load and hard reload lose the saved ID and show `Transcript thought` at both
1280 and 390 px. The repaired product commit
`6a3c432fde1a97d5261696bdac04cdbb210d8cf1` preserves the saved row/event IDs,
`Distinct saved thought`, completed status, and exactly one final answer.
Both missing and empty metadata cases pass all eight width/load combinations.
Thinking details were opened with real header clicks before capture.

| Width | Before | After |
| --- | --- | --- |
| Desktop 1280 | [Before](no-metadata-before-1280.png) | [After](no-metadata-after-1280.png) |
| Narrow/mobile 390 | [Before](no-metadata-before-390.png) | [After](no-metadata-after-390.png) |

```sh
python tests/browser_reasoning_identity_hydration.py --no-tool-reasoning missing-metadata --artifact-dir /tmp/reasoning-no-metadata
python tests/browser_reasoning_identity_hydration.py --no-tool-reasoning empty-metadata --artifact-dir /tmp/reasoning-empty-metadata
```

The driver launches isolated temporary state/workspace and an Agent-free stub;
no real provider or personal session is used. Existing visible-prose/final-answer
filtering and equal-text/different-ID browser cases also passed.


## October 1 third review: transcript Markdown owns the prose body

Baseline: reviewed head `b0bf02710664` plus pinned upstream
`e33da25c6cfde3ea337e1b6d7701161d02e995cf`, normal merge
`b6198a23fa9493f9a24d5f45c549fbd722196e52`. The saved running prose row
carries `saved-prose-event` / `saved-prose-row`, but its live text has collapsed
whitespace. The settled transcript contains exact Markdown and a `read_file`
tool call before the final answer.

Before the repair, hydration inherits the saved row's flattened body as well as
its identity: the fenced block has no native `pre code` element, and the two
lists collapse into one item. After the repair, hydration retains the exact
transcript text and payload, source and grouping metadata, while inheriting
only durable identity fields. The browser keeps a native fenced block with
`print(42)`, an unordered list with `a` / `b`, an ordered list with `c`, one tool
row, and one final answer through both initial load and hard reload. The images
below capture hard reload at 1280 × 900 and 390 × 900.

| Fixture / state | Desktop | Narrow/mobile |
| --- | --- | --- |
| Fenced code before | [Before](markdown-fenced-code-before-1280.png) | [Before](markdown-fenced-code-before-390.png) |
| Fenced code after | [After](markdown-fenced-code-after-1280.png) | [After](markdown-fenced-code-after-390.png) |
| Lists before | [Before](markdown-lists-before-1280.png) | [Before](markdown-lists-before-390.png) |
| Lists after | [After](markdown-lists-after-1280.png) | [After](markdown-lists-after-390.png) |

```sh
.venv/bin/python tests/browser_reasoning_identity_hydration.py --transcript-markdown fenced-code --artifact-dir /tmp/prose-fenced
.venv/bin/python tests/browser_reasoning_identity_hydration.py --transcript-markdown lists --artifact-dir /tmp/prose-lists
.venv/bin/python tests/browser_reasoning_identity_hydration.py --transcript-markdown indented-code --artifact-dir /tmp/prose-indented
```

The exact indented-code review fixture is covered too: hydration must retain
`    print(42)\n    print(43)` byte-for-byte in scene text and payload. Current
`renderMd` already renders this fixture as a paragraph when given the exact
transcript, rather than a native indented code block. The browser's indented
case checks source ownership and parity with that existing renderer; this
repair does not claim to add native indented-code syntax support. Existing
Thinking text/identity behavior and metadata-only no-tool reasoning boundaries
remain covered by the same driver and focused regressions.

These are synthetic persisted sessions, real HTTP loads and Chromium DOM
checks, with isolated temporary Hermes/state/workspace and no provider request
or personal data. Machine-readable snapshots capture both load and reload.
