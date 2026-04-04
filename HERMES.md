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

Hermes fills that gap. It runs on your server, retains context across every session, and acts
on your behalf whether or not you are at a keyboard.

```
Assistant model:  You -> [Tool] -> Answer -> Done
                  (tool forgets everything when the window closes)

Agent model:      You <-> [Hermes] <-> (memory, skills, schedule, tools)
                  (persistent, learns your stack, acts on your behalf, runs while you're offline)
```

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
When it learns your stack, that knowledge carries into every future session.

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

Hermes runs on your server and is reachable from every surface: terminal over SSH, the web UI
(this project), and messaging apps including Telegram, Discord, Slack, WhatsApp, Signal, and
Matrix. Start a task from your phone, check it from the browser on your laptop, continue it in
a terminal on a remote server. The same agent, memory, and history follow you everywhere.

---

## A Framework for AI Tools

There are four distinct categories of AI tool. Understanding the category tells you what a tool
can and cannot do.

### Category 1: Chat Assistants
*Claude.ai, ChatGPT, Gemini*

You open a window, ask something, get an answer. No persistent memory beyond the conversation,
no ability to run code or touch files, no way to act on your behalf. Excellent for Q&A,
drafting, and brainstorming. You re-explain your context every session.

### Category 2: IDE Integrations
*GitHub Copilot, Cursor, Windsurf, Zed AI*

Deep inside your editor. Autocomplete, inline diffs, refactors -- all excellent. Windsurf was
earliest with workspace-scoped memory (Cascade Memories); Copilot has been shipping repo-level
memory since late 2025 and is catching up. Cursor has no native memory as of early 2026. None
have scheduling or messaging access. Tied to one machine and one editor.

### Category 3: Agentic CLI Tools
*Claude Code, Codex CLI, OpenCode, Aider*

The current frontier for most developers. Can use real tools -- run shell commands, read and
write files, search the web, call APIs. Great for deep, multi-step tasks in a single terminal
session. All are adding memory and scheduling features to varying degrees (see comparisons below),
but the core model is still session-scoped: you invoke it, it works, it stops.

### Category 4: Persistent Autonomous Agents
*Hermes, OpenClaw (as of early 2026)*

All the tool use of Category 3, plus memory that accumulates across sessions, plus always-on
scheduling, plus multi-modal access from any device or messaging app. Gets more useful over time
rather than resetting to zero. Hermes and OpenClaw are the two primary open-source, self-hosted
tools in this category. OpenClaw is a gateway-centric automation platform; Hermes is a
self-improving agent that writes and reuses its own procedures from experience.

---

## How Hermes Compares

### vs. OpenClaw

OpenClaw is the most direct comparison to Hermes and the question most people ask first.
Both are open-source, self-hosted, always-on agents with persistent memory, cron scheduling,
and messaging app integration. If you're evaluating Hermes, you should evaluate OpenClaw too.

OpenClaw (MIT, ~347k GitHub stars) is built around a **Gateway** control plane written in
Node.js/TypeScript. It excels at broad personal automation: native Chrome/Chromium control for
browser automation, the widest messaging platform support in the space (WhatsApp, Telegram,
Signal, iMessage, LINE, WeChat, Slack, Discord, Teams, Matrix, and more), voice wake words,
and a ClawHub skill marketplace where users share pre-built automations. The community is large
and the ecosystem is growing fast.

Hermes takes a different approach. It is built in Python and centers on a **self-improving
agent loop** rather than a gateway control plane. The core difference is in how skills work:
OpenClaw skills are primarily human-authored plugins installed from a marketplace; Hermes
**writes and saves its own skills automatically** as part of every session. When Hermes solves
a problem a new way, it saves the procedure and reuses it going forward without any user effort.

Beyond the skills architecture, there are two other practical differences worth knowing:

**Stability.** OpenClaw's community forums and GitHub issues document a recurring pattern of
update-breaking regressions -- for example, Telegram integration was broken across multiple
releases in early 2026. The unofficial WhatsApp Web protocol OpenClaw uses is known to
disconnect and requires periodic re-pairing (this is documented in OpenClaw's own FAQ).
Hermes has had no equivalent release breakages.

**Security.** ClawHub's open publishing model has been exploited repeatedly. A community audit
identified over a thousand malicious skills in the marketplace including prompt injections and
tool-poisoning payloads; the community-maintained awesome-openclaw-skills list tracks confirmed
removals and flags known bad actors. Hermes has no third-party marketplace and a correspondingly
smaller attack surface.

**OpenClaw's genuine strengths** are worth stating plainly: it has broader messaging coverage
(iMessage, LINE, WeChat, Teams -- platforms Hermes does not support), native browser and
computer control via Chrome CDP, voice wake words on macOS and iOS, a larger community, and
more third-party integrations than Hermes. If those capabilities matter most to you, OpenClaw
is worth a serious look.

Where Hermes is the better fit: you want an agent that self-improves from experience without
manual plugin authoring, you work in Python and want access to the ML/data science ecosystem,
you want a stable deployment that does not break between updates, or you want a full web chat
UI rather than a monitoring dashboard.

| | OpenClaw | Hermes |
|---|---|---|
| Persistent memory | Yes | Yes |
| Scheduled jobs (cron) | Yes | Yes |
| Messaging app access | Yes (15+ platforms, incl. iMessage/WeChat) | Yes (10+ platforms) |
| Web UI | Gateway dashboard (monitoring only) | Full three-panel chat UI |
| Self-hosted | Yes | Yes |
| Open source | Yes (MIT) | Yes |
| Self-improving skills | Partial (AI can generate skills; not the default loop) | Yes (automatic, first-class) |
| Browser / computer control | Yes (native Chrome CDP) | Via shell / tools |
| Voice wake words | Yes (macOS/iOS) | No |
| Python / ML ecosystem | No (Node.js) | Yes |
| Orchestrates Claude Code / Codex | No | Yes |
| Multi-profile support | Via binding-rule routing | Yes (first-class named profiles) |
| Provider-agnostic | Yes | Yes |
| Update reliability | Moderate (documented regressions) | High |

### vs. Claude Code (Anthropic)

Claude Code is Anthropic's official agentic CLI and one of the best tools in Category 3.
In a single focused session it is capable -- deep code understanding, shell access, file
editing, multi-step reasoning.

Claude Code has been adding features rapidly and the gap is narrowing:

- **Hooks system** -- 13 event types (SessionStart, PreToolUse, PostToolUse, Stop, etc.) with
  4 handler types (shell command, HTTP endpoint, LLM prompt, sub-agent); deterministic
  non-LLM control over the agent lifecycle
- **Plugins / Skills** -- installable via `/plugin install`, hot-reloaded from `~/.claude/skills`,
  with a marketplace; skills and slash commands unified as of v2.1.0
- **Scheduling** -- `/loop` (session-scoped), cloud-managed cron via `claude.ai/code/scheduled`
  (Anthropic infrastructure, minimum interval applies), and desktop app automations
- **Messaging channels** -- Telegram, Discord, iMessage, and webhooks via the Channels feature
  (research preview, v2.1.80+); deep Slack integration that triggers cloud sessions and creates PRs
- **Claude Cowork** -- a separate product for knowledge workers; connects to 38+
  services via MCP including Slack, Gmail, Microsoft Teams, Notion, Jira, Salesforce, and more
- **Memory** -- CLAUDE.md and MEMORY.md for project-level context; auto-memory rolling out

These are real features. The key differences that remain:

- Claude Code's scheduling runs on **Anthropic's cloud** (or requires the desktop app open),
  not a self-hosted server; cloud jobs have a minimum interval and your data leaves your hardware
- Memory is **project-file-based** (CLAUDE.md / MEMORY.md), not a knowledge graph that
  accumulates automatically across all your work; auto-memory is still rolling out
- **Not provider-agnostic** -- routes through Bedrock or Vertex but always hits a Claude model;
  you cannot switch to GPT, Gemini, or a local model
- **Not open source** -- proprietary; the CLI ships obfuscated JavaScript
- Messaging channels are a **research preview** requiring Bun runtime; not yet production-grade

Hermes can use Claude Code as a sub-agent. For large implementation tasks, Hermes can spawn
Claude Code to handle the heavy lifting and fold the result back into its own memory and history.

| | Claude Code | Hermes |
|---|---|---|
| Persistent memory (automatic) | Partial (CLAUDE.md / MEMORY.md, rolling out) | Yes |
| Skills / hooks system | Yes (Hooks + Plugin/Skills marketplace) | Yes (auto-generated from experience) |
| Scheduled jobs (self-hosted) | No (cloud or desktop-app only) | Yes |
| Messaging access | Partial (Telegram/Discord/iMessage via research preview; Slack native) | Yes (10+ platforms, production) |
| Cowork connectors (Slack, Gmail, etc.) | Yes (via Claude Cowork, separate product) | Via agent tool use |
| Web UI | Yes (claude.ai/code, Anthropic-hosted) | Yes (self-hosted) |
| Provider-agnostic | No (Claude models only, via Bedrock/Vertex) | Yes (any provider) |
| Self-hosted scheduling | No | Yes |
| Open source | No | Yes |
| Runs as sub-agent of Hermes | Yes | N/A |

### vs. Codex CLI (OpenAI)

Codex CLI is OpenAI's open-source agentic terminal tool (Apache 2.0, ~73k GitHub stars). It
supports 10+ providers including Anthropic, Google, Mistral, Groq, and local models via Ollama.
It added persistent session memory in v0.100.0 with `codex resume`. The desktop app has an
Automations feature for scheduled local tasks.

The CLI itself has no native scheduling (open feature request as of early 2026). Memory is
session-history-based rather than a living knowledge graph. No messaging app access. A strong
tool for single-session coding; Hermes adds the always-on layer on top.

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

No native scheduled jobs (a community background plugin exists), no first-party messaging
integration (community Telegram bots exist but require manual setup), and no automatic
cross-session semantic memory. Good for interactive terminal coding sessions.

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
Not competing for the same job as Hermes, and they work well alongside it.

Windsurf was earliest with workspace-scoped memory (Cascade Memories); Copilot has been
shipping repo-level memory since late 2025. Cursor has no native cross-session memory as of
early 2026. None have scheduling or messaging access.

| | Cursor | Windsurf | Copilot | Hermes |
|---|---|---|---|---|
| In-editor autocomplete | Excellent | Excellent | Excellent | No |
| Inline diff / refactor | Yes | Yes | Yes | Via shell |
| Cross-session memory | No | Yes (workspace) | Partial (repo, early access) | Yes |
| Scheduled background jobs | No | No | No | Yes |
| Messaging app / mobile | No | No | No | Yes |
| Terminal tool use | Limited | Limited | Limited | Full |
| Self-hosted | No | No | No | Yes |
| Provider-agnostic | Partial | Partial | No | Yes |
| Open source | No | No | No | Yes |

### vs. Claude.ai / ChatGPT

Category 1. For drafting, Q&A, and brainstorming in the moment, both are excellent.

Claude.ai memory has been improving -- it now generates memory from chat history, not just
user-curated entries. Claude.ai can also execute code and read/write files in a sandboxed
environment via Artifacts. These are real capabilities, just not the same as direct filesystem
or shell access on your own server.

| | Claude.ai / ChatGPT | Hermes |
|---|---|---|
| Memory across conversations | Yes (improving; auto-generated from history) | Yes (deep, automatic) |
| Runs shell commands | No | Yes |
| Code execution | Sandboxed (Artifacts) | Yes (full shell) |
| Reads / writes files | Sandboxed (Artifacts) | Yes (full filesystem) |
| Schedules background jobs | No | Yes |
| Web UI | Yes | Yes |
| Messaging apps | No | Yes |
| Self-hosted | No | Yes |
| Provider-agnostic | No | Yes |
| Open source | No | Yes |

---

## The Compounding Advantage

What matters most about Hermes is that it improves over time. That is the point.

Every time Hermes encounters a new environment, it saves facts to memory. Every time it solves
a problem a new way, it saves the approach as a skill. Every time you correct it, it updates its
profile of you. Every session, every scheduled job, every tool call, the agent gets more
calibrated to you and your workflow.

A Claude Code session on day one and day one hundred are identical. A Hermes agent on day one
and day one hundred is smarter about you -- it knows your stack, your conventions, your
preferences, and the solutions that have worked before.

---

## Who Hermes Is For

**Solo developers and power users** who don't want to re-explain their stack every session and
want an AI that actually knows their environment.

**Teams on a shared server** where multiple people want Claude-quality AI access without each
paying for a separate subscription or running local tooling.

**Automation-heavy workflows** where you want an AI running tasks on a schedule, delivering
results to your phone, without babysitting it.

**Privacy-conscious users** who want their conversations, memory, and files on their own
hardware.

**Multi-model users** who want to switch between OpenAI, Anthropic, Google, DeepSeek, and
others based on cost, capability, or rate limits, without rebuilding their workflow each time.

---

## Scope and Limits

**Hermes lives in the terminal, browser, and messaging apps.** For in-editor autocomplete and
inline diffs, use Cursor or Windsurf alongside it -- they do that job better.

**You run Hermes on your own server.** That means initial setup, but your data stays on your
hardware and you control the schedule, the models, and the costs.

**Hermes is an orchestration and memory layer.** It makes whatever model you point it at more
useful over time. The models do the reasoning; Hermes makes sure that reasoning accumulates into
something durable.

---

## Quick Reference

| | OpenClaw | Claude Code | Codex CLI | OpenCode | Cursor | Claude.ai | Hermes |
|---|---|---|---|---|---|---|---|
| Persistent memory (auto) | Yes | Partial† | Partial | Partial | No | Yes (improving) | **Yes** |
| Scheduled / background jobs | Yes | Partial‡ | Partial§ | No | No | No | **Yes (self-hosted)** |
| Messaging app access | Yes (15+ platforms) | Partial (Telegram/Discord preview; Slack native) | No | No | No | No | **Yes (10+ platforms)** |
| Web UI | Dashboard only | Yes (Anthropic cloud) | No | Yes | No | Yes | **Yes (self-hosted)** |
| Skills system | Yes (marketplace) | Yes (Hooks + Plugins) | No | No | No | No | **Yes** |
| Self-improving skills | Partial | No | No | No | No | No | **Yes** |
| Browser / computer control | Yes (Chrome CDP) | No | No | No | No | No | Via shell |
| Python / ML ecosystem | No (Node.js) | No | No | No | No | No | **Yes** |
| In-editor autocomplete | No | No | No | No | Yes | No | No |
| Orchestrates other agents | No | No | No | No | No | No | **Yes** |
| Provider-agnostic | Yes | No (Claude only) | Yes | Yes | Partial | No | **Yes** |
| Self-hosted | Yes | No | Yes | Yes | No | No | **Yes** |
| Open source | Yes (MIT) | No | Yes | Yes | No | No | **Yes** |
| Always-on / autonomous | Yes | No | No | No | No | No | **Yes** |

† Claude Code has CLAUDE.md / MEMORY.md project context and rolling auto-memory, but not full automatic cross-session recall  
‡ Claude Code scheduling: cloud-managed (Anthropic infrastructure) or desktop-app only; no self-hosted cron  
§ Codex scheduling: desktop app Automations only; CLI has no native scheduling
