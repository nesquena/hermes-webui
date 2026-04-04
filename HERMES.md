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

No other open-source tool in this space does this.

### 3. Reach It From Anywhere

Hermes runs on your server and is reachable from every surface:

- **Terminal / SSH** -- the native interface; full power, full tool use
- **Web UI** -- this project; Claude-style three-panel browser interface, works over SSH tunnel
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
the moment. No memory beyond the current conversation, no ability to run code or touch files,
no way to act on your behalf. Excellent for Q&A, drafting, and brainstorming. You re-explain
your context every session.

### Category 2: IDE Integrations
*GitHub Copilot, Cursor, Windsurf, Zed AI*

Deep inside your editor. Autocomplete, inline diffs, refactors -- all excellent. Stateful within
a project session but not across them. Tied to one machine and one editor. No scheduling, no
cross-session memory, no way to reach them from a phone or a terminal on another box.

### Category 3: Agentic CLI Tools
*Claude Code, Codex CLI, OpenCode, Aider*

The current frontier for most developers. Can use real tools -- run shell commands, read and
write files, search the web, call APIs. Great for deep, multi-step tasks in a single terminal
session. But when the session ends, so does the context. No scheduling, no background operation,
no memory of what you worked on yesterday.

### Category 4: Persistent Autonomous Agents
*Hermes*

All the tool use of Category 3, plus memory that accumulates across sessions, plus autonomous
scheduling, plus multi-modal access. Gets more useful over time rather than resetting to zero.
Hermes is currently the only tool in this category that is open-source, self-hosted, and
provider-agnostic.

---

## How Hermes Compares

### vs. Claude Code (Anthropic)

Claude Code is Anthropic's official agentic CLI and one of the best tools in Category 3.
In a single focused session it is exceptionally capable -- deep code understanding, shell access,
file editing, multi-step reasoning.

The ceiling is the session boundary. Next time you open it, it does not know you. It has no
memory of your codebase conventions, the bugs you fixed last week, or the fact that you prefer
concise responses without preamble. You start from scratch every time.

Hermes can use Claude Code as a sub-agent. For a large implementation task, Hermes can spawn
Claude Code to handle the coding heavy-lifting, then fold the result back into its own memory
and history. You get Claude Code's full capability *plus* everything Hermes adds on top.

| | Claude Code | Hermes |
|---|---|---|
| Persistent memory across sessions | No | Yes |
| Skills (saved reusable procedures) | No | Yes |
| Scheduled / background jobs | No | Yes |
| Messaging app access | No | Yes (10+ platforms) |
| Web UI | No | Yes |
| Provider-agnostic | No | Yes (10+ providers) |
| Self-hosted | Yes | Yes |
| Agentic tool use (shell, files, web) | Yes | Yes |
| Can orchestrate Claude Code | N/A | Yes |
| Open source | No | Yes |

### vs. Codex CLI (OpenAI)

Same category and similar positioning to Claude Code. Excellent for focused single-session
coding tasks, resets on exit, OpenAI-only. Hermes can orchestrate Codex as a sub-agent the
same way it can with Claude Code.

| | Codex CLI | Hermes |
|---|---|---|
| Persistent memory | No | Yes |
| Scheduled jobs | No | Yes |
| Messaging app access | No | Yes |
| Web UI | No | Yes |
| Provider-agnostic | No (OpenAI only) | Yes |
| Self-hosted | Yes | Yes |
| Open source | Yes | Yes |

### vs. OpenCode

Open-source TUI agentic coding assistant. Provider-agnostic like Hermes, which is a real
differentiator in this space. Good for interactive terminal coding sessions. Falls in Category 3:
no persistent memory, no scheduling, no messaging, no web UI.

| | OpenCode | Hermes |
|---|---|---|
| Persistent memory | No | Yes |
| Scheduled jobs | No | Yes |
| Messaging app access | No | Yes |
| Web UI | No (TUI only) | Yes |
| Mobile access | No | Yes |
| Skills system | No | Yes |
| Provider-agnostic | Yes | Yes |
| Open source | Yes | Yes |

### vs. Cursor / Windsurf / Copilot

These are Category 2 tools and exceptional at what they do. If your primary workflow is
in-editor autocomplete and PR review inside an IDE, they will serve you better than Hermes
for that specific use case. They are not competing for the same job.

For teams running Hermes, the two are complementary: Cursor or Copilot for in-editor work,
Hermes for everything outside the editor -- terminal tasks, scheduling, monitoring, cross-session
memory, mobile access.

| | Cursor / Windsurf / Copilot | Hermes |
|---|---|---|
| In-editor autocomplete | Excellent | No |
| Inline diff / refactor | Yes | Via shell |
| Persistent cross-session memory | No | Yes |
| Scheduled background jobs | No | Yes |
| Messaging app / mobile access | No | Yes |
| Terminal tool use | Limited | Full |
| Self-hosted | No | Yes |
| Open source | No | Yes |

### vs. Claude.ai / ChatGPT

Category 1. Stateless assistants. Great for drafting, Q&A, and brainstorming in the moment.
The gap with Hermes is wide on every dimension that matters for ongoing technical work.

| | Claude.ai / ChatGPT | Hermes |
|---|---|---|
| Memory across conversations | Shallow / opt-in | Yes (deep, layered) |
| Runs shell commands | No | Yes |
| Reads / writes files | No | Yes |
| Schedules background jobs | No | Yes |
| Web UI | Yes | Yes |
| Messaging apps | No | Yes |
| Self-hosted | No | Yes |
| Provider-agnostic | No | Yes |
| Open source | No | Yes |

---

## Who Hermes Is For

**Solo developers and power users** who are tired of re-explaining their stack every session
and want an AI that actually knows their environment.

**Teams on a shared server** where multiple people want Claude-quality AI access without each
paying for a separate subscription or running local tooling.

**Automation-heavy workflows** where you want an AI running tasks on a schedule, delivering
results to your phone, without you babysitting it.

**Privacy-conscious users** who want their conversations, memory, and files on their own
hardware -- not inside a SaaS provider's cloud.

**Multi-model users** who want to switch between OpenAI, Anthropic, Google, DeepSeek, and
others based on cost, capability, or rate limits, without rebuilding their workflow each time.

---

## What Hermes Is Not

**Not an IDE.** If in-editor autocomplete and inline diffs are your primary workflow,
Cursor or Copilot will be better for that specific use case. Hermes lives in the terminal,
the browser, and your messaging apps -- not inside VS Code.

**Not a hosted SaaS.** You run it on your own server. That is the point -- your data stays
on your hardware -- but it does mean there is an initial setup step.

**Not a replacement for the underlying models.** Hermes is an orchestration and memory layer
on top of whatever model you point it at. The models do the reasoning. Hermes makes sure that
reasoning accumulates into something durable.

---

## Quick Reference

| | Claude Code | Codex CLI | OpenCode | Cursor | Claude.ai | **Hermes** |
|---|---|---|---|---|---|---|
| Persistent memory | No | No | No | No | Shallow | **Yes** |
| Scheduled / background jobs | No | No | No | No | No | **Yes** |
| Messaging app access | No | No | No | No | No | **Yes** |
| Web UI | No | No | No | No | Yes | **Yes** |
| Self-hosted | Yes | Yes | Yes | No | No | **Yes** |
| Provider-agnostic | No | No | Yes | No | No | **Yes** |
| Skills system | No | No | No | No | No | **Yes** |
| In-editor autocomplete | No | No | No | Yes | No | No |
| Mobile / messaging access | No | No | No | No | Yes | **Yes** |
| Orchestrates other agents | No | No | No | No | No | **Yes** |
| Open source | No | Yes | Yes | No | No | **Yes** |
| Always-on / autonomous | No | No | No | No | No | **Yes** |
