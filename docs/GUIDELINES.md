# Change guidelines — writing a PR that lands cleanly

Hermes WebUI is deliberately simple: Python on the server, vanilla JS in the browser, no build
step. That simplicity only survives if every change is scoped, verified, and complete. This
document is the distilled set of principles that make the difference between a PR that merges on the
first review and one that needs several rounds of back-and-forth. It applies to human and
AI-assisted contributions alike — but it is written especially for AI coding agents, because the
mistakes below are the ones they make most often and most confidently.

If you read nothing else, read the four root causes. The ten rules are just their enforcement.

---

## The four root causes of a change that has to be redone

Almost every change that fails review fails for one of these reasons. Learn to catch them in your
own work before you open the PR.

1. **Fixing the instance instead of the class.** *The single most common one.* A bug usually has
   more than one home — the same mistake lives at several call sites, across more than one backend
   or code path, in a companion endpoint, in a second layout, on more than one lifecycle exit. It's
   easy to fix the exact line the report points at and leave the siblings broken. The missed one is
   usually a *different dimension* than the one you were looking at (a second backend, an unusual
   layout, the cancel path, the multi-item case).

2. **Mistaking a proxy for proof.** "The test passes," "the code contains the string," "that branch
   ran," "I mocked it and it returned the right thing" — none of these prove the *user-visible
   behavior* is correct. A test that would still pass if you reverted the fix is testing nothing.

3. **Reasoning about a value's content but not its lifetime.** You get the value right but miss when
   it goes stale, who is responsible for cleaning it up, and what happens to it on every exit path
   (success, error, cancel, replace, shrink, concurrent access). Leaked resources, orphaned caches,
   stale reads, and "the decision used one value but the action used another" all live here.

4. **Letting the diff grow past the task.** Extra changes you weren't asked for, or re-implementing
   something the codebase already centralizes (a fallback, a shared helper, an extension point).
   Every extra line is a new risk surface and makes the change harder to review and to trust.

A fifth applies only to *visible* changes: **placing a control where the code is, not where the
user's attention should go.**

---

## The ten rules

### 1. Fix the class, not the instance.
Before writing the fix, search for every sibling — other call sites, producers and consumers,
alternate backends, companion endpoints, other layouts, every lifecycle exit — and fix at the
**shared chokepoint**. If you deliberately leave one out of scope, name it in the PR and say why.
One guard in the shared function is a smaller and more correct change than one guard per caller.
The chokepoint is the *smallest* boundary that contains the fault, not the widest one you can
reach: fixing the class means guarding every sibling of the bug, not disabling a whole stage or
pipeline to suppress one bad output. If your fix blocks more than the fault occupies, it has grown
past the task (rule 9).

### 2. Trace one authoritative value end-to-end.
Follow the value from `input → normalize → decision → action → persist → cleanup`, and use the
**same** resolved value at every stage. Don't let the code that *decides* see one value while the
code that *acts* uses the stale or un-normalized one — that split is a silent correctness bug.
Before you key a decision on a value, confirm it's *authoritative*: written at the point of intent
(a canonical kind field, a provenance marker stamped by the action itself, a recorded origin) — not
inferred from an id prefix, content shape, emptiness, or DOM state. Search for an existing canonical
field first; inference that happens to work on the case in front of you is the most common way a
guard misfires on a sibling. If no authoritative field exists, add a test with an adjacent case that
matches your heuristic and must *not* trigger.

### 3. When you can't confirm something, fail closed and say so.
For anything touching authority, capability, identity, or containment: if you cannot *confirm* it's
safe, deny it — don't take the permissive branch on uncertainty, and never report a failure as a
success. Treat "unknown", "absent", and "unverifiable" as distinct from "allowed." Stating "I could
not verify X" in the PR is good engineering; hiding it is how a security or reliability bug ships.

### 4. Enumerate the state-space before you edit.
Write down which dimensions the change touches — entry point, backend, item count (0 / 1 / many /
duplicate), lifecycle exit (success / error / cancel / replace / shrink / teardown), auth on vs off,
concurrency (two profiles or workers at once), input shape (empty / hostile / aliased) — and cover
each, or mark it out of scope on purpose. Most redo rounds are one un-considered dimension. Those
axes are generic; the ones that actually bite are subsystem-specific and are not obvious to a
first-time contributor. Before editing an unfamiliar subsystem, find its real variants: read the
closed PRs and review threads that touched the same code, where the discriminating dimensions
(every session-lineage kind, every replay/recovery source, each stream response shape) were already
enumerated. Reuse that list rather than inventing your own from the one case you were handed.

### 5. Assume inputs and check-then-use gaps are adversarial.
Input crossing a trust boundary can be crafted to break a naive check (odd delimiters, casing, YAML
aliases, path traversal). Filesystem and process state can change between the moment you check it
and the moment you use it, so validate at the **point of use** (hold a file descriptor/handle rather
than re-resolving a path). Scope caches by their *complete* identity or they leak across
profiles/sessions under concurrency. Validation is worthless if the thing you finally use isn't the
thing you validated.

### 6. A test must fail before your fix and pass after it.
Run your new test against the current code *first* and confirm it fails for the right reason; only
then apply the fix. Assert the **observable behavior** (the row updates, the secret is absent from
the log, the request carries the right value) — not a source string, and not through a mock that
stands in for the very thing under test. If the bug is about picking the right one of several items,
the test must include several items, or it can't catch picking the wrong one. When the issue ships a
reproduction — a session capture, a script, exact steps, an attachment — your test loads **that**,
not a fixture you rebuilt from your reading of it. A fix and a test written from the same wrong model
of the bug will base-fail and head-pass on the fixture forever while the real symptom sits untouched;
loading the reporter's actual scene is what breaks that agreement. A reproduction is whatever *pins
the bug's shape* — a downloadable capture, but a fenced JSON structure, the field-level trigger
conditions, or exact steps bind it just as tightly. Bind your fixture to that shape: satisfy every
condition it names and let the assertion fail when any one is violated, instead of granting the
fixture a property the report never had so a guard will fire. Reconstruct the shape yourself only
when the issue genuinely leaves it unpinned — and then say so, and say what you assumed.

### 7. Name the owner of every piece of state and prove it's released.
For each resource or mutation you introduce (a cache entry, a lock, a temporary env change, a
loading flag, a DOM node, a pending-map entry), show it is cleaned up or invalidated on **every**
exit — success, error, cancellation, replacement, shrink/clear, and teardown — not just the happy
path. Happy-path testing structurally cannot see these leaks.

### 8. Fallbacks and defaults are contracts — extend the mechanism, don't copy it.
If your change means editing several parallel blocks identically, stop: the codebase almost
certainly has a mechanism for this (a fallback, a shared helper, an extension point) and you've
missed it. Add to the one canonical place. For example, new user-facing copy goes in the `en` locale
and rides the existing fallback — don't paste English into every locale block.

### 9. The diff is the task and nothing else.
Change exactly what the task requires. Anything else you noticed goes in the PR description as a
note, not into the diff. Before opening the PR, run the affected tests **and** a broader sweep of
neighboring tests (not only the ones you added), and remove every unrelated change.

### 10. A visible control costs attention on every future visit — place it accordingly.
Where a control lives should be decided by how often it's used and by where mainstream chat apps put
the equivalent — not by where your diff already is. Rare per-item actions belong in an overflow/`⋮`
menu; global or data actions belong in Settings; only genuinely daily-use controls earn a spot on a
hot surface like the composer. Then verify it *visually*: capture desktop and narrow-viewport,
before and after, and confirm nothing is clipped, no overflow-collapse is tripped, and no hover-only
affordance is stranded on touch.

---

## Show your work in the PR description

The fastest review is one where the reviewer can *see* that the above is handled instead of having
to discover the gaps. Make your PR description carry the evidence:

- **The siblings you found** for rule 1/4 — and any you deliberately left out of scope.
- **Proof the test bites** — that your new test failed before the fix.
- **Verification run** — the affected + neighboring tests, not just your new ones.
- **Before/after images** for any visible change (desktop + narrow).
- **What you could not verify** — an explicit list. An admitted gap is fine; a hidden one is a bug
  waiting to be found in review.
- **Who owns the truth** for any claim the repo doesn't — browser behavior, a provider's API, a
  registry, an OS convention. Name the owner and check your proof is one they'd accept: in-page
  automation proves page behavior, not that the browser didn't swallow the event first; a mock
  proves your intent, not the provider's contract; a synthetic fixture proves nothing about the live
  system. The failure mode here isn't knowing you couldn't verify — it's thinking you did.

Following this imperfectly still helps enormously, because the reviewer can see exactly where your
coverage stops instead of finding it the hard way.

---

*Companion to [`AGENTS.md`](../AGENTS.md), [`CONTRIBUTING.md`](../CONTRIBUTING.md), and
[`docs/CONTRACTS.md`](CONTRACTS.md). For UI/UX specifics see [`docs/UIUX-GUIDE.md`](UIUX-GUIDE.md)
and [`DESIGN.md`](../DESIGN.md).*
