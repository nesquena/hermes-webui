# HR Update Scout Report — 2026-05-12

Status: read-only scout synthesis for Company HR / People Ops v0.1
Related: [[company-hr-people-ops-team-v0.1]]

## Scope

Kei asked Yuto to create Team HR from the book canon and send HR team members to study current updates. This report captures the first read-only HR scout outputs.

Scout outputs are not policy authority. They are update candidates. Yuto verified selected source URLs for reachability, but future formal policy must re-check source versions and local legal relevance.

## Scouts dispatched

### 1. HR Role Designer Scout

Focus:
- AI-agent workforce role design
- role charters
- hiring/activation criteria
- role pruning

Key update candidates:
- Every worker needs an Agent Role Charter.
- New roles should pass a hiring funnel: demand intake -> role design -> build-vs-workflow -> prototype -> eval -> shadow/supervised mode -> activation.
- Use an autonomy ladder from reference-only to bounded high autonomy.
- Role sprawl must be controlled with collision review and retirement rules.
- Separate role responsibility from implementation details such as prompt/model/tool stack.

Source URLs reported:
- https://www.anthropic.com/engineering/building-effective-agents
- https://openai.github.io/openai-agents-python/
- https://platform.openai.com/docs/guides/evals
- https://google.github.io/adk-docs/
- https://microsoft.github.io/autogen/stable/
- https://langchain-ai.github.io/langgraph/concepts/multi_agent/
- https://docs.crewai.com/concepts/agents
- https://www.nist.gov/itl/ai-risk-management-framework
- https://www.iso.org/standard/81230.html

Yuto URL reachability check:
- Anthropic Building Effective Agents: 200
- OpenAI Agents SDK: 200
- OpenAI Evals: 200, redirected to developers.openai.com
- Google ADK docs: 200, redirected to adk.dev
- CrewAI Agents: 200, redirected to docs.crewai.com/en/concepts/agents
- NIST AI RMF: 200
- ISO/IEC 42001 page: 200, redirected to /standard/42001

### 2. HR Compliance & Safety Steward Scout

Focus:
- responsible AI governance
- human oversight
- accountability
- risk management
- safety gates

Key update candidates:
- HR must define owners for AI systems, safety, data, security, human approval, and incidents.
- Every agent/workflow needs owner, risk tier, approved purpose, allowed tools/data, monitoring plan.
- High-risk actions need human-in-the-loop; monitored low/medium-risk workflows may use human-on-the-loop.
- Safety gates are required before external actions, production access, public claims, real case data, legal/forensic/employment/finance decisions.
- HR acceptable-use policy must ban unapproved AI tools for sensitive data, bypassing logs/approvals, and autonomous external action without approval.

Source URLs reported:
- https://www.nist.gov/itl/ai-risk-management-framework
- https://www.nist.gov/itl/ai-risk-management-framework/nist-ai-rmf-generative-ai-profile
- https://oecd.ai/en/ai-principles
- https://www.mofa.go.jp/files/100573473.pdf
- https://www.mofa.go.jp/files/100573472.pdf
- https://artificialintelligenceact.eu/the-act/
- https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai
- https://www.iso.org/standard/81230.html
- https://www.iso.org/standard/77304.html
- https://www.whitehouse.gov/omb/management/ai/
- https://www.meti.go.jp/english/press/2024/0419_002.html
- https://aisi.go.jp/en/
- https://www.aisi.gov.uk/
- https://www.gov.uk/government/publications/frontier-ai-safety-commitments-ai-seoul-summit-2024
- https://www.anthropic.com/news/anthropics-responsible-scaling-policy
- https://openai.com/safety/preparedness

Yuto URL reachability check:
- NIST AI RMF: 200
- OECD AI Principles: 200
- ISO/IEC 42001 page: 200, redirected to /standard/42001
- Japan AISI root https://aisi.go.jp/: 200
- NIST GenAI profile URL reported by scout returned 404 in direct check; needs corrected official URL before formal citation.
- METI Japan AI Guidelines press URL timed out in direct check; re-check before formal citation.
- https://aisi.go.jp/en/ returned 404; use root or find correct English page before formal citation.

### 3. HR Performance & Receipt Analyst Scout

Focus:
- evaluating AI workers
- receipts
- supervision
- continuous monitoring
- keep / modify / retire lifecycle

Key update candidates:
- Measure outcome + verification + risk + traceability, not activity.
- Receipt should be the unit of performance review.
- Add fields for role_id, autonomy_level, risk, acceptance criteria, trace/evidence refs, verifier type, human intervention, policy violations, reviewer burden, lifecycle recommendation.
- Safety is a gate, not a weighted score.
- Avoid token count, message count, speed, self-reported confidence, and task count as primary KPIs.

Source URLs reported:
- https://github.com/openai/evals
- https://crfm.stanford.edu/helm/latest/
- https://www.swebench.com/
- https://github.com/THUDM/AgentBench
- https://huggingface.co/gaia-benchmark
- https://github.com/explodinggradients/ragas
- https://github.com/confident-ai/deepeval
- https://docs.smith.langchain.com/
- https://github.com/Arize-ai/phoenix
- https://opentelemetry.io/docs/concepts/semantic-conventions/
- https://owasp.org/www-project-top-10-for-large-language-model-applications/
- https://atlas.mitre.org/
- https://www.w3.org/TR/prov-overview/

Yuto URL reachability check:
- OpenTelemetry semantic conventions: 200
- OWASP Top 10 for LLM Applications: 200
- Some benchmark/tool URLs were not individually verified in this pass; verify before formal source notes.

## Integrated decision for HR v0.1

Adopt now as internal design:
- HR team exists as a design/control layer.
- HR workers are read-only or draft-only by default.
- Role charter, hiring funnel, autonomy ladder, activation checklist, receipts, and pruning policy are mandatory before worker activation.
- Culture & Safety Steward has stop-work recommendation authority; Kei/Yuto retain final authority.

Do not adopt yet:
- live HR runtime
- automated role activation
- real employee monitoring
- external employment/compliance claims
- formal legal policy based on scout URLs alone

## Next step

Create machine-readable HR role manifests and a validator after Kei accepts this v0.1 structure.
