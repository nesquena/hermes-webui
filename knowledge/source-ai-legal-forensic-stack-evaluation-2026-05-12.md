# AI Legal-Forensic Stack Evaluation 2026-05-12

Status: source-backed assessment of Kei's proposed model/worker stack
Checked: 2026-05-12 via Hugging Face API and GitHub API spot checks
Related: [[source-ai-legal-forensic-specialist-worker-bench-2026-05-12]] [[ai-era-legal-advocacy-company-blueprint]] [[digital-forensic-lab/README]]

## Verdict

The stack is directionally strong and fits the fixed AI Legal-Forensic Intelligence Team if treated as a tiered, evidence-first worker bench rather than a single autonomous legal/forensic oracle.

Recommended posture:

- Main reasoning: Qwen3.6-27B
- Text Evidence RAG: Qwen3 Embedding/Reranker; use 0.6B first, 4B for high-value corpora
- Legal RAG: Kanon 2 only after source/license/runtime verification; otherwise use Qwen3 multilingual retrieval plus legal citation verifier
- Cyber: SecureBERT and CyberSecQwen-4B look useful; security-slm and LogLM-derived workers are benchmark-only
- Privacy: OpenAI privacy-filter and OpenMed multilingual are strong candidates; GLiNER2-PII name was not found in HF search and needs exact source
- Visual/Deepfake: Qwen3-VL embedding/reranker for retrieval only; C2PA verifier for provenance; detector ensembles only as weak signals
- Japanese: LLM-jp-4-8B preferred over LLM-jp-3-7.2B based on recency/downloads; use as Japanese legal-language reviewer, not legal authority
- Custom workers are essential and should be implemented as deterministic/schema-first tools with AI explanation layers

## Evidence Highlights

HF API observed:

- `Qwen/Qwen3.6-27B`: Apache-2.0, modified 2026-04-24, high downloads.
- `Qwen/Qwen3-Embedding-0.6B`: Apache-2.0, high downloads.
- `Qwen/Qwen3-Embedding-4B`: Apache-2.0, high downloads.
- `Qwen/Qwen3-Reranker-0.6B`: Apache-2.0, high downloads.
- `Qwen/Qwen3-Reranker-4B`: Apache-2.0, high downloads.
- `athena129/CyberSecQwen-4B` and `lablab-ai-amd-developer-hackathon/CyberSecQwen-4B`: modified 2026-05-08, tags include cybersecurity/CTI/CWE/vulnerability-analysis.
- `Nguuma/security-slm-unsloth-1.5b`: modified 2026-05-07, tags include cybersecurity, qwen2/deepseek-r1/trl.
- `cisco-ai/SecureBERT2.0-biencoder`: downloads ~77k, cybersecurity retrieval.
- `cisco-ai/SecureBERT2.0-cross_encoder`: downloads ~52k, reranking.
- `cisco-ai/SecureBERT2.0-NER`: cyber NER.
- `openai/privacy-filter`: downloads ~190k, Apache-2.0, token-classification.
- `OpenMed/privacy-filter-multilingual`: modified 2026-05-03, multilingual PII/NER/redaction.
- `Qwen/Qwen3-VL-Embedding-2B` and `8B`: high downloads, multimodal embedding.
- `Qwen/Qwen3-VL-Reranker-2B` and `8B`: multimodal rerank.
- `llm-jp/llm-jp-4-8b-thinking` and `instruct`: modified 2026-04-24, Apache-2.0, ja/en.
- `llm-jp/llm-jp-3-7.2b-*`: older, lower downloads.

GitHub API observed:

- `contentauth/c2pa-rs`: active repo, updated 2026-05-12.
- `contentauth/c2patool`: Apache-2.0, updated 2026-05-09.

Not verified / needs exact source:

- Kanon 2 Embedder / Reranker: no HF API result for searched names.
- GLiNER2-PII: no HF API result for searched name.
- OpenAI Privacy Filter: HF repo exists; operational constraints and false positives still need benchmark.
- LogLM-derived worker: relevant search results exist but low adoption; must be synthetic-log benchmark only.

## Rating By Lane

- Qwen3.6-27B: Adopt as main control/reviewer.
- Kanon 2 Embedder/Reranker: Conditional; verify exact source first.
- Qwen3 Embedding/Reranker: Adopt for Evidence RAG; 0.6B default, 4B for high-stakes rerank/recall.
- CyberSecQwen-4B: Pilot for CTI/CWE/vulnerability triage.
- security-slm-unsloth-1.5b: Benchmark-only.
- SecureBERT: Adopt for cyber retrieval/NER/rerank, especially CTI and IOC text.
- LogLM-derived worker: Build/pilot as custom synthetic log classifier; do not rely on generic LLM alone.
- Privacy filters: Adopt as redaction candidate with Japanese/Thai/English tests.
- Qwen3-VL embedding/reranker: Adopt for visual evidence retrieval, not authenticity verdicts.
- C2PA verifier: Adopt as deterministic provenance tool.
- Deepfake/audio spoofing ensembles: Use as weak signal with limitation memo.
- LLM-jp-4-8B: Pilot as Japanese reviewer.
- LLM-jp-3-7.2B: Keep fallback only.
- Custom workers: Highest priority because they enforce evidence discipline.

## Next Benchmark

Use synthetic/internal-only benchmark packs:

- Legal RAG: 30 Japanese/English/Thai legal snippets with exact citation targets.
- Evidence RAG: 50 artifacts/logs/screenshots with known provenance.
- Cyber: 25 CTI/CWE/phishing/log triage cases.
- Privacy: 50 multilingual PII spans including Japanese names, addresses, account IDs, medical/financial data.
- Visual: 20 image/video/audio provenance cases with known C2PA/no-C2PA and detector uncertainty.
- Custom worker tests: schema validity, evidence-link correctness, abstention, overclaim detection.
