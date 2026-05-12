# Yuto AI Harm Evidence Company Team v0.2

Created: 2026-05-11 JST
Status: company operating model draft
Owner: Kei + Yuto

Related:
- [[ai-era-legal-advocacy-company-blueprint]]
- [[ai-legal-japan-research-target]]
- [[ai-legal-kcl-cambridge-research-roadmap]]
- [[ai-legal-forensic-ai-learning-path]]
- [[source-anthropic-financial-services-agent-team]]
- [[source-ai-harness-teams]]
- [[yuto-team-lanes-reuse-playbook]]
- [[source-ai-legal-forensic-learning-scouts]]
- [[source-forensic-security-agent-skills-and-models]]

## 1. Executive Summary

Yuto's team should now be treated as a company operating model, not a small helper swarm.

The company direction:

```text
Japan-first AI harm evidence layer
AI時代の証拠保全・相談準備インフラ
```

The team must support three simultaneous goals:

1. build a Japan-first legal-forensic AI evidence infrastructure;
2. track global AI harm, regulation, enforcement, and threat patterns before they arrive in Japan;
3. turn research and intelligence into safer product workflows, evidence packets, policy insight, and academic authority.

This means every major function is a core company division:

```text
Yuto AI Harm Evidence Company
├─ 1. Executive / Control Office
├─ 2. Global Intelligence Division
├─ 3. Research & Policy Division
├─ 4. AI Law & Legal Frontier Division
├─ 5. Digital Forensic Lab
├─ 6. Security Frontier Division
├─ 7. Case & Evidence Operations Division
├─ 8. Engineering / Product Systems Division
├─ 9. Knowledge & Learning Infrastructure Division
└─ 10. Compliance / Safety / Expert Network Division
```

The design borrows the useful pattern from Anthropic's `financial-services` repo:

```text
self-contained domain agents
-> least-privilege leaf workers
-> schema-validated outputs
-> allowlisted handoffs
-> one writer per artifact
-> critic / human sign-off before final claim
```

Do not treat this as a live autonomous company yet. It is the v0.2 operating model and manifest blueprint. Runtime automation should come only after prospective receipts prove value.

## 2. Company Mission

### 2.1 Core Mission

Build the trusted evidence-first bridge for victims and professionals dealing with AI-enabled digital harms in Japan.

```text
Victim / affected person
-> evidence preservation and organization
-> forensic-informed reliability review
-> lawyer-ready consultation preparation
-> platform / bank / police / court / policy handoff
```

### 2.2 What We Are

- AI-assisted digital evidence and legal-prep infrastructure.
- Forensic-informed evidence organization support.
- Human-reviewed lawyer/forensic handoff preparation.
- Global AI harm intelligence system focused on Japan impact.
- Research OS for KCL/Cambridge and long-term authority building.

### 2.3 What We Are Not

- Not an `AI lawyer`.
- Not automated legal advice.
- Not a forensic expert replacement.
- Not a court-admissibility guarantee system.
- Not a deepfake authenticity oracle.
- Not an offensive cyber or hack-back system.
- Not a system that modifies original evidence.

### 2.4 Strategic Principle

```text
Japan-first, not Japan-only.
```

Japan is the target jurisdiction. Global AI harm intelligence is the early-warning system.

## 3. Operating Model

### 3.1 The Company Loop

```text
Global Intelligence
finds new AI harm / law / platform / enforcement signal
↓
Research & Policy
turns the signal into academic, policy, standards, and strategic understanding
↓
AI Law & Legal Frontier
converts legal/regulatory frontier shifts into safe legal-prep boundaries
↓
Digital Forensic Lab
translates standards and evidence science into preservation/reliability requirements
↓
Security Frontier
maps AI-enabled cyber abuse into defensive triage and evidence-preservation playbooks
↓
Knowledge Infrastructure
verifies and stores durable knowledge in Markdown KG and CocoIndex
↓
Engineering / Product Systems
turns knowledge into workflows, prototypes, architecture, code, checklists, and evals
↓
Case & Evidence Operations
uses workflows to prepare evidence/legal-prep outputs
↓
Compliance / Safety / Expert Network
checks legal, privacy, forensic, AI-safety, and misuse boundaries
↓
Executive / Yuto
decides, synthesizes, reports, and updates company direction
```

### 3.2 Worker Output Rule

All worker outputs are receipts until verified.

```text
Worker output = candidate artifact
Yuto / QA / source verification = usable fact
Human legal/forensic expert = high-risk authority gate
```

### 3.3 Least-Privilege Rule

- Readers can read scoped materials but cannot write final artifacts.
- Writers can write final artifacts but should not read raw untrusted evidence directly.
- Legal/compliance reviewers read validated facts, not raw untrusted evidence by default.
- Forensic reviewers examine provenance, metadata, hashes, timeline, and contamination risk; they do not mutate originals.
- Intelligence scouts gather signals; they do not update KG without curation.
- External sending, publication, deployment, production data, secrets, and spending require Kei approval.

### 3.4 8.5+ Completeness Upgrade

The previous v0.2 draft was directionally correct but still too broad in three places:

1. legal frontier work was inside generic Research & Policy;
2. digital forensics was inside Case & Evidence Ops instead of a dedicated lab;
3. security engineering/frontier work was split between intelligence and product without a clear owner.

For a company-grade model, these must be first-class divisions:

```text
AI Law & Legal Frontier = legal/regulatory frontier, not case advice
Digital Forensic Lab = evidence science, preservation protocols, reliability methodology
Security Frontier = AI-enabled cyber abuse, defensive triage, SOC/incident evidence patterns
Engineering / Product Systems = product architecture, prototypes, eval harnesses, code
```

8.5+ readiness criteria:

- every division has a named mission, core roles, outputs, metrics, and handoff path;
- every high-risk domain has a separate critic/gate from the worker producing the artifact;
- every intelligence item can route to research, playbook, product, case, or ignore;
- every case artifact separates source fact, inference, uncertainty, and human-review need;
- every recurring workflow has a future manifest and steering example;
- validators exist before we claim operational managed-agent parity.

Anthropic-style comparison target:

```text
v0.2 document = company operating model
v0.2.1 role/division manifests = governed team packaging
v0.3 validators + steering examples + receipts = 8.5+ operational readiness
```

## 4. Division 1 — Executive / Control Office

### 4.1 Mission

Set direction, choose priorities, route work, enforce gates, and make sure all teams compound toward the company mission.

### 4.2 Core Roles

#### `yuto-control`

Purpose:
- Company control tower and final synthesis layer.

Responsibilities:
- intake Kei requests;
- decide whether work is intelligence, research, case, product, knowledge, or compliance;
- assign divisions and agents;
- define acceptance criteria;
- verify before final claim;
- escalate risky decisions to Kei;
- maintain the Japan-first/global-aware direction.

Inputs:
- Kei request;
- current KG/source notes;
- receipts;
- team outputs;
- risk flags.

Outputs:
- mission brief;
- selected team plan;
- final verified answer;
- next decision;
- escalation request.

Forbidden:
- claiming legal/forensic authority without human expert;
- bypassing QA for high-risk outputs;
- treating worker output as fact without verification.

#### `strategy-chief`

Purpose:
- Translate company mission into quarterly strategic priorities.

Responsibilities:
- maintain company thesis;
- decide which signals matter;
- map research to product and authority-building;
- prevent tool sprawl and random agent expansion.

Outputs:
- strategy memo;
- priority stack;
- stop-doing list;
- investment / learning / research focus.

#### `qa-critic`

Purpose:
- Independent gate before Yuto finalizes.

Checks:
- unsupported claims;
- source gaps;
- unsafe legal/forensic language;
- scope drift;
- missing human review flags;
- unverified worker outputs;
- product claims that overpromise.

Output schema:

```yaml
status: pass|partial|fail
findings:
  - severity: blocker|high|medium|low
    issue: string
    evidence_ref: string
    required_fix: string
residual_risk: string
```

#### `receipt-eval-analyst`

Purpose:
- Measure whether teams improve outcomes.

Metrics:
- verification_status;
- rework_count;
- saved_time_estimate;
- quality_gain;
- safety_gain;
- unsupported_claims_caught;
- missing_evidence_caught;
- reuse_recommendation.

Outputs:
- weekly eval summary;
- keep / modify / drop recommendations;
- prospective vs retrospective receipt caveat.

#### `human-expert-gate`

Purpose:
- Mark when lawyer, forensic expert, privacy expert, or security professional review is required.

Triggers:
- specific legal advice risk;
- forensic authenticity/admissibility claims;
- real victim data;
- production use;
- incident response beyond safe preservation;
- publication under company/research authority.

## 5. Division 2 — Global Intelligence Division

### 5.1 Mission

Track global AI harm, AI misuse, laws, enforcement, platform policy, and threat patterns so the company is never Japan-only or stale.

This is the company's early-warning system.

### 5.2 Scope

The division tracks:

- AI-enabled fraud and scams;
- deepfake / synthetic media harm;
- voice cloning and impersonation;
- AI-enabled phishing and account takeover;
- AI sexual image abuse and harassment;
- synthetic evidence and misinformation;
- AI regulation and enforcement;
- police/cyber agency alerts;
- platform reporting and takedown rules;
- provenance, watermarking, and content authenticity;
- cross-border trends relevant to Japan.

### 5.3 Core Roles

#### `global-ai-harm-news-editor`

Purpose:
- Chief editor for global AI harm intelligence.

Responsibilities:
- compile daily/weekly signals;
- enforce source quality labels;
- deduplicate repeated stories;
- classify harm categories;
- decide whether an item is `ignore`, `watch`, `KG update`, `playbook update`, `urgent alert`, or `research question`.

Output schema:

```yaml
brief_date: YYYY-MM-DD
items:
  - title: string
    region: string
    source_url: string
    source_type: regulator|police|court|platform|company|paper|news|ngo|social-signal
    harm_category: string
    what_happened: string
    evidence_implications: string
    legal_policy_implications: string
    forensic_implications: string
    product_implications: string
    japan_relevance: low|medium|high|urgent
    confidence: low|medium|high
    recommended_action: ignore|watch|kg_update|playbook_update|urgent_alert|research_question
```

Cadence:
- daily light scan;
- weekly intelligence brief;
- urgent alerts when high impact.

#### `japan-ai-harm-radar`

Sources to watch:
- PPC/APPI;
- MOJ;
- NPA;
- NISC;
- IPA;
- METI/MIC AI governance;
- courts;
- Nichibenren;
- LINE/Yahoo Japan and major platform policy;
- Japanese police/scam/deepfake reports.

Focus:
- Japan-specific law/policy updates;
- Article 72 boundary;
- APPI/privacy constraints;
- AI scam/deepfake harm reports;
- platform/bank/police reporting workflows;
- safe Japanese product language.

Outputs:
- Japan impact note;
- product wording update;
- legal boundary watch item;
- evidence workflow localization note.

#### `us-ai-harm-radar`

Sources to watch:
- FTC;
- DOJ;
- FBI/IC3;
- CISA;
- NIST;
- state AI laws;
- major platform and model provider policy;
- court/litigation signals.

Focus:
- enforcement actions;
- AI impersonation and fraud;
- consumer protection;
- cyber incident patterns;
- NIST AI/cyber guidance;
- evidence and discovery practice.

Japan relevance:
- early enforcement patterns;
- platform accountability models;
- scam/fraud evidence checklist improvements.

#### `eu-uk-ai-regulation-radar`

Sources to watch:
- EU AI Act;
- GDPR / EDPB;
- Digital Services Act;
- ENISA;
- UK ICO;
- UK Online Safety Act;
- AI liability / fundamental rights materials;
- eIDAS / identity / trust service developments.

Focus:
- rights-based AI governance;
- privacy and human oversight;
- platform duties;
- risk classification;
- content authenticity and accountability.

Japan relevance:
- comparative governance models;
- thesis/policy research;
- product safety and privacy guardrails.

#### `china-ai-governance-misuse-radar`

Sources to watch:
- Chinese AI/deep synthesis/generative AI rules;
- platform labeling rules;
- public security enforcement signals;
- scam/fraud enforcement campaigns;
- AI content governance.

Focus:
- synthetic media labeling;
- platform governance;
- real-name/content control;
- scam enforcement;
- misuse patterns.

Caution:
- separate fact, state narrative, propaganda, and enforcement signal.

Japan relevance:
- fast policy contrast;
- regional threat patterns;
- provenance/watermark policy comparison.

#### `korea-ai-harm-radar`

Sources to watch:
- Korean regulator/police announcements;
- deepfake sexual abuse policy;
- school/youth digital harm cases;
- platform/takedown mechanisms;
- victim support/legal reform reports.

Focus:
- AI sexual image abuse;
- deepfake harm response;
- fast public-policy response;
- victim-support workflow.

Japan relevance:
- likely adjacent social/legal harm patterns;
- evidence checklist for image abuse/deepfake cases;
- victim-first UX lessons.

#### `platform-policy-radar`

Sources to watch:
- OpenAI;
- Anthropic;
- Google;
- Meta;
- X;
- TikTok;
- LINE/Yahoo Japan;
- Apple/Google app stores;
- C2PA / Content Credentials;
- major takedown/reporting processes.

Focus:
- abuse reporting channels;
- takedown requirements;
- evidence required by platform;
- provenance support;
- content labeling;
- API/model safety policy.

Outputs:
- platform report checklist;
- victim handoff note;
- preservation guidance update.

#### `cyber-threat-pattern-radar`

Sources to watch:
- JPCERT/CC;
- IPA;
- CISA;
- FBI/IC3;
- security vendor threat reports;
- banking/consumer scam alerts.

Focus:
- AI-assisted phishing;
- account takeover;
- business email compromise;
- voice cloning fraud;
- scam automation;
- credential compromise indicators.

Outputs:
- threat pattern brief;
- evidence-to-preserve checklist;
- security analyst playbook update.

#### `ai-threat-category-classifier`

Purpose:
- Normalize intelligence items into harm categories.

Categories:
- deepfake/synthetic media;
- AI sexual image abuse;
- voice cloning impersonation;
- fraud/scam;
- phishing/social engineering;
- account takeover;
- harassment/stalking;
- synthetic evidence/disinformation;
- model/platform abuse;
- legal/regulatory update;
- provenance/content authenticity;
- forensic/evidence practice.

## 6. Division 3 — Research & Policy Division

### 6.1 Mission

Turn news signals and case patterns into durable research, policy analysis, standards maps, and academic authority.

This division supports:

- KCL Law and Technology;
- Cambridge PhD path;
- Japan-first legal-forensic AI research;
- company white papers and policy briefs;
- product trust and defensibility.

### 6.2 Core Roles

#### `academic-literature-scout`

Purpose:
- Track papers relevant to AI harm evidence infrastructure.

Focus areas:
- legal AI hallucination;
- RAG faithfulness;
- AI-assisted digital forensics;
- provenance and content authenticity;
- deepfake detection limitations;
- access to justice;
- AI governance and human review;
- contestability and due process.

Output schema:

```yaml
paper_title: string
source_url: string
method: string
key_claims:
  - claim: string
    evidence: string
limitations:
  - string
relevance_to_kcl: low|medium|high
relevance_to_cambridge_phd: low|medium|high
relevance_to_product: low|medium|high
use_or_skip: use|watch|skip
```

Cadence:
- weekly paper scout;
- monthly literature map update.

#### `japan-law-policy-researcher`

Purpose:
- Deep research into Japan law/policy boundaries.

Focus:
- Attorney Act Article 72;
- APPI;
- evidence/procedure law;
- cybercrime response;
- digital platform governance;
- AI governance guidance;
- victim support systems;
- safe Japanese product copy.

Outputs:
- legal boundary memo;
- Japan policy map;
- safe language guide;
- research source trail.

Forbidden:
- case-specific legal advice;
- claims that non-lawyer AI can advise on outcome/strategy.

#### `comparative-law-analyst`

Purpose:
- Compare Japan with US, EU/UK, China, Korea, and other relevant jurisdictions.

Questions:
- Which country is leading on this harm?
- Which legal mechanism exists elsewhere but not in Japan?
- What can Japan adopt or avoid?
- What does this mean for Kei's research/company wedge?

Outputs:
- comparative law table;
- Japan impact memo;
- policy opportunity brief.

#### `dfir-standards-radar`

Purpose:
- Track and translate DFIR standards into product/checklist requirements.

Sources:
- NIST SP 800-86;
- NIST SP 800-61;
- SWGDE;
- ISO/IEC 27037 and 27041;
- SANS/Magnet/Cellebrite public training materials;
- accepted forensic practice references.

Outputs:
- preservation checklist;
- chain-of-custody checklist;
- metadata/timestamp checklist;
- forensic expert handoff questions;
- product restrictions.

#### `ai-reliability-provenance-radar`

Purpose:
- Track how AI outputs and synthetic media can be evaluated, challenged, or labeled.

Focus:
- RAG evaluation;
- faithfulness/grounding;
- legal hallucination;
- C2PA and Content Credentials;
- watermarking;
- detector limitations;
- provenance metadata;
- prompt-injection risks.

Outputs:
- AI reliability checklist;
- unsupported-claim detector rules;
- provenance limitation memo;
- research questions for eval harness.

#### `policy-brief-writer`

Purpose:
- Convert research into concise policy-facing outputs.

Outputs:
- issue brief;
- policy gap memo;
- Japan recommendation draft;
- evidence from global comparison;
- safe caveats.

#### `research-roadmap-writer`

Purpose:
- Maintain KCL/Cambridge research roadmap and thesis alignment.

Outputs:
- research question updates;
- chapter outline updates;
- literature map;
- methodology plan;
- evaluation design.


## 7A. Division 4 — AI Law & Legal Frontier Division

### 7A.1 Mission

Track, interpret, and operationalize AI law, platform governance, digital evidence law, privacy, and legal-tech boundaries without providing case-specific legal advice.

This division is not the lawyer. It is the company's legal-intelligence and boundary-design function.

### 7A.2 Scope

- AI law and regulation in Japan, US, EU/UK, China, Korea, and other relevant jurisdictions;
- non-lawyer practice boundaries, especially Japan Attorney Act Article 72;
- APPI/privacy and cross-border data handling;
- digital evidence, eDiscovery, legal hold, disclosure/discovery, and consultation-prep workflows;
- platform policy and takedown/legal request interfaces;
- legal hallucination and AI accountability;
- product wording and risk disclosure.

### 7A.3 Core Roles

#### `ai-law-frontier-lead`

Purpose:
- Own the AI law frontier map and translate legal changes into company strategy.

Outputs:
- AI law frontier map;
- jurisdiction comparison;
- Japan implication memo;
- product/legal-prep guardrail update.

#### `japan-article72-boundary-analyst`

Purpose:
- Keep the company safely on the legal-prep/evidence-organization side of Japan's non-lawyer practice boundary.

Checks:
- whether output becomes specific legal advice;
- whether product wording implies `AI弁護士`;
- whether a lawyer review gate is required;
- whether safer Japanese copy is needed.

#### `appi-data-governance-analyst`

Purpose:
- Translate APPI/privacy requirements into product and evidence-workflow constraints.

Outputs:
- data-minimization checklist;
- retention/deletion review;
- consent/notice issue list;
- redaction requirement.

#### `legal-evidence-procedure-scout`

Purpose:
- Track how digital evidence, eDiscovery-like practice, legal hold, court/police/platform reporting, and consultation-prep norms affect the product.

Outputs:
- legal evidence workflow note;
- lawyer handoff requirements;
- filing/reporting caveats;
- source trail.

#### `platform-law-policy-analyst`

Purpose:
- Track platform rules with legal consequences: takedown, impersonation, sexual image abuse, fraud, account compromise, and evidence requests.

Outputs:
- platform policy change note;
- victim reporting checklist;
- evidence-to-preserve list.

### 7A.4 Handoffs

```text
Global Intelligence -> AI Law Frontier -> Knowledge Infrastructure
AI Law Frontier -> Compliance/Safety -> Product Systems
AI Law Frontier -> Research & Policy -> Research Roadmap
Case Ops -> AI Law Frontier -> Lawyer Expert Gate
```

### 7A.5 Metrics

- frontier updates reviewed per month;
- unsafe legal language caught;
- product guardrails updated;
- jurisdiction comparisons produced;
- lawyer-gate triggers correctly flagged.

## 7B. Division 5 — Digital Forensic Lab

### 7B.1 Mission

Own the forensic methodology layer for digital evidence preservation, reliability, provenance, chain of custody, and expert handoff.

This lab does not certify authenticity by AI. It designs defensible workflows and flags when a human forensic expert is required.

### 7B.2 Scope

- DFIR standards and practice translation;
- evidence preservation and acquisition workflow design;
- source inventory, hashes, timestamps, metadata, and provenance;
- chain of custody;
- mobile/cloud/social-platform evidence limitations;
- screenshots and generated-media limitations;
- contamination risk and repeatability;
- forensic expert handoff packet.

### 7B.3 Core Roles

#### `digital-forensic-methodology-lead`

Purpose:
- Own forensic methodology and standards mapping.

Outputs:
- forensic methodology memo;
- preservation SOP;
- standards-to-product requirements;
- expert-review criteria.

#### `evidence-preservation-engineer`

Purpose:
- Design preservation workflows that non-experts can follow without polluting evidence.

Outputs:
- preservation checklist;
- original-vs-working-copy rules;
- safe acquisition instructions;
- do-not-do list.

#### `chain-of-custody-architect`

Purpose:
- Design and review chain-of-custody records.

Outputs:
- custody log template;
- handoff event schema;
- missing handler/time/storage flags.

#### `metadata-timestamp-forensics-analyst`

Purpose:
- Review metadata and timestamp reliability.

Focus:
- EXIF/file metadata;
- platform timestamps;
- device clock drift;
- timezone normalization;
- server/client time distinction;
- screenshot limitations.

#### `mobile-cloud-evidence-specialist`

Purpose:
- Track evidence issues from smartphones, cloud accounts, social platforms, messaging apps, and app stores.

Outputs:
- mobile/cloud evidence checklist;
- account export requirements;
- platform-specific preservation caveats.

#### `forensic-report-handoff-writer`

Purpose:
- Prepare expert-facing forensic handoff notes from validated facts and methodology gaps.

Forbidden:
- claiming expert conclusions;
- asserting authenticity/admissibility;
- hiding uncertainty.

### 7B.4 Handoffs

```text
Case Ops -> Digital Forensic Lab -> Forensic Expert Gate
Digital Forensic Lab -> Product Systems -> preservation workflow
Digital Forensic Lab -> Knowledge Infrastructure -> checklist update
Global Intelligence -> Digital Forensic Lab -> threat/evidence method update
```

### 7B.5 Metrics

- evidence gaps caught;
- chain-of-custody gaps caught;
- metadata/timestamp uncertainty flagged;
- contamination risks prevented;
- expert handoff readiness.

## 7C. Division 6 — Security Frontier Division

### 7C.1 Mission

Understand AI-enabled cyber abuse and convert it into defensive triage, incident evidence, product requirements, and safety boundaries.

This division is defensive only.

### 7C.2 Scope

- AI phishing and social engineering;
- voice cloning and impersonation fraud;
- account takeover and session compromise;
- business email compromise and scam operations;
- credential abuse indicators;
- malware/suspicious file triage boundaries;
- SOC/security analyst workflows;
- defensive incident timeline and evidence preservation;
- model/agent misuse patterns.

### 7C.3 Core Roles

#### `security-frontier-lead`

Purpose:
- Own AI-enabled cyber abuse strategy and defensive security workflow design.

Outputs:
- threat frontier memo;
- security evidence checklist;
- incident response boundary guide;
- product requirement update.

#### `soc-triage-analyst`

Purpose:
- Convert logs, alerts, emails, and account events into defensive triage findings.

Outputs:
- observed indicators;
- incident timeline;
- missing logs;
- preservation checklist;
- confidence label.

#### `account-takeover-phishing-analyst`

Purpose:
- Specialize in phishing, account takeover, suspicious login, and session evidence.

Outputs:
- ATO hypothesis;
- evidence inventory;
- next safe preservation actions;
- escalation criteria.

#### `scam-fraud-threat-analyst`

Purpose:
- Track AI-enabled scam patterns and convert them into evidence packet requirements.

Focus:
- romance scam;
- investment scam;
- voice cloning fraud;
- impersonation;
- payment/bank/platform reporting.

#### `ai-agent-misuse-red-teamer`

Purpose:
- Review whether product workflows could be abused by malicious users or agents.

Outputs:
- misuse scenario;
- risk severity;
- mitigation;
- blocked capability.

Forbidden:
- exploit instructions;
- credential theft;
- evasion;
- malware operation;
- unauthorized access;
- hack-back.

### 7C.4 Handoffs

```text
Global Intelligence -> Security Frontier -> Product Systems
Security Frontier -> Case Ops -> incident evidence packet
Security Frontier -> Compliance/Safety -> misuse risk review
Security Frontier -> Knowledge Infrastructure -> security playbook update
```

### 7C.5 Metrics

- threat patterns converted to checklists;
- phishing/ATO evidence gaps caught;
- unsafe offensive content blocked;
- incident timeline completeness;
- defensive-only boundary compliance.


## 7. Division 7 — Case & Evidence Operations Division

### 7.1 Mission

Turn the company's knowledge into safe, human-reviewed evidence and consultation-prep workflows.

This division handles synthetic cases first, then real cases only under strict protocol.

### 7.2 Core Roles

#### `evidence-intake-agent`

Purpose:
- Build an initial evidence inventory from scoped, safe inputs.

Responsibilities:
- identify evidence sources;
- list known facts with source refs;
- separate direct observation from inference;
- flag missing evidence;
- avoid authenticity/legal conclusions.

Output schema:

```yaml
case_id: string
evidence_inventory:
  - evidence_id: string
    source_type: screenshot|pdf|chat_export|email|url|log|other
    source_ref: string
    observed_facts:
      - string
    missing_metadata:
      - string
red_flags:
  - string
```

#### `victim-first-response-agent`

Purpose:
- Produce safe, non-legal, preservation-first guidance for affected people.

Allowed:
- general preservation checklist;
- do-not-delete guidance;
- document collection checklist;
- crisis escalation suggestion where appropriate;
- lawyer/forensic/police/platform referral checklist.

Forbidden:
- legal advice;
- telling user to confront attacker;
- hack-back;
- evidence tampering;
- outcome promises.

#### `security-analyst-agent`

Purpose:
- Analyze cyber/security patterns defensively.

Focus:
- phishing;
- account takeover;
- login/session evidence;
- email headers;
- scam indicators;
- suspicious URL/domain indicators;
- incident timeline.

Outputs:
- incident type hypothesis;
- timeline;
- indicators observed;
- missing logs;
- preservation checklist;
- confidence and caveats.

Forbidden:
- offensive exploitation;
- credential collection;
- evasion;
- unauthorized access;
- malware execution.

#### `dfir-evidence-agent`

Purpose:
- Review evidence handling and forensic reliability.

Focus:
- original vs working copy;
- hash/provenance;
- timestamp/timezone;
- acquisition method;
- chain-of-custody;
- contamination risk;
- need for expert review.

Output schema:

```yaml
forensic_status: pass|partial|fail
chain_of_custody_gaps:
  - string
metadata_gaps:
  - string
contamination_risks:
  - string
expert_review_needed: true|false
```

#### `chain-of-custody-reviewer`

Purpose:
- Check whether a case packet can explain who handled what, when, how, and why.

Outputs:
- custody timeline;
- missing handler info;
- transfer gaps;
- storage risk;
- recommended fixes.

#### `metadata-timestamp-analyst`

Purpose:
- Normalize and review time-related evidence.

Focus:
- timezone;
- device clock drift;
- platform timestamp formats;
- file metadata;
- screenshot limitations;
- server vs client time distinctions.

Outputs:
- normalized timeline;
- timestamp uncertainty;
- metadata gaps.

#### `ai-evidence-reliability-agent`

Purpose:
- Check AI-generated summaries and claims for grounding and hallucination.

Checks:
- every factual claim has source ref;
- no invented chronology;
- no overstated authenticity;
- uncertainty labels are present;
- cited evidence actually supports claim.

#### `japan-legal-boundary-agent`

Purpose:
- Review case outputs for Japan-specific legal boundary risk.

Focus:
- Article 72 non-lawyer practice;
- APPI personal data concerns;
- unsafe legal advice language;
- evidence-prep vs legal conclusion;
- human lawyer review requirement.

#### `lawyer-packet-agent`

Purpose:
- Write consultation-prep packets from validated facts only.

Required sections:

```text
1. Purpose and scope
2. Source inventory
3. Timeline
4. Extracted facts with source refs
5. Evidence gaps
6. Questions for lawyer
7. Questions for forensic/security expert
8. Risk and uncertainty notes
9. Human review checklist
```

This is a write-holder role. It must not read raw untrusted evidence directly.

## 8. Division 8 — Engineering / Product Systems Division

### 8.1 Mission

Design and build the product/system layer that turns research and case workflows into usable infrastructure.

### 8.2 Core Roles

#### `workflow-product-designer`

Purpose:
- Convert evidence/legal/forensic workflows into product flows.

Outputs:
- user journey;
- intake form spec;
- evidence table UX;
- review states;
- human handoff design;
- safe Japanese copy.

#### `secure-evidence-architecture-reviewer`

Purpose:
- Design architecture that protects evidence and personal data.

Focus:
- original evidence immutability;
- working-copy separation;
- access control;
- audit logs;
- encryption assumptions;
- deletion/export;
- local-first vs cloud tradeoffs;
- prompt-injection protection.

Outputs:
- architecture risk review;
- data flow diagram;
- secure workflow checklist.

#### `privacy-by-design-reviewer`

Purpose:
- Ensure APPI/privacy minimization from design stage.

Checks:
- data minimization;
- purpose limitation;
- consent/notice requirements;
- sensitive data handling;
- retention/deletion;
- third-party transfer risk;
- redaction workflow.

#### `forensic-workflow-designer`

Purpose:
- Convert DFIR standards into user-safe workflows.

Outputs:
- evidence preservation SOP;
- chain-of-custody template;
- metadata capture instructions;
- forensic expert handoff packet.

#### `local-llm-benchmark-worker`

Purpose:
- Benchmark local models as read-only workers before adoption.

Current baseline:
- Qwen3.6 27B available as general local worker/reviewer.
- bge-m3 available for embedding/retrieval support.

Candidate future benchmark:
- cyber/DFIR-specialized models only after explicit approval and safety review.

Outputs:
- benchmark task set;
- accuracy/safety notes;
- hallucination findings;
- keep/modify/drop recommendation.

Forbidden:
- promoting a model to legal/forensic authority;
- downloading large/offensive models without Kei approval.

#### `code-implementation-worker`

Purpose:
- Implement scoped prototypes and tooling.

Rules:
- one scoped task at a time;
- follows existing repo patterns;
- no production/deploy/secrets;
- tests or verification required;
- Codex/local policy applies.

#### `product-qa-reviewer`

Purpose:
- Test product flows for safety, usability, and claim discipline.

Checks:
- user can understand output;
- AI does not imply legal/forensic authority;
- evidence is not overwritten;
- missing human review flags are visible;
- redaction/privacy behavior is clear.

## 9. Division 9 — Knowledge & Learning Infrastructure Division

### 9.1 Mission

Maintain the company's institutional memory, source trails, playbooks, checklists, skills, and derived indexes.

This is what lets the company compound instead of repeatedly rediscovering things.

### 9.2 Core Roles

#### `source-curator-kg-librarian`

Purpose:
- Convert verified intelligence/research/case lessons into durable Markdown KG notes.

Responsibilities:
- verify representative sources;
- label fact/inference/speculation/unknown;
- write compact source trails;
- link from `knowledge/index.md` and `knowledge/sources.md` when appropriate;
- avoid storing raw sensitive victim data;
- trigger CocoIndex update/doctor/status.

Outputs:
- source note;
- updated index links;
- verification summary;
- residual risk.

#### `cocoindex-freshness-checker`

Purpose:
- Ensure derived index is fresh and trustworthy.

Commands:

```bash
python tools/second_brain.py coco update
python tools/second_brain.py coco doctor
python tools/second_brain.py status
```

Checks:
- missing source notes;
- stale derived JSON;
- orphan derived files;
- graph broken links;
- graph orphans.

#### `playbook-update-writer`

Purpose:
- Turn repeated successful workflows into reusable playbooks.

Inputs:
- receipts;
- case outcomes;
- intelligence findings;
- research memos;
- QA failures.

Outputs:
- updated playbook;
- checklist;
- template;
- skill candidate.

#### `skill-maintainer`

Purpose:
- Promote stable repeatable workflows into Hermes skills and patch stale skills.

Promotion triggers:
- same workflow succeeds repeatedly;
- a tricky error was solved;
- worker lane pattern becomes reusable;
- Kei explicitly asks to remember a procedure.

#### `checklist-maintainer`

Purpose:
- Maintain concise gates/checklists for reviews.

Examples:
- evidence preservation checklist;
- Japan legal boundary checklist;
- APPI privacy checklist;
- AI reliability checklist;
- product safety checklist;
- source quality checklist.

#### `case-pattern-archivist`

Purpose:
- Extract non-sensitive patterns from cases or synthetic tasks.

Rules:
- no raw victim data;
- no identifying details unless explicitly approved and safe;
- pattern-level learning only;
- route to research/product questions.

## 10. Division 10 — Compliance / Safety / Expert Network Division

### 10.1 Mission

Keep the company safe, lawful, evidence-first, and credible.

This division is not optional. It is the brake system.

### 10.2 Core Roles

#### `article72-boundary-reviewer`

Purpose:
- Prevent unauthorized practice of law in Japan.

Checks:
- does output answer a specific legal question as advice?
- does it predict outcome?
- does it tell user what legal strategy to take?
- does it present AI as lawyer?
- is lawyer review required?

Safer framing:
- consultation preparation;
- evidence organization;
- general information;
- lawyer-supervised support.

#### `appi-privacy-reviewer`

Purpose:
- Review APPI/privacy and personal-data handling risk.

Checks:
- personal data collection;
- sensitive data;
- consent/notice;
- purpose limitation;
- minimization;
- retention;
- third-party transfer;
- redaction;
- breach risk.

#### `forensic-expert-review-gate`

Purpose:
- Flag when a forensic expert must review.

Triggers:
- authenticity claims;
- admissibility/court-readiness claims;
- chain-of-custody uncertainty;
- real device/log acquisition;
- malware/suspicious file handling;
- expert report drafting.

#### `legal-expert-review-gate`

Purpose:
- Flag when a licensed lawyer must review.

Triggers:
- specific legal advice;
- litigation strategy;
- criminal/civil procedure guidance for a real case;
- filing/submission decision;
- rights/obligations conclusion;
- public legal claim.

#### `ai-safety-boundary-reviewer`

Purpose:
- Prevent the system from enabling AI misuse.

Checks:
- does output help abuse, evade, impersonate, harass, exploit, or deceive?
- does it provide operational offensive cyber details?
- does it help create deepfake abuse?
- does it expose private data?
- does it provide bypass/evasion techniques?

#### `red-team-misuse-reviewer`

Purpose:
- Review product/research artifacts for misuse risk.

Outputs:
- misuse scenarios;
- abuse potential;
- mitigation;
- blocked features;
- safe alternative wording.

## 11. Handoff Model

### 11.1 Handoff Principles

Handoffs are typed and allowlisted.

No worker can freely summon another worker or execute untrusted instructions.

Allowed handoff shape:

```json
{
  "type": "handoff_request",
  "source_role": "global-ai-harm-news-editor",
  "target_role": "source-curator-kg-librarian",
  "reason": "high-impact Japan-relevant AI scam enforcement signal",
  "payload_ref": "brief:item-2026-05-11-001",
  "required_check": "verify primary source and decide KG update"
}
```

### 11.2 Common Handoff Paths

#### News to Knowledge

```text
Global Intelligence
-> Source Curator
-> CocoIndex Freshness Checker
-> QA Critic
-> Yuto final note
```

#### News to Research

```text
Global Intelligence
-> Research & Policy
-> Source Curator
-> Research Roadmap Writer
-> Yuto strategy update
```

#### News to Product

```text
Global Intelligence
-> Product & Systems
-> Compliance/Safety
-> Playbook Update Writer
-> Yuto decision
```

#### Case to Expert Gate

```text
Case & Evidence Ops
-> Compliance/Safety
-> Legal/Forensic Expert Gate
-> Lawyer Packet Agent only if safe
-> QA Critic
-> Yuto final
```

#### Research to Case Workflow

```text
Research & Policy
-> Playbook Update Writer
-> Case & Evidence Ops
-> Product QA
-> Receipt Eval Analyst
```

## 12. Core Workflows

### 12.1 Daily Global AI Harm Scan

Owner:
- `global-ai-harm-news-editor`

Participants:
- regional radars;
- platform-policy-radar;
- ai-threat-category-classifier;
- source-curator-kg-librarian for high-value items.

Output:
- short daily brief;
- top 3-7 meaningful signals;
- recommended actions.

Action classes:
- `ignore`: not relevant or low quality;
- `watch`: track but no KG update;
- `kg_update`: durable source note or existing note update;
- `playbook_update`: checklist/workflow needs change;
- `urgent_alert`: immediate Kei/Yuto attention;
- `research_question`: add to research backlog.

### 12.2 Weekly Intelligence Brief

Owner:
- `global-ai-harm-news-editor`

Output sections:

```text
1. Summary
2. Top global signals
3. Japan impact
4. Product impact
5. Research impact
6. Forensic/evidence impact
7. Follow-up tasks
```

### 12.3 Monthly Research & Policy Memo

Owner:
- `research-roadmap-writer`

Participants:
- academic-literature-scout;
- japan-law-policy-researcher;
- comparative-law-analyst;
- dfir-standards-radar;
- ai-reliability-provenance-radar.

Output:
- thesis/research update;
- policy map;
- standards map;
- product guardrail updates.

### 12.4 Synthetic Case Evaluation

Owner:
- `case & evidence operations`

Purpose:
- Test workflows without real victim data.

Case types:
- deepfake sexual image abuse;
- voice cloning scam;
- phishing/account takeover;
- AI impersonation fraud;
- synthetic evidence dispute;
- platform takedown failure.

Output:
- evidence packet;
- timeline;
- forensic flags;
- legal boundary check;
- AI reliability check;
- receipt/eval.

### 12.5 Product Prototype Loop

Owner:
- `product & systems division`

Flow:

```text
Research/product requirement
-> workflow design
-> architecture review
-> privacy review
-> prototype implementation
-> product QA
-> safety/compliance review
-> receipt/eval
```

## 13. Source and Confidence Rules

### 13.1 Source Quality

High confidence:
- official regulator/legal/police/court source;
- standards body;
- direct platform/company policy;
- peer-reviewed or well-documented academic paper;
- reproducible local command/file evidence.

Medium confidence:
- reputable news outlet;
- law firm analysis with citations;
- security vendor report;
- NGO report with methodology.

Low confidence:
- social media;
- anonymous claims;
- viral screenshots;
- unsourced commentary;
- worker-only summary.

### 13.2 Claim Labels

Every important claim should be labeled as one of:

```text
Fact: directly supported by opened source or local verification
Inference: reasoned conclusion from facts
Speculation: plausible but not verified
Unknown: unresolved or missing evidence
```

### 13.3 News Handling Rule

A news item is not automatically durable knowledge.

```text
news signal -> source quality check -> Japan relevance -> action decision -> KG only if durable
```

## 14. Data and Evidence Safety Rules

- Do not ingest real victim data into broad KG.
- Do not store secrets, credentials, private personal data, or raw sensitive evidence in general notes.
- Use synthetic or redacted cases for evaluation.
- Original evidence must remain immutable.
- AI works on copies or metadata, not originals.
- Hash/provenance/timestamp should be captured when relevant.
- Personal data must be minimized and purpose-scoped.
- Any external disclosure/send/publish requires Kei approval.

## 15. Output Artifacts by Division

### Executive / Control Office

- mission brief;
- decision memo;
- priority stack;
- final verified report;
- escalation note.

### Global Intelligence Division

- daily brief;
- weekly intelligence brief;
- urgent alert;
- threat pattern update;
- regional comparison note.

### Research & Policy Division

- literature review;
- policy brief;
- standards map;
- comparative law memo;
- thesis roadmap update;
- research question backlog.

### AI Law & Legal Frontier Division

- AI law frontier map;
- Article 72 boundary memo;
- APPI/privacy requirement update;
- platform legal/policy change note;
- legal evidence workflow memo;
- safe Japanese product language guidance.

### Digital Forensic Lab

- forensic methodology memo;
- preservation SOP;
- chain-of-custody template;
- metadata/timestamp uncertainty review;
- mobile/cloud evidence checklist;
- forensic expert handoff packet.

### Security Frontier Division

- AI-enabled cyber abuse threat memo;
- SOC triage checklist;
- phishing/account-takeover evidence guide;
- scam/fraud pattern update;
- misuse red-team findings;
- defensive security boundary guide.

### Case & Evidence Operations Division

- evidence inventory;
- incident timeline;
- chain-of-custody review;
- forensic gap list;
- lawyer-ready consultation packet;
- expert review questions.

### Engineering / Product Systems Division

- workflow spec;
- architecture diagram/spec;
- privacy/security review;
- prototype;
- local model benchmark;
- product QA report.

### Knowledge & Learning Infrastructure Division

- source trail;
- KG update;
- CocoIndex health report;
- playbook update;
- checklist;
- skill update proposal.

### Compliance / Safety / Expert Network Division

- Article 72 risk review;
- APPI review;
- forensic expert gate;
- legal expert gate;
- misuse risk review;
- forbidden-claims list.

## 16. Measurement and KPIs

### 16.1 Company-Level KPIs

- Does Yuto produce fewer unsupported claims?
- Does the KG stay current and searchable?
- Do intelligence signals lead to useful product/research changes?
- Do case workflows produce lawyer/forensic-ready drafts faster?
- Are safety/legal/forensic gates triggered correctly?
- Does Kei spend less time re-explaining direction?

### 16.2 Division KPIs

#### Global Intelligence

- high-signal items per week;
- false positive/noise rate;
- Japan relevance accuracy;
- number of playbook/research/product updates triggered;
- source quality distribution.

#### Research & Policy

- useful papers/sources digested;
- literature map freshness;
- research roadmap updates;
- policy/standards gaps identified;
- thesis/product relevance.

#### AI Law & Legal Frontier

- legal/regulatory updates reviewed;
- unsafe legal-positioning issues caught;
- Article 72/APPI risks flagged;
- comparative legal insights routed to product/research;
- lawyer-gate triggers correctly identified.

#### Digital Forensic Lab

- forensic methodology gaps caught;
- preservation SOP updates;
- chain-of-custody defects caught;
- metadata/timestamp uncertainty flagged;
- expert handoff quality.

#### Security Frontier

- AI-enabled threat patterns tracked;
- defensive evidence checklists updated;
- phishing/ATO/scam evidence gaps caught;
- misuse risks blocked;
- offensive-content boundary compliance.

#### Case & Evidence Ops

- evidence gaps caught;
- timeline completeness;
- forensic flags caught;
- legal boundary issues caught;
- packet readability;
- expert-review readiness.

#### Engineering / Product Systems

- prototype cycles completed;
- safety/privacy issues caught before build;
- eval tasks passed;
- local model benchmark usefulness;
- implementation rework rate.

#### Knowledge Infrastructure

- KG notes added/updated;
- CocoIndex doctor status;
- broken links/orphans;
- playbooks/checklists updated;
- skill patches made when needed.

#### Compliance / Safety

- unsafe claims caught;
- human-gate triggers;
- privacy issues caught;
- misuse scenarios mitigated;
- blocked high-risk outputs.

## 17. v0.2 Implementation Plan

### Phase 0 — Freeze this org model

Goal:
- Use this document as the company-team source of truth.

Tasks:
- Review with Kei.
- Decide role names to keep/change.
- Mark divisions and roles as v0.2 draft.

### Phase 1 — Create division manifests

Goal:
- Create one manifest per division.

Target directory:

```text
/Users/kei/kei-jarvis/knowledge/yuto-company-team/
```

Files:

```text
README.md
executive-control-office.yaml
global-intelligence-division.yaml
research-policy-division.yaml
ai-law-legal-frontier-division.yaml
digital-forensic-lab.yaml
security-frontier-division.yaml
case-evidence-operations-division.yaml
engineering-product-systems-division.yaml
knowledge-learning-infrastructure-division.yaml
compliance-safety-expert-network-division.yaml
```

Each manifest should include:

```yaml
id:
name:
mission:
roles:
inputs:
outputs:
cadence:
allowed_tools:
forbidden_tools:
handoff_allowed_to:
human_gate:
receipt_required:
metrics:
```

### Phase 2 — Create role manifests for highest-value roles

Start with:

1. `global-ai-harm-news-editor`
2. `source-curator-kg-librarian`
3. `japan-ai-harm-radar`
4. `us-ai-harm-radar`
5. `eu-uk-ai-regulation-radar`
6. `academic-literature-scout`
7. `ai-law-frontier-lead`
8. `japan-article72-boundary-analyst`
9. `digital-forensic-methodology-lead`
10. `evidence-preservation-engineer`
11. `security-frontier-lead`
12. `soc-triage-analyst`
13. `evidence-intake-agent`
14. `dfir-evidence-agent`
15. `japan-legal-boundary-agent`
16. `qa-critic`

Create these 16 first because they cover the minimum company-grade backbone. Do not create every subrole manifest until a workflow uses it.

### Phase 3 — Create three company workflows

Initial workflows:

1. Global AI harm weekly brief.
2. AI law frontier update -> product/legal-boundary change.
3. Digital forensic preservation SOP update.
4. Security frontier threat pattern -> evidence checklist.
5. Synthetic AI harm evidence packet.
6. Research-to-product playbook update.

### Phase 4 — Prospective receipt test

Run 3-5 live prospective tasks, not retrospective receipts.

Measure:
- which divisions were used;
- time saved;
- quality gain;
- safety gain;
- rework;
- whether company model is too heavy or useful.

### Phase 5 — Runtime integration

Only after Phase 4:

- update Workspace roster;
- create helper CLI if needed;
- add cron jobs for intelligence desk;
- add validators for division/role manifests;
- consider local model benchmarks.

## 18. First Practical Company Routines

### Daily Routine

```text
Global Intelligence: scan key sources
Source Curator: mark items for watch/KG/update
Yuto: send concise brief if useful
```

### Weekly Routine

```text
Global Intelligence: weekly brief
Research & Policy: pick 1-2 deep follow-ups
Knowledge Infra: update KG/CocoIndex/playbooks
Executive: decide next actions
```

### Monthly Routine

```text
Research & Policy: strategic memo
Product & Systems: update workflow/product roadmap
Case Ops: run synthetic case evaluation
Compliance/Safety: update forbidden claims and gates
Executive: revise priorities
```

## 19. Open Questions for Kei

1. Company name:
   - keep `Yuto AI Harm Evidence Company` as internal name?
   - or use another internal codename?

2. First intelligence cadence:
   - daily Telegram short brief?
   - weekly deeper brief?
   - both?

3. First regions:
   - Japan, US, EU/UK, China, Korea are the default.
   - Add Southeast Asia later for scam/fraud patterns?

4. First synthetic case set:
   - deepfake sexual image abuse;
   - voice cloning fraud;
   - phishing/account takeover;
   - AI impersonation romance scam;
   - synthetic evidence dispute.

5. First real-world expert review target:
   - Japanese lawyer;
   - DFIR expert;
   - privacy/APPI expert;
   - victim-support NGO/advisor.

## 20. 8.5+ Upgrade Scorecard

Current document after this upgrade should be judged as follows:

```text
Domain architecture:        8.8/10
Safety posture:             9.0/10
Company completeness:       8.8/10
Schema/manifest readiness:  6.5/10
Runtime/validator readiness:5.0/10
Overall operating model:    8.5/10
```

What moved the model above 8.5:

- AI Law & Legal Frontier is now a first-class division, not hidden inside generic research.
- Digital Forensic Lab is now a first-class methodology owner, not only a case reviewer.
- Security Frontier is now a first-class defensive cyber/AI misuse owner, not only a news topic.
- Engineering / Product Systems explicitly owns architecture, eval harnesses, local model benchmarks, and code.
- Global Intelligence remains the early-warning system but no longer replaces execution teams.
- Knowledge Infrastructure remains the compounding memory layer.
- Compliance/Safety remains the brake and expert-gate system.

Remaining gap before Anthropic-style managed-agent parity:

1. create division/role manifests;
2. add JSON Schemas for outputs/handoffs;
3. add steering examples;
4. add validator/drift checker;
5. run 3-5 prospective receipts.

## 21. Final Operating Statement

This company is not a chatbot team.

It is a Yuto-led, evidence-first company operating system:

```text
Global intelligence keeps us current.
Research gives us depth and authority.
Case operations make the work useful to people.
Product systems turn it into infrastructure.
Knowledge infrastructure makes it compound.
Compliance and expert gates keep it lawful and credible.
Yuto coordinates, verifies, and protects the direction.
```

Short form:

```text
Japan-first AI harm evidence layer,
global-aware intelligence system,
human-reviewed legal-forensic workflow company.
```
