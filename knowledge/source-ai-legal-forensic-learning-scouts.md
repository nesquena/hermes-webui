# Source Trail — AI Legal-Forensic Learning Scouts

Checked: 2026-05-11 JST
Owner: Kei + Yuto
Status: scout synthesis; use as routing map, not final authority
Related: [[ai-legal-forensic-ai-learning-path]], [[ai-legal-kcl-cambridge-research-roadmap]], [[ai-legal-japan-research-target]], [[yuto-team-lanes-reuse-playbook]]

## 1. Conclusion

The next knowledge-gathering lane should be read-only scouting across three clusters:

1. eDiscovery and legal-support workflow;
2. DFIR / digital evidence standards and practice;
3. AI evidence reliability, provenance, RAG, hallucination, and human review.

Decision:

> Let Chamin/Codex or other workers scout knowledge, but keep them read-only until Yuto verifies sources and routes durable findings into the Markdown KG.

The scout output supports Kei's path toward:

> Japan-first AI harm evidence layer / AI時代の証拠保全・相談準備インフラ.

## 2. Scout Method

Three read-only scout missions were delegated:

- eDiscovery / legal support;
- DFIR / digital forensics;
- AI evidence reliability / provenance.

Guardrails used:

- no signups;
- no external contact;
- no installs;
- no file edits by workers;
- primary/official sources preferred;
- output as source-backed brief only.

Verification note:

- Subagent summaries are not treated as proof by themselves.
- Yuto spot-checked representative URLs after the scouts.
- Some URLs returned 403 to Python fetching but had been inspectable in browser earlier; treat those as browser-observed, not programmatically fetched.

## 3. Representative Source Verification

Programmatic spot-checks succeeded for:

- NIST SP 800-86 — Guide to Integrating Forensic Techniques into Incident Response: https://csrc.nist.gov/publications/detail/sp/800-86/final
- NIST SP 800-61 Rev.2 — Computer Security Incident Handling Guide: https://csrc.nist.gov/publications/detail/sp/800-61/rev-2/final
- SWGDE Documents: https://www.swgde.org/documents
- ISO/IEC 27037 overview: https://www.iso.org/standard/44381.html
- Magnet Forensics Training: https://www.magnetforensics.com/training/
- NIST AI Risk Management Framework: https://www.nist.gov/itl/ai-risk-management-framework
- RAG paper: https://arxiv.org/abs/2005.11401
- RAGAS paper: https://arxiv.org/abs/2309.15217
- Legal hallucination paper: https://arxiv.org/abs/2401.01301
- PPC/APPI legal materials: https://www.ppc.go.jp/personalinfo/legal/
- Japan MOJ Civil Affairs Bureau: https://www.moj.go.jp/MINJI/

Browser-observed earlier in the session:

- EDRM current model: https://edrm.net/edrm-model/current/
- ACEDS CEDS certification page: https://aceds.org/ceds-certification/
- Udacity Cybersecurity school: https://www.udacity.com/school/cybersecurity

## 4. eDiscovery / Legal Support Cluster

Useful sources:

- EDRM: https://edrm.net/
- EDRM current model: https://edrm.net/edrm-model/current/
- ACEDS / CEDS: https://aceds.org/ceds-certification/
- The Sedona Conference publications: https://thesedonaconference.org/publications
- US Federal Rules of Civil Procedure: https://www.uscourts.gov/rules-policies/current-rules-practice-procedure/federal-rules-civil-procedure
- Japan MOJ Civil Affairs Bureau: https://www.moj.go.jp/MINJI/
- Courts in Japan: https://www.courts.go.jp/
- PPC/APPI: https://www.ppc.go.jp/personalinfo/legal/
- Nichibenren: https://www.nichibenren.or.jp/

What to learn:

- eDiscovery lifecycle;
- legal hold;
- preservation;
- collection;
- review;
- production;
- privilege/redaction;
- litigation support roles;
- lawyer-ready evidence packets;
- Japan non-lawyer practice boundary.

Product implications:

- Do not build `AI lawyer`.
- Build `lawyer-supervised evidence preparation`.
- Output should help counsel review, not decide legal strategy.
- Japan does not have US-style broad discovery; localize language to evidence organization, consultation preparation, and preservation guidance.

Recommended next reading:

1. EDRM current model;
2. ACEDS/CEDS overview;
3. Sedona legal hold/proportionality publications;
4. Japan MOJ/courts procedure pages;
5. PPC/APPI guidance.

## 5. DFIR / Digital Forensics Cluster

Useful sources:

- NIST SP 800-86: https://csrc.nist.gov/publications/detail/sp/800-86/final
- NIST SP 800-61 Rev.2: https://csrc.nist.gov/publications/detail/sp/800-61/rev-2/final
- NIST SP 800-101 Rev.1 mobile forensics: https://csrc.nist.gov/publications/detail/sp/800-101/rev-1/final
- SWGDE documents: https://www.swgde.org/documents
- ISO/IEC 27037: https://www.iso.org/standard/44381.html
- ISO/IEC 27041: https://www.iso.org/standard/44404.html
- ISO/IEC 27042: https://www.iso.org/standard/44405.html
- ISO/IEC 27043: https://www.iso.org/standard/44407.html
- RFC 3227: https://www.rfc-editor.org/rfc/rfc3227
- Magnet Forensics Training: https://www.magnetforensics.com/training/
- SANS DFIR courses: https://www.sans.org/cyber-security-courses?focus-area=digital-forensics
- IACIS: https://www.iacis.com/
- CISA incident response: https://www.cisa.gov/topics/cyber-threats-and-advisories/incident-response

What to learn:

- chain of custody;
- original vs forensic image vs working copy;
- hashing;
- metadata;
- timestamp/timezone handling;
- mobile/cloud/social evidence limitations;
- forensic report structure;
- tool validation;
- contamination risk;
- incident response lifecycle.

Product implications:

- AI must never mutate original evidence.
- AI should process verified working copies only.
- Every derived artifact needs source reference, timestamp, and audit log.
- Model/prompt/output metadata should be treated as part of the analysis trail.
- Human forensic review is required before forensic/legal claims.

Recommended next reading:

1. NIST SP 800-86;
2. SWGDE collection/preservation/reporting documents;
3. NIST SP 800-61;
4. ISO/IEC 27037 overview;
5. Magnet or SANS course outlines for practical skill map.

## 6. AI Evidence Reliability / Provenance Cluster

Useful sources:

- C2PA specifications: https://c2pa.org/specifications/
- C2PA specification 2.1: https://c2pa.org/specifications/specifications/2.1/specs/C2PA_Specification.html
- C2PA trust model: https://c2pa.org/specifications/specifications/2.1/specs/C2PA_Trust_Model.html
- Content Credentials: https://contentcredentials.org/
- Content Authenticity Initiative: https://contentauthenticity.org/
- NIST AI RMF: https://www.nist.gov/itl/ai-risk-management-framework
- NIST Generative AI Profile: https://www.nist.gov/itl/ai-risk-management-framework/generative-artificial-intelligence
- RAG paper: https://arxiv.org/abs/2005.11401
- Lost in the Middle: https://arxiv.org/abs/2307.03172
- RAGAS: https://arxiv.org/abs/2309.15217
- Legal hallucinations paper: https://arxiv.org/abs/2401.01301
- Verifiability in generative search: https://arxiv.org/abs/2304.09848
- AI-generated text detection reliability: https://arxiv.org/abs/2303.11156
- Watermark reliability: https://arxiv.org/abs/2306.04634
- Japan AI Guidelines for Business: https://www.meti.go.jp/english/press/2024/0419_001.html

What to learn:

- C2PA/content provenance;
- trust model limitations;
- RAG and source-grounded summarization;
- citation faithfulness;
- legal hallucination;
- evaluation datasets;
- source-span support;
- abstention when evidence is insufficient;
- human review and contestability;
- watermark/detector limits.

Product implications:

- C2PA/content credentials indicate provenance claims, not truth.
- Watermarking/detection cannot be the main proof layer.
- RAG reduces hallucination but does not remove it.
- Every AI-generated claim should link to source evidence or be labeled inference/unknown.
- UI must let lawyer/forensic reviewer contest, correct, and approve outputs.

Recommended next reading:

1. NIST AI RMF;
2. C2PA technical specification and trust model;
3. RAG paper;
4. RAGAS;
5. Legal hallucinations paper;
6. Lost in the Middle;
7. watermark/detection reliability papers.

## 7. Recommended Scout Missions Next

Use these as read-only tasks for Chamin/Codex or Yuto lanes.

### Mission A — Japan evidence workflow localization

Goal:
- Translate eDiscovery/DFIR concepts into Japan-safe legal-prep/evidence-support language.

Sources:
- MOJ;
- Courts in Japan;
- PPC/APPI;
- Nichibenren;
- NISC/JPCERT/IPA for cyber response.

Output:
- Japan evidence workflow map;
- unsafe wording list;
- lawyer/forensic review gates.

### Mission B — Synthetic evidence dataset design

Goal:
- Define 5 synthetic AI-harm cases for prototype/evaluation.

Sources:
- NIST/SWGDE evidence handling;
- C2PA/provenance;
- legal hallucination/eval papers;
- local roadmap docs.

Output:
- synthetic case schema;
- evidence artifact list;
- expected timeline/evidence-table answers;
- hallucination traps.

### Mission C — Course shortlist with cost/time/fit

Goal:
- Compare Udacity, ACEDS, Magnet, SANS, CyberDefenders, TryHackMe, Hugging Face, DeepLearning.AI by fit to Kei's 12-month path.

Output:
- ranked course table;
- start-now recommendation;
- budget-conscious and professional tracks.

## 8. Worker Prompt Template

Use this prompt for Chamin/Codex read-only scouting:

```text
Scout this topic for Kei/Yuto. Read primary or official sources first. Do not install tools, sign up, contact anyone, edit files, read secrets, or make legal/forensic conclusions. Return a concise source-backed brief with: topic, sources checked, key facts, relevance to Japan-first AI harm evidence layer, risks/unknowns, recommended next reading, and suggested KG update text only.
```

## 9. Open Questions

- Which Japan-specific evidence procedure sources should become the canonical references for civil, criminal, and consultation-prep contexts?
- Which DFIR standard should be the minimum internal design baseline: NIST/SWGDE/ISO or a hybrid checklist?
- Which AI evaluation framework should be used first for prototype v0.1: RAGAS-style metrics, claim-level source support, or a custom synthetic-case judge?
- How should AI assistance be disclosed in lawyer/forensic handoff packets?
- What data should be stored, redacted, or deleted under APPI-safe design?

## 10. Decision

Proceed with read-only scout missions. Do not automate external outreach, enroll in courses, or let workers edit core KG without Yuto verification.

Next best action:

> Run Mission C to produce a ranked course shortlist, then choose the first 30-day study block.
