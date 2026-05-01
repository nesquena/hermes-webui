# Source: Recursive Language Models

Created: 2026-04-29
Source URL: https://alexzhang13.github.io/blog/2025/rlm/
Paper URL: https://arxiv.org/abs/2512.24601v1
Minimal implementation: https://github.com/alexzhang13/rlm-minimal
Source type: web article + paper/source trail
Related: [[sources]] [[research]] [[yuto-recursive-context-operator]] [[workflows]] [[memory-system]]

## Why This Matters

This source is the strongest current analogy for how Yuto should operate when Kei uses it as a research/control-plane assistant rather than a main coding agent.

The article is not merely saying "store context outside the model." It proposes an inference strategy where the prompt/context becomes an object inside an environment, and the model controls how to inspect, transform, decompose, and recursively query pieces of that object.

## Key Claims From Source

Source-backed claims, paraphrased rather than copied:

1. RLM is an inference strategy, not a new model architecture.
   - A Recursive Language Model wraps an LM call so it can spawn recursive LM calls for intermediate computation.
   - From the user/programmer view, `rlm.completion(messages)` can act like a replacement for a standard LM completion call.

2. RLM is context-centric.
   - The root LM receives the query, while a potentially huge context is stored in an environment.
   - The article explicitly frames the input context as a variable that the model can interact with.

3. The concrete implementation uses a Python REPL-like environment.
   - The context is preloaded as a variable in memory.
   - The root LM writes code cells, observes outputs, and can use code to inspect or transform the context.

4. The root LM can launch recursive subqueries.
   - The root LM can call a recursive LM/LM inside the REPL over selected snippets/chunks.
   - In the blog experiments, recursion depth is mostly depth=1: root LM calls sub-LMs, not necessarily full nested RLMs.

5. The benefit is not magical non-confusion.
   - The root context is less clogged because it does not directly see the entire context.
   - The model can use strategies such as peeking, grepping, partitioning, map-style subcalls, summarization, and variable stitching.
   - This changes the failure mode from "context window rot" to "bad exploration/decomposition strategy," which is more inspectable.

6. The article reports early strong results, but not a guarantee.
   - Reported results include gains on OOLONG and BrowseComp-Plus-style long-context/deep-research tasks.
   - Limitations include runtime/cost variance, blocking subcalls, weak guarantees on total cost/runtime, and need for better trained RLM-specific behavior.

## Non-Digested Operating Model

Keep this model close to the source language:

```text
RLM(query, context)
  root LM sees query + environment affordances
  context is stored as variable P in environment E
  root LM writes code to inspect P
  root LM may print small observations from P
  root LM may grep/filter/partition P
  root LM may construct snippets/chunks
  root LM may call sub-LM(snippet, subquery)
  root LM stores intermediate results as variables
  root LM returns FINAL(answer) or FINAL_VAR(answer_var)
```

## Practical Translation For Yuto

Yuto should imitate the RLM pattern at the operating level:

```text
Kei asks a question
  -> Yuto keeps root context focused on the question and success criteria
  -> source/context stays outside active prose when possible
  -> Yuto inspects via browser/search_files/read_file/terminal/session_search/KG
  -> Yuto uses code or structured notes to filter, count, cluster, and compare
  -> Yuto delegates only bounded snippets/questions to subagents when useful
  -> Yuto merges evidence and states fact/inference/unknown
  -> Yuto stores durable patterns in knowledge, not active memory bulk
```

## Immediate Use Plan

Use this as the default plan for research-heavy Yuto tasks starting now.

### Phase 0: Classify Task

Choose one mode before acting:

- THINK: companion/advisor/brake question; do not over-execute.
- RESEARCH: source reading, landscape, comparison, product/technical intelligence.
- PLAN: create a spec or decision path.
- EXECUTE: modify files, run commands, send messages, or create artifacts.

### Phase 1: Externalize Context

For any non-trivial task, identify where the true context lives:

- URL/article/paper
- local file/KG note
- repo/log/test output
- prior session via `session_search`
- browser page or document corpus

Do not try to answer from active memory when the context can be inspected.

### Phase 2: Peek and Shape

Before summarizing:

- inspect title/date/author/source type
- extract headings or structure
- capture key claims and limitations
- identify which sections answer Kei's actual question

### Phase 3: Decompose Only If Needed

Use subagents or subqueries only when one of these is true:

- multiple independent branches
- large corpus that would pollute root context
- need fresh review/critique
- distinct specialist lens such as security, market, product, or implementation feasibility

Each subtask must include:

- objective
- bounded source/context
- output schema
- stop condition
- what not to do

### Phase 4: Verify and Merge

Before final answer:

- tie important claims to source URLs/file paths/command output
- label fact/inference/speculation/unknown
- avoid saying "ไม่มีวันหลง" or other absolute claims
- note residual failure modes and what would catch them

### Phase 5: Route Memory

- source trail -> `knowledge/sources.md` or focused source note
- Yuto behavior change -> `knowledge/yuto.md` or focused pattern note
- repeatable workflow after repeated use -> skill
- active memory -> only compact always-needed pointer

## RLM Failure Modes To Watch

- wrong initial search/grep terms
- over-trusting sampled snippets
- chunk boundaries hide cross-chunk dependencies
- sub-LM outputs silently hallucinate or compress away details
- recursive calls multiply cost/runtime
- root LM over-verifies or flips from correct to wrong answer late
- environment/tool output is treated as trusted when it contains untrusted instructions

## Yuto Canaries

Use these during research and self-improvement tasks:

1. Did Yuto inspect the real source or file before making the claim?
2. Did Yuto preserve source nuance rather than over-simplify into a slogan?
3. Is the context outside active memory, with only pointers/patterns promoted?
4. Were subagents used for breadth/isolation rather than for show?
5. Is there a visible trail: source -> extraction -> inference -> recommendation?

## Open Questions

- What is the smallest local script/tool that would make Yuto more RLM-like without building a full platform?
- Should Yuto maintain a tiny `rlm-task-template.md` for research tasks, or is this note enough for now?
- Which repeated research tasks would justify a future dedicated skill?

## Copyright / Storage Policy

This note intentionally does not store the full article text. The source URL remains the source of truth. Stored content is a compact source trail, paraphrased operating model, and Yuto-specific application plan.
