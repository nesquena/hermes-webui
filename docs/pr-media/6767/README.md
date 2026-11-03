# PR #6767 visual and browser evidence

## Scope and sources

Chromium, 1440×1000 and 390×1000, default dark appearance, English (UK).
The real WebUI server, HTML, CSS, task rendering, run-output loader and completion
poller were used with an isolated empty state directory and an inert Agent root.
Cron GET responses are browser fixtures containing two sample jobs and one output;
Pause/Resume POSTs are intercepted and change only the fixture; other cron writes
are rejected. No real jobs, provider calls or production state are used.
This establishes browser rendering and refresh behaviour, not backend scheduling.

`before-*`: master `3179f55609d2483d6489ec707b88ad186716e6cd` panels/CSS.
`review-before-*`: reviewed PR head `9eb8f947c8f47e5fa04b5b4126e88cdf173d9900`
panels/CSS, served in the same current HTML shell to isolate this regression.
`after-*`: fixed implementation in this evidence commit, including the follow-up
that preserves a pending explicit detail refresh when a background poll supersedes it.

## Scenario and observed results

Open Morning summary and click its run header to load sample output. Seed its
unread state so the output highlight is visible. Deliver an unrelated completion
for Sample backup check through the real completion-poll code. Compare the list
and output after the asynchronous work settles.

On master the badge records the completion but the list does not refresh; its
output highlight is green. On the reviewed PR head the new red list dots appear,
but the open output is rebuilt: the run closes and loaded content disappears.
On the fixed head the red dots appear, matching the desktop rail badge, and the
selected run remains open with exactly the same loaded text at both widths.
No uncaught browser errors were observed in any of the six runs.

The follow-up browser checks deliberately hold the list refresh from Pause/Resume,
let a newer completion poll finish first, then release the held request. At both
widths Pause displays the paused state and Resume control; Resume displays the
active state and Pause control. `action-paused-*` and `action-active-*` screenshots
and the `action_races` measurements record these results. An explicit user action
retains the existing detail-refresh behaviour; ordinary completion-only polls
still preserve the loaded output. Subsequent polls do not repeat a completed
explicit detail refresh. Node regressions also cover both completion orders,
form/selection changes and recovery after a failed latest request.

The same-selection Prompt redraw follow-up also holds Pause/Resume's foreground
response, clicks the real Prompt toggle, and starts a background list refresh.
It releases the two requests in both orders at both widths and checks selected
state and actual Pause/Resume button visibility. `prompt-*` screenshots and
`prompt_races` measurements record these checks. The Node regression uses the
real Prompt toggle, detail renderer, status helper and header-button renderer:
all eight action/order/expand-collapse cases failed on `d0289c95` before the fix.
The final suite also checks that changing selection or clearing detail cancels
pending intent. These contributor fixtures are not maintainer gate acceptance.

## Images

| State | Desktop list | Desktop output | Phone drawer | Phone output |
| --- | --- | --- | --- | --- |
| Master | [list](before-1440-list.png) | [output](before-1440-output.png) | [drawer](before-390-list.png) | [output](before-390-output.png) |
| Reviewed PR | [list](review-before-1440-list.png) | [collapsed output](review-before-1440-output.png) | [drawer](review-before-390-list.png) | [collapsed output](review-before-390-output.png) |
| Fixed | [list](after-1440-list.png) | [preserved output](after-1440-output.png) | [drawer](after-390-list.png) | [preserved output](after-390-output.png) |

## UX inspection and limitations

The phone drawer and phone detail were checked separately using the actual
hamburger button. Sample job names truncate in the compact list but retain their
full title attribute; the detail heading and output remain legible. The red dots
and output border use the same unread colour rather than success green. No new
control or layout change was introduced. The isolated gateway warning is expected
because this fixture intentionally has no running Agent gateway.

The desktop document-wide `scrollWidth > innerWidth` probe is true on all three
versions; it is false at phone width. This unchanged measurement is recorded,
not treated as proof that the whole application is overflow-free. The inspected
task list/output screenshots show no new clipping from this PR. This check covers
only the default dark appearance, not all skins, browsers, or real scheduled runs.
Final visual acceptance belongs to the maintainer; these screenshots are evidence,
not a claim of maintainer approval.

## Reproduce

With the repository's Python environment plus Playwright and Chromium installed:

```sh
python docs/pr-media/6767/capture.py
```

`capture.py` starts and stops its own loopback-only server, allocates temporary
fixture state, captures the 24 PNGs, and writes exact browser measurements to
`results.json`. It asserts the reviewed-head collapse and the fixed-head
preservation at both widths. The `after` assets come from the current checkout;
rerunning on changed implementation will produce new evidence.

Related automated verification: 56 tests passed across
`test_cron_refresh_button_835.py`, `test_issue6767_cron_refresh_races.py`,
`test_issue3460_cron_session_unread.py`, and `test_issue1140_cron_badge_per_job.py`.
The refresh-button static signature test now accepts the optional `options`
parameter while retaining `animate` as the first parameter and preserving all
existing button restoration/accessibility checks.

## Review 5522230057 follow-up

The earlier captures and totals above are historical. `review_fixes.py` and
`review_fixes_results.json` qualify this follow-up, comparing the exact reviewed
panels source `448539d4793e5267fec06079ade04d0335cfa4c7` against the current checkout
in the same updated HTML shell. Eight Chromium runs cover both revisions at
1440×1000 and 390×1000, default skin, actual dark and light theme application.

Each run holds the successful Pause/Resume action's list response, injects a
completion, fails the newer list refresh with HTTP 503, then releases the action
response. The reviewed version retains the old status and wrong inverse control;
the fixed version performs a bounded foreground retry and shows the correct
status and control. Both actions are checked. An away-and-back selection race is
also reproduced before and passes after. Empty and nonempty lists that remove
the selected job retain its controls before; after, selection clears and Run/Edit
are hidden. There are no uncaught page errors in these eight runs.

| View | Reviewed version after successful Pause | Fixed version |
| --- | --- | --- |
| Desktop dark | [stale active](review-before-503-1440-dark.png) | [paused with Resume](review-fixed-503-1440-dark.png) |
| Desktop light | [stale active](review-before-503-1440-light.png) | [paused with Resume](review-fixed-503-1440-light.png) |
| Phone dark | [stale active](review-before-503-390-dark.png) | [paused with Resume](review-fixed-503-390-dark.png) |
| Phone light | [stale active](review-before-503-390-light.png) | [paused with Resume](review-fixed-503-390-light.png) |

The list-scroll concern was inferred in the review, not measured. Chromium
retained scrollTop=250 in both versions in this fixture; it did not reproduce a
browser scroll reset. The implementation now explicitly captures scroll after
the response arrives and restores it after a background rebuild. A Node harness
that resets scroll when innerHTML is cleared fails before and passes after;
this is defensive coverage, not proof of a reproduced Chromium defect.

Verification for this follow-up: the eight affected/neighbouring files pass
101 tests; the broader cron, scheduled-job isolation and master/detail selection
sweep passes 330 tests with 8 skips (Agent-dependent/platform coverage).
Collection confirms all 38 refresh-race cases. The previously failing master
selection test is unchanged and passes. New regressions reproduce removal,
missing retry, away-and-back and simulated scroll-reset failures before fixing.
Node syntax, Ruff on changed Python and git diff whitespace checks pass.

Reproduce with the repository test environment plus Playwright/Chromium:
`python docs/pr-media/6767/review_fixes.py`. Use a local filesystem for disposable
pytest state; SQLite fixture creation on a CIFS mount encountered locking errors.
The browser script starts only its own loopback server and stops it in finally;
all cron responses and actions are controlled fixtures with an inert Agent root.
No live scheduling, production restart or maintainer visual approval is claimed.
The full-suite and GitHub CI status are reported separately in the PR update.

## Review 5522946238: save completion must not reopen a deleted task

`save_review.py` compares exact reviewed source `5dcb2fa54b04582f0ae012a983328d011c646701`
with the fixed checkout. It starts its own isolated loopback server and intercepts
create/update/delete/list requests; no production task, provider call or scheduler
is used. Chromium runs the actual form, detail and delete functions. The save's
list response is held; the task is selected and deleted; a newer accepted list
contains only the other task; then the stale save response is released. For create,
a separate accepted read first makes the new task selectable.

All four before runs reopen the deleted task with Run/Edit visible despite its
absence from the list. All four fixed runs keep an empty selection and hide both
controls. Both create and edit are tested at 1440×1000 and 390×1000, default dark
skin, with no uncaught page errors. Exact state is recorded in
`save_review_results.json`; images are `save-before-*` and `save-fixed-*`.
Reproduce with `python docs/pr-media/6767/save_review.py` using Playwright/Chromium.

Save completion now checks the original form element, panel, mode and selection
before mutating form state and before navigating. It only opens a job from the
accepted current list, not the discarded response. Normal accepted saves still
open their saved task. Edit/duplicate metadata is not cleared while a read is
pending or when a newer form replaces it.

The old regression explicitly expected a superseded save response to reopen a
job absent from the latest list; that unsafe expectation is intentionally replaced.
Auto-selection now yields to superseding reads and newer user navigation. This
changes browser completion ownership, not backend save/delete semantics.
Eight stale-response cases failed on the reviewed source before the fix; normal
save cases already passed. Additional tests cover navigation during an accepted
read and form replacement during the pending write. Review 5523242756 follow-up:
a successful create/update whose original form is no longer current still refreshes
the list in the background while Tasks is visible; only the still-current form
may auto-select the saved task. Parameterized create/edit regressions prove the
updated list includes saved data without rebuilding the selected task's open
output. The broader cron, selection, profile-isolation and read-only suite passes
353 tests with 8 skips. Full-suite/CI results remain separately reported, not
inferred from these fixtures.
