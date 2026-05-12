# AI Legal-Forensic Specialist Worker Bench 2026-05-12

Status: source trail + routing recommendation
Checked: 2026-05-12T10:54:39Z UTC via Hugging Face API/model cards and local blueprint read
Related: [[ai-era-legal-advocacy-company-blueprint]] [[source-small-qwen-security-forensic-models]] [[digital-forensic-lab/README]]

## Authority / Constraint

Kei supplied the team structure as the authority for the AI Legal-Forensic Intelligence Team and explicitly said: do not modify or rewrite the team.

This note does not change the team. It only maps current model/tool candidates into the existing lanes.

Core positioning to preserve:

> Lawyer-in-the-loop AI system for digital evidence, cybercrime, deepfake, AI misuse, cloud evidence, and litigation strategy.

## Current Team Fit

The existing team should stay as-is and be implemented as lane contracts:

- Legal Intelligence
- Evidence Intelligence
- Cyber & AI Crime Intelligence
- Courtroom Intelligence
- Quality & Risk Control

The Digital Forensic Lab remains synthetic/internal-only in Phase 0. No real victim data, no offensive tooling, no production/legal conclusions.

## Current Model Recon Facts

### Prompt Injection / AI Attack Guard Candidates

`abedegno/prompt-injection-classifier-qwen3-0p6b`

- Last modified: 2026-05-05
- Base: `Qwen/Qwen3-0.6B`
- License: Apache-2.0
- Card claim: LoRA adapter emits `unsafe` or `safe` using class-token method.
- Fit: AI Misuse / Deepfake Evidence AI, Risk & Limitation AI, AI-attack intake filter.
- Caveat: low downloads; must benchmark with synthetic prompt-injection examples.

`llm-semantic-router/mmbert32k-jailbreak-detector-merged`

- Last modified: 2026-03-06
- Downloads observed: 2309
- License: Apache-2.0
- Tags: jailbreak-detection, security, text-classification
- Datasets listed: `lmsys/toxic-chat`, `OpenSafetyLab/Salad-Data`
- Fit: AI Misuse / Deepfake Evidence AI and Risk & Limitation AI for long-prompt safety classification.
- Caveat: non-Qwen; still needs local benchmark.

### Web Security / Secure Code / CWE Candidates

`lablab-ai-amd-developer-hackathon/Qwen-security-auditor-14b`

- Last modified: 2026-05-10
- Base: `Qwen/Qwen2.5-Coder-14B-Instruct`
- License: Apache-2.0
- Card claim: code vulnerability analysis, CWE classification, severity, mitigation.
- Fit: Cybercrime / Incident Response AI, Platform Terms / Tech Contract AI support for technical risk, Web Security lane.
- Caveat: 14B is heavier than Kei's small-specialist preference; not default always-on.

`lablab-ai-amd-developer-hackathon/Qwen-security-builder-14b`

- Last modified: 2026-05-10
- Base: `Qwen/Qwen2.5-Coder-14B-Instruct`
- License: Apache-2.0
- Card claim: secure patch generation.
- Fit: defensive remediation explanation only, not legal/forensic conclusion.
- Caveat: builder should not auto-patch production or evidence systems.

`navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct`

- Last modified: 2025-06-04
- Base: `unsloth/qwen2.5-1.5b-instruct-unsloth-bnb-4bit`
- Dataset: `Mackerel2/cybernative_code_vulnerability_cot`
- Fit: small code-vulnerability scout for toy/synthetic web security drills.
- Caveat: weak adoption evidence; benchmark only.

### Phishing / Cybercrime Candidates

`rudycaz/qwen3-4b-phishing-detection`

- Last modified: 2026-03-04
- Base: `Qwen/Qwen3-4B`
- Card claim: labels email content as `PHISHING` or `LEGIT`.
- Fit: Cybercrime / Incident Response AI, Digital Evidence AI for phishing evidence triage.
- Caveat: model card says repo may be adapter-only or merged; verify loading before use.

### Tiny General Cyber Candidate

`IAG-Group/Qwen3.5-0.8b-CyberSecurity`

- Last modified: 2026-05-01
- Base: `unsloth/Qwen3.5-0.8B`
- License: Apache-2.0
- Fit: possible tiny cyber note classifier.
- Caveat: README is thin and task/eval are unclear. Do not trust without benchmark.

### Deepfake / Synthetic Media Candidates

HF search found many recent `deepfake detection` uploads, including `rohitkhaire/deepfake-detection-colab` last modified 2026-05-11, but evidence quality was weak: no README in that repo and no model-card methodology in the checked artifact.

Recommendation: do not promote a random deepfake detector into the core team yet. Treat deepfake detection as a multi-signal lane: provenance, C2PA/watermark checks, platform logs, metadata, visual/audio forensic indicators, detector disagreement, and expert review.

## Recommended Worker Bench Without Changing Team

### Tier 0: Default Control / Reasoning

- Keep `qwen3.6:27b` local Ollama as the general reviewer/control model for synthetic Digital Forensic Lab work.
- Use it for summarization, issue mapping, limitation memos, and Thai explanation.
- Do not use it as sole forensic truth source.

### Tier 1: Safe Small Classifiers To Pilot First

1. AI Attack Guard
   - Candidate: `abedegno/prompt-injection-classifier-qwen3-0p6b`
   - Lane: AI Misuse / Deepfake Evidence AI + Risk & Limitation AI
   - Task: classify prompt injection / tool-instruction attacks in synthetic case materials.

2. Jailbreak / Prompt Safety Guard
   - Candidate: `llm-semantic-router/mmbert32k-jailbreak-detector-merged`
   - Lane: Quality & Risk Control
   - Task: detect jailbreak/prompt-injection risk before worker prompts reach tool-enabled agents.

3. Phishing Classifier
   - Candidate: `rudycaz/qwen3-4b-phishing-detection` or previous `Aleksandr505/phishing-text-classifier-qwen-2.5-0.5B`
   - Lane: Cybercrime / Incident Response AI
   - Task: label synthetic phishing/legit emails with confidence and evidence snippets.

4. Small Vulnerability Scout
   - Candidate: `navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct`
   - Lane: Cybercrime / Incident Response AI / Web Security sublane
   - Task: toy vulnerable snippets only; generate issue candidates, not final findings.

### Tier 2: Heavier Specialist Candidates, Not Always-On

1. Web Security / CWE Auditor
   - Candidate: `lablab-ai-amd-developer-hackathon/Qwen-security-auditor-14b`
   - Use only when the case involves source code/web app vulnerabilities and hardware budget allows.
   - Output must be converted into evidence-linked CWE candidate memo and reviewed by human/coder.

2. Secure Patch Explainer
   - Candidate: `lablab-ai-amd-developer-hackathon/Qwen-security-builder-14b`
   - Use only for defensive patch suggestions in sandbox repos.
   - Never auto-apply patches to production or evidence material.

### Tier 3: Exclude From Core / Sandbox Only

- Offensive pentest models.
- Abliterated/uncensored cyber models.
- Models tagged red-team/offensive-security unless a bounded defensive research task is explicitly approved.
- Random deepfake detectors with no README/eval/provenance.

## Lane Mapping To Kei's Fixed Team

- Legal Research AI: legal RAG + citation verifier, not security models.
- Litigation Strategy AI: qwen3.6 reviewer + evidence-to-issue schemas.
- Digital Evidence AI: hash/metadata/timeline validators, no generative-only truth.
- Digital Forensic AI: artifact parsers + qwen3.6 explanation + human examiner gate.
- Cybercrime / Incident Response AI: phishing classifier, IOC enrichment, incident timeline, web security scout.
- Cloud & SaaS Evidence AI: provider log parsers, retention-gap detector, permission/session timeline.
- AI Misuse / Deepfake Evidence AI: prompt-injection guard, media provenance checklist, detector-result skepticism.
- OSINT / Attribution AI: read-only public-source lead generator with confidence labels.
- Privacy / Data Protection AI: PDPA/APPI/GDPR issue classifier and notification checklist.
- Platform Terms / Tech Contract AI: ToS/AUP/DPA/SLA clause extractor.
- Expert Report AI: report-draft generator with Fact / Inference / Opinion / Unknown separation.
- Cross-Examination AI: question generator targeting methodology, chain-of-custody, detector limits, attribution gaps.
- Evidence Timeline AI: normalized event timeline with timezone/clock-drift warnings.
- Risk & Limitation AI: hallucination/citation/evidence-support/overclaim gate.

## Minimum Benchmark Before Any Adoption

Use synthetic/internal-only data:

- 20 prompt-injection / benign prompt examples
- 20 phishing / legitimate emails
- 15 toy web vulnerability snippets covering OWASP-style classes
- 10 synthetic cloud/SaaS log mini-cases
- 10 synthetic deepfake/media provenance scenarios
- 10 forensic timeline contradiction cases

Required output metrics:

- exact label accuracy
- false positive / false negative notes
- citation/evidence linking correctness
- abstention when evidence is insufficient
- no offensive leakage
- parseable JSON/Markdown schema
- latency and RAM/VRAM

## Recommendation

Do not change Kei's team structure. Improve the current Digital Forensic setup by adding a `specialist worker bench` behind the same lanes:

1. AI Attack Guard: `abedegno/prompt-injection-classifier-qwen3-0p6b`
2. Jailbreak Guard: `llm-semantic-router/mmbert32k-jailbreak-detector-merged`
3. Phishing: `rudycaz/qwen3-4b-phishing-detection` plus the prior 0.5B phishing baseline
4. Web Security / CWE: start with `navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct`; consider `Qwen-security-auditor-14b` only as heavier optional benchmark
5. Deepfake / AI media: no model promotion yet; use provenance-first workflow and human expert review
6. Keep `qwen3.6:27b` as the local control/reviewer model, not as a replacement for evidence tools or human experts

## Source URLs Checked

- https://huggingface.co/abedegno/prompt-injection-classifier-qwen3-0p6b
- https://huggingface.co/llm-semantic-router/mmbert32k-jailbreak-detector-merged
- https://huggingface.co/lablab-ai-amd-developer-hackathon/Qwen-security-auditor-14b
- https://huggingface.co/lablab-ai-amd-developer-hackathon/Qwen-security-builder-14b
- https://huggingface.co/IAG-Group/Qwen3.5-0.8b-CyberSecurity
- https://huggingface.co/rudycaz/qwen3-4b-phishing-detection
- https://huggingface.co/navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct
- Local: `/Users/kei/kei-jarvis/knowledge/ai-era-legal-advocacy-company-blueprint.md`
