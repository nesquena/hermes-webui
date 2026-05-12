# Source Trail — Forensic/Security Agent Skills and Local Models

Checked: 2026-05-11 JST
Owner: Kei + Yuto
Status: scout source trail; do not treat any model as trusted forensic authority
Related: [[source-ai-legal-forensic-learning-scouts]], [[ai-legal-forensic-ai-learning-path]], [[yuto-team-lanes-reuse-playbook]]

## 1. Question

Can Yuto create dedicated Forensic and Security Analyst teams now, and are there existing skills or local/specialized models trained for cybersecurity/forensic work?

## 2. Local Skill / Hermes Skill Hub Findings

Local Yuto already has a `forensic-reviewer` lane:

- Path: `/Users/kei/kei-jarvis/knowledge/yuto-team-lanes/forensic-reviewer.yaml`
- Purpose: review evidence-handling posture for provenance, hash, timestamp, source trail, chain of custody, and contamination risk.
- Guardrail: does not prove authenticity or replace a forensic expert.

Hermes Skills Hub search results:

- `hermes skills search forensic` found `official/security/oss-forensics`.
- `oss-forensics` description: supply chain investigation, evidence recovery, forensic analysis for GitHub repositories; covers deleted commit recovery, force-push detection, IOC extraction, multi-source evidence collection, hypothesis formation/validation, structured forensic reporting.
- Preview showed strong anti-hallucination guardrails: evidence IDs, source-lane separation, fact vs hypothesis, no evidence fabrication, SHA/URL double-verification, suspicious-code static analysis, secret redaction.

Other searches:

- `hermes skills search cybersecurity` found community entries including Kubernetes/security and cybersecurity assistants, but mixed trust and some unsafe/unconstrained entries.
- `hermes skills search security analyst` found no direct match.
- `hermes skills search dfir` found no direct match.

Decision:

- Do not install community cybersecurity skills blindly.
- Borrow the `oss-forensics` evidence-ID/reporting pattern, or install/inspect it only if doing GitHub supply-chain forensic work.
- Build Kei/Yuto-specific Security Analyst and DFIR lanes as local lane contracts first.

## 3. Current Local Model State

Live local Ollama check:

- `qwen3.6:27b` installed and loaded, 17 GB model, 22 GB loaded, 100% GPU, context 32768, keepalive Forever.
- `bge-m3:latest` installed and loaded, embedding model.

Implication:

- Current local generative worker is general Qwen, not specifically cybersecurity/DFIR fine-tuned.
- Use it as read-only reviewer/extractor only until benchmarked.

## 4. Hugging Face Cybersecurity / Forensic Model Findings

Representative Hugging Face API searches were run for:

- `cybersecurity llm`
- `security analyst llm`
- `dfir`
- `digital forensics`
- `incident response llm`
- `SOC analyst`
- `SecBERT`
- `CyberBERT`
- `forensic llm`

Notable models found:

### 4.1 Trendyol Cybersecurity LLM Qwen3 32B GGUF

Model:
- `Trendyol/Trendyol-Cybersecurity-LLM-Qwen3-32B-Q8_0-GGUF`

HF metadata observed:
- base model: `Qwen/Qwen3-32B`
- tags: cybersecurity, GGUF, English/Turkish
- license: Apache-2.0
- downloads at check: 470
- likes at check: 65

Fit:
- Best-looking candidate for a local cybersecurity reasoning worker near Kei's Qwen lane.
- Larger than current 27B; likely feasible on 128 GB RAM / M4 Max only if tested carefully, but may be heavier than current Qwen3.6 27B.

Caution:
- Cybersecurity tuning may include offensive/pentest data.
- Must benchmark on defensive SOC/DFIR/evidence tasks before use.

### 4.2 Trendyol Cybersecurity LLM v2 70B GGUF

Model:
- `Trendyol/Trendyol-Cybersecurity-LLM-v2-70B-Q4_K_M`

HF metadata observed:
- base model: `meta-llama/Llama-3.3-70B-Instruct`
- datasets include `Trendyol/Trendyol-Cybersecurity-Instruction-Tuning-Dataset`, `AlicanKiraz0/Cybersecurity-Dataset-Fenrir-v2.0`, `AlicanKiraz0/Cybersecurity-Dataset-Heimdall-v1.1`
- license: Apache-2.0
- downloads at check: 431
- likes at check: 46

Fit:
- Interesting for cybersecurity knowledge, but likely too heavy for default local workflow.

Caution:
- 70B local inference cost/latency high.
- Do not make it standing worker.

### 4.3 Seneca Cybersecurity LLM GGUF

Model:
- `AlicanKiraz0/Seneca-Cybersecurity-LLM-Q4_K_M-GGUF`

HF metadata observed:
- base model: `AlicanKiraz0/SenecaLLM-x-Llama3.1-8B`
- tags: cybersecurity, security, cyber, pentest
- license: MIT
- downloads at check: 552
- likes at check: 40

Fit:
- Lightweight local candidate.

Caution:
- Pentest/offensive orientation; restrict to read-only defensive review.
- Smaller model may hallucinate or overfit security jargon.

### 4.4 BaronLLM Offensive Security GGUF

Model:
- `AlicanKiraz0/Cybersecurity-BaronLLM_Offensive_Security_LLM_Q6_K_GGUF`

HF metadata observed:
- base model includes `meta-llama/Llama-3.1-8B-Instruct`
- tags include offensive security
- license: MIT
- downloads at check: 887
- likes at check: 230

Fit:
- Not recommended for Kei/Yuto default system due to offensive-security orientation.

Caution:
- Use only if a tightly scoped defensive lab needs attacker-mindset review; otherwise avoid.

### 4.5 SecBERT

Model:
- `jackaduma/SecBERT`

HF metadata observed:
- pipeline: fill-mask
- datasets: APTnotes, Stucco-Data, CASIE
- license: Apache-2.0
- downloads at check: 11901
- likes at check: 61

Fit:
- Not a chat/generative worker.
- Useful as a specialized NLP encoder/classifier base for threat intelligence, attack entity recognition, or security text classification.

Caution:
- Not suitable as Yuto/Chamin-style conversational agent.

### 4.6 SecBERT CWE classifier

Model:
- `Sana9/secbert-cwe-flat`

HF metadata observed:
- base model: `jackaduma/SecBERT`
- pipeline: text-classification
- tags: cybersecurity, vulnerability, CWE
- license: MIT

Fit:
- Possible classifier for vulnerability/CWE text, not DFIR assistant.

### 4.7 Digital Forensics Text2SQLite

Model:
- `pawlaszc/DigitalForensicsText2SQLite`

HF metadata observed:
- base model: `unsloth/Llama-3.2-3B-Instruct`
- dataset: `pawlaszc/mobile-forensics-sql`
- tags: forensics, text-to-sql
- license: Apache-2.0
- downloads at check: 254

Fit:
- Interesting niche model for mobile forensic text-to-SQL workflows.

Caution:
- Too narrow for general forensic reasoning.
- Could be useful later when working with structured mobile forensic databases, but not first team worker.

## 5. Recommendation

Create local lane contracts now, but do not make specialized models standing workers yet.

Recommended lanes:

1. `security-analyst`
   - reads logs/alerts/timelines;
   - classifies incident type;
   - identifies missing evidence;
   - no exploit/payload generation;
   - no remediation actions without approval.

2. `dfir-evidence-reviewer`
   - checks preservation, hash, timestamp, source trail, chain of custody, original vs working copy;
   - flags contamination risk;
   - requires human forensic expert review.

3. `ai-evidence-reliability-reviewer`
   - checks AI outputs for unsupported claims, hallucination, missing citations, source-span support, fact/inference/unknown separation;
   - uses NIST AI RMF/RAGAS/legal-hallucination patterns.

4. Keep existing `forensic-reviewer` but either split or extend it after prospective usage.

Model recommendation:

- Keep `qwen3.6:27b` as the general local reviewer for now.
- Benchmark `Trendyol/Trendyol-Cybersecurity-LLM-Qwen3-32B-Q8_0-GGUF` as first specialized local cyber candidate, if Kei approves download/testing.
- Do not use offensive-tuned models by default.
- Treat all model output as reviewer opinion, not forensic/legal proof.

## 6. Benchmark Before Adoption

Before any specialized cyber/forensic model becomes a worker, test it on synthetic tasks:

- SOC log triage: identify event type and missing evidence;
- evidence preservation checklist: find chain-of-custody gaps;
- AI summary review: detect unsupported claims;
- Japan product-language review: avoid legal-advice/forensic-proof claims;
- refusal/safety test: ensure no exploit payloads, credential theft, evasion, or hack-back guidance.

Pass criteria:

- cites source lines/evidence refs;
- separates fact/inference/unknown;
- flags human review need;
- refuses offensive instructions;
- does not claim authenticity/legal outcome;
- produces lower rework than general Qwen.

## 7. Decision

Yes, Kei/Yuto can create a Forensic/Security Analyst team now.

But the team should start as:

```text
Yuto control
-> Security Analyst lane
-> DFIR Evidence Reviewer lane
-> AI Evidence Reliability Reviewer lane
-> QA Critic
-> human lawyer/forensic expert gate
```

Not as:

```text
autonomous forensic expert
or offensive cyber agent
or legal decision-maker
```

Next action:

- Add lane manifests for `security-analyst` and `ai-evidence-reliability-reviewer`.
- Consider whether to split current `forensic-reviewer` into `dfir-evidence-reviewer` later.
- Run 3 synthetic receipt-based tasks before downloading or promoting specialized cybersecurity models.
