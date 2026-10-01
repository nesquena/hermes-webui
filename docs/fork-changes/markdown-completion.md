# Prompt Markdown settlement

Classification: upstream-candidate
Upstream base: origin/master, 3fae64a9e1283f45c53e73ad5b76644ff4dc1017
Maintenance owner: deployment fork maintainer
Upstream status: issue-ready; not filed
Private details removed: yes

## Problem and reproduction

With word fades enabled, a completed response remains busy and unformatted while
cosmetic playout drains. Run `python tests/browser_markdown_completion.py` with
the repository test interpreter and Playwright Chromium/WebKit installed.
The fixture sends a Markdown token burst, then a canonical `done` session.
Before the fix, the immediate formatted-answer, buffered-tail, and idle assertions
fail. After it, all are satisfied within the same event dispatch.

## Contract change

Old: completion waits for a bounded fade drain and a final animation delay.
New: canonical `done` settles immediately using the existing completion handler.
Live word fades remain unchanged; pending decorative words may snap into the
completed answer. This is intentional: animation must not gate available content.
The state layer changed is client presentation scheduling, not server persistence,
stream transport, Markdown parsing, or the canonical session representation.

## Change and verification

Remove the terminal fade-drain branch, its two private helpers, and obsolete
completion-only constants. Existing completion performs parser cleanup, cancels
scheduled token rendering, projects the settled anchor, and adopts the canonical
session. No alternate renderer, new setting, timer, dependency, or fork-only hook.

The browser regression covers both activity modes, fade on/off, short and buffered
answers, desktop/narrow widths, and a trailing stream_end. It asserts complete
Markdown and idle state synchronously, then checks no duplicate settled heading.
191 neighboring tests and both-engine browser completion/reconnect checks passed.
Synthetic transport is used; no claim of physical iPad energy measurement or
provider last-token-to-done latency. Full suite not rerun for this patch.

## Maintenance and upstream handoff

This is one logical diff in messages.js plus its tests and this record. The
production hunks apply independently of the tool-event optimization; reconstruct
on current upstream and run the regression before contribution. Do not submit
the deployment branch and its unrelated history as an upstream PR.
Rollback: revert this logical commit. On an equivalent upstream fix, merge upstream,
retain this regression, compare semantics, and retire the redundant local delta.
No blind conflict resolution: verify buffered tails, terminal cleanup, stale events,
and mode/fade variants. An upstream implementation need not resemble this code.
