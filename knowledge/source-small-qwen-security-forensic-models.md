# Small Qwen Security / Forensic Model Recon — 2026-05-12

Status: source trail and routing recommendation. No model downloaded or installed.

## Question

Find small Qwen-family models trained for cybersecurity, forensics, phishing, vulnerability analysis, or adjacent defensive/security work, and decide how they should be used for Yuto's Digital Forensic Lab.

## Evidence checked

Source method:

- Hugging Face model API searches for: `qwen cybersecurity`, `qwen forensic`, `qwen phishing`, `qwen malware`, `qwen vulnerability`, `qwen3 4b cybersecurity`, `qwen2.5 3b cybersecurity`.
- Hugging Face model API detail pages for candidate models.
- Hugging Face README raw files for candidate cards.
- Local runtime check: `ollama list` and current Digital Forensic worker backend.

Local current model state:

- Installed locally in Ollama: `qwen3.6:27b`, `bge-m3:latest`.
- Digital Forensic continuous worker currently uses `ollama run qwen3.6:27b` as the local reviewer backend.

## Candidate models

### 1. `Aleksandr505/phishing-text-classifier-qwen-2.5-0.5B`

Source facts:

- Base: `Qwen/Qwen2.5-0.5B`.
- Task: `text-classification`.
- Model card says it is fine-tuned for phishing text classification.
- Reported metric: Accuracy `0.9870` on the card, but dataset is listed as unknown in the auto-generated card.
- License: Apache-2.0.
- Downloads observed: 10.

Fit:

- Good small candidate for a narrow phishing triage lane.
- Not a forensic reasoning model.
- Needs local benchmark before use because the training/eval data are weakly documented.

Recommended use:

- `phishing_labeler_candidate`: classify synthetic phishing/not-phishing emails or URLs generated inside the lab.
- Use as a signal only, not authority.

### 2. `lopezfelipe/Qwen2.5-1.5B-phishing`

Source facts:

- Tags show Qwen2, MIT license.
- Downloads observed: 287.
- README/card has minimal detail.

Fit:

- Attractive size and higher downloads, but poor documentation.
- Better as a benchmark candidate than immediate worker.

Recommended use:

- Compare against the 0.5B classifier on synthetic phishing set before adopting.

### 3. `rudycaz/qwen3-4b-phishing-detection`

Source facts:

- Base: `Qwen/Qwen3-4B`.
- Phishing/email-security/cybersecurity tags.
- Card says intended output is exactly `PHISHING` or `LEGIT`.
- Dataset: Naser Abdullah Alam “Phishing Email Dataset” on Kaggle.
- Downloads observed: 83.

Fit:

- Stronger phishing specialist than 0.5B/1.5B candidates, but 4B and adapter/merged loading details need verification.

Recommended use:

- Good candidate for phishing email classification if a small specialist is useful.
- Use only on synthetic or explicitly provided non-sensitive messages.

### 4. `navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct`

Source facts:

- Base: `unsloth/qwen2.5-1.5b-instruct-unsloth-bnb-4bit` / Qwen2.5 1.5B Instruct.
- Dataset: `Mackerel2/cybernative_code_vulnerability_cot`.
- Card says fine-tuned for detecting code vulnerabilities with Chain-of-Thought.
- License: MIT.
- Downloads observed: 0, likes 2.

Fit:

- Size is right, but adoption evidence is weak.
- Good as a small code vulnerability analyst candidate after local benchmark.

Recommended use:

- `code_vuln_scout_candidate` for toy vulnerable code snippets only.
- Avoid production code decisions without Yuto/Codex verification.

### 5. `Mohamedabul/Qwen2.5-3B-CyberSecurity-Instruct`

Source facts:

- Base: `unsloth/qwen2.5-3b-instruct-unsloth-bnb-4bit`.
- Tags: cybersecurity, vulnerability-analysis, exploit-code.
- Datasets listed: NVD/CVE, ExploitDB, MITRE/CWE.
- Card claims ~187,700 instruction samples and 45,000+ real-world exploits from Exploit-DB.
- License: Apache-2.0.
- Downloads observed: 136.

Fit:

- Potentially useful as a small general cybersecurity analyst, but dual-use/offensive risk is high due to exploit-code training.

Recommended use:

- Do not put into always-on Digital Forensic worker by default.
- If tested, constrain to defensive summarization of CVE/CWE and mitigation, no exploit generation, no external targets.

### 6. `DexopT/Qwen3-4B-Cybersecurity`

Source facts:

- Base: `unsloth/Qwen3-4B-Instruct-2507`.
- Card claims Qwen3-4B fine-tuned on 1.28M cybersecurity samples.
- Tags include cybersecurity, penetration-testing, offensive-security, red-team.
- Dataset: `DexopT/cyber_heretic` and tokenized variant.
- License: Apache-2.0.
- Downloads observed: 5 for non-Heretic safetensors; related Heretic GGUF has higher downloads but is explicitly uncensored/offensive.

Fit:

- Technically relevant, but default use is risky for Yuto's Phase 0 Digital Forensic Lab because of offensive/red-team framing.

Recommended use:

- Avoid for always-on worker.
- If ever used, sandbox only with strict no-offense prompt and no tools/network, after reviewing dataset/card more deeply.

### 7. `TheTharindu/c_vulnerability_check_and_explain_qwen_gguf`

Source facts:

- GGUF model.
- Base: `unsloth/qwen3-4b-base-unsloth-bnb-4bit`.
- License: Apache-2.0.
- Downloads observed: 53.
- Card is minimal and mostly auto-generated.

Fit:

- Could be easy to run via llama.cpp/Ollama import, but documentation/eval is weak.

Recommended use:

- Low priority unless Kei specifically wants C vulnerability toy-code drills.

### 8. `IAG-Group/Qwen3.5-0.8b-CyberSecurity`

Source facts:

- Base: `unsloth/Qwen3.5-0.8B`.
- License: Apache-2.0.
- README says uploaded fine-tuned model, but provides little task/eval detail.
- Downloads observed: 96.

Fit:

- Interesting tiny cybersecurity model, but unclear quality and task fit.

Recommended use:

- Research/benchmark candidate only, not default worker.

## Non-Qwen but useful small/security models

These should not replace Qwen worker policy, but are better task tools for specific sub-lanes:

### `cisco-ai/SecureBERT2.0-biencoder`

- Sentence-transformers model for cybersecurity semantic search / retrieval.
- Downloads observed: 77,030.
- License: Apache-2.0.
- Good for threat report/advisory retrieval or clustering.

### `cisco-ai/SecureBERT2.0-NER`

- Token classification for cybersecurity NER.
- Extracts entities such as indicators, malware, organizations, systems, vulnerabilities.
- License: Apache-2.0.
- Good for source-extract enrichment and KG tagging.

### `cybersectony/phishing-email-detection-distilbert_v2.4.1`

- DistilBERT text classifier for phishing email/URL labels.
- Downloads observed: 308,399.
- License: Apache-2.0.
- Better-documented/high-signal phishing classifier than most Qwen phishing candidates.

### `pawlaszc/DigitalForensicsText2SQLite`

- Llama 3.2 3B fine-tuned for generating SQLite queries over mobile forensic databases.
- Model card says training dataset has 800 examples and 191 forensic artifact categories.
- Integrated conceptually with FQLite forensic workflow.
- License: Apache-2.0.
- Very relevant to mobile forensic DB query training, but it is Llama, not Qwen.

## Recommendation for Yuto / Digital Forensic Lab

Do not replace `qwen3.6:27b` yet. It is already installed and active in Ollama, and it works as a general local reviewer.

Add small models only as narrow specialist lanes after a benchmark:

1. Phishing lane:
   - Primary Qwen candidate: `Aleksandr505/phishing-text-classifier-qwen-2.5-0.5B`.
   - Alternative Qwen: `rudycaz/qwen3-4b-phishing-detection`.
   - Best non-Qwen baseline: `cybersectony/phishing-email-detection-distilbert_v2.4.1`.

2. Code vulnerability toy-drill lane:
   - Qwen candidate: `navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct`.
   - C-specific fallback: `TheTharindu/c_vulnerability_check_and_explain_qwen_gguf`.

3. General cybersecurity mini-analyst:
   - Candidate: `IAG-Group/Qwen3.5-0.8b-CyberSecurity` for tiny experiments.
   - Candidate with more capability but higher risk: `Mohamedabul/Qwen2.5-3B-CyberSecurity-Instruct`.

4. Retrieval/NER sidecar:
   - Use SecureBERT2.0 biencoder/NER if Yuto needs source retrieval/entity extraction. These are not generative and are safer for always-on enrichment.

Avoid as always-on defaults:

- Heretic/abliterated/offensive/red-team models such as `sillykiwi/Qwen3-4B-Cybersecurity-Heretic-16bit-Q4_K_M-GGUF` and WhiteRabbitNeo variants. They may be capable, but safety posture conflicts with Phase 0 forensic lab boundaries.

## Benchmark before adoption

Create a local benchmark with only synthetic data:

- 20 synthetic phishing/legit email snippets.
- 10 toy vulnerable code snippets with expected CWE category.
- 10 synthetic forensic log/artifact classification tasks.
- Metrics: exact label accuracy, abstention when uncertain, no offensive instruction leakage, latency, RAM/VRAM, and output parseability.

Promotion gate:

- Must beat or complement `qwen3.6:27b` on its narrow task.
- Must have no unsafe output in the synthetic benchmark.
- Must be runnable locally without external API.
- Must not be given network/tools/real data.

## Current conclusion

Best immediate path:

- Keep `qwen3.6:27b` as the continuous Digital Forensic Lab reviewer.
- Research/download later only after approval: benchmark `Aleksandr505/phishing-text-classifier-qwen-2.5-0.5B`, `navodPeiris/Vulnerability-Analyst-Qwen2.5-1.5B-Instruct`, and maybe `IAG-Group/Qwen3.5-0.8b-CyberSecurity`.
- Consider non-Qwen SecureBERT2.0 models for retrieval/NER sidecar because they are safer and better documented than many small Qwen security fine-tunes.
