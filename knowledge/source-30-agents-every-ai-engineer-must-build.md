# 30 Agents Every AI Engineer Must Build

Source type: local PDF book
Path: `/Users/kei/Desktop/AI-Books/30 Agents Every AI Engineer Must Build Build production-ready agent systems using proven architectures and patterns (Imran Ahmad) (z-library.sk, 1lib.sk, z-lib.sk).pdf`
Ingested: 2026-04-26
Related: [[sources]] [[research]] [[yuto-growth-loop]] [[memory-system]] [[workflows]] [[source-agentic-architectural-patterns]]

## Bibliographic / File Metadata

- Title observed in PDF TOC: *30 Intelligent Agents Every AI Engineer Should Know*
- Filename title: *30 Agents Every AI Engineer Must Build*
- Subtitle observed: *Build production-ready agent systems using proven architectures and patterns*
- Author from filename/source: Imran Ahmad
- Format: PDF 1.7
- Size checked: 88,271,017 bytes
- Pages: 542
- TOC entries: 540
- Text pages detected: 524
- Estimated extracted words: 168,122
- Creation metadata: 2026-03-31

## Copyright-Safe Ingest Policy

This note stores metadata, TOC-level structure, and Yuto-relevant patterns only. Do not store long verbatim excerpts. Use the local PDF as source of truth for detailed reading.

## Main Coverage Observed

Broad practical catalog for building production-ready agent systems. Early TOC coverage includes:

- foundations of agent engineering
- cognitive loop and agent architectures: reactive, deliberative, hybrid
- MCP and Agent-to-Agent protocols
- agent development lifecycle: requirements, architecture/design, implementation/integration, evaluation, governance
- interaction paradigms: direct LLM, proxy agent, assistant system, autonomous agent, multi-agent systems
- agentic AI progression framework: manual, reactive, tool-using, planning, learning agents
- agent engineer toolkit: LangChain, LangGraph, LlamaIndex, AutoGPT, CrewAI, AutoGen, cloud platforms
- memory/vector databases, retrieval, reranking, metadata, observability
- prompting as constitution: persona, task, context, format; few-shot; chain/tree of thought; communication protocols
- deployment and responsible development: scaling, infrastructure, cost/performance, zero trust, defense-in-depth, ethics
- foundational cognitive architectures: autonomous decision-making, planning, memory-augmented agents
- information retrieval and knowledge agents: RAG, document intelligence, scientific research agents
- tool manipulation and orchestration agents

## Reusable Patterns for Yuto

### 1. Agent engineering is a lifecycle

Pattern: useful agents move through requirements, architecture/design, implementation, evaluation, and governance.

Yuto implication: do not jump from idea to daemon. For any future Yuto agent, require a small lifecycle: purpose, boundary, tools, eval, memory policy, stop condition.

### 2. Agent types should map to capability, not personality

Pattern: reactive, deliberative, hybrid, planning, learning, retrieval, tool-using, document-intelligence, research, and orchestration agents are capability patterns.

Yuto implication: if Yuto later splits real agents, name them by capability and workflow, not fictional persona.

### 3. Memory and retrieval need observability

Pattern: retrieval systems need chunking, reranking, metadata, and observability.

Yuto implication: KG growth should remain inspectable. Markdown/KG first; vector/RAG only after real need and evaluation.

### 4. Prompt constitution is not enough

Pattern: prompts define identity/task/context/format, but production agents need evaluation, lifecycle, security, and governance.

Yuto implication: HERMES.md should stay compact; deeper behavior belongs in workflows/skills/tests/canaries, not one giant prompt.

### 5. Security is part of deployment architecture

Pattern: zero trust, incident preparedness, defense-in-depth, transparency, accountability, and compliance are covered as agent deployment concerns.

Yuto implication: tool execution stays scoped; sensitive input and memory promotion remain guarded; external actions require confirmation.

## Open Questions for Deeper Reading

- Which of the 30 agent patterns best map to Yuto's Companion, Research OS, Auditor, and Operator roles?
- What minimal evaluation harness should exist before a Yuto role becomes a real agent?
- Which memory-augmented agent design is compatible with Yuto's Markdown-first KG?
- How does the book treat MCP/A2A in production compared with simple local tool calls?

## Next Reading Targets

1. Foundations of Agent Engineering
2. Agentic AI Progression Framework
3. Agent Engineer's Toolkit
4. Memory/vector database sections
5. Prompt constitution sections
6. Deployment/responsible development
7. Memory-augmented agent
8. Knowledge retrieval / document intelligence / scientific research agents

## Ingest Verification

Evidence checked locally with PyMuPDF:

- PDF exists and opens.
- Page count: 542.
- TOC entries: 540.
- Text pages: 524.
- Estimated words: 168,122.
