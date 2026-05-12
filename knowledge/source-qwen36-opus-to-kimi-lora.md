# Source: hotdogs/qwen3.6-35b-opus-to-kimi-lora

Checked: 2026-05-04
Source URL: https://huggingface.co/hotdogs/qwen3.6-35b-opus-to-kimi-lora
PDF URL checked without tracking params: https://huggingface.co/hotdogs/qwen3.6-35b-opus-to-kimi-lora/blob/main/paper.pdf
Local PDF inspection path: /tmp/qwen36_35b_opus_to_kimi_lora_paper.pdf
PDF sha256: 4496de341b81cc956013d35cab504fe7a245204dbd96dc2ee74fc6333739ed8e

## What it claims

A rank-16 LoRA adapter that shifts `lordx64/Qwen3.6-35B-A3B-Claude-4.7-Opus-Reasoning-Distilled` toward `lordx64/Qwen3.6-35B-A3B-Kimi-K2.6-Reasoning-Distilled` style by subtracting the two merged models and compressing selected attention-weight deltas via truncated SVD.

## Source facts verified

- Hugging Face model ID: `hotdogs/qwen3.6-35b-opus-to-kimi-lora`.
- HF API on 2026-05-04 showed: public, Apache-2.0, PEFT, text-generation, created 2026-05-02, last modified 2026-05-02, 273 downloads, 2 likes, not gated.
- Base adapter config points to `lordx64/Qwen3.6-35B-A3B-Claude-4.7-Opus-Reasoning-Distilled`.
- Files include `README.md`, `adapter_model.safetensors`, `adapter_config.json`, `qwen3.6-35b-opus-to-kimi-lora.gguf`, `extract_lora_diff.py`, `extraction_stats.json`, `paper.pdf`, `paper.md`, multilingual METHOD files.
- `adapter_config.json`: LoRA r=16, alpha=16, target modules `q_proj`, `k_proj`, `v_proj`, `o_proj`, task `CAUSAL_LM`.
- PDF metadata: 8 pages, created 2026-05-02 by LaTeX/pdfTeX.
- PDF claims: no training, tensor-by-tensor SVD, 44 attention tensors, ~145GB temporary disk, 186 seconds on 12 CPU cores/23GB RAM/no GPU, adapter 7.2MB PEFT / 14MB GGUF, mean thinking tokens 849 -> 2933.
- Source model APIs checked:
  - Opus source: `lordx64/Qwen3.6-35B-A3B-Claude-4.7-Opus-Reasoning-Distilled`, public, Apache-2.0, base model `Qwen/Qwen3.6-35B-A3B`, 26 safetensor shards, created 2026-04-18, downloads ~171,863 at check time.
  - Kimi source: `lordx64/Qwen3.6-35B-A3B-Kimi-K2.6-Reasoning-Distilled`, public, Apache-2.0, base model `Qwen/Qwen3.6-35B-A3B`, 26 safetensor shards, created 2026-04-26, downloads ~1,115 at check time.

## Assessment for Kei/Yuto

Useful idea: yes. Direct adoption/routing: no.

The technique is mathematically plausible when both models are same-architecture, same-base, LoRA-trained-and-merged. It is best treated as a model-delta/SVD experiment and a possible future sandbox candidate.

For Yuto operational routing, this should not replace Kei's verified MLX 27B OpenCode path. Current checked config `/Users/kei/tools/pai-opencode/opencode.json` uses MLX provider at `http://127.0.0.1:8097/v1` with `unsloth/Qwen3.6-27B-MLX-8bit`. `knowledge/rules.md` states Yuto local-LLM orchestration targets MLX/27B and forbids Ollama or 35B routing unless Kei explicitly changes it.

## Risks / cautions

- Not peer-reviewed; hosted as HF repo artifact.
- Claims about quality are mostly style/verbosity metrics, not full reasoning benchmark improvement.
- Increasing thinking tokens 3.5x may increase latency/cost and can be worse for Yuto's concise control-plane role.
- README includes uncensored/refusal-removal stacking examples. Do not use those in Yuto core.
- Running 35B locally is outside current Yuto routing policy unless Kei explicitly reverses it.
- To reproduce extraction, disk requirement is ~145GB and source models are large.
- Need independent eval before trusting: code, security, Thai, instruction-following, refusal/safety behavior, tool-use compatibility.

## Recommended next step

Do not install now. If Kei wants a pilot, do it as an isolated llama.cpp/GGUF test with non-core routing and a small eval harness. Success criteria should be measured task quality, not just longer chain-of-thought.

Related: [[sources]] [[memory-system]] [[source-mempalace]] [[second-brain-dashboard]]
