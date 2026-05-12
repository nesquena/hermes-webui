# Source Trail — AI Legal Research Topic Selection

Checked: 2026-05-11 01:30 JST
Project anchor: [[ai-era-legal-advocacy-company-blueprint]]
Status: working research-direction note

## Conclusion

Best single research direction for Kei's Master's -> PhD continuity and the AI-era legal advocacy project:

> Human-reviewed AI system for preserving, organizing, and validating digital evidence in AI-enabled harms.

Recommended academic title shape:

> Evidence-Centered AI for Digital Harm Response: Human-in-the-Loop Preservation, Provenance, and Reliability of AI-Assisted Digital Forensic Evidence

Thai framing:

> ระบบ AI ช่วยเก็บ รักษา จัดระเบียบ และตรวจสอบความน่าเชื่อถือของหลักฐานดิจิทัลสำหรับผู้เสียหายจากภัย AI โดยมีมนุษย์ตรวจทานและมี chain of custody

This is stronger than choosing only deepfake detection or only legal chatbot because it sits exactly at the project's intersection:

- victim first response
- digital evidence preservation
- forensic reliability
- legal-prep and courtroom explainability
- policy standards for AI-era harms

## Fit With Project Blueprint

Blueprint lines of fit:

- company identity: legal advocacy + digital forensics + AI safety + victim support + courtroom evidence strategy + policy reform
- core problem: people lose before court because they cannot turn digital harm into usable evidence
- service pillars: Help, Prove, Argue, Reform, Teach
- research topics: synthetic media risk, digital evidence standards, chain of custody, forensic artifact handling, legal intake automation, defensive AI agents, platform accountability

Therefore, the research topic should not be framed as `AI lawyer` or `legal chatbot`.

The strongest frame is:

```text
AI-assisted evidence infrastructure for digital harm victims and legal advocates.
```

## Topic Ranking

### 1. Recommended Core Topic: Evidence-Centered AI for Digital Harm Response

Research question:

> How can an AI-assisted, human-reviewed system help non-expert victims preserve, structure, and validate digital evidence from AI-enabled harms while maintaining provenance, chain of custody, and legal usefulness?

Why this is best:

- directly serves real users before evidence disappears
- produces a buildable prototype for Kei's company
- can become Master's thesis as system design/evaluation
- can extend to PhD as standards, reliability, governance, and courtroom evidence framework
- safer and more lawful than offensive cyber or surveillance-heavy research

Possible thesis artifacts:

- evidence intake workflow
- evidence checklist model
- provenance and chain-of-custody schema
- fact/inference/unknown labeling rubric
- AI-assisted timeline builder
- human review dashboard
- reliability evaluation benchmark
- policy recommendations for platforms/courts/legal aid

### 2. Strong Supporting Topic: Reliability Evaluation of LLM-Assisted Digital Forensics

Research question:

> How reliable are LLM-assisted forensic artifact extraction and timeline generation, and what verification protocols reduce hallucination and evidentiary error?

Use this as the technical spine of topic 1.

Why it matters:

- directly addresses court risk: AI must not invent facts
- arXiv trend shows active movement in LLM + digital forensics benchmarks and reliability
- can produce measurable experiments

### 3. Supporting Topic: Synthetic Media Provenance and Court-Ready Explanation

Research question:

> How should legal advocates combine detection, watermarking, provenance metadata, and uncertainty explanation when evaluating alleged AI-generated media?

Why not choose this as the only core topic:

- deepfake detection is crowded and model-chasing
- detector accuracy decays as generation models change
- for legal work, provenance + uncertainty + evidence handling matter more than claiming detector certainty

Use as a case domain inside topic 1.

### 4. Lower Priority: Legal RAG / Legal Chatbot

Research question:

> How can legal RAG reduce hallucination in legal consultation?

Why lower priority:

- crowded
- risks unauthorized legal advice if productized badly
- less differentiated from generic legal tech
- weaker connection to forensic evidence and victim first response

Use only as a component for legal information retrieval, not the headline thesis.

### 5. Lower Priority / Avoid as Core: Cybercrime LLM Threat Studies

Research question:

> How are cybercriminals using LLMs?

Why lower priority:

- useful background, but can drift into dual-use/offensive territory
- less directly tied to Kei's legal advocacy product unless scoped to victim response and policy

Use as threat landscape, not core research method.

## arXiv Evidence Checked

### Digital forensics + LLM reliability / benchmarks

1. `2505.03100` — Towards a standardized methodology and dataset for evaluating LLM-based digital forensic timeline analysis
   - arXiv: https://arxiv.org/abs/2505.03100
   - Evidence: proposes standardized methodology inspired by NIST Computer Forensic Tool Testing Program for evaluating LLM-based digital forensic timeline analysis.
   - Relevance: strongest support for building evaluation/benchmarking around AI-assisted evidence timelines.

2. `2602.20202` — Evaluating the Reliability of Digital Forensic Evidence Discovered by Large Language Model: A Case Study
   - arXiv: https://arxiv.org/abs/2602.20202
   - Evidence: proposes automated forensic artifact extraction refined by LLM analysis and validated using a Digital Forensic Knowledge Graph.
   - Relevance: directly aligned with `AI finds evidence` needing validation before legal use.

3. `2505.19973` — DFIR-Metric: A Benchmark Dataset for Evaluating Large Language Models in Digital Forensics and Incident Response
   - arXiv: https://arxiv.org/abs/2505.19973
   - Evidence: benchmark for evaluating LLMs across theoretical and practical DFIR domains, motivated by hallucination risk in high-stakes contexts.
   - Relevance: supports a Master's-level experimental evaluation track.

4. `2507.18478` — Scout: Leveraging Large Language Models for Rapid Digital Evidence Discovery
   - arXiv: https://arxiv.org/abs/2507.18478
   - Evidence: uses LLMs for rapid digital evidence discovery as investigators sift through large data volumes.
   - Relevance: conceptually close to first-response evidence discovery, but Kei's version should be victim-safe and legal-prep oriented.

5. `2402.19366` — Exploring the Potential of Large Language Models for Improving Digital Forensic Investigation Efficiency
   - arXiv: https://arxiv.org/abs/2402.19366
   - Evidence: literature review on integrating LLMs into digital forensics, discussing bias, explainability, censorship, resource constraints, and ethical/legal considerations.
   - Relevance: useful literature-review foundation.

6. `2307.10195` — ChatGPT for Digital Forensic Investigation: The Good, The Bad, and The Unknown
   - arXiv: https://arxiv.org/abs/2307.10195
   - Evidence: early assessment of ChatGPT impact on digital forensics.
   - Relevance: baseline paper for evolution of the field.

7. `2312.14607` — ChatGPT, Llama, can you write my report? An experiment on assisted digital forensics reports written using (Local) Large Language Models
   - arXiv: https://arxiv.org/abs/2312.14607
   - Evidence: studies LLM assistance in forensic report writing.
   - Relevance: supports legal/forensic report-drafting but must keep human review.

8. `2506.00274` — Chances and Challenges of the Model Context Protocol in Digital Forensics and Incident Response
   - arXiv: https://arxiv.org/abs/2506.00274
   - Evidence: argues transparency, explainability, and reproducibility are adoption blockers for LLMs in forensics; explores MCP for forensic workflows.
   - Relevance: supports tool-augmented, reproducible workflows over black-box chatbots.

9. `2604.05589` — Foundations for Agentic AI Investigations from the Forensic Analysis of OpenClaw
   - arXiv: https://arxiv.org/abs/2604.05589
   - Evidence: studies how agentic AI internal state/actions can be reconstructed for forensic analysis.
   - Relevance: useful for future PhD angle: forensic accountability of AI agents themselves.

10. `2603.23996` — Forensic Implications of Localized AI: Artifact Analysis of Ollama, LM Studio, and llama.cpp
    - arXiv: https://arxiv.org/abs/2603.23996
    - Evidence: local LLM runners create evidentiary blind spots and require artifact analysis.
    - Relevance: relevant to AI-era evidence: local AI tools themselves become forensic objects.

### Deepfake / synthetic media / provenance

11. `2602.18681` — Media Integrity and Authentication: Status, Directions, and Futures
    - arXiv: https://arxiv.org/abs/2602.18681
    - Evidence: compares provenance, watermarking, and fingerprinting for distinguishing AI-generated media from authentic capture.
    - Relevance: best umbrella for courtroom-friendly media authenticity work.

12. `2604.24890` — Verifying Provenance of Digital Media: Why the C2PA Specifications Fall Short
    - arXiv: https://arxiv.org/abs/2604.24890
    - Evidence: independent security analysis of C2PA; reports claimed security gaps in current specs.
    - Relevance: important warning: provenance standards help, but cannot be blindly trusted in court.

13. `2504.03765` — Watermarking for AI Content Detection: A Review on Text, Visual, and Audio Modalities
    - arXiv: https://arxiv.org/abs/2504.03765
    - Evidence: survey/taxonomy of watermarking techniques and limitations across modalities.
    - Relevance: use to frame watermarking as one signal, not proof.

14. `2505.13847` — Forensic deepfake audio detection using segmental speech features
    - arXiv: https://arxiv.org/abs/2505.13847
    - Evidence: interpretable segmental speech features can help identify deepfake audio.
    - Relevance: fits voice clone scam case domain.

15. `2604.03558` — LOGER: Local-Global Ensemble for Robust Deepfake Detection in the Wild
    - arXiv: https://arxiv.org/abs/2604.03558
    - Evidence: robust in-the-wild deepfake detection remains hard because cues exist at local and global levels.
    - Relevance: supports uncertainty-aware detector use.

16. `2404.17867` — Are Watermarks Bugs for Deepfake Detectors? Rethinking Proactive Forensics
    - arXiv: https://arxiv.org/abs/2404.17867
    - Evidence: watermarking may interfere with deployed deepfake detectors.
    - Relevance: shows why legal systems need multi-signal explanation, not one magic authenticity signal.

### Legal RAG / legal hallucination

17. `2401.01301` — Large Legal Fictions: Profiling Legal Hallucinations in Large Language Models
    - arXiv: https://arxiv.org/abs/2401.01301
    - Evidence: systematic evidence of legal hallucinations across jurisdictions/courts/time periods/cases.
    - Relevance: must cite as baseline warning against unsupervised `AI lawyer` positioning.

18. `2505.02164` — Incorporating Legal Structure in Retrieval-Augmented Generation: A Case Study on Copyright Fair Use
    - arXiv: https://arxiv.org/abs/2505.02164
    - Evidence: uses legal knowledge graphs and court citation networks to improve RAG retrieval/reasoning reliability.
    - Relevance: supports structured legal knowledge over plain vector RAG.

19. `2502.20640` — LexRAG: Benchmarking Retrieval-Augmented Generation in Multi-Turn Legal Consultation Conversation
    - arXiv: https://arxiv.org/abs/2502.20640
    - Evidence: benchmark for RAG in multi-turn legal consultations.
    - Relevance: useful if Kei later builds legal information intake, but not the main research differentiator.

20. `2510.06999` — Towards Reliable Retrieval in RAG Systems for Large Legal Datasets
    - arXiv: https://arxiv.org/abs/2510.06999
    - Evidence: identifies document-level retrieval mismatch in legal RAG over large similar document sets.
    - Relevance: legal RAG reliability is an active problem; use carefully.

21. `2504.01840` — LRAGE: Legal Retrieval Augmented Generation Evaluation Tool
    - arXiv: https://arxiv.org/abs/2504.01840
    - Evidence: evaluates RAG components for legal decision retrieval and generation.
    - Relevance: useful evaluation reference if legal RAG becomes a component.

22. `2506.00694` — Measuring Faithfulness and Abstention: An Automated Pipeline for Evaluating LLM-Generated 3-ply Case-Based Legal Arguments
    - arXiv: https://arxiv.org/abs/2506.00694
    - Evidence: evaluates faithfulness, factor utilization, and appropriate abstention in legal argument generation.
    - Relevance: supports the `AI should abstain / label uncertainty` principle.

### Cybercrime and AI threat landscape

23. `2408.03354` — The Use of Large Language Models (LLM) for Cyber Threat Intelligence (CTI) in Cybercrime Forums
    - arXiv: https://arxiv.org/abs/2408.03354
    - Evidence: evaluates LLMs for extracting CTI information from cybercrime forums.
    - Relevance: useful for threat intelligence, but data/source legality and safety matter.

24. `2603.29545` — Stand-Alone Complex or Vibercrime? Exploring the adoption and innovation of GenAI tools, coding assistants, and agents within cybercrime ecosystems
    - arXiv: https://arxiv.org/abs/2603.29545
    - Evidence: studies GenAI adoption and innovation in cybercrime ecosystems.
    - Relevance: good policy/threat-background reading.

25. `2512.21371` — The Imitation Game: Using Large Language Models as Chatbots to Combat Chat-Based Cybercrimes
    - arXiv: https://arxiv.org/abs/2512.21371
    - Evidence: explores LLM chatbots to combat chat-based cybercrime.
    - Relevance: relevant to scam/impersonation defense, but needs careful ethics and safety design.

26. `2401.03315` — Malla: Demystifying Real-world Large Language Model Integrated Malicious Services
    - arXiv: https://arxiv.org/abs/2401.03315
    - Evidence: systematic study of malicious LLM-integrated services.
    - Relevance: background for AI abuse ecology.

## Master's to PhD Path

### Master's thesis version

Suggested title:

> Designing and Evaluating a Human-in-the-Loop AI System for Digital Evidence Preservation in AI-Enabled Harms

Scope:

- build a prototype intake + evidence timeline + provenance checklist
- use synthetic case scenarios: deepfake harassment, voice scam, account takeover, AI-generated defamation
- evaluate with legal/forensic criteria: completeness, source traceability, hallucination avoidance, user comprehension, reviewer workload
- compare baseline forms/checklists vs AI-guided workflow

Deliverables:

- prototype
- evidence schema
- evaluation rubric
- synthetic benchmark cases
- human-review protocol
- policy brief

### PhD version

Suggested title:

> Trustworthy AI Evidence Infrastructure for Digital Harm: Provenance, Reliability Evaluation, and Human-Reviewed Legal Advocacy Workflows

Scope extension:

- multi-jurisdiction legal evidence standards
- reliability benchmarks for AI-assisted forensic outputs
- courtroom explainability and uncertainty communication
- platform/bank/law-enforcement response workflows
- policy model for AI-era digital harm evidence handling

## Research Method Options

### Method A: Design Science Research

Build an artifact, evaluate it, iterate.

Best for Kei because the company needs a working tool and the thesis can produce a practical system.

### Method B: Mixed Methods

Combine prototype metrics with interviews/surveys of lawyers, victims, forensic practitioners, NGOs, or platform trust/safety experts.

Best for policy relevance.

### Method C: Benchmark / Evaluation Research

Create synthetic case set and score system outputs.

Best for technical defensibility and publication.

Recommended combination:

```text
Design Science + Benchmark Evaluation + Expert Review
```

## Safety and Boundary Notes

- Use synthetic cases or authorized/public data only.
- Do not ingest real victim evidence until ethics approval and legal process exist.
- AI must label fact / inference / unknown.
- Original evidence must remain immutable; AI works on copies/metadata.
- No legal advice automation; produce legal-prep and evidence organization for human review.
- No hack-back, credential theft, unauthorized access, doxxing, or surveillance.

## Immediate Literature Review Buckets

1. LLMs in digital forensics and DFIR benchmarks.
2. Digital evidence reliability, chain of custody, and forensic reporting.
3. Synthetic media provenance, watermarking, detection, and C2PA limitations.
4. Legal hallucination and legal RAG reliability.
5. Human-in-the-loop AI, uncertainty labeling, and explainable AI for high-stakes workflows.
6. Victim support / access to justice / cybercrime reporting process.

## Next Research Questions

- What minimal evidence schema helps victims preserve legally useful evidence without over-collecting sensitive data?
- Can an AI-guided intake workflow improve evidence completeness compared with a static checklist?
- How should AI distinguish fact, inference, and unknown in evidence summaries?
- What verification protocol prevents LLM-generated forensic timelines from becoming hallucinated evidence?
- How should courts and lawyers interpret AI-assisted evidence organization without treating AI output as proof?

## Recommended One-Sentence Direction

> Research and build a human-reviewed AI evidence assistant that helps victims of AI-enabled digital harms preserve evidence, construct timelines, label uncertainty, and prepare legally useful case materials without replacing lawyers or forensic experts.
