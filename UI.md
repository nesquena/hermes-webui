# Hermes WebUI — UI/UX Philosophy

**Author:** Aron Prins — Lead UI/UX, Hermes WebUI
**Status:** Living document. The project is early; expect this to evolve.
**Audience:** Anyone opening a PR that touches the Hermes WebUI.

## Foreword

This document exists because PRs are landing fast and many of them — well-intentioned, often great work — push against a UX vision that hasn't been written down yet. That's on me. This is the fix.

Everything below comes from love for the project and a desire to push it forward, not to gatekeep contributions. Read it before you touch the UI, and a lot of friction disappears.

If your change touches a primary surface (title bar, composer, sidebar items, message lines), this document is the bar your PR will be reviewed against.

---

## Core Philosophy

The Hermes WebUI is built around one guiding instinct:

> **Show the least possible to do the most possible.**

Every pixel of UI real estate is contested space. Every icon, counter, badge, and toggle has to earn its place — and "it's useful to me" is not enough. The default answer to *"should we add this here?"* is **no**, unless it's absolutely necessary at this surface.

This isn't minimalism for aesthetics' sake. It's a working principle. The UI has to scale across:

- Desktop browsers (today's primary surface)
- Mobile browsers (a real, tested target — not a fallback)
- A forthcoming desktop application
- An expanding feature set as we reach parity with Hermes Agent

The only way to keep that scalable is **ruthless restraint now**. A surface we keep clean today is a surface we can extend tomorrow without rewriting it.

---

## The Four Principles

### 1. Cleanness & Emptiness

Empty space is a feature, not wasted real estate. Title bars, composer rows, message lines, sidebar items — all of these should default to *as empty as possible*. If we cram them now, we have nowhere to grow.

When you're tempted to fill empty space, ask: *what feature, six months from now, will I have to evict to make room?*

### 2. Space Is Scarce

There is no slack. The composer is already crowded. The chat history sidebar is already tight. Message line items have almost no horizontal room left. Treat every surface as if it's at 90% capacity, because it is.

A useful exercise before adding anything: open the UI on a 320px-wide mobile viewport. If the surface is tight there, it's tight everywhere — small screens just expose it first.

### 3. Progressive Disclosure (the Three-Click Rule)

Nothing should be more than **three clicks away**. Detail, configuration, and power-user features belong *behind* something — a menu, a panel, a settings screen — not on the primary surface.

- If you need it, it's reachable.
- If you don't, it's out of sight.
- Three clicks is a **ceiling**, not a target. Don't pad navigation to reach it.

The Hermes Control Center is the canonical home for depth. Most "useful but not always needed" controls belong there before they belong anywhere else.

### 4. Show Detail Where Detail Belongs

Information should appear at the surface where the user has signaled they want depth. Token counts, model metadata, performance stats — these are valuable, but they belong on a detail view, not on the conversation list or the title bar.

> **Right information, right surface.**

A token-per-second readout on a session list item competes with the conversation title. The same readout, on a per-message inspector or a session detail view, is genuinely useful — because the user clicked through specifically asking for that depth.

---

## Component Guidelines

### Title Bar

- Keep it **clean and as empty as possible**, especially at this stage.
- The title bar is reserved space for the upcoming desktop app's window controls (close / minimize / expand). That's why certain icons live on the opposite side of the screen — leave them there.
- Tokens-per-second readouts, latency meters, and similar telemetry **do not belong in the title bar**. They will be removed.
- If you genuinely think something belongs here, open an issue first. The bar to land a new title bar element is high.

### Composer (Bottom Input Area)

- The composer is already at its limit. **Resist adding controls directly into it.**
- We will introduce a **secondary row below the composer** to host overflow controls. Propose additions there, not in the composer itself.
- On mobile, icons without labels become unreadable fast. Any control we add must remain comprehensible at small widths — that usually means *fewer* controls, not cleverer icons.
- Model, profile, and workspace controls already live in the composer footer because they are needed *while composing*. Most other controls are not.

### Sidebar — The Three-Bar Structure

The three-bar layout (primary nav, secondary panel, content) is intentional and opens up significant room for future feature expansion.

- **Do not add a collapse toggle for the second bar** unless there is a genuinely compelling reason. The default answer is no.
- Treat the second bar as load-bearing for upcoming features. Collapsing it would constrain choices we haven't made yet.
- The primary nav is for top-level navigation only. New entries here should be rare and discussed.

### Chat History / Session Items

Session list items are one of the tightest surfaces in the entire UI. Anything added here directly competes with the conversation title — which is the whole point of the list.

**Approved on session items:**

- **Spinner** — indicates which conversation is actively working. High value across multiple open chats.
- **Pin indicator** — tells the user this session is anchored.

That is the list. Token counters, message counts, model badges, and similar additions on individual session line items will be rejected. Surface that information elsewhere — typically a session detail/inspector view, or the Control Center.

If you believe a new indicator belongs in this list, the PR description should answer: *what is this displacing, and why is it more important than the conversation title?*

### Message Line Items

Same principle as session items: extremely scarce space, and the message itself is the content.

- Per-message token counters and similar metadata should not crowd the line.
- If this data is valuable, it belongs in a **detail/inspector surface**, not inline.
- Status indicators that materially change how the user reads the message (e.g., "this message is still streaming") are different from metadata about the message — the former can earn space, the latter generally cannot.

### Settings

Settings is a legitimate place for depth — this is where we *can* expose configuration. But the same principles apply within it:

- **Group related options.** A wall of toggles is just clutter at a different surface.
- **Use clear, translated strings.** No raw translation keys, no missing keys, no English-only fallbacks shipped to non-English locales.
- **Make scope unmistakable.** When a control's behavior is non-obvious — e.g., a button that refreshes models for *one* provider vs. *all* providers — prefer a small refresh icon next to the relevant item over a global button whose blast radius is unclear.
- Settings should still feel calm. If a panel needs a search box, that's a sign it's grown too dense.

### Mobile

Mobile isn't a downgraded desktop view — it's the **stress test** for every decision above.

- If a surface only works on desktop because of icon density, that's a sign the desktop version is also too dense.
- Design for mobile constraints and let desktop benefit from the breathing room.
- Tap targets need real space. Hover-only interactions need a non-hover equivalent.
- Test mobile during PR development, not after review.

---

## How to Contribute Well

1. **Default to "no" on additions to primary surfaces** (title bar, composer, sidebar items, message lines). Propose additions to detail views, settings, or the planned secondary composer row instead.

2. **If your PR adds visual elements, justify the surface.** Explain in the description *why this surface is the right home* for it, and what was considered and rejected. A one-line "added a token counter to the title bar" is not enough context for a reviewer.

3. **Three-click rule is a constraint, not a target.** Don't pad navigation to reach three; use it as a ceiling.

4. **Feature parity with Hermes is the current priority.** Net-new UI ideas are welcome but should be opened as discussion issues first if they touch the surfaces above.

5. **Translations matter.** No raw translation keys in the UI. If you add a string, add the key — and make sure it renders correctly in the locales we support.

6. **Mobile is not optional.** If you add or modify a primary surface, verify it works at mobile widths before opening the PR.

7. **When in doubt, ask before building.** A 5-minute conversation beats a rejected PR. Open a draft PR, an issue, or a discussion.

---

## A Quick Decision Checklist

Before you add anything to a primary surface, walk through this:

- [ ] Is this information needed *while doing the primary task on this surface*, or only sometimes?
- [ ] Does it survive the mobile stress test at 320–375px wide?
- [ ] Is there a detail view, settings panel, or Control Center entry where it would live more comfortably?
- [ ] If five other contributors each added "just one small thing" to this surface, would it still feel calm?
- [ ] What does this displace, and is the trade worth it?

If you can't answer those cleanly, the addition probably belongs somewhere else.

---

## Closing

The philosophy in one line:

> **Keep the surface quiet so the product can grow loud.**

Restraint now is what gives Hermes WebUI room to expand into a serious desktop app, a strong mobile experience, and full Hermes feature parity without collapsing under its own UI weight.

Thanks for building this with me.

— Aron Prins
