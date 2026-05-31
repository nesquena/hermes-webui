# Hermes Agent Setup Test

Date: 2026-06-01  
Task ID: HERM-WIN-GIT-001  
Machine: Main PC  
Runtime: Hermes WebUI from WSL  
Repo path from WSL: `/mnt/c/Users/josie/WebstormProjects/hermes-webui`

## Purpose

This file was created by Hermes/local Qwen as a smoke test.

The test proves that the model can:

- write into the Windows-mounted Hermes repo from WSL
- create a simple setup document
- commit the change on a branch
- push the branch to GitHub

## Current model topology

```txt
Main PC Windows
  └─ Ollama + Qwen models
      └─ LAN endpoint: http://192.168.1.80:11434
      └─ OpenAI-compatible endpoint: http://192.168.1.80:11434/v1

Main PC WSL
  └─ Hermes WebUI
      └─ uses Main PC Windows Ollama through LAN

Cloud fallback
  └─ DeepSeek V4 Flash / OpenRouter
```

## Local Qwen provider

```txt
Provider: Custom endpoint / OpenAI-compatible
Compatibility mode: Chat Completions
Base URL: http://192.168.1.80:11434/v1
API key: ollama
Model: qwen36-27b-24k-coding:latest
```

## Result

If this file is visible on GitHub in branch `herm-win-git-smoke-test`, the smoke test passed.