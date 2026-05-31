# Hermes WebUI Baseline — Main PC WSL

## Runtime

- Hermes WebUI runs from WSL Ubuntu.
- Browser/client runs from Windows.
- Ollama runs natively on Main PC Windows.
- Hermes connects to Windows Ollama through LAN.

## Working model providers

### Cloud

- Provider: OpenRouter
- Model: DeepSeek V4 Flash
- Status: working

### Local LAN Ollama

- Provider: custom OpenAI-compatible endpoint
- Compatibility mode: Chat Completions
- Base URL: http://192.168.1.80:11434/v1
- API key: ollama
- Model tested: qwen36-27b-24k-coding:latest
- Status: working

## Available local models

- qwen36-27b-24k-coding:latest
- qwen36-27b-32k-coding:latest
- qwen36-27b-64k-coding:latest
- qwen3.6:27b

## Notes

- Ignore WSL host bridge route `10.255.255.254`; LAN IP route works.
- Do not install duplicate Ollama models inside WSL for now.
- Mac Hermes WebUI should later use the same LAN Ollama endpoint.