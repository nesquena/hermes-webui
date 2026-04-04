# Why Hermes: A Deep Dive

Hermes is a persistent, autonomous AI agent that lives on your server. This document explains the mental model behind Hermes, why it exists, and how it compares to every major AI coding tool available today.

---

## The Core Mental Model: Assistant vs. Agent

Most AI tools today are **assistants**. You open a window, type a question, get an answer, and close it. The conversation ends. The tool forgets. You start over tomorrow.

Hermes is an **agent**. It runs 24/7 on your server. It has memory that accumulates across every session. It can schedule its own tasks, monitor things while you sleep, and get smarter the longer it runs. It does not forget.

Think of it this way:

```
Assistant model:  You -> [Tool] -> Answer -> Done (tool has no memory of you)
Agent model:      You <-> [Hermes] (persistent, learns, acts on your behalf, schedules work)
```

The difference is not cosmetic. It changes what is possible.

---

## The Three Pillars of Hermes

### 1. Persistent Memory

Hermes has layered memory that survives across every session, every reboot, every provider switch:

- **User profile** - who you are, your preferences, your communication style, your pet peeves
- **Agent memory** - facts about your environment, your tools, your project conventions
- **Skills** - reusable procedures Hermes discovers and saves so it never has to relearn them
- **Session history** - every past conversation is searchable; Hermes can recall what you worked on last week

When you correct Hermes ("don't do that again"), it remembers. When it solves a hard problem, it saves the approach as a skill. It compounds.

### 2. Autonomous Scheduling (Cron)

Hermes can schedule jobs that run without you present. Every hour, every morning, on a custom schedule -- Hermes will fire up a fresh session, run the task, and deliver the result to your messaging platform of choice (Telegram, Discord, Slack, Signal, WhatsApp, SMS, email, and more).

This means Hermes can:
- Monitor a GitHub repo for new PRs and review them automatically
- Send you a daily briefing of news or market data
- Run your test suite every morning and alert you if something breaks
- Watch a competitor's blog for new posts
- Scrape and summarize content on a schedule

No other tool in this space does this.

### 3. Multi-Modal Access

Hermes runs on your server and is accessible from everywhere:

- **Terminal** (SSH) - the native interface, full power
- **Web UI** - this project; Claude-style three-panel browser interface
- **Messaging apps** - Telegram, Discord, Slack, WhatsApp, Signal, Matrix, and more

You can start a task from your phone on Telegram, check the progress from the web UI on your laptop, and pick up the terminal session on a remote server. All the same agent, all the same memory, all the same history.

---

## How AI Coding Tools Actually Differ

There are four distinct categories of AI coding tool. Understanding which category a tool falls into tells you almost everything about what it can and cannot do.

### Category 1: Chat Assistants (Stateless)
*Examples: Claude.ai, ChatGPT, Gemini*

- No persistent memory beyond a single conversation
- No ability to run code, browse files, or take real actions
- No scheduling; no background operation
- Excellent for Q&A, drafting, brainstorming
- You repeat context every session

### Category 2: IDE Integrations (Context-bound)
*Examples: GitHub Copilot, Cursor, Windsurf, Zed AI*

- Stateful within a project/IDE session, but not across sessions
- Deep IDE integration (autocomplete, inline edits, diff review)
- Tied to one machine, one editor
- No scheduling, no cross-session memory, no mobile access
- Excellent for in-editor autocomplete and refactoring

### Category 3: Agentic CLI Tools (Session-scoped)
*Examples: Claude Code (Anthropic), OpenCode, Codex CLI (OpenAI), Aider*

- Can use tools: run shell commands, read/write files, search the web
- One session at a time; memory resets between runs
- No scheduling or background operation (except experimental)
- Excellent for multi-step coding tasks in a single terminal session
- Requires you to re-explain context each time you open a new session

### Category 4: Persistent Autonomous Agents (Always-on)
*Examples: Hermes*

- Persistent memory that accumulates across every session
- Scheduling: can run jobs autonomously while you are offline
- Multi-modal access: terminal, web UI, and messaging apps
- Skills system: discovers and saves reusable procedures
- Full agentic tool use: shell, files, browser, code execution, web search
- Gets more capable the longer it runs

Hermes is the only tool in Category 4 that is open-source, self-hosted, and provider-agnostic.

---

## Detailed Comparison

### Hermes vs. Claude Code (Anthropic)

Claude Code is Anthropic's official agentic CLI. It is excellent at single-session deep coding tasks. When you open a terminal, `claude` is one of the most capable tools you can reach for in that moment.

But the session ends. Next time you open it, it does not know who you are, what you worked on, what your conventions are, or what bugs you have already fixed. You re-explain yourself every time.

Hermes uses Claude Code as a sub-agent. Hermes orchestrates it. If Hermes encounters a large coding task, it can spawn Claude Code to handle the implementation details, then fold the result back into its own memory and history. You get the full power of Claude Code plus persistence, scheduling, and multi-modal access on top.

| Feature | Claude Code | Hermes |
|---|---|---|
| Persistent memory across sessions | No | Yes |
| Skills system (saved procedures) | No | Yes |
| Cron / scheduled jobs | No | Yes |
| Messaging app access | No | Yes (10+ platforms) |
| Web UI | No | Yes |
| Self-hosted | Yes | Yes |
| Provider-agnostic | Limited | Yes (10+ providers) |
| Agentic tool use | Yes | Yes |
| Runs Claude Code as sub-agent | N/A | Yes |
| Open source | No | Yes |

### Hermes vs. Codex CLI (OpenAI)

Codex CLI is OpenAI's agentic terminal tool. Similar positioning to Claude Code: excellent for a single focused session, resets between runs.

Hermes can run Codex as a sub-agent for coding tasks, just as it can with Claude Code.

| Feature | Codex CLI | Hermes |
|---|---|---|
| Persistent memory | No | Yes |
| Scheduled jobs | No | Yes |
| Multi-modal access | No | Yes |
| Web UI | No | Yes |
| Provider-agnostic | No (OpenAI only) | Yes |
| Runs Codex as sub-agent | N/A | Yes |
| Open source | Yes | Yes |

### Hermes vs. OpenCode

OpenCode is an open-source TUI agentic coding assistant. Provider-agnostic like Hermes. Good for in-terminal interactive coding sessions.

| Feature | OpenCode | Hermes |
|---|---|---|
| Persistent memory across sessions | No | Yes |
| Scheduled jobs | No | Yes |
| Messaging app access | No | Yes |
| Web UI | No (TUI only) | Yes |
| Mobile access | No | Yes |
| Skills system | No | Yes |
| Provider-agnostic | Yes | Yes |
| Open source | Yes | Yes |

### Hermes vs. Cursor / Windsurf / Copilot (IDE Tools)

These tools are exceptional at what they do: in-editor autocomplete, inline diffs, and PR review inside an IDE. They are not competing for the same use case as Hermes. But for teams using Hermes, they are complementary: use Cursor for in-editor work, use Hermes for everything else (terminal tasks, scheduling, monitoring, cross-session memory).

| Feature | IDE Tools (Cursor/Windsurf/Copilot) | Hermes |
|---|---|---|
| In-editor autocomplete | Excellent | No |
| Inline diff/refactor | Yes | Via shell |
| Persistent cross-session memory | No | Yes |
| Scheduled background jobs | No | Yes |
| Multi-modal (web/mobile/chat) | No | Yes |
| Terminal / shell tool use | Limited | Full |
| Self-hosted | No | Yes |

### Hermes vs. Claude.ai / ChatGPT (Web Chat)

Web chat tools are stateless assistants. Hermes is an agent. The gap is wide:

| Feature | Web Chat (Claude.ai / ChatGPT) | Hermes |
|---|---|---|
| Memory across conversations | Limited (opt-in, shallow) | Yes (deep, layered) |
| Runs shell commands | No | Yes |
| Reads/writes files | No | Yes |
| Schedules background jobs | No | Yes |
| Terminal access | No | Yes |
| Web UI | Yes | Yes |
| Messaging apps | No | Yes |
| Self-hosted | No | Yes |
| Provider-agnostic | No | Yes |

---

## The Compounding Advantage

The most important thing about Hermes is not any single feature. It is that Hermes improves over time.

Every time Hermes encounters a new environment, it saves facts to memory. Every time it solves a problem a clever way, it saves the approach as a skill. Every time you correct it, it updates its profile of you. Every session, every scheduled job, every tool call -- the agent gets more calibrated to you, your environment, and your workflow.

A Claude Code session on day one and day one hundred are identical. A Hermes agent on day one and day one hundred is meaningfully smarter about you.

---

## Who Hermes Is For

**Solo developers and power users** who want an AI that knows their stack, their conventions, and their preferences without re-explaining every session.

**Teams running on a shared server** where multiple people want Claude-style AI access without each needing an expensive subscription or local setup.

**Automation-heavy workflows** where you want an AI to monitor things, run regular tasks, and deliver results to your phone without babysitting it.

**Privacy-conscious users** who want to keep their conversations, files, and memory on their own hardware, not in a SaaS provider's cloud.

**Multi-model experimenters** who want to switch between OpenAI, Anthropic, Google, DeepSeek, and others without rebuilding their workflow.

---

## What Hermes Is Not

Hermes is not an IDE. If your primary workflow is in-editor autocomplete and diff review, Cursor or Copilot will serve you better for that specific use case.

Hermes is not a hosted SaaS. You run it on your own server. That is a feature (privacy, control, cost), but it does require initial setup.

Hermes is not a replacement for the underlying models. It is a layer on top of them. If Anthropic or OpenAI's models get worse, Hermes gets worse too. The value is in the orchestration, memory, and scheduling layer.

---

## Quick Reference

| Dimension | Claude Code | Codex CLI | OpenCode | Cursor | Claude.ai | **Hermes** |
|---|---|---|---|---|---|---|
| Persistent memory | No | No | No | No | Shallow | **Yes** |
| Scheduled jobs | No | No | No | No | No | **Yes** |
| Messaging apps | No | No | No | No | No | **Yes** |
| Web UI | No | No | No | No | Yes | **Yes** |
| Self-hosted | Yes | Yes | Yes | No | No | **Yes** |
| Provider-agnostic | No | No | Yes | No | No | **Yes** |
| Skills system | No | No | No | No | No | **Yes** |
| In-editor autocomplete | No | No | No | Yes | No | No |
| Mobile chat access | No | No | No | No | Yes | **Yes** |
| Runs other agents | No | No | No | No | No | **Yes** |
| Open source | No | Yes | Yes | No | No | **Yes** |
| Always-on / autonomous | No | No | No | No | No | **Yes** |
