# AI News Radar

Purpose: recurring source-backed AI/news radar for Kei and Yuto.

Policy:
- Capture only concise, reusable signals: AI tools, GitHub repos, agent infrastructure, models, inference, evaluation, safety, and research that could affect Kei/Yuto workflows.
- Prefer primary sources: GitHub API/README/releases, official blogs/RSS, arXiv pages, Hugging Face model cards/blogs, vendor docs. Hacker News or social sources are discovery signals, not final evidence.
- Each entry should include checked timestamp, concise summary, why it matters, and citation URLs.
- Do not store secrets or private Telegram IDs here.

---

## 2026-05-07 14:56:00 UTC

สรุปเร็ว:
- รอบนี้มีสัญญาณใหม่ฝั่ง agent infra/safety มากกว่ารุ่นโมเดล: Docker sandboxing, GitHub agent validation, Gemini webhooks.
- OpenAI Codex และ Playwright MCP มี release ใหม่ใน GitHub metadata แต่รายละเอียด changelog ดึงผ่าน API ไม่ได้เพราะ GitHub rate limit; จึงบันทึกเป็นสัญญาณแบบจำกัดความมั่นใจ.
- งานวิจัย LongSeeker เสนอ `elastic context orchestration` สำหรับ long-horizon search agents; น่าอ่านสำหรับ memory/context policy ของ Yuto.

รายการสำคัญ:
1. Docker — Comparing Sandboxing Approaches for AI Agents. อธิบายความเสี่ยงเมื่อ agent มี write access และเปรียบเทียบ chroot/container/VM/microVM; มี framing ว่า agent environment ต้องเริ่มจาก isolation. Action: Read. URL: https://www.docker.com/blog/comparing-sandboxing-approaches-ai-agents/
2. Google Gemini API — Event-Driven Webhooks. เพิ่ม push-based notifications สำหรับ long-running Gemini jobs แทน polling; ใช้ signed headers และ at-least-once retry up to 24h ตามบทความ. Action: Watch/Try in sandbox. URL: https://blog.google/innovation-and-ai/technology/developers-tools/event-driven-webhooks/
3. GitHub Blog — Validating agentic behavior when “correct” isn’t deterministic. เสนอ Trust Layer สำหรับ Copilot Coding Agent/Computer Use โดยมอง execution เป็น graph และใช้ dominator analysis แยก essential outcomes จาก incidental path noise. Action: Read. URL: https://github.blog/ai-and-ml/generative-ai/validating-agentic-behavior-when-correct-isnt-deterministic/
4. Hugging Face/ServiceNow — vLLM V0 to V1 correctness in RL. บทเรียน migration: แก้ backend correctness/logprob/runtime/weight-update parity ก่อนปรับ RL objective; relevant ต่อ eval/serving ของ training loop. Action: Read. URL: https://huggingface.co/blog/ServiceNow-AI/correctness-before-corrections
5. Hugging Face — Open ASR Leaderboard private data / Benchmaxxer Repellant. เพิ่ม private high-quality datasets เพื่อลด leaderboard overfitting/contamination signal. Action: Watch. URL: https://huggingface.co/blog/open-asr-leaderboard-private-data
6. OpenAI Codex GitHub — release `rust-v0.129.0-alpha.15` published 2026-05-07. Repo metadata showed active pushes and new alpha release; changelog body not retrieved due GitHub API rate limit. Action: Watch. URLs: https://github.com/openai/codex/releases/tag/rust-v0.129.0-alpha.15 , https://api.github.com/repos/openai/codex
7. Microsoft Playwright MCP — release `v0.0.74` published 2026-05-06. Browser automation MCP remains active; changelog body not retrieved due GitHub API rate limit. Action: Watch/Try in sandbox only if browser-agent test need emerges. URL: https://github.com/microsoft/playwright-mcp/releases/tag/v0.0.74
8. arXiv — LongSeeker: Elastic Context Orchestration for Long-Horizon Search Agents. Introduces Context-ReAct operations (`Skip`, `Compress`, `Rollback`, `Snippet`, `Delete`) and reports BrowseComp/BrowseComp-ZH gains; paper claims need independent validation. Action: Read. URL: http://arxiv.org/abs/2605.05191v1

ควรทำต่อ:
- อ่าน Docker + GitHub validation เป็นคู่ แล้วสกัดเป็น checklist สำหรับ Yuto agent sandbox/eval gate.
- อ่าน LongSeeker เพื่อเทียบกับ Hermes/Yuto context compression และ memory routing.
- เฝ้า OpenAI Codex/Playwright MCP release notes รอบถัดไปเมื่อ GitHub API limit กลับมา.

ความเสี่ยง/สิ่งที่ยังไม่ชัวร์:
- GitHub release bodies บางรายการไม่ได้อ่านเพราะ API rate limit; ใช้ได้แค่เป็นสัญญาณ release metadata.
- arXiv LongSeeker เป็น paper ใหม่ ยังไม่ถือว่า benchmark claims ผ่าน independent replication.

Citations checked:
- https://www.docker.com/blog/comparing-sandboxing-approaches-ai-agents/
- https://blog.google/innovation-and-ai/technology/developers-tools/event-driven-webhooks/
- https://github.blog/ai-and-ml/generative-ai/validating-agentic-behavior-when-correct-isnt-deterministic/
- https://huggingface.co/blog/ServiceNow-AI/correctness-before-corrections
- https://huggingface.co/blog/open-asr-leaderboard-private-data
- https://github.com/openai/codex/releases/tag/rust-v0.129.0-alpha.15
- https://github.com/microsoft/playwright-mcp/releases/tag/v0.0.74
- http://arxiv.org/abs/2605.05191v1


## AI News Radar — 2026-05-09 00:56 JST

Source coverage: primary/near-primary feeds checked for Google AI Blog, Hugging Face Blog, GitHub Changelog/Copilot Changelog, AWS Machine Learning Blog, Ollama GitHub Releases, LangChain GitHub Releases, OpenAI RSS. arXiv API was rate-limited/timed out, so paper coverage is partial.

1. Google: “The Small Brief” uses AI with creative legends to make ads for small businesses. Why it matters: Google is pushing generative AI from demos into practical creative-production workflows for SMB marketing. Citation: https://blog.google/company-news/inside-google/company-announcements/the-small-brief/
2. AWS: Halliburton uses Amazon Bedrock and Generative AI for seismic workflow creation. Why it matters: a concrete enterprise/industrial Bedrock case for domain workflow generation, not only chat assistants. Citation: https://aws.amazon.com/blogs/machine-learning/halliburton-enhances-seismic-workflow-creation-with-amazon-bedrock-and-generative-ai/
3. Hugging Face: MedQA fine-tuning on AMD ROCm without CUDA. Why it matters: clinical fine-tuning examples on ROCm broaden the hardware path beyond NVIDIA/CUDA-only stacks. Citation: https://huggingface.co/blog/lablab-ai-amd-developer-hackathon/medqa
4. GitHub Copilot: upcoming deprecation of GPT-4.1. Why it matters: teams using Copilot/model pinning need to check fallback behavior and model migration timelines. Citation: https://github.blog/changelog/2026-05-07-upcoming-deprecation-of-gpt-4-1/
5. GitHub Copilot: Claude Sonnet 4 deprecated. Why it matters: another model lifecycle change that can affect coding-agent quality, reproducibility, and enterprise allowlists. Citation: https://github.blog/changelog/2026-05-07-claude-sonnet-4-deprecated/
6. Ollama v0.23.2 released. Why it matters: /api/show response caching claims ~6.7x median latency improvement, improving local-model integrations such as VS Code; launch integration behavior also changed. Citation: https://github.com/ollama/ollama/releases/tag/v0.23.2
7. AWS: secure short-term GPU capacity for ML workloads with EC2 Capacity Blocks for ML and SageMaker training plans. Why it matters: capacity reservation remains a practical bottleneck for training/eval jobs. Citation: https://aws.amazon.com/blogs/machine-learning/secure-short-term-gpu-capacity-for-ml-workloads-with-ec2-capacity-blocks-for-ml-and-sagemaker-training-plans/
8. AWS: verifiable rewards-based RL with GRPO on SageMaker AI. Why it matters: GRPO/verifiable reward workflows are moving into managed-cloud implementation patterns. Citation: https://aws.amazon.com/blogs/machine-learning/overcoming-reward-signal-challenges-verifiable-rewards-based-reinforcement-learning-with-grpo-on-sagemaker-ai/
9. AWS: Amazon Bedrock AgentCore payments with Coinbase and Stripe. Why it matters: agent infrastructure is adding transaction/payment rails, raising both product opportunity and safety/compliance needs. Citation: https://aws.amazon.com/blogs/machine-learning/agents-that-transact-introducing-amazon-bedrock-agentcore-payments-built-with-coinbase-and-stripe/
10. LangChain 1.2.18 released. Why it matters: small but relevant agent-framework maintenance release; notes include reverting `ls_agent_type` tag on `create_agent` calls and deprecating/limiting hub loads/dumps in `langchain-classic`. Citation: https://github.com/langchain-ai/langchain/releases/tag/langchain%3D%3D1.2.18


## AI news radar — 2026-05-09 13:00 JST

Source coverage: partial. Checked primary/near-primary feeds: OpenAI RSS, Google AI RSS, Hugging Face blog feed, GitHub Changelog feed, arXiv API, selected GitHub releases. Anthropic/Mistral/Meta/DeepMind RSS endpoints tried but unavailable/404; arXiv cs.AI timed out and was skipped per cron reliability rules.

1. OpenAI — Running Codex safely at OpenAI.
   - Why it matters: OpenAI published concrete operational controls for coding agents: sandboxing, approvals, network policies, and agent-native telemetry, useful for safe agent adoption.
   - Citation: https://openai.com/index/running-codex-safely

2. Google — The Small Brief uses AI for small-business ads.
   - Why it matters: Google is pushing generative AI from demos into SMB advertising workflows, with creative production as the wedge.
   - Citation: https://blog.google/company-news/inside-google/company-announcements/the-small-brief/

3. Hugging Face — CyberSecQwen-4B.
   - Why it matters: Reinforces the trend toward small, specialized, locally-runnable defensive cybersecurity models instead of only frontier general models.
   - Citation: https://huggingface.co/blog/lablab-ai-amd-developer-hackathon/cybersecqwen-4b

4. Hugging Face / AllenAI — EMO: Pretraining mixture of experts for emergent modularity.
   - Why it matters: MoE pretraining and emergent modularity remain active research paths for more efficient and interpretable scaling.
   - Citation: https://huggingface.co/blog/allenai/emo

5. GitHub — Upcoming deprecation of Grok Code Fast 1.
   - Why it matters: Copilot users relying on Grok Code Fast 1 need to migrate before May 15 across chat, inline edits, ask/agent modes, and completions.
   - Citation: https://github.blog/changelog/2026-05-08-upcoming-deprecation-of-grok-code-fast-1

6. GitHub — Copilot code review comment types in usage metrics API.
   - Why it matters: Teams can now measure Copilot code-review activity by suggestion/comment type, improving AI code-review governance.
   - Citation: https://github.blog/changelog/2026-05-08-copilot-code-review-comment-types-now-in-usage-metrics-api

7. GitHub — More flexible secrets and variables for Copilot cloud agent.
   - Why it matters: Background coding agents need scoped environment configuration; this update makes Copilot cloud-agent delegation easier but raises secret-handling review needs.
   - Citation: https://github.blog/changelog/2026-05-08-more-flexible-secrets-and-variables-for-copilot-cloud-agent

8. GitHub — CodeQL 2.25.3 adds Swift 6.3 support.
   - Why it matters: Static-analysis coverage expands for Swift projects; relevant to AI-assisted coding because generated code still needs scanning gates.
   - Citation: https://github.blog/changelog/2026-05-08-codeql-2-25-3-adds-swift-6-3-support

9. GitHub — Upcoming deprecation of GPT-4.1 in Copilot.
   - Why it matters: Another model-lifecycle change in developer tooling; teams should check pinned Copilot model assumptions and migration timelines.
   - Citation: https://github.blog/changelog/2026-05-07-upcoming-deprecation-of-gpt-4-1


## AI news radar — 2026-05-10 01:03 JST

Source coverage: partial but bounded. Checked primary/near-primary sources: GitHub releases for openai/codex, google/adk-python, modelcontextprotocol/python-sdk, microsoft/playwright-mcp, langchain-ai/langchain, ollama/ollama; official feeds for GitHub Changelog/AI Blog, Google AI Blog, AWS ML Blog, OpenAI RSS; arXiv API; Hugging Face API. No attempt was made to create/modify cron jobs. Several official docs/feed endpoints were unavailable, rate-limited, or blocked and were skipped.

รอบนี้คัดได้ 3 ข่าวที่น่าเชื่อถือ:

1. OpenAI Codex CLI `rust-v0.130.0` released (published 2026-05-08T23:09:55Z).
   - What changed: adds plugin hook visibility/sharing metadata, `codex remote-control` for headless remote-control app-server entry, paged app-server thread views, Bedrock auth via `aws login` profiles, multi-environment `view_image`, plus fixes for live config reload, patch diff accuracy, remote compaction, Windows sandbox access, and telemetry/review analytics.
   - Why it matters: Codex is moving toward remotely controlled, observable agent infrastructure rather than only an interactive CLI.
   - Citation: https://github.com/openai/codex/releases/tag/rust-v0.130.0

2. Google ADK Python `v1.33.0` released (published 2026-05-08T21:08:38Z).
   - What changed: adds `BufferableSessionService`, configurable ADK environment-tools truncation limit, `get_function_calls`/`get_function_responses` on `LlmResponse`, Apigee credential injection, hot reload fixes, clearer sandbox/auth errors, and session/state bug fixes.
   - Why it matters: Google’s agent framework is tightening session, tool-output, and function-call handling—core reliability areas for production agents.
   - Citation: https://github.com/google/adk-python/releases/tag/v1.33.0

3. MCP Python SDK `v1.27.1` released (published 2026-05-08T16:44:58Z).
   - What changed: fixes Pydantic 2.13 output-schema generation, OAuth client metadata empty-string URL coercion, restricts `httpx <1.0.0`, and imports `SSEError` from the public `httpx_sse` API.
   - Why it matters: small but important compatibility/security hygiene update for Python MCP servers and clients.
   - Citation: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.27.1

Skipped/low-signal this round:
- `llama.cpp` b9090 was current but only a BoringSSL CMake update; treated as tiny patch.
- Vercel AI SDK releases around this window were dependency-only gateway patches; treated as low signal.
- GitHub Copilot/GitHub AI blog items from May 8 were already covered in the previous radar entry.
- arXiv latest API results were mostly 2026-05-07 submissions or already covered; not counted as fresh 12–24h updates.


## AI news radar — 2026-05-10 13:06 JST

Coverage: source coverage partial. Checked bounded primary/near-primary sources: OpenAI RSS, Google AI Blog RSS, Hugging Face Blog/RSS/API, arXiv API, GitHub releases for selected AI infra repos. Within the last ~12-24h, 2 meaningful verified updates were selected; older/tiny patch releases and marketing-only items were skipped.

1. Hugging Face published “OncoAgent: A Dual-Tier Multi-Agent Framework for Privacy-Preserving Oncology Clinical Decision Support” (published May 9, 2026).
   - Why it matters: It is a concrete multi-agent clinical-AI architecture using LangGraph/RAG/QLoRA with explicit human-in-the-loop, per-patient memory isolation, and Zero-PHI privacy posture; useful signal for safe domain-agent design, but clinical claims remain paper-level and need independent validation before use.
   - Citation: https://huggingface.co/blog/lablab-ai-amd-developer-hackathon/oncoagent-official-paper

2. EMO paper/source package surfaced on Hugging Face/arXiv: “Pretraining Mixture of Experts for Emergent Modularity” (arXiv v1 May 7; Hugging Face paper activity May 8/9).
   - Why it matters: The paper reports a 1B-active/14B-total MoE trained so expert subsets become modular; authors claim only ~1% absolute drop using 25% of experts and ~3% drop using 12.5%, pointing toward more memory-efficient/composable inference if reproduced.
   - Citations: https://arxiv.org/abs/2605.06663 and https://huggingface.co/papers/2605.06663

Skipped / not selected:
- OpenAI RSS latest items were strong but dated May 7-8 UTC, outside the requested ~12-24h window for this run.
- GitHub releases such as llama.cpp b9093 were recent but looked like routine release-build artifacts, not a clearly meaningful AI update from release notes.


## AI news radar — 2026-05-11 01:08 JST

Coverage: source coverage partial. Checked bounded primary/near-primary sources: OpenAI RSS, Google AI Blog RSS, Google DeepMind RSS, Hugging Face Blog/API, arXiv API, and GitHub releases for selected AI infra/repos including vLLM, llama.cpp, Transformers, Ollama, OpenAI Agents Python, MCP Python SDK, Gemini CLI, Codex, DSPy, LiteLLM, LangChain, SGLang, TensorRT-LLM, Open WebUI. Anthropic RSS returned 404; arXiv broad query initially returned 429 and a smaller retry showed older May 7 papers, so paper coverage is limited. Within the last ~12-24h, 2 meaningful verified updates were selected; tiny/older/duplicate releases were skipped.

รอบนี้คัดได้ 2 ข่าวที่น่าเชื่อถือ:

1. vLLM `v0.20.2` released (published 2026-05-10T07:37:57Z).
   - What changed: small patch release with bug fixes for DeepSeek V4, `gpt-oss`, and Qwen3-VL; notes specifically mention DeepSeek V4 sparse-attention/KV-cache fixes and CUDA graph capture behavior.
   - Why it matters: DeepSeek/Qwen serving stacks are still stabilizing at the inference-runtime layer; if using vLLM with these models, this is an upgrade candidate but should be canaried first.
   - Citation: https://github.com/vllm-project/vllm/releases/tag/v0.20.2

2. llama.cpp build `b9095` released (published 2026-05-10T09:43:20Z).
   - What changed: adds an internal CUDA AllReduce provider for tensor parallelism, intended as a NCCL-free reduction path for `LLAMA_SPLIT_MODE_TENSOR`.
   - Why it matters: llama.cpp continues moving beyond single-device local inference toward more serious multi-GPU/tensor-parallel serving paths, but this is a fast-moving build-level release and should be treated as experimental.
   - Citation: https://github.com/ggml-org/llama.cpp/releases/tag/b9095

Skipped / not selected:
- llama.cpp `b9094` was current but looked like a narrow model-type check fix; not counted as a separate meaningful briefing item.
- OpenAI/Google/Hugging Face blog items available in feeds were mostly May 7-9 or already covered in previous radar entries.
- Several GitHub releases within the broader window were patch/security/dependency maintenance only, or older than the last radar window.


## AI news radar — 2026-05-11 13:11 JST

Coverage: source coverage partial. Checked bounded primary/near-primary sources: OpenAI RSS, Google AI RSS, Hugging Face Blog/RSS/API daily papers, GitHub Changelog RSS, AWS ML RSS, and GitHub releases for selected AI infrastructure repos including vLLM, llama.cpp, Ollama, OpenAI Codex, Google ADK Python, MCP Python SDK, OpenAI Agents Python, LangChain, Transformers, SGLang, LiteLLM, Vercel AI, CrewAI, MLX-LM, AutoGen, LlamaIndex, Semantic Kernel, TensorRT-LLM. OpenAI/AWS feeds required bounded partial parsing; arXiv API timed out for selected paper IDs, so paper coverage uses Hugging Face Papers pages plus arXiv-style IDs and is marked as research signal. Within the last ~12-24h, 8 meaningful verified or near-primary updates were selected; tiny/duplicate patches were skipped.

รอบนี้คัดได้ 8 ข่าวที่น่าเชื่อถือ:

1. Hugging Face published “MachinaCheck: Building a Multi-Agent CNC Manufacturability System on AMD MI300X” (RSS published 2026-05-10T18:44:11Z).
   - Why it matters: a concrete domain-specific multi-agent workflow for manufacturing/CNC on AMD MI300X, useful as evidence that agent orchestration is moving into industrial decision support rather than generic chat only.
   - Citation: https://huggingface.co/blog/lablab-ai-amd-developer-hackathon/machinacheck

2. llama.cpp `b9100` released (published 2026-05-10T20:06:59Z).
   - What changed: backend sampling now supports returning post-sampling probabilities; release notes also mention avoiding `0.0` post-sampling probabilities in server responses.
   - Why it matters: token probability visibility is useful for local inference diagnostics, evaluation, and downstream calibration, though this is still a fast-moving build release.
   - Citation: https://github.com/ggml-org/llama.cpp/releases/tag/b9100

3. llama.cpp `b9101` released (published 2026-05-10T20:27:51Z).
   - What changed: server prints a warning when HTTP timeout is exceeded.
   - Why it matters: small operational observability improvement for local/edge inference servers; counted because timeout visibility matters for agent loops using local model endpoints.
   - Citation: https://github.com/ggml-org/llama.cpp/releases/tag/b9101

4. Hugging Face Papers surfaced “HyperEyes: Dual-Grained Efficiency-Aware Reinforcement Learning for Parallel Multimodal Search Agents” (submitted to HF daily 2026-05-11T01:51:54Z; paper ID 2605.07177).
   - Why it matters: points to multimodal agents that parallelize tool/search calls rather than only extending sequential reasoning, with relevance to agent efficiency research; claims need paper-level validation.
   - Citation: https://huggingface.co/papers/2605.07177

5. Hugging Face Papers surfaced “Beyond Retrieval: A Multitask Benchmark and Model for Code Search” / CoREB (submitted to HF daily 2026-05-11T01:18:58Z; paper ID 2605.04615).
   - Why it matters: evaluates code search beyond first-stage retrieval with reranking and developer-style queries, a useful direction for coding-agent retrieval benchmarks.
   - Citation: https://huggingface.co/papers/2605.04615

6. Hugging Face Papers surfaced “UniSD: Towards a Unified Self-Distillation Framework for Large Language Models” (submitted to HF daily 2026-05-11T01:16:10Z; paper ID 2605.06597).
   - Why it matters: self-distillation without stronger external teachers is an important efficiency/adaptation path, but generated-rationale quality and task-dependent correctness remain validation risks.
   - Citation: https://huggingface.co/papers/2605.06597

7. Hugging Face Papers surfaced “TextLDM: Language Modeling with Continuous Latent Diffusion” (submitted to HF daily 2026-05-11T02:35:19Z; paper ID 2605.07748).
   - Why it matters: explores diffusion-style latent generation for text, a non-autoregressive/alternative architecture signal to watch if it improves controllability or sampling tradeoffs.
   - Citation: https://huggingface.co/papers/2605.07748

8. Hugging Face Papers surfaced “Flow-OPD: On-Policy Distillation for Flow Matching Models” (submitted to HF daily 2026-05-11T01:20:36Z; paper ID 2605.08063).
   - Why it matters: tackles reward sparsity/interference and reward hacking in aligned text-to-image/flow-matching models, relevant to evaluation and multi-objective alignment.
   - Citation: https://huggingface.co/papers/2605.08063

Skipped / not selected:
- vLLM `v0.20.2`, Ollama `v0.23.2`, Google ADK Python `v1.33.0`, MCP Python SDK `v1.27.1`, GitHub Copilot May 8 changelog items, and OpenAI Codex safety posts were already covered in prior radar entries or outside the freshest window.
- Many recent GitHub releases were dependency-only, alpha tags with no useful changelog body, or tiny maintenance patches; they were not counted as meaningful AI updates.
- arXiv API timed out for selected IDs; paper details should be treated as near-primary HF Papers/RSS coverage until arXiv pages are rechecked.


## AI news radar — 2026-05-12 01:14 JST

Coverage: source coverage partial. Checked bounded primary/near-primary sources: OpenAI RSS, Google AI RSS, AWS Machine Learning RSS, Hugging Face blog/API/daily papers, arXiv abs pages for selected IDs, and GitHub releases for selected AI infrastructure repos including llama.cpp, Vercel AI SDK, OpenAI Agents Python, Microsoft Semantic Kernel, OpenAI Codex, vLLM, Ollama, LangChain, Transformers, SGLang, LiteLLM, CrewAI, Gemini CLI, MCP Python SDK, Playwright MCP, TensorRT-LLM, DSPy, Pydantic AI. DeepMind RSS returned 404; arXiv API timed out once, so selected arXiv pages were checked directly. Within the last ~12-24h, 9 meaningful verified updates were selected; tiny, duplicate, alpha-without-changelog, and older items were skipped.

รอบนี้คัดได้ 9 ข่าวที่น่าเชื่อถือ:

1. OpenAI published “How enterprises are scaling AI” (RSS 2026-05-11 10:00 UTC).
   - Why it matters: OpenAI is emphasizing production AI maturity—trust, governance, workflow design, and quality-at-scale—rather than isolated demos.
   - Citation: https://openai.com/business/guides-and-resources/how-enterprises-are-scaling-ai

2. OpenAI launched DeployCo (RSS 2026-05-11 06:00 UTC).
   - Why it matters: OpenAI is adding an enterprise deployment company around frontier-AI implementation, signaling more focus on production integration and measurable business impact.
   - Citation: https://openai.com/index/openai-launches-the-deployment-company

3. Google expanded AI-powered Google Finance to Europe (RSS 2026-05-11 06:00 UTC).
   - Why it matters: AI search/product experiences are moving into finance workflows with local-language European rollout, raising the bar for consumer financial information UX.
   - Citation: https://blog.google/products-and-platforms/products/search/ai-powered-google-finance-in-europe/

4. AWS published Amazon Quick enterprise data-to-AI decision workflow (RSS 2026-05-11 15:56 UTC).
   - Why it matters: AWS is packaging enterprise data access, trusted insights, and AI-powered decision support as a concrete platform workflow, not just model hosting.
   - Citation: https://aws.amazon.com/blogs/machine-learning/amazon-quick-accelerating-the-path-from-enterprise-data-to-ai-powered-decisions/

5. OpenAI Agents Python `v0.17.1` released (published 2026-05-11 06:56 UTC).
   - Why it matters: fixes include sandbox provider error detail, archive-extraction limits, and Git repo subpath validation—practical hardening for agent sandbox/file access.
   - Citation: https://github.com/openai/openai-agents-python/releases/tag/v0.17.1

6. Microsoft Semantic Kernel `.NET 1.76.0` released (published 2026-05-11 09:37 UTC).
   - Why it matters: release notes include hardened CloudDrivePlugin defaults, path validation, OpenAPI input validation, gRPC plugin address handling, and ImageContent in tool/function results.
   - Citation: https://github.com/microsoft/semantic-kernel/releases/tag/dotnet-1.76.0

7. llama.cpp `b9106` released (published 2026-05-11 13:26 UTC).
   - Why it matters: adds Vulkan support for asymmetric flash attention paths, a useful local/edge inference backend improvement, though build releases should be treated as experimental.
   - Citation: https://github.com/ggml-org/llama.cpp/releases/tag/b9106

8. Vercel AI SDK `ai@7.0.0-canary.131` released (published 2026-05-11 14:46 UTC).
   - Why it matters: canary notes add `instructions` as `prepareStep` input and flexible tool descriptions, both relevant to agent/tool-call orchestration design.
   - Citation: https://github.com/vercel/ai/releases/tag/ai%407.0.0-canary.131

9. arXiv paper “What if AI systems weren't chatbots?” was checked directly (submitted 2026-05-08; surfaced in HF daily papers during this window).
   - Why it matters: argues chatbot interfaces are not neutral and may impose sociotechnical costs; useful as a research signal for designing AI systems beyond chat UI.
   - Citations: https://arxiv.org/abs/2605.07896 and https://huggingface.co/papers/2605.07896

Skipped / not selected:
- OpenAI Campus Network was AI-adjacent but not a technical/platform update.
- OpenAI Codex `rust-v0.131.0-alpha.6` was current but release body only said “Release 0.131.0-alpha.6”; not enough meaningful changelog detail.
- llama.cpp `b9105` and `b9103` looked like narrow dependency/include maintenance; not counted separately.
- Vercel AI SDK package-split canaries that only repeated dependency updates were not counted separately.
- Several Hugging Face model/API results were older than the requested window or low-confidence recency signals.


## AI news radar — 2026-05-12 13:19 JST

Source coverage: partial but useful. Checked primary/near-primary sources with bounded requests: OpenAI RSS, Google AI RSS, GitHub Changelog/AI feeds, Hugging Face blog feed, AWS Machine Learning blog feed, selected GitHub releases. arXiv API returned HTTP 429 and was skipped. Skipped tiny/no-body alpha releases where practical change was unclear.

1. OpenAI — How ChatGPT adoption broadened in early 2026 (published 2026-05-11 15:00 GMT).
   - Why it matters: OpenAI reports broader mainstream adoption in Q1 2026, including fastest growth among users over 35 and more balanced gender usage, indicating AI use is spreading beyond early adopters.
   - Citation: https://openai.com/signals/research/2026q1-update

2. OpenAI — How enterprises are scaling AI (published 2026-05-11 10:00 GMT).
   - Why it matters: OpenAI frames successful enterprise AI as governance, trust, workflow design, and quality at scale rather than isolated chatbot pilots.
   - Citation: https://openai.com/business/guides-and-resources/how-enterprises-are-scaling-ai

3. OpenAI — DeployCo launched for enterprise AI deployment (published 2026-05-11 06:00 GMT).
   - Why it matters: OpenAI is adding a services/deployment layer to help companies move frontier AI into production, suggesting demand is shifting from model access to implementation capability.
   - Citation: https://openai.com/index/openai-launches-the-deployment-company

4. AWS — Building web search-enabled agents with Strands and Exa (published 2026-05-11 21:58 GMT).
   - Why it matters: AWS shows a concrete pattern for agentic web-search tools inside Strands Agents, useful for multi-step research/search agents.
   - Citation: https://aws.amazon.com/blogs/machine-learning/building-web-search-enabled-agents-with-strands-and-exa/

5. AWS — Claude Platform on AWS generally available (published 2026-05-11 18:43 GMT).
   - Why it matters: Anthropic's native Claude Platform can now be accessed through AWS accounts, tightening enterprise procurement/infrastructure paths for Claude.
   - Citation: https://aws.amazon.com/blogs/machine-learning/introducing-claude-platform-on-aws-anthropics-native-platform-through-your-aws-account/

6. AWS — Amazon Nova Multimodal Embeddings for manufacturing intelligence (published 2026-05-11 17:08 GMT).
   - Why it matters: AWS demonstrates multimodal retrieval over aerospace manufacturing documents using Amazon Bedrock and S3 Vectors, showing document+image embedding use in industrial settings.
   - Citation: https://aws.amazon.com/blogs/machine-learning/manufacturing-intelligence-with-amazon-nova-multimodal-embeddings/

7. AWS — Miro uses Amazon Bedrock for bug routing (published 2026-05-11 17:03 GMT).
   - Why it matters: Miro reports six times fewer team reassignments and five times shorter time-to-resolution from AI-assisted bug routing, a practical software-ops GenAI workflow.
   - Citation: https://aws.amazon.com/blogs/machine-learning/how-miro-uses-amazon-bedrock-to-boost-software-bug-routing-accuracy-and-improve-time-to-resolution-from-days-to-hours/

8. Google — AI-powered Google Finance expands to Europe (published 2026-05-11 06:00 GMT).
   - Why it matters: Google is moving AI search/analysis features into consumer finance workflows with local language support across Europe.
   - Citation: https://blog.google/products-and-platforms/products/search/ai-powered-google-finance-in-europe/

9. Hugging Face / AWS — Building Blocks for Foundation Model Training and Inference on AWS (published 2026-05-11 23:18 GMT).
   - Why it matters: Hugging Face and AWS are documenting stack components for foundation-model training/inference, useful for teams choosing between managed and open tooling.
   - Citation: https://huggingface.co/blog/amazon/foundation-model-building-blocks

10. LangChain — langchain-core 1.4.0 released (published 2026-05-11 18:42 GMT).
   - Why it matters: The release includes dependency/security-maintenance changes and a fix to avoid eager pydantic.v1 import in deprecated paths; relevant for agent apps depending on LangChain core.
   - Citation: https://github.com/langchain-ai/langchain/releases/tag/langchain-core%3D%3D1.4.0

Notes:
- arXiv coverage unavailable this run due HTTP 429.
- GitHub releases checked included OpenAI Codex, Google ADK Python, MCP Python SDK, Playwright MCP, Ollama, and LangChain; Codex/Ollama current items were alpha/RC or too small for the main brief except LangChain core.


## AI news radar — 2026-05-13 01:21 JST

Source coverage: partial but useful. Checked primary/near-primary sources with bounded requests: OpenAI RSS, Google AI RSS, GitHub Changelog/AI feeds, Hugging Face blog feed, AWS Machine Learning blog feed, DeepMind RSS, selected GitHub releases, and arXiv API. Anthropic/Mistral/Meta RSS endpoints returned 404; Google Developers search page was HTML not RSS. Skipped no-body alpha/canary releases and duplicate items from the previous brief.

1. AWS — Automate schema generation for intelligent document processing (published 2026-05-12 15:54 UTC).
   - Why it matters: AWS shows automated document clustering and schema generation as a pre-processing layer for IDP, reducing manual setup for multi-document AI pipelines.
   - Citation: https://aws.amazon.com/blogs/machine-learning/automate-schema-generation-for-intelligent-document-processing/

2. AWS — Navigating EU AI Act requirements for LLM fine-tuning on Amazon SageMaker AI (published 2026-05-12 15:48 UTC).
   - Why it matters: The post operationalizes EU AI Act-style audit readiness with FLOPs tracking and documentation during LLM fine-tuning.
   - Citation: https://aws.amazon.com/blogs/machine-learning/navigating-eu-ai-act-requirements-for-llm-fine-tuning-on-amazon-sagemaker-ai/

3. GitHub Blog — Dungeons & Desktops: Building a procedurally generated roguelike with GitHub Copilot CLI (published 2026-05-12 15:00 UTC).
   - Why it matters: GitHub is demonstrating Copilot CLI as an extensible creative/developer automation surface, not only a Q&A coding assistant.
   - Citation: https://github.blog/ai-and-ml/github-copilot/dungeons-desktops-building-a-procedurally-generated-roguelike-with-github-copilot-cli/

4. LangChain — langchain==1.3.0 released (published 2026-05-12 14:46 UTC).
   - Why it matters: Adds `version="v3"` support in `stream_events` / `astream_events` for LangChain agents, improving event-streaming observability surfaces.
   - Citation: https://github.com/langchain-ai/langchain/releases/tag/langchain%3D%3D1.3.0

5. llama.cpp — b9119/b9116 releases (published 2026-05-12 12:46–15:49 UTC).
   - Why it matters: Recent builds include Intel Xe2 Vulkan BF16 performance-regression fix and MiMo v2.5 vision support, relevant to local/edge multimodal inference.
   - Citations: https://github.com/ggml-org/llama.cpp/releases/tag/b9119 , https://github.com/ggml-org/llama.cpp/releases/tag/b9116

6. Vercel AI SDK — @ai-sdk/mcp@1.0.42 released (published 2026-05-11 20:44 UTC).
   - Why it matters: Exposes MCP server instructions to clients and fixes negotiated protocol-version headers, a practical interoperability improvement for MCP tool clients.
   - Citation: https://github.com/vercel/ai/releases/tag/%40ai-sdk/mcp%401.0.42

7. Ollama — v0.23.3-rc1 released (published 2026-05-12 03:48 UTC).
   - Why it matters: Release candidate refines MLX model push behavior, hardens update flows, and strengthens integration tests; useful but still RC, not stable.
   - Citation: https://github.com/ollama/ollama/releases/tag/v0.23.3-rc1

8. arXiv — Shepherd: A Runtime Substrate Empowering Meta-Agents with a Formalized Execution Trace (published 2026-05-11 17:50 UTC).
   - Why it matters: Proposes typed, replayable execution traces for meta-agents, with claims about faster fork/replay and prompt-cache reuse that merit follow-up validation.
   - Citation: https://arxiv.org/abs/2605.10913v1

9. arXiv — Dynamic Skill Lifecycle Management for Agentic Reinforcement Learning (published 2026-05-11 17:55 UTC).
   - Why it matters: Treats external skills as a lifecycle-management problem for agents, directly relevant to long-running agent memory/skill hygiene.
   - Citation: https://arxiv.org/abs/2605.10923v1

10. arXiv — DECO: Sparse Mixture-of-Experts with Dense-Comparable Performance on End-Side Devices (published 2026-05-11 17:58 UTC).
   - Why it matters: Targets MoE deployment bottlenecks on end-side devices, a useful signal for efficient local inference architecture.
   - Citation: https://arxiv.org/abs/2605.10933v1

Skipped / not selected:
- OpenAI RSS latest entries were duplicates from the previous brief.
- OpenAI Codex `rust-v0.131.0-alpha.*` bodies only said `Release ...`; not enough practical changelog detail.
- Very small dependency/security-build patches were skipped unless they changed agent/inference behavior.
