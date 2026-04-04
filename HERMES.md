# Why Hermes

Hermes is a persistent, autonomous AI agent that lives on your server. It remembers everything,
schedules work while you sleep, and gets more capable the longer it runs. This document explains
the mental model, why that matters, and how Hermes compares to every major AI tool available today.

---

## The Core Idea: Assistants Forget. Agents Don't.

Every time you open Claude Code, Codex, or a chat window, the tool starts from zero. It does not
know who you are, what you worked on yesterday, how your repo is structured, or what bugs you
already fixed. You re-explain yourself every single session. The tool is powerful in the moment
and useless the next day.

That gap -- between a capable tool and a capable *collaborator* -- is what Hermes closes.

```
Assistant model:  You -> [Tool] -> Answer -> Done
                  (tool forgets everything when the window closes)

Agent model:      You <-> [Hermes] <-> (memory, skills, schedule, tools)
                  (persistent, learns your stack, acts on your behalf, runs while you're offline)
```

The difference is not a feature. It changes what is possible.

---

## The Three Pillars

### 1. Memory That Compounds

Hermes has layered memory that survives every session, every reboot, every model swap:

- **User profile** -- who you are, your preferences, your communication style, things you've
  corrected Hermes on
- **Agent memory** -- facts about your environment, your toolchain, your project conventions
- **Skills** -- reusable procedures Hermes discovers and saves; it never has to relearn how to
  deploy your app, run your tests, or review a PR
- **Session history** -- every past conversation is searchable; Hermes can recall what you
  worked on last Tuesday

When you correct Hermes, it remembers. When it solves a tricky problem, it saves the approach.
When it learns your stack, that knowledge carries into every future session. It compounds.

A Claude Code session on day one and day one hundred are identical.
A Hermes agent on day one and day one hundred is meaningfully smarter about you.

### 2. Autonomous Scheduling

Hermes can run jobs without you present -- every hour, every morning, on any cron schedule.
It fires up a fresh session, runs the task, and delivers the result to wherever you want it:
Telegram, Discord, Slack, Signal, WhatsApp, SMS, email, and more.

Things Hermes can do while you sleep:

- Review new pull requests on your GitHub repo and post a full verdict comment
- Send you a morning briefing of news, markets, or anything else you care about
- Run your test suite and alert you if something breaks
- Watch a competitor's blog for new posts and summarize them
- Monitor a datasource and notify you when a threshold is crossed

### 3. Reach It From Anywhere

Hermes runs on your server and is reachable from every surface:

- **Terminal / SSH** -- the native interface; full power, full tool use
- **Web UI** -- Claude-style three-panel browser interface, works over SSH tunnel
- **Messaging apps** -- Telegram, Discord, Slack, WhatsApp, Signal, Matrix, and more

Start a task from your phone on Telegram, check it from the web UI on your laptop, finish it
in a terminal on a remote server. Same agent, same memory, same history everywhere.

---

## A Framework for AI Tools

There are four distinct categories of AI tool. Understanding the category tells you almost
everything about what a tool can and cannot do -- and where its ceiling is.

### Category 1: Chat Assistants
*Claude.ai, ChatGPT, Gemini*

The original model. You open a window, ask something, get an answer. Extremely capable in
the moment. No persistent memory beyond the conversation, no ability to run code or touch files,
no way to act on your behalf. Excellent for Q&A, drafting, and brainstorming. You re-explain
your context every session.

### Category 2: IDE Integrations
*GitHub Copilot, Cursor, Windsurf, Zed AI*

Deep inside your editor. Autocomplete, inline diffs, refactors -- all excellent. Windsurf
has the most mature built-in memory of this group (Cascade Memories, workspace-scoped). Copilot
launched early-access per-repo memory in late 2025. Cursor has no native memory. None have
scheduling or messaging access. Tied to one machine and one editor.

### Category 3: Agentic CLI Tools
*Claude Code, Codex CLI, OpenCode, Aider*

The current frontier for most developers. Can use real tools -- run shell commands, read and
write files, search the web, call APIs. Great for deep, multi-step tasks in a single terminal
session. All are adding memory and scheduling features to varying degrees (see comparisons below),
but the core model is still session-scoped: you invoke it, it works, it stops.

### Category 4: Persistent Autonomous Agents
*Hermes, OpenClaw*

All the tool use of Category 3, plus memory that accumulates across sessions automatically,
plus always-on autonomous scheduling, plus multi-modal access from any device or messaging app.
Gets more useful over time rather than resetting to zero. Hermes and OpenClaw are the two
primary open-source, self-hosted tools in this category. The key distinction: OpenClaw is a
gateway-centric automation platform; Hermes is a self-improving agent that writes and reuses
its own procedures from experience.

---

## How Hermes Compares

### vs. OpenClaw

OpenClaw is the most direct comparison to Hermes and the question most people ask first.
Both are open-source, self-hosted, always-on agents with persistent memory, cron scheduling,
and messaging app integration. If you're evaluating Hermes, you should evaluate OpenClaw too.

OpenClaw (MIT, 347k+ GitHub stars) is built around a **Gateway** control plane written in
Node.js/TypeScript. It excels at broad personal automation: native Chrome/Chromium control for
browser automation, the widest messaging platform support in the space (WhatsApp, Telegram,
Signal, iMessage, LINE, WeChat, Slack, Discord, Teams, Matrix, and more), voice wake words,
and a ClawHub skill marketplace where users share pre-built automations.

Hermes takes a different approach. It is built in Python and centers on a **self-improving
agent loop** rather than a gateway control plane. The defining difference is the skills system:
where OpenClaw skills are primarily human-authored plugins installed from a marketplace, Hermes
**discovers and writes its own skills** as a core first-class behavior. Every time Hermes solves
a problem a new way, it saves the procedure automatically and reuses it in future sessions. The
agent gets smarter about your specific environment and workflows without you having to author
anything.

**Where OpenClaw wins:**
- Broader messaging coverage -- 15+ platforms including iMessage, LINE, WeChat, Teams
- Native Chrome/Chromium browser and computer control via CDP
- Voice wake words (macOS/iOS)
- Massive community skill library (13,700+ on ClawHub)
- Larger community (347k stars)

**Where Hermes wins:**
- **Self-improving by default** -- skills are written and saved automatically from experience,
  not installed from a marketplace; the agent compounds over time without user effort
- **Reliability** -- Hermes is significantly more stable; OpenClaw has a documented history of
  update-breaking regressions, persistent messaging failures (Telegram broke across multiple
  releases in early 2026), and an unofficial WhatsApp protocol that frequently disconnects
- **Security** -- OpenClaw's ClawHub marketplace has had repeated supply chain attacks (1,184
  malicious skills identified in one audit, 156 CVEs tracked, one scored 9.9/10); Hermes does
  not have a third-party skill marketplace and has a much smaller attack surface
- **Python / ML ecosystem** -- Hermes runs natively in Python; every data science library,
  model inference framework, and research tool is one import away; OpenClaw is Node.js
- **Web UI** -- Hermes ships a full-featured three-panel chat interface (this project);
  OpenClaw has a basic gateway dashboard for monitoring but no full chat UI
- **Orchestrates other agents** -- Hermes can spawn Claude Code or Codex as sub-agents for
  heavy implementation tasks and fold results back into its own memory; OpenClaw runs its
  own agent sessions but does not wrap Claude Code or Codex specifically
- **Multi-profile support** -- Hermes has first-class named profiles, each with its own
  config, models, memory, and skills; OpenClaw uses complex binding-rule routing

The honest summary: if you want the broadest messaging coverage and native browser/computer
control, and you're comfortable managing the operational overhead, OpenClaw is a powerful choice.
If you want an agent that self-improves reliably from experience, lives in the Python ecosystem,
has a polished web UI, and stays stable across updates, Hermes is the better fit.

| | OpenClaw | Hermes |
|---|---|---|
| Persistent memory | Yes | Yes |
| Scheduled jobs (cron) | Yes | Yes |
| Messaging app access | Yes (15+ platforms, incl. iMessage/WeChat) | Yes (10+ platforms) |
| Web UI | Dashboard only (no full chat UI) | Yes (full three-panel chat UI) |
| Self-hosted | Yes | Yes |
| Open source | Yes (MIT) | Yes |
| Self-improving skills (automatic) | Partial (AI can generate skills, not default loop) | Yes (first-class, automatic) |
| Browser / computer control | Yes (native Chrome CDP) | Via shell / tools |
| Voice wake words | Yes (macOS/iOS) | No |
| Python / ML ecosystem | No (Node.js) | Yes |
| Orchestrates Claude Code / Codex | No | Yes |
| Multi-profile support | Complex (binding-rule routing) | Yes (first-class named profiles) |
| Provider-agnostic | Yes | Yes |
| Stability / update reliability | Moderate (documented regressions) | High |
| Supply chain security | Moderate (ClawHub has had malicious skills) | High (no third-party marketplace) |

### vs. Claude Code (Anthropic)

Claude Code is Anthropic's official agentic CLI and one of the best tools in Category 3.
In a single focused session it is exceptionally capable -- deep code understanding, shell access,
file editing, multi-step reasoning.

Claude Code has been adding features rapidly and the gap is narrowing:

- **Hooks system** -- 13 event types (SessionStart, PreToolUse, PostToolUse, Stop, etc.) with
  4 handler types (shell command, HTTP endpoint, LLM prompt, sub-agent); deterministic
  non-LLM control over the agent lifecycle
- **Plugins / Skills** -- installable via `/plugin install`, hot-reloaded from `~/.claude/skills`,
  with a marketplace; skills and slash commands unified as of v2.1.0
- **Scheduling** -- `/loop` (session-scoped), cloud-managed cron via `claude.ai/code/scheduled`
  (Anthropic infrastructure, 1-hour minimum), and desktop app automations
- **Messaging channels** -- Telegram, Discord, iMessage, and webhooks via the Channels feature
  (research preview, v2.1.80+); deep Slack integration that triggers cloud sessions and creates PRs
- **Claude Cowork** -- a separate but related product for knowledge workers; connects to 38+
  services via MCP including Slack, Gmail, Microsoft Teams, Notion, Jira, Salesforce, and more
- **Memory** -- CLAUDE.md and MEMORY.md for project-level context; auto-memory rolling out

These are real features. The key differences that remain:

- Claude Code's scheduling runs on **Anthropic's cloud**, not your server; your data leaves
  your hardware; there is a 1-hour minimum interval for cloud jobs
- Memory is **project-file-based** (CLAUDE.md / MEMORY.md), not a living knowledge graph that
  accumulates automatically across all your work
- Not **provider-agnostic** -- routes through Bedrock or Vertex but always hits a Claude model;
  you cannot switch to GPT, Gemini, or a local model
- **Not open source** -- proprietary; the CLI ships obfuscated JavaScript
- Messaging channels are a **research preview** requiring Bun runtime; not yet production-grade

Hermes can also use Claude Code as a sub-agent. For large implementation tasks, Hermes can
spawn Claude Code to handle the coding heavy-lifting, then fold the result back into its own
memory and history.

| | Claude Code | Hermes |
|---|---|---|
| Persistent memory (automatic) | Partial (CLAUDE.md / MEMORY.md, rolling out) | Yes |
| Skills / hooks system | Yes (Hooks + Plugin/Skills marketplace) | Yes (auto-generated from experience) |
| Scheduled jobs (self-hosted) | No (cloud-managed or session-scoped only) | Yes |
| Messaging access | Partial (Telegram/Discord/iMessage via research preview; Slack native) | Yes (10+ platforms, production) |
| Cowork connectors (Slack, Gmail, etc.) | Yes (via Claude Cowork, separate product) | Via agent tool use |
| Web UI | Yes (claude.ai/code, Anthropic-hosted) | Yes (self-hosted) |
| Provider-agnostic | No (Claude models only, via Bedrock/Vertex) | Yes (any provider) |
| Self-hosted scheduling | No | Yes |
| Open source | No | Yes |
| Runs as sub-agent of Hermes | Yes | N/A |

### vs. Codex CLI (OpenAI)

Codex CLI is OpenAI's open-source agentic terminal tool (Apache 2.0, 73k+ GitHub stars). It is
genuinely provider-agnostic -- supports 10+ providers including Anthropic, Google, Mistral,
Groq, and local models via Ollama. It added persistent session memory in v0.100.0 with
`codex resume`. The desktop app has an Automations feature for scheduled local tasks.

The CLI itself has no native scheduling (open feature request as of early 2026). Memory is
session-history-based, not a living knowledge graph. No messaging app access. Strong tool
for single-session coding; Hermes adds the always-on layer on top.

| | Codex CLI | Hermes |
|---|---|---|
| Persistent memory | Partial (session history + AGENTS.md) | Yes (automatic, layered) |
| Scheduled jobs | Partial (desktop app only; CLI has none) | Yes |
| Messaging app access | No | Yes |
| Web UI | No | Yes (self-hosted) |
| Provider-agnostic | Yes (10+ providers) | Yes (10+ providers) |
| Self-hosted | Yes | Yes |
| Open source | Yes (Apache 2.0) | Yes |

### vs. OpenCode

OpenCode is an open-source TUI agentic coding assistant, provider-agnostic across 75+ providers.
It has a WebUI embedded in its binary and an official desktop app. It uses SQLite for session
history and AGENTS.md for project context.

It has no native scheduled jobs (a community background plugin exists), no first-party messaging
integration (community Telegram bots exist but require manual setup), and no automatic
cross-session semantic memory. Good for interactive terminal coding; Hermes adds the
persistence, scheduling, and reach layers.

| | OpenCode | Hermes |
|---|---|---|
| Persistent memory | Partial (session history + AGENTS.md) | Yes (automatic, layered) |
| Scheduled jobs | No (community plugin only) | Yes |
| Messaging app access | No (community Telegram bot only) | Yes (first-party, 10+ platforms) |
| Web UI | Yes (embedded + desktop app) | Yes (self-hosted) |
| Mobile access | No | Yes |
| Skills system | No | Yes |
| Provider-agnostic | Yes (75+ providers) | Yes |
| Open source | Yes | Yes |

### vs. Cursor / Windsurf / Copilot

Category 2 tools -- exceptional at in-editor autocomplete, inline diffs, and code review.
Windsurf has the most mature memory in this group (workspace-scoped Cascade Memories).
Copilot launched early-access repo-level memory in late 2025. Cursor has no native memory.
None have scheduling or messaging access. They are not competing for the same job as Hermes.

For teams using Hermes, these are complementary: Cursor or Windsurf for in-editor work,
Hermes for everything outside the editor.

| | Cursor | Windsurf | Copilot | Hermes |
|---|---|---|---|---|
| In-editor autocomplete | Excellent | Excellent | Excellent | No |
| Inline diff / refactor | Yes | Yes | Yes | Via shell |
| Cross-session memory | No | Yes (workspace) | Partial (repo, early access) | Yes (full) |
| Scheduled background jobs | No | No | No | Yes |
| Messaging app / mobile | No | No | No | Yes |
| Terminal tool use | Limited | Limited | Limited | Full |
| Self-hosted | No | No | No | Yes |
| Provider-agnostic | Partial | Partial | No | Yes |
| Open source | No | No | No | Yes |

### vs. Claude.ai / ChatGPT

Category 1. Stateless assistants. Great for drafting, Q&A, and brainstorming in the moment.
Claude.ai has optional memory but it is shallow, user-curated, and has no ability to run
code or take real actions on your behalf.

| | Claude.ai / ChatGPT | Hermes |
|---|---|---|
| Memory across conversations | Shallow / opt-in | Yes (deep, automatic) |
| Runs shell commands | No | Yes |
| Reads / writes files | No | Yes |
| Schedules background jobs | No | Yes |
| Web UI | Yes | Yes |
| Messaging apps | No | Yes |
| Self-hosted | No | Yes |
| Provider-agnostic | No | Yes |
| Open source | No | Yes |

---

## The Compounding Advantage

The most important thing about Hermes is not any single feature. It is that Hermes improves
over time.

Every time Hermes encounters a new environment, it saves facts to memory. Every time it solves
a problem a clever way, it saves the approach as a skill. Every time you correct it, it updates
its profile of you. Every session, every scheduled job, every tool call -- the agent gets more
calibrated to you, your environment, and your workflow.

A Claude Code session on day one and day one hundred are identical.
A Hermes agent on day one and day one hundred is meaningfully smarter about you.

---

## Who Hermes Is For

**Solo developers and power users** who are tired of re-explaining their stack every session
and want an AI that actually knows their environment.

**Teams on a shared server** where multiple people want Claude-quality AI access without each
paying for a separate subscription or running local tooling.

**Automation-heavy workflows** where you want an AI running tasks on a schedule, delivering
results to your phone, without you babysitting it.

**Privacy-conscious users** who want their conversations, memory, and files on their own
hardware -- not inside Anthropic's or OpenAI's cloud.

**Multi-model users** who want to switch between OpenAI, Anthropic, Google, DeepSeek, and
others based on cost, capability, or rate limits, without rebuilding their workflow each time.

---

## What Hermes Is Not

**Not an IDE.** If in-editor autocomplete and inline diffs are your primary workflow,
Cursor or Windsurf will be better for that specific use case. Hermes lives in the terminal,
the browser, and your messaging apps -- not inside VS Code.

**Not a hosted SaaS.** You run it on your own server. That is the point -- your data stays
on your hardware -- but it does mean there is an initial setup step.

**Not a replacement for the underlying models.** Hermes is an orchestration and memory layer
on top of whatever model you point it at. The models do the reasoning. Hermes makes sure that
reasoning accumulates into something durable.

---

## Quick Reference

| | OpenClaw | Claude Code | Codex CLI | OpenCode | Cursor | Claude.ai | **Hermes** |
|---|---|---|---|---|---|---|---|
| Persistent memory (auto) | Yes | Partial† | Partial | Partial | No | Shallow | **Yes** |
| Scheduled / background jobs | Yes | Partial‡ | Partial§ | No | No | No | **Yes (self-hosted)** |
| Messaging app access | Yes (15+ platforms) | Partial (Telegram/Discord preview; Slack native) | No | No | No | No | **Yes (10+ platforms)** |
| Web UI | Dashboard only | Yes (Anthropic cloud) | No | Yes | No | Yes | **Yes (self-hosted)** |
| Skills system | Yes (marketplace) | Yes (Hooks + Plugins) | No | No | No | No | **Yes (self-improving)** |
| Self-improving skills | Partial | No | No | No | No | No | **Yes** |
| Browser / computer control | Yes (Chrome CDP) | No | No | No | No | No | Via shell |
| Python / ML ecosystem | No (Node.js) | No | No | No | No | No | **Yes** |
| In-editor autocomplete | No | No | No | No | Yes | No | No |
| Orchestrates other agents | No | No | No | No | No | No | **Yes** |
| Provider-agnostic | Yes | No (Claude only) | Yes | Yes | Partial | No | **Yes** |
| Self-hosted | Yes | No | Yes | Yes | No | No | **Yes** |
| Open source | Yes (MIT) | No | Yes | Yes | No | No | **Yes** |
| Always-on / autonomous | Yes | No | No | No | No | No | **Yes** |
| Stability / reliability | Moderate | High | High | High | High | High | **High** |

† Claude Code has CLAUDE.md / MEMORY.md project context and rolling auto-memory, but not full automatic cross-session recall  
‡ Claude Code scheduling: cloud-managed (Anthropic infrastructure, 1hr min) or session-scoped `/loop`; no self-hosted cron  
§ Codex scheduling: desktop app Automations only; CLI has no native scheduling
