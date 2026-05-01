# Source: AI Project Manager Agent Pattern

Date checked: 2026-05-01
Topic: Whether Yuto/Hermes can act as an AI Project Manager over coding agents

Conclusion:
A practical AI Project Manager is feasible if scoped as an orchestrator/verifier over coding agents, not as an unrestricted autonomous replacement for Kei. The strongest pattern is manager-style orchestration: intake -> plan -> delegate bounded tasks -> answer low-risk questions from policy -> verify with tests/browser/logs -> escalate risky decisions.

Primary evidence checked:
- Local tool availability on Kei's machine: `hermes`, `claude`, `codex`, `kimi`, `opencode`, `git`, and `tmux` binaries were found via `shutil.which`; this proves the basic execution surface exists, not that all credentials or runtime configs are healthy. Operational correction from Kei: Yuto must not call Claude Code; Yuto may orchestrate only Codex and verified local LLMs.
- Hermes skill docs: Hermes supports provider-agnostic models, toolsets, persistent memory, skills, cron, delegation, profiles, gateway platforms, and spawning independent Hermes instances; it can run one-shot or tmux-backed agent sessions.
- Claude Code best practices docs: strongest workflows are explore -> plan -> implement -> verify; Claude Code benefits from explicit verification criteria, tests, screenshots, expected outputs, context management, subagents for investigation, checkpoints, and multiple sessions.
- Anthropic multi-agent research system article: multi-agent systems work best for breadth-first, parallelizable, high-value tasks; they use much more tokens, need coordination/evaluation, and are not ideal when all agents must share the same evolving context. Coding is less naturally parallel than broad research, so task boundaries matter.
- OpenAI Agents SDK docs: practical agent systems use instructions, tools, structured outputs, guardrails, tracing, lifecycle hooks, and two common orchestration patterns: manager-as-tools and handoffs.
- OpenAI Agents guardrails docs: input/output/tool guardrails and tripwires are used to prevent unsafe or out-of-scope actions; tool guardrails are important when managers invoke tools/subagents.
- OpenAI tracing docs: traces/spans record LLM generations, tool calls, handoffs, guardrails, and custom events, useful for debugging/monitoring workflows; sensitive data capture must be controlled.
- Local Yuto note [[yuto-recursive-context-operator]]: Yuto should act as a recursive context operator: inspect external context with tools, delegate bounded subproblems, merge evidence, and verify against acceptance criteria.

Recommended architecture:
1. Yuto PM root controller
   - Receives Kei intent.
   - Creates project brief, non-goals, constraints, acceptance criteria, and verification plan.
   - Decides what can be answered by policy vs what needs Kei escalation.

2. Decision policy / escalation rules
   - Auto-answer low-risk implementation details that are already specified by project conventions.
   - Escalate product direction, irreversible changes, deploy/publish, production data, secrets, spending, destructive ops, and ambiguous user-facing choices.

3. Coding workers
   - Codex and MLX 27B local LLM workers perform bounded implementation/review tasks.
   - Claude Code is not an allowed worker for Yuto, even if the binary exists locally.
   - Ollama, 35B local-model routing, and hidden backend changes are not allowed unless Kei explicitly reverses this.
   - Fresh context per task when possible.
   - Do not let multiple workers edit tightly coupled files without coordination.

4. Review workers
   - Spec compliance review first.
   - Code quality/security review second.
   - Integration review after all tasks.

5. Verification gate
   - Tests/build/lint/typecheck as appropriate.
   - Browser/E2E screenshot or live check for UI.
   - Logs and diffs captured.
   - Completion status based on metric movement, not generated reports.

6. Observability / transcript
   - Every agent run needs task ID, prompt, decisions, commands/tests run, artifacts, blockers, and residual risk.
   - Optional future tracing/structured logs should redact secrets.

MVP scope:
- Start with a single repo and a single non-production project.
- Use Yuto to produce a `PROJECT_PM.md` contract and task queue.
- Delegate one implementation task at a time to a coding agent.
- Use a review+verification loop before proceeding.
- Require Kei approval for deploy/destructive/production/secret decisions.

Non-goals:
- No autonomous deploy.
- No production data changes.
- No secret reading/printing/transmission.
- No broad multi-agent swarm without bounded tasks.
- No claim that the PM is a full replacement for Kei's product judgment.

Risks:
- False completion if verification is weak.
- Context rot if the root session becomes bloated.
- Cost explosion if multi-agent fanout is used where dependencies are tight.
- Workers may ask questions the PM should not answer without Kei.
- Browser/UI quality can still be poor unless visual acceptance criteria exist.
- Tool output and untrusted web/code content can cause prompt-injection risk.

Next pilot:
Create a lightweight `Yuto PM Mode v0` runbook/skill only after running one real scoped pilot. The pilot success metric should be: fewer Kei interruptions, all critical decisions escalated correctly, full verification evidence captured, and no claim drift.

Related: [[yuto-recursive-context-operator]] [[workflows]] [[sources]] [[rules]] [[security]] [[yuto-growth-loop]]
