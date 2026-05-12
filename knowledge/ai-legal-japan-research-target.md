# Japan Target — AI Legal Evidence Research

Checked: 2026-05-11 01:54 JST
Owner: Kei + Yuto
Status: working Japan-specific research target note
Anchors: [[ai-era-legal-advocacy-company-blueprint]], [[source-ai-legal-research-topic-selection]], [[ai-legal-kcl-cambridge-research-roadmap]]

## 1. Conclusion

Japan should be the primary target jurisdiction / empirical case study for Kei's AI legal research.

Refined research direction:

> Japan-focused trustworthy AI-assisted digital evidence infrastructure for AI-enabled harms.

Recommended thesis framing:

> Governing AI-Assisted Digital Evidence Workflows in Japan: Provenance, Reliability, Contestability, and Human Review for AI-Enabled Harms

Thai framing:

> วิจัยว่าญี่ปุ่นควรกำกับและออกแบบ workflow ที่ใช้ AI ช่วยเก็บและจัดหลักฐานดิจิทัลอย่างไร เพื่อช่วยผู้เสียหายจากภัย AI โดยไม่ละเมิด privacy, ไม่ทำลาย chain of custody, ไม่เป็น non-lawyer legal practice, และยังตรวจสอบ/โต้แย้งได้ในกระบวนการยุติธรรม

Why Japan makes the topic stronger:

- Japan has real digital-harm pressure: cybercrime, fraud, account takeover, online harassment, AI-generated media, platform/bank response gaps.
- Japan has a clear non-lawyer legal practice boundary under Attorney Act Article 72.
- Japan has strong personal-data constraints under APPI.
- Japan's civil/criminal evidence rules make reliability, authenticity, and source traceability central.
- Japan has an active policy interest in AI governance, cybersecurity, and digital trust.
- A Japan target differentiates Kei from generic UK/US legal AI research and gives a concrete field context for KCL/Cambridge.

## 2. Japan-First Research Question

Primary question:

> How should Japan govern AI-assisted digital evidence workflows for victims of AI-enabled digital harm so that evidence can be preserved and organized quickly while remaining reliable, contestable, privacy-preserving, and human-reviewed?

Short version:

> How can AI help Japanese victims preserve digital evidence without turning into an unsafe AI lawyer or unreliable forensic tool?

## 3. Japan-Specific Sub-Questions

### 3.1 Attorney Act / non-lawyer practice

- Where is the boundary between legal information, evidence organization, legal-prep support, and legal advice under Japan's Attorney Act?
- Can an AI evidence assistant safely provide checklist/intake/timeline support without handling legal matters as a business?
- What human lawyer review or referral model is needed?

### 3.2 APPI / privacy

- What personal data is necessary for digital evidence support?
- How should the system minimize sensitive data collection?
- How should consent, purpose specification, third-party data, and security controls be designed?
- How should research use synthetic data before real victim data?

### 3.3 Evidence law and forensic reliability

- What makes a digital artifact credible in Japanese civil/criminal procedure?
- How should AI-generated timelines be distinguished from source evidence?
- What authenticity/provenance/chain-of-custody records are needed?
- How can opposing parties contest AI-organized evidence?

### 3.4 Cybercrime and digital harm response

- How do victims in Japan currently report cybercrime, fraud, account takeover, deepfake abuse, or online harassment?
- Where are the response gaps between platforms, banks, police, lawyers, and forensic experts?
- What first-response evidence pack would reduce loss before legal consultation?

### 3.5 Policy

- What minimum standards should Japan require for AI-assisted evidence tools?
- Should platforms/banks/police/legal aid providers have standard evidence-preservation guidance for AI-enabled harms?
- What disclosure duties should apply when AI assists evidence organization?

## 4. Official Japan Sources Checked

### 4.1 Attorney Act — 弁護士法 Article 72

Source: e-Gov law API, law id `324AC1000000205`.

Key text checked:

> 弁護士又は弁護士法人でない者は、報酬を得る目的で...一般の法律事件に関して鑑定、代理、仲裁若しくは和解その他の法律事務を取り扱い...することを業とすることができない。

Research implication:

- Product/research must not be framed as `AI lawyer` or automated legal advice.
- Safe lane: legal information, evidence organization, checklist, timeline, lawyer-ready brief, referral, human lawyer review.
- Research should explicitly analyze the boundary between `evidence support` and `legal advice/legal affairs`.

### 4.2 APPI — 個人情報保護法

Source: e-Gov law API, law id `415AC0000000057`; PPC legal/guidelines page checked: https://www.ppc.go.jp/personalinfo/legal/

Articles checked:

- Article 1: purpose balances personal information usefulness with protection of individual rights/interests.
- Article 17: purpose of use must be specified as much as possible.
- Article 18: use beyond specified purpose generally requires consent, with exceptions.
- Article 20: improper acquisition prohibited; special care-required personal information generally needs prior consent unless exception applies.
- Article 23: business operators must take necessary and appropriate security control measures.

Research implication:

- Evidence tool must use privacy-by-design.
- The system should ask only for necessary evidence, state purpose clearly, avoid over-collection, and protect sensitive victim/third-party data.
- For research, use synthetic cases unless ethics/legal basis exists.

### 4.3 Electronic Signatures Act — 電子署名法 Article 3

Source: e-Gov law API, law id `412AC0000000102`.

Key text checked:

> 電磁的記録...本人による電子署名...が行われているときは、真正に成立したものと推定する。

Research implication:

- Japan has a legal presumption mechanism around authenticity of electronically signed electronic records.
- This does not solve all AI/digital evidence issues, but it supports the research focus on authenticity, provenance, and trust signals.

### 4.4 Civil Procedure Act — 民事訴訟法 Articles 228 and 247

Source: e-Gov law API, law id `408AC0000000109`.

Articles checked:

- Article 228: document authenticity must be proved; certain presumptions apply for official/private documents.
- Article 247: court evaluates facts based on oral argument and evidence examination under free evaluation of evidence.

Research implication:

- For civil cases, AI-organized evidence must help prove authenticity and context, not replace proof.
- The thesis should discuss how AI outputs should be treated as organization/summary aids, while source artifacts and authenticity records remain central.

### 4.5 Code of Criminal Procedure — 刑事訴訟法 Articles 317 and 321

Source: e-Gov law API, law id `323AC0000000131`.

Articles checked:

- Article 317: facts are recognized based on evidence.
- Article 321: written statements and certain documents become evidence only under specified conditions.

Research implication:

- Criminal context is stricter and should be a later or comparative chapter, not the first prototype target.
- Initial Master's scope should prefer civil/legal-prep/victim-support contexts, with criminal reporting support carefully bounded.

### 4.6 Unauthorized Access Act — 不正アクセス禁止法 Articles 3 and 4

Source: e-Gov law API, law id `411AC0000000128`.

Articles checked:

- Article 3: unauthorized access is prohibited.
- Article 4: acquisition of another person's identification code for unauthorized access is prohibited.

Research implication:

- Defensive forensic AI must avoid any hack-back, unauthorized access, credential collection, or evidence gathering by intrusion.
- Victim evidence support should rely on user-owned artifacts, lawful requests, platform/bank reports, and expert forensic channels.

### 4.7 Cybersecurity Basic Act — サイバーセキュリティ基本法 Articles 1 and 3

Source: e-Gov law API, law id `426AC1000000104`.

Articles checked:

- Article 1: cybersecurity policy purpose includes promoting measures and realizing a safe/secure society.
- Article 3: cybersecurity measures should involve multiple actors and must not unjustly infringe citizens' rights.

Research implication:

- Good policy framing: multi-actor response, resilience, rights protection.
- Fits Japan-specific institutional model: victims, platforms, banks, police, lawyers, forensic experts, regulators.

### 4.8 NPA cyber threat statistics page

Source checked: https://www.npa.go.jp/publications/statistics/cybersecurity/

Evidence checked:

- NPA publishes annual and half-year reports on `サイバー空間をめぐる脅威の情勢等`.
- The page lists reports and data files for Reiwa 7, Reiwa 6, Reiwa 5, etc.
- Latest listed on page during check included `令和７年におけるサイバー空間をめぐる脅威の情勢等について` and data files.

Research implication:

- Japan has an official cyber-threat evidence base usable for thesis background and policy problem definition.
- Next step: extract specific incident categories and victim-response gaps from NPA PDFs/XLSX.

## 5. What Changes From the Previous Roadmap

Previous roadmap was jurisdiction-neutral.

Japan target changes the project in five ways:

1. Attorney Act Article 72 becomes a central product/research boundary.
2. APPI privacy minimization becomes a design requirement, not a side note.
3. Evidence authenticity/provenance must map to Japanese civil/criminal procedure.
4. Product should start as `弁護士が確認する相談準備・証拠整理サポート`, not `AI弁護士`.
5. PhD contribution can become a Japan-grounded governance framework with comparative value for the UK/EU.

## 6. Recommended Master's Focus for KCL

Title:

> Governing AI-Assisted Digital Evidence Workflows in Japan: Human Review, Privacy, and Evidentiary Reliability in AI-Enabled Harms

Better if product/prototype included:

> Designing a Human-Reviewed AI Evidence Assistant for AI-Enabled Digital Harm in Japan

Master's objective:

- Build a law-and-technology framework for a Japan-safe AI evidence assistant.
- Evaluate a prototype or workflow with synthetic Japanese scenarios.
- Identify legal, privacy, forensic, and institutional safeguards.

Master's case scenarios:

1. Voice-clone bank transfer scam.
2. Deepfake image/video harassment.
3. Account takeover involving payment or contract evidence.
4. AI-generated defamation/fake screenshots on social media.

Master's evaluation:

- evidence completeness
- source traceability
- hallucination/unsupported inference
- APPI/data-minimization compliance checklist
- Article 72 boundary compliance checklist
- forensic chain-of-custody adequacy
- human reviewer usefulness

## 7. Recommended Cambridge PhD Focus

Title:

> Law for AI-Assisted Evidence: Governing Reliability, Contestability, and Human Accountability in Japan's Response to AI-Enabled Digital Harm

Core contribution:

> A Japan-grounded legal and socio-technical governance framework for AI-assisted digital evidence workflows, with comparative relevance to UK/EU AI governance and access-to-justice debates.

PhD chapters should add Japan-specific material:

1. AI-enabled digital harm and Japan's evidence gap.
2. Japanese legal boundaries: Attorney Act, APPI, evidence law, cybersecurity law.
3. Forensic reliability and chain-of-custody model for AI-assisted evidence.
4. Contestability and due process in AI-organized evidence.
5. Synthetic/controlled evaluation of an evidence workflow.
6. Institutional model for Japan: platforms, banks, police, lawyers, forensic experts, regulators.
7. Comparative discussion with UK/EU AI governance.
8. Policy recommendations for Japan.

## 8. Product/Company Implications in Japan

Consumer-facing Japanese positioning should avoid `AI弁護士`.

Safer product language:

- `弁護士が確認する相談準備サポート`
- `AI時代の証拠整理サポート`
- `デジタル被害の証拠保全ガイド`
- `被害状況と証拠を整理する相談準備ツール`
- `フォレンジック専門家と連携した証拠整理支援`

Avoid:

- `AI弁護士`
- `自動法律相談`
- `勝てる証拠を作る`
- `裁判で使えると保証`
- any claim that AI determines truth, authenticity, or legal outcome

## 9. Japan Leadership Strategy

Goal:

> Become one of Japan's leading voices and builders in AI-era digital evidence preservation, legal-prep support, and forensic-informed victim response.

The leadership niche is not `AI lawyer`.

The leadership niche is:

```text
Japan's AI harm evidence layer
```

or:

```text
AI時代の証拠保全・相談準備インフラ
```

### 9.1 Strategic wedge

Start with the narrowest high-trust wedge:

> AI-assisted evidence preservation and lawyer-ready consultation preparation for victims of AI-enabled digital harm in Japan.

This wedge is strong because it sits before lawyers, courts, police, banks, and platforms lose time or receive unusable evidence.

It should help users answer:

- What happened?
- What evidence exists?
- What should not be deleted or modified?
- What original files should be preserved?
- What metadata/context is missing?
- What should be shown to a lawyer, forensic expert, platform, bank, or police?
- What is fact, inference, unknown, or expert-only?

### 9.2 Leadership moat

Build moats that generic legal AI/chatbot competitors cannot easily copy:

1. Japan-specific evidence protocol
   - screenshot/URL/account ID/timestamp guidance
   - chat export guidance
   - platform/bank/police report inventory
   - original vs working-copy rules
   - hash/inventory/timeline templates

2. Article 72-safe doctrine
   - no `AI lawyer`
   - no automated legal advice
   - consultation preparation and evidence organization only
   - lawyer/forensic expert review path

3. APPI-safe intake design
   - purpose limitation
   - data minimization
   - sensitive-data caution
   - third-party privacy handling
   - synthetic data first for research

4. Forensic-reviewed credibility
   - forensic advisor review of evidence schema
   - chain-of-custody model
   - uncertainty language
   - tool-output risk taxonomy

5. Research-backed authority
   - KCL dissertation
   - Cambridge PhD proposal
   - white paper / policy memo
   - Japan-focused evidence standard proposal
   - public explainers in Japanese

6. Victim-centered UX
   - plain Japanese
   - calm emergency-first intake
   - no shame/blame language
   - clear escalation path
   - usable by non-technical people

### 9.3 Public authority plan

Produce public-facing materials before trying to scale product automation:

- `詐欺に遭った時の証拠保全チェックリスト`
- `スクショだけでは足りない理由`
- `ディープフェイク被害で最初に保存すべきもの`
- `AI時代の証拠整理とは何か`
- `弁護士に相談する前に整理すべきこと`
- `フォレンジック専門家とAIの役割分担`

These should be educational, not legal advice.

### 9.4 0-24 month leadership roadmap

#### 0-3 months

- Japan evidence checklist v0.1
- Article 72-safe product doctrine
- APPI-safe intake schema
- Japanese synthetic case pack v0.1
- one-page research statement
- forensic advisor outreach list
- Japanese landing/research page draft

#### 3-6 months

- prototype evidence intake + timeline + case pack
- forensic/lawyer review of output format
- 3-5 public explainers in Japanese
- white paper draft: `AI時代のデジタル証拠保全`
- benchmark/evaluation rubric for synthetic cases

#### 6-12 months

- limited human-reviewed pilot with synthetic or carefully controlled data
- partnership outreach to law firms, forensic experts, victim-support orgs, and cybercrime support actors
- Japan evidence-loss pattern report
- KCL dissertation framing locked
- policy memo draft for Japan

#### 12-24 months

- recognized Japan-first research/product voice on AI harm evidence
- Cambridge proposal grounded in Japan case study
- publishable working paper
- standards/playbook for lawyers, NGOs, platforms, and victim-support groups
- product becomes research-backed infrastructure, not unsupported chatbot

### 9.5 Metrics for leadership

Evidence of leadership should be measured by:

- number of Japan-specific protocols/checklists published
- expert review from forensic/legal practitioners
- citations or references in academic/policy contexts
- partnerships or pilot conversations
- quality of synthetic case benchmark
- public education reach
- clear compliance posture under Article 72 and APPI
- ability to produce lawyer/forensic-ready case packs from controlled cases

### 9.6 Strategic warning

Do not try to lead Japan by being the loudest AI legal brand.

Lead by becoming the most trusted evidence-first bridge between:

```text
victims -> evidence preservation -> lawyer/forensic expert -> platform/bank/police/court -> policy reform
```

## 10. Forensic Collaboration in Japan

Forensic team should help define:

- acquisition protocol
- original vs working-copy workflow
- hash and inventory fields
- metadata preservation
- chain-of-custody log
- uncertainty language in reports
- what requires expert analysis
- what AI may only classify as investigative lead

Japan-specific forensic advisor questions:

1. What minimal evidence-preservation steps are realistic for ordinary victims in Japan?
2. What fields should be in a Japanese lawyer-ready digital evidence inventory?
3. How should screenshots, URLs, platform IDs, bank records, device logs, and chat exports be preserved?
4. What should AI never claim in a forensic/evidence summary?
5. What would make the output easier or harder to explain in Japanese legal process?

## 11. Immediate Next Research Tasks

1. Extract NPA cybercrime/cyber threat report categories from the latest PDF/XLSX.
2. Locate and read official AI governance guidance from MIC/METI/Cabinet Office sources; previous METI pages timed out during this run, so re-check later.
3. Read PPC guidance pages relevant to personal-data handling and generative AI use.
4. Build a Japan-specific literature matrix.
5. Draft Japanese synthetic case pack v0.1.
6. Create `Article 72 boundary checklist` for product/research outputs.
7. Create `APPI data-minimization checklist` for evidence intake.
8. Create `Japanese evidence inventory schema v0.1`.

## 12. Brake Checks

Stop and revise if the project drifts into:

- AI legal advice without lawyer review
- collecting real victim data before ethics/legal safeguards
- over-claiming deepfake/authenticity detection
- cyber investigation beyond authorized evidence
- treating AI output as proof
- ignoring APPI/third-party privacy
- ignoring contestability for opposing parties

## 13. Updated One-Sentence Direction

> Kei's research should study Japan-focused, human-reviewed AI evidence workflows that help victims of AI-enabled digital harm preserve and organize digital evidence while respecting Attorney Act boundaries, APPI privacy duties, forensic reliability, and legal contestability.
>
> Strategic product goal: become Japan's trusted AI harm evidence layer, not an AI lawyer brand.
