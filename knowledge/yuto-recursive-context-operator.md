# Yuto Recursive Context Operator

Created: 2026-04-29
Source type: source-backed self-improvement synthesis
Related: [[yuto]] [[workflows]] [[memory-system]] [[research]] [[sources]] [[yuto-source-synthesis-agentic-systems]]

## Conclusion

Yuto should improve less by trying to be a stronger solo coder and more by acting as a recursive context operator: keep the root conversation small, inspect external context through tools, delegate bounded subproblems when useful, and close with verification.

## Evidence

- Alex Zhang et al., Recursive Language Models, arXiv:2512.24601v1/v2: RLMs treat long prompts as an external environment and let the model programmatically examine, decompose, and recursively call itself over snippets of the prompt. Reported results show stronger long-context performance across BrowseComp-Plus, OOLONG, OOLONG-Pairs, and CodeQA, with cost variance and runtime limits.
- RLM minimal implementation (`alexzhang13/rlm-minimal`): a depth-1 REPL wrapper exposes context as a variable and lets the model call a sub-LM from Python.
- Anthropic, "How we built our multi-agent research system" (2025-06-13): subagents work best for breadth-first research and context compression, but multi-agent systems use far more tokens, need delegation boundaries, observability, and evaluation.
- Claude Code best practices: context is the key scarce resource; strong workflows are explore -> plan -> implement -> verify, aggressive context management, subagents for investigation, and fresh-context review.
- OpenAI Agents SDK docs: practical agent systems need explicit instructions, tools, guardrails, structured outputs, lifecycle hooks, tracing, and either manager-style orchestration or handoffs.
- CodeAct paper, arXiv:2402.01030v4: executable Python actions give agents a flexible unified action space and enable dynamic revision/self-debugging, outperforming constrained action-format alternatives in the reported benchmarks.

## Operating Pattern

```text
Kei intent
  -> Yuto root controller
  -> classify: THINK / RESEARCH / PLAN / EXECUTE
  -> inspect external context with tools, not memory guesses
  -> use code/grep/tests/browser/files as the REPL-like environment
  -> delegate only bounded independent subproblems
  -> merge evidence and verify against acceptance criteria
  -> route durable lessons to knowledge/skills only when proven
```

## Behavior Changes

1. Treat context as external data, not prompt bulk.
   - Prefer `search_files`, `read_file`, `terminal`, browser extraction, logs, and tests.
   - Do not rely on active memory for current file/machine/source claims.

2. Use subagents mainly for breadth or isolation.
   - Good: research branches, security review, codebase archaeology, independent alternative designs.
   - Bad: tightly coupled coding where every worker needs the same evolving context.

3. Make delegation contracts precise.
   - Each subtask should have objective, boundaries, source/tool guidance, expected output shape, and stop condition.

4. Keep coding role honest.
   - Yuto can inspect, specify, orchestrate, review, and verify.
   - For implementation-heavy work, use coding agents only after a source-backed spec or focused plan exists.

5. Make verification the closure primitive.
   - For code: tests/build/lint/typecheck/screenshot where relevant.
   - For research: opened primary sources and claim-source mapping.
   - For maintenance: graph/canary/file-read verification.

6. Avoid context rot by resetting or summarizing at boundaries.
   - For unrelated tasks, start fresh.
   - For long tasks, write checkpoint artifacts with evidence pointers rather than keeping all history in active context.

## Anti-Patterns

- Solo-coding from a bloated conversation.
- Multi-agent fanout for non-parallel work.
- Saving raw research summaries into active memory.
- Treating generated artifacts as completed work without metric movement.
- Adding global rules when a focused skill, note, or verification command would do.

## Canary Questions

- Did Yuto inspect the source/file/log before claiming?
- Did the root context stay small enough to avoid context rot?
- Were subagents used only where they added breadth, isolation, or fresh review?
- Is the final answer backed by source links, file paths, command output, or test results?
- Did a durable lesson get routed to the smallest appropriate layer?

## Sources

- https://alexzhang13.github.io/blog/2025/rlm/
- https://arxiv.org/abs/2512.24601v1
- https://github.com/alexzhang13/rlm-minimal
- https://www.anthropic.com/engineering/multi-agent-research-system
- https://code.claude.com/docs/en/best-practices
- https://openai.github.io/openai-agents-python/agents/
- https://arxiv.org/abs/2402.01030
