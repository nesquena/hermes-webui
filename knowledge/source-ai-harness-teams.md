# Source Trail — AI Harness Teams

Checked: 2026-05-11 JST

Question:
- Can Yuto be considered an AI Harness Team, and what would make it mature?

## Conclusion

Yuto is already a personal AI harness/control plane: an LLM wrapped with tools, memory, skills, source-of-truth Markdown, verification, routing, cron, delegation, and local indexes.

Yuto is not yet a fully operational AI harness team in the production multi-agent sense. The missing parts are durable worker identities/profiles, task queue/dispatch as the normal operating path, receipt/evaluation metrics, and repeated proof that multiple workers outperform a single Yuto loop for specific task classes.

Recommended label:

```text
Yuto = personal AI harness + control-plane lead with an emerging callable worker bench.
```

## Source facts

### Parallel — agent harness definition

Source: https://parallel.ai/articles/what-is-an-agent-harness

Relevant claims observed:
- An agent harness is software infrastructure around an LLM/agent.
- It connects the model to tools, memory stores, workflows, and external environments.
- It manages context lifecycle, tool execution, verification, and persistence.
- It exists because one-shot prompt/response models are insufficient for long-running, tool-oriented, multi-step tasks.

Use for Yuto:
- This maps directly to Hermes tools, memory, skills, terminal/browser/file tools, context compression, and Yuto's verification loop.

Caution:
- Parallel is not a standards body. Treat this as a useful industry framing, not an official taxonomy.

### Anthropic — Building effective agents

Source: https://www.anthropic.com/engineering/building-effective-agents

Relevant claims observed:
- Start with the simplest solution possible; increase complexity only when needed.
- Distinguish workflows from agents: workflows follow predefined code paths; agents dynamically direct their own tool use.
- Basic building block is an augmented LLM with retrieval, tools, and memory.
- Useful patterns include prompt chaining, routing, parallelization, orchestrator-worker, evaluator-optimizer, and autonomous agents.
- Frameworks can help but may hide prompts/responses; understand the underlying loop.

Use for Yuto:
- Yuto should not become a standing swarm by default.
- Prefer simple composable lanes: current Yuto session, delegate_task, cron, Kanban, local LLM reviewer, Codex/Claude coding worker.

### Anthropic — Multi-agent research system

Source: https://www.anthropic.com/engineering/multi-agent-research-system

Relevant claims observed:
- Multi-agent systems help with broad research because subagents operate in parallel with separate context windows, compressing findings back to a lead agent.
- Anthropic uses an orchestrator-worker pattern: lead agent creates subagents to search different aspects, then synthesizes.
- Their internal result: multi-agent research can outperform a single agent for breadth-first queries, but token cost is much higher.
- Coordination complexity is real; early failures included spawning too many subagents, endless search, or distracting updates.

Use for Yuto:
- Yuto should use subagents for breadth/isolation, not for every task.
- Kei's token-efficiency preference argues for callable workers, not always-on team chatter.

### Anthropic — Effective harnesses for long-running agents

Source: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents

Relevant claims observed:
- Long-running agents fail because each context window starts without memory of prior work.
- Harness improvements include initializer agents, progress files, git history, feature requirements, one-feature-at-a-time execution, and explicit end-to-end testing.
- Agents may falsely declare work complete without proper testing unless prompted and equipped to verify.

Use for Yuto:
- Yuto's strongest pattern should be: one active task, artifact/receipt, verification before closure, context handoff, and clean state.
- GoalBuddy-style board/receipt fits this source-backed pattern.

### OpenAI Agents SDK

Source: https://openai.github.io/openai-agents-python/

Relevant claims observed:
- Core primitives: Agents with instructions/tools, Handoffs/delegation, Guardrails/validation, Sessions/memory, MCP tools, tracing, human-in-the-loop.

Use for Yuto:
- Yuto already has analogous primitives through Hermes: tools, delegation, skills, memory, cron, session search, and human approval gates.
- Missing maturity layer is stronger tracing/eval/receipt metrics per worker lane.

### Microsoft AutoGen teams

Source: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html

Relevant claims observed:
- A team is a group of agents working together toward a common goal.
- Teams are for complex tasks requiring collaboration/diverse expertise.
- Start with a single agent for simpler tasks; move to teams when a single agent is inadequate.
- Teams need more scaffolding and observability.

Use for Yuto:
- Current Yuto can be called a control-plane lead with team patterns, but full team status requires regular multi-agent operation, observability, and stop conditions.

### CrewAI

Source: https://docs.crewai.com/en/introduction

Relevant claims observed:
- Flows provide stateful/event-driven workflow control.
- Crews are teams of specialized agents with goals/tools/tasks.
- Architecture aims to balance autonomy with control.

Use for Yuto:
- Hermes Workspace/Swarm roster resembles a visual Crew/office, but our current roster is mostly configuration/visual planning, not proven autonomous execution.

### LangChain / LangGraph

Sources:
- https://docs.langchain.com/oss/python/langchain/agents
- https://docs.langchain.com/oss/python/langgraph/overview

Relevant claims observed:
- Agents use tools in a loop until a stop condition or final output.
- LangGraph focuses on durable execution, persistence, streaming, human-in-the-loop, and observability for long-running agents.

Use for Yuto:
- Durable execution and observability are the main gaps before calling Yuto a mature harness team.

## Yuto current-state evidence

Checked locally 2026-05-11:

- Hermes Agent: v0.12.0.
- Main model config: `gpt-5.5` via `openai-codex`, context length 400000.
- Delegation config: `max_concurrent_children=3`, `max_spawn_depth=1`, `orchestrator_enabled=true`.
- Second brain status: notes=53, graph `nodes=89 edges=301 broken=0 orphans=0`.
- CocoIndex derived cache: installed, DB exists, derived_json_files=53, health `ok=true`.
- Workspace roster file: `/Users/kei/hermes-workspace/swarm.yaml`, 8 lean roles including Yuto control, scope-planner, researcher, analyst, inbox-triage, reminder-ops, scribe, reviewer-qa.
- Ollama/local LLM is available but not the Yuto main brain at time of check.

## Maturity model for Yuto

### Level 0 — LLM chat

Only conversation, no durable tools/memory/verification.

Yuto status: passed.

### Level 1 — Augmented assistant

LLM has tools, file access, terminal/browser, memory, and can complete tasks.

Yuto status: passed.

### Level 2 — Personal agent harness

Adds source-of-truth knowledge, procedural skills, routing, verification, cron, indexing, and safety gates.

Yuto status: mostly passed.

### Level 3 — Harness-led callable team

A lead agent routes to specialized workers/subagents when useful; workers are temporary/callable; outputs are verified by the lead; receipts and artifacts are preserved.

Yuto status: partially passed / current target state.

### Level 4 — Operational AI harness team

Standing profiles/workers, durable task queue, human-in-loop gates, observability/tracing, evaluation metrics, repeatable role-specific workflows, and evidence that team mode improves outcomes for specific task classes.

Yuto status: not yet.

### Level 5 — Production agent organization

Multi-user, audited, permissioned, cost-controlled, secure, deployment-safe, with rollback/incident handling and formal evals.

Yuto status: intentionally not the current goal.

## Recommended next step

Do not add a standing swarm yet. Build a small proof loop:

```text
Yuto lead -> one worker lane -> receipt -> Yuto verification -> metric update
```

Best first lanes:

1. Researcher: source collection for broad topics.
2. Reviewer QA: read-only verification of Yuto outputs.
3. Local LLM Scout: cheap local extraction/classification only, not Thai prose authority.
4. Codex/Claude coding worker: implementation only after Yuto defines acceptance criteria.

Completion contract for each lane:

- Task type
- Worker used
- Input artifact
- Output artifact
- Verification performed
- Failure mode observed
- Whether worker saved time/quality compared with Yuto alone

## Decision

Call Yuto:

```text
personal AI harness / Yuto-led control plane
```

Do not yet claim:

```text
fully operational AI harness team
```

The right upgrade path is evidence-based worker lanes, not more always-on infrastructure.

Related: [[yuto-personal-assistant-operating-system]], [[source-ai-project-manager-agent]], [[source-agentic-architectural-patterns]], [[source-goalbuddy]], [[source-cocoindex]], [[yuto-recursive-context-operator]]
