# Why Hermes

Hermes is a persistent, autonomous AI agent that runs on your server. It has layered memory that
accumulates across sessions, a cron scheduler that fires jobs while you're offline, and a
self-improving skills system that saves reusable procedures automatically. You reach it from a
terminal, a browser, or a messaging app — and it's the same agent with the same history every time.

This document explains the mental model, how Hermes compares to other tools honestly, and where
it is and is not the right choice.

For repository work, start with `AGENTS.md` at the repo root. It contains the current working
rules for Codex and Hermes on this codebase.

---

## The real problem: most tools are excellent in the moment and weak over time

Memory is no longer a differentiator on its own. ChatGPT, Claude, Cursor, and GitHub Copilot all
have some form of memory now. Anthropic, OpenAI, and Microsoft are all shipping scheduling and
agent features. The category boundaries that existed twelve months ago are blurring fast.

Hermes is not the only tool with memory or automation. It is the tool that makes those
capabilities durable, self-hosted, cross-surface, and cumulative on your own server. The
distinction that matters is not "has memory" vs. "has no memory" — it's whether context persists
across sessions automatically, whether execution happens on hardware you control, whether you can
reach the same agent identity from any device, and whether the system gets meaningfully better at
your specific workflow over time without manual configuration.

```
Session-scoped:   You -> [Tool] -> Answer -> Done
                  (some tools now carry memory, but the execution is stateless)

Persistent agent: You <-> [Hermes] <-> (memory, skills, schedule, tools, surfaces)
                  (runs on your server, accumulates context, acts on your behalf offline)
```

---

## A note on convergence

The market is converging. Chat assistants are adding task scheduling and file connectors. IDE
tools are launching cloud agent modes. CLI tools are adding skills systems and mobile surfaces.
The lines between "assistant," "editor," and "agent" are dissolving.

This makes comparisons harder but also makes the question sharper: what actually matters when
every tool is claiming some version of every feature? For Hermes, the answer is synthesis. Any
single feature — memory, scheduling, messaging — is available somewhere else. The value is
having all of them in one self-hosted system, running continuously, with a persistent identity
that accumulates real knowledge of your stack over time.

---

## The three pillars

### 1. Memory that compounds

Hermes has layered memory that survives every session, every reboot, and every model swap:

- User profile — who you are, your preferences, your communication style, things you've corrected Hermes on
- Agent memory — facts about your environment, your toolchain, your project conventions
- Skills — reusable procedures Hermes discovers and saves automatically; it never has to relearn how to deploy your app, run your tests, or review a PR
- Session history — every past conversation is searchable; Hermes can recall what you worked on last Tuesday

When you correct Hermes, it remembers. When it solves a tricky problem, it saves the approach.
When it learns your stack, that knowledge carries into every future session. You never configure
this manually — it happens in the background as a side effect of normal use.

### 2. Autonomous scheduling

Hermes can run jobs without you present — every hour, every morning, on any cron schedule. It
fires up a fresh session with full access to your memory and skills, runs the task, and delivers
the result wherever you want it: Telegram, Discord, Slack, Signal, WhatsApp, SMS, email, and more.

Things Hermes can do while you sleep:

- Review new pull requests on your GitHub repo and post a full verdict comment
- Send a morning briefing of news, markets, or anything else you track
- Run your test suite and alert you if something breaks
- Watch a competitor's blog for new posts and summarize them
- Monitor a datasource and notify you when a threshold is crossed

The difference from cloud-scheduled alternatives is that the job runs on your server, with your
memory and skills, and your data never leaves your hardware.

### 3. Reach it from anywhere

Hermes runs on your server and is reachable from every surface: terminal over SSH, the web UI
(this project), and messaging apps including Telegram, Discord, Slack, WhatsApp, Signal, and
Matrix. Start a task from your phone, check it from the browser on your laptop, continue it in
a terminal on a remote server. The same agent, memory, and history follow you across all of them.

---

## How AI tools are layered today

The old four-category model — chat, editor, CLI, agent — is too clean. These layers are actively
collapsing into each other. Here is a more honest picture:

Chat assistants (Claude.ai, ChatGPT) now have persistent memory, task scheduling, 50+ service
connectors, and in some cases full agent modes with computer use. They are no longer "just chat."

IDE tools (Cursor, Windsurf, Copilot) have shipped or are shipping cross-session memory,
cloud-based background agents, and in Cursor's case a full Automations platform with Slack
integration. Cursor v3.0 (April 2026) is explicitly agent-first.

CLI tools (Claude Code, Codex, OpenCode) have added hooks, skills, desktop app automations,
and multi-surface reach. Claude Code now spans terminal, IDE, desktop, and browser. Codex has
become a product family: CLI, IDE extension, desktop app, and Codex Cloud.

Persistent self-hosted agents (Hermes, OpenClaw) sit at the intersection: they combine the
tool-use power of CLI agents, the memory of chat assistants, the scheduling of automation
platforms, and the cross-surface reach of messaging integrations — running continuously on
hardware you own.

The question is not which category a tool belongs to. The question is which combination of
capabilities you actually need, where that execution lives, and whether the system gets better
at your specific context over time.

---

## How Hermes compares

### vs. OpenClaw

OpenClaw is the most direct comparison and the question most people ask first. Both are
open-source, self-hosted, always-on agents with persistent memory, cron scheduling, and messaging
app integration. If you're evaluating Hermes, evaluate OpenClaw too.

OpenClaw (MIT) is built around a Gateway control plane written in Node.js/TypeScript. It has the
widest messaging coverage in the space — 24+ channels including WhatsApp, Telegram, Signal,
iMessage, LINE, WeChat, Slack, Discord, Teams, Matrix, Google Chat, Feishu, Mattermost, IRC,
Nextcloud Talk, and more. It has native Chrome/Chromium control via CDP, voice wake words on
macOS and iOS, and a ClawHub marketplace with 10,700+ skills. The community is large (350k+
GitHub stars, 16,900+ commits) and growing.

Hermes is built in Python and centers on a self-improving agent loop rather than a gateway
control plane. The core architectural difference is in skills: OpenClaw skills are primarily
human-authored plugins installed from a marketplace. Hermes writes and saves its own skills
automatically as part of every session. When Hermes solves a problem a new way, it saves the
procedure and reuses it without any user effort. That's not a subtle distinction — it's the
reason Hermes gets meaningfully better at your workflow without you maintaining a plugin library.

Two practical differences worth knowing directly:

Stability. OpenClaw's GitHub issues and community forums document recurring update-breaking
regressions. Telegram integration was broken across multiple releases from early 2026 through
at least April 2026. The unofficial WhatsApp Web protocol OpenClaw relies on disconnects and
requires periodic re-pairing — this is in OpenClaw's own FAQ.

Security. ClawHub's open publishing model has been exploited at scale. Three separate audits in
early 2026 found serious problems: Koi Security (January 2026) linked 335 skills to a campaign
called "ClawHavoc" that delivered Atomic Stealer malware on macOS; Bitdefender found roughly
900 malicious packages representing about 20% of the ecosystem at the time; Snyk's "ToxicSkills"
report (February 2026) found malicious skills across roughly 4,000 scanned packages. China's
CNCERT issued a national warning about ClawHub. Hermes has no third-party marketplace and a
correspondingly smaller attack surface.

OpenClaw's genuine strengths are worth stating plainly: broader messaging coverage (iMessage,
LINE, WeChat, Teams, Google Chat — platforms Hermes does not support), native browser and
computer control via Chrome CDP, voice wake words, a larger community, and more third-party
integrations than Hermes. If those capabilities matter most, OpenClaw is worth a serious look.

Where Hermes fits better: you want an agent that self-improves from experience without managing
a plugin library, you work in Python and want the ML/data science ecosystem, you want a stable
deployment that doesn't break between updates, or you want a full web chat UI rather than a
control dashboard.

| | OpenClaw | Hermes |
|---|---|---|
| Persistent memory | Yes | Yes |
| Scheduled jobs (cron) | Yes | Yes |
| Messaging app access | Yes (24+ platforms, incl. iMessage/WeChat/LINE) | Yes (many platforms) |
| Web UI | Chat UI + control dashboard | Full three-panel chat UI |
| Self-hosted | Yes | Yes |
| Open source | Yes (MIT) | Yes |
| Self-improving skills | Partial (AI can generate; not the default loop) | Yes (automatic, first-class) |
| Browser / computer control | Yes (native Chrome CDP) | Via shell / tools |
| Voice wake words | Yes (macOS/iOS) | No |
| Python / ML ecosystem | No (Node.js) | Yes |
| Orchestrates Claude Code / Codex | No | Yes |
| Multi-profile support | Via binding-rule routing | Yes (first-class named profiles) |
| Provider-agnostic | Yes | Yes |
| Update reliability | Moderate (documented regressions) | High |
| Memory inspectability | Limited | Yes (markdown files, editable) |
| Self-hosted autonomous execution | Yes | Yes |

### vs. Claude Code (Anthropic)

Claude Code is Anthropic's official agentic tool and one of the strongest options for focused
coding sessions. It has deep code understanding, shell access, file editing, and multi-step
reasoning. It has been expanding rapidly — it now spans terminal, IDE plugin, desktop app, and
browser surfaces — and the gap is closing in several areas.

What Claude Code has that's worth knowing:

- Hooks system — 26 event types (SessionStart, PreToolUse, PostToolUse, Stop, and more) with
  4 handler types (shell command, HTTP endpoint, LLM prompt, sub-agent); gives deterministic
  non-LLM control over the agent lifecycle
- Plugins / Skills — installable via `/plugin install`, hot-reloaded from `~/.claude/skills`,
  with a marketplace; includes the official ralph-wiggum plugin (`/ralph-loop`) for
  autonomous iteration toward a completion goal (distinct from `/loop`)
- `/loop` — a native bundled skill, available in every session without any plugin, that runs
  a prompt on a repeating schedule within an active CLI session (polling/monitoring use case);
  session-scoped, dies when the terminal closes
- Scheduling — cloud-managed cron (Anthropic infrastructure, minimum 1-hour interval) and
  desktop app scheduled tasks (run locally while the app is open, minimum 1-minute interval,
  full local file access); no self-hosted cron
- Messaging channels — Telegram, Discord, and iMessage via the Channels feature (research
  preview, requires Bun runtime); Slack is the most-requested addition and has not yet shipped
- Memory — CLAUDE.md and MEMORY.md for project-level context; auto-memory since v2.1.59+
- Claude Cowork — a separate knowledge-worker product connecting 38+ services via MCP
  including Gmail, Microsoft Teams, Notion, Jira, Salesforce, and more

Claude Code's source was briefly and accidentally made public in March 2026 before being taken
down. The CLI ships as minified/bundled TypeScript compiled with Bun — it is not open source.

Key differences that remain:

- Scheduling requires cloud (Anthropic infrastructure, data off your hardware, 1-hour minimum)
  or the desktop app (runs locally, but the app must stay open — not a headless server process);
  neither runs as a server daemon the way Hermes cron does
- Memory is project-file-based (CLAUDE.md / MEMORY.md plus rolling auto-memory); it doesn't
  automatically accumulate a cross-project knowledge graph the way Hermes does
- Not provider-agnostic — routes through Anthropic, Bedrock, Vertex, or Foundry, but always
  a Claude model; you can't switch to GPT, Gemini, or a local model
- Messaging channels are still a research preview, not production

Hermes can use Claude Code as a sub-agent. For large implementation tasks, Hermes can spawn
Claude Code to handle the heavy lifting and fold the result back into its own memory and history.

| | Claude Code | Hermes |
|---|---|---|
| Persistent memory (automatic) | Partial (CLAUDE.md / MEMORY.md + auto-memory v2.1.59+) | Yes |
| Skills / hooks system | Yes (26-event Hooks + Plugin/Skills marketplace) | Yes (auto-generated from experience) |
| Scheduled jobs (self-hosted) | No (cloud or desktop-app only) | Yes |
| Messaging access | Partial (Telegram/Discord/iMessage research preview; Slack not yet) | Yes (many platforms, production) |
| Cowork connectors (Slack, Gmail, etc.) | Yes (via Claude Cowork, separate product) | Via agent tool use |
| Web UI | Yes (claude.ai/code, Anthropic-hosted) | Yes (self-hosted) |
| Provider-agnostic | No (Claude models only) | Yes (any provider) |
| Self-hosted scheduling | No | Yes |
| Open source | No | Yes |
| Background/cloud agent mode | Yes (cloud-scheduled) | Yes (self-hosted cron) |
| Runs as sub-agent of Hermes | Yes | N/A |
| Memory inspectability | Partial (CLAUDE.md readable; auto-memory less so) | Yes (markdown files) |

### vs. Codex CLI (OpenAI)

Codex CLI (Apache 2.0, ~60k GitHub stars) started as a straightforward terminal tool and has
expanded into a product family. It was rewritten from TypeScript to Rust. It now includes an IDE
extension, a desktop app with an Automations feature, and Codex Cloud for remote execution. A
Skills system is shared across surfaces. It supports 12+ built-in providers: OpenAI, Anthropic,
Google/Gemini, Mistral, Groq, Ollama, OpenRouter, LM Studio, Together AI, DeepSeek, xAI,
Azure OpenAI, and custom endpoints.

The CLI itself has no native scheduling (open feature request). Session continuity is available
via `codex resume`. Memory is session-history-based plus AGENTS.md project context — not a
living knowledge graph that accumulates across all your projects. No first-party messaging
integration. The Automations feature in the desktop app covers scheduled local tasks but doesn't
reach the cross-session, cross-surface continuity Hermes has.

| | Codex CLI | Hermes |
|---|---|---|
| Persistent memory | Partial (session history + AGENTS.md) | Yes (automatic, layered) |
| Scheduled jobs | Partial (desktop app Automations; CLI has none) | Yes |
| Messaging app access | No | Yes |
| Web UI | No (CLI + desktop app) | Yes (self-hosted) |
| Provider-agnostic | Yes (12+ providers) | Yes |
| Self-hosted | Yes | Yes |
| Open source | Yes (Apache 2.0) | Yes |
| Background/cloud agent mode | Yes (Codex Cloud) | Yes (self-hosted cron) |
| Self-improving skills | No | Yes |

### vs. OpenCode

OpenCode is an open-source TUI agentic coding assistant supporting 75+ providers. It has a WebUI
embedded in its binary, an official desktop app, SQLite session history, and AGENTS.md project
context. It supports CLAUDE.md as a fallback for users migrating from Claude Code. There are 30+
community plugins, and community messaging integrations exist for Telegram, Slack, Discord, and
Microsoft Teams — though none are first-party and all require manual setup.

OpenCode Go ($10/month) and OpenCode Zen (curated model service) are subscription tiers. The
GitHub Copilot official integration launched January 2026. There is no native scheduling; a
community background plugin exists. No automatic cross-session semantic memory.

| | OpenCode | Hermes |
|---|---|---|
| Persistent memory | Partial (session history + AGENTS.md) | Yes (automatic, layered) |
| Scheduled jobs | No (community plugin only) | Yes |
| Messaging app access | Community integrations only (Telegram/Slack/Discord/Teams) | Yes (first-party, many platforms) |
| Web UI | Yes (embedded + desktop app) | Yes (self-hosted) |
| Mobile access | No | Yes |
| Skills / plugins | Yes (30+ community plugins) | Yes (auto-generated, first-party) |
| Provider-agnostic | Yes (75+ providers) | Yes |
| Open source | Yes | Yes |
| Self-hosted autonomous execution | No | Yes |

### vs. Cursor

Cursor has changed substantially. The "no memory, no scheduling, no messaging" description was
accurate in 2024 and is wrong now.

Memories (per-project cross-session knowledge base) shipped in beta with v1.0 in June 2025.
Automations launched March 5, 2026 — time-based, event-based (GitHub/Linear/PagerDuty), and
communication-based (Slack) triggers that fire background agents on cloud VMs. The web app,
mobile agent, and Slack bot give it multi-surface reach. Cursor v3.0 (April 2, 2026) is
explicitly agent-first with Design Mode and 30+ marketplace plugins. Cursor acquired Supermaven
for autocomplete. As of early 2026 it's valued at $29.3B with $2B ARR. It is not a narrow editor
tool anymore.

Hermes still has a different profile: it's self-hosted and server-resident, the same persistent
identity follows you across every surface without cloud intermediation, and it works with any
model family rather than being cloud-VM-based. For workflows that require data sovereignty,
self-hosted scheduling, or deep Python/ML tooling on your own hardware, Cursor's cloud-agent
architecture is a fundamental mismatch. For teams that want editor-native agents with strong
IDE integration, Cursor's recent evolution is significant.

| | Cursor | Windsurf | Copilot | Hermes |
|---|---|---|---|---|
| In-editor autocomplete | Excellent (Supermaven) | Excellent (Cascade) | Excellent | No |
| Inline diff / refactor | Yes | Yes | Yes | Via shell |
| Cross-session memory | Yes (Memories, per-project) | Yes (Cascade Memories, workspace) | Yes (Agentic Memory, repo-scoped, 28-day expiry) | Yes (automatic, persistent) |
| Scheduled background jobs | Yes (Automations, cloud VM) | No | Via Coding Agent (issue-driven) | Yes (self-hosted cron) |
| Messaging app / multi-surface | Yes (Slack bot, web app, mobile) | No | Via Copilot CLI / fleet | Yes (many platforms) |
| Background/cloud agent mode | Yes (Automations on cloud VMs) | No | Yes (Coding Agent, GA Mar 2026) | Yes (self-hosted) |
| Terminal tool use | Limited | Limited | Limited | Full |
| Self-hosted | No | No | No | Yes |
| Self-hosted autonomous execution | No | No | No | Yes |
| Provider-agnostic | Partial | Partial | No (GitHub models) | Yes |
| Open source | No | No | No | Yes |
| Memory inspectability | Partial | Yes (stored locally) | Limited | Yes (markdown files) |

### vs. Claude.ai and ChatGPT

These are no longer simple chat tools. The description of "no memory, no scheduling, no
messaging" is inaccurate for both.

Claude Cowork (in Claude Desktop) launched scheduled tasks on February 25, 2026 — hourly,
daily, weekly, weekdays, and on-demand. It runs in an isolated VM with file and shell access.
Claude has 50+ service connectors as of February 2026 including Slack (launched January 26,
2026), Gmail, Google Calendar, Google Drive, Microsoft 365, Notion, Asana, Linear, and Jira.
Memory auto-generates from chat history, not just user-curated entries. Code execution and
file access in Artifacts is sandboxed, not the same as shell access on your own server.

ChatGPT has Agent Mode (launched July 17, 2025), Scheduled Tasks (January 2025, recurring
automated prompts), a computer-using agent, Projects, 50+ connectors including Gmail, GitHub,
and Google Drive, dual-mode memory (auto + manual), and ChatGPT Pulse for Pro users (daily
research briefings). It is not a passive Q&A interface.

Where Claude.ai and ChatGPT differ from Hermes: neither is self-hosted, neither is
provider-agnostic, and neither gives you execution on your own hardware. Connectors and
scheduling exist, but they run on Anthropic's or OpenAI's infrastructure. Your memory, session
history, and agent execution live on their servers, not yours. For many use cases that's fine
— they are capable and well-supported. For privacy-conscious users, regulated environments, or
workflows that require persistent server-side execution on controlled hardware, it's a
disqualifying constraint.

| | Claude.ai | ChatGPT | Hermes |
|---|---|---|---|
| Memory across conversations | Yes (auto-generated from history) | Yes (dual-mode: auto + manual) | Yes (deep, automatic) |
| Scheduled tasks | Yes (Cowork: hourly/daily/weekly) | Yes (since Jan 2025) | Yes (any cron, self-hosted) |
| Service connectors / messaging | Yes (50+ via Cowork) | Yes (50+ connectors) | Yes (many platforms, direct) |
| Runs shell commands | Sandboxed (Cowork VM) | Sandboxed | Yes (full shell) |
| Code execution | Sandboxed | Sandboxed | Yes (full shell) |
| Reads / writes files | Sandboxed | Sandboxed | Yes (full filesystem) |
| Web UI | Yes (Anthropic-hosted) | Yes (OpenAI-hosted) | Yes (self-hosted) |
| Self-hosted | No | No | Yes |
| Provider-agnostic | No | No | Yes |
| Open source | No | No | Yes |
| Self-hosted autonomous execution | No | No | Yes |
| Memory inspectability | Limited | Limited | Yes (markdown files) |

---

## The compounding advantage

What distinguishes Hermes from most of the tools above is that it gets meaningfully better at
your specific workflow over time without manual configuration.

Every time Hermes encounters a new environment, it saves facts to memory. Every time it solves
a problem a new way, it saves the approach as a skill. Every time you correct it, it updates its
profile of you. Every session, every scheduled job, every tool call adds to a body of knowledge
that is specific to you, stored on your hardware, and available to every future interaction.

A Claude Code session on day one and day one hundred are identical — it starts fresh. A Hermes
agent on day one and day one hundred knows your stack, your conventions, your preferences, and
the solutions that have worked before. That's the actual compounding.

---

## Who Hermes is for

Solo developers and power users who don't want to re-explain their stack every session and want
an AI that actually knows their environment.

Teams on a shared server where multiple people want capable AI access without each paying for
a separate subscription or running separate local tooling.

Automation-heavy workflows where you want an AI running tasks on a schedule, delivering results
to your phone, without babysitting it.

Privacy-conscious users who want their conversations, memory, and files on their own hardware.

Multi-model users who want to switch between OpenAI, Anthropic, Google, DeepSeek, and others
based on cost, capability, or rate limits, without rebuilding their workflow each time.

---

## What Hermes is not

Hermes is not the best in-editor autocomplete tool. Cursor and Windsurf do that job better.
Use one alongside Hermes.

It is not zero-setup. You are running a server. That means initial configuration, and it means
you're responsible for uptime, upgrades, and backups. The tradeoff is data sovereignty and
control; that only makes sense if you actually want it.

It does not make weaker models magical. Memory and skills help, but the underlying model still
determines reasoning quality. Hermes with a weak model is a well-organized weak model.

It still needs guardrails, approvals, and observability for high-stakes automations. Autonomous
execution on a schedule with shell access is powerful and requires judgment about what to
approve. Terminal commands can require confirmation before running; use that for anything
consequential.

If you need the absolute lowest-friction path to a one-off answer or a quick edit, a chat
interface or an in-editor tool is the right call. Hermes is for continuity and autonomy, not
minimum-friction one-shots.

---

## Scope and limits

Hermes lives in the terminal, browser, and messaging apps. For in-editor autocomplete and inline
diffs, use Cursor or Windsurf — they do that job better and work well alongside Hermes.

You run Hermes on your own server. That means initial setup, but your data stays on your
hardware and you control the schedule, the models, and the costs.

Hermes is an orchestration and memory layer. It makes whatever model you point at it more useful
over time. The models do the reasoning; Hermes makes sure that reasoning accumulates into
something durable.

---

## Security and control

Memory is stored locally on your server as readable, editable files: user profile, agent memory,
and skills are all markdown. Session history is in SQLite on your machine. You can inspect,
edit, or delete any of it directly.

If you want external memory providers, eight are supported: Mem0, Honcho, Hindsight, RetainDB,
ByteRover, Supermemory, Holographic, and others. These are optional and configurable.

Execution runs in configurable backends: local shell, Docker, SSH, Daytona, Singularity, or
Modal. You choose what execution environment Hermes operates in and what it can reach.

Terminal commands can require confirmation before running. For any automation that touches
production systems or makes external calls, enable approval controls.

Secrets stay on your hardware. Hermes does not phone home; it calls whatever model APIs you
configure directly.

Multiple profiles give isolation between users or projects. A shared server can have separate
profiles with separate memory, separate skills, and separate history.

---

## Quick reference

| | OpenClaw | Claude Code | Codex | OpenCode | Cursor | Copilot | Claude.ai | ChatGPT | Hermes |
|---|---|---|---|---|---|---|---|---|---|
| Persistent memory (auto) | Yes | Partial† | Partial | Partial | Yes (per-project) | Yes (repo-scoped‡) | Yes | Yes | Yes |
| Scheduled / background jobs | Yes | Partial§ | Partial¶ | No | Yes (Automations) | Via Coding Agent | Yes (Cowork) | Yes | Yes (self-hosted) |
| Messaging / multi-surface | Yes (24+ platforms) | Partial (preview) | No | Community only | Yes (Slack/web/mobile) | Via CLI/fleet | Yes (50+ connectors) | Yes (50+ connectors) | Yes (many platforms) |
| Web UI | Chat UI + control dashboard | Anthropic-hosted | No | Yes | Yes + mobile | github.com | Yes (Claude Desktop) | Yes | Yes (self-hosted) |
| Skills system | Yes (ClawHub marketplace) | Yes (Hooks + Plugins) | Partial (Skills) | Community plugins | Yes (marketplace) | No | No | No | Yes (auto-generated) |
| Self-improving skills | Partial | No | No | No | No | No | No | No | Yes |
| Browser / computer control | Yes (Chrome CDP) | No | No | No | No | No | No | Yes (CUA) | Via shell |
| In-editor autocomplete | No | No | Via extension | No | Excellent | Excellent | No | No | No |
| Orchestrates other agents | No | No | No | No | No | No | No | No | Yes |
| Provider-agnostic | Yes | No (Claude only) | Yes | Yes | Partial | No | No | No | Yes |
| Self-hosted | Yes | No | Yes (CLI) | Yes | No | No | No | No | Yes |
| Self-hosted autonomous execution | Yes | No | No | No | No | No | No | No | Yes |
| Background/cloud agent mode | Yes | Yes (cloud) | Yes (Codex Cloud) | No | Yes (cloud VMs) | Yes (Coding Agent) | Yes (Cowork VM) | Yes (Agent Mode) | Yes (self-hosted) |
| Memory inspectability | Limited | Partial | Partial | Partial | Partial | Limited | Limited | Limited | Yes (markdown files) |
| Open source | Yes (MIT) | No | Yes (Apache 2.0) | Yes | No | No | No | No | Yes |
| Always-on autonomous execution | Yes | No | No | No | No | No | No | No | Yes |

† Claude Code: CLAUDE.md / MEMORY.md project context plus auto-memory since v2.1.59+; no automatic cross-project accumulation
‡ Copilot Agentic Memory: public preview Jan 15, 2026; enabled by default Mar 4, 2026; repo-scoped, auto-expires after 28 days
§ Claude Code scheduling: cloud-managed (Anthropic infrastructure) or desktop-app only; no self-hosted cron
¶ Codex scheduling: desktop app Automations only; CLI has no native scheduling
