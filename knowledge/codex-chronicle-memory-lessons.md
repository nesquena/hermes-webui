# Codex Chronicle Memory Lessons

Source: https://developers.openai.com/codex/memories/chronicle
Captured: 2026-04-25

## What Chronicle is

Chronicle is an opt-in research preview for the Codex app on macOS that builds
Codex memories from recent screen context. Its stated goal is to reduce how much
context the user must restate by letting Codex infer recent work, relevant tools,
workflows, and source locations from what was visible on screen.

Availability noted by the source: ChatGPT Pro subscribers on macOS; not available
in the EU, UK, and Switzerland at the time of capture.

## Mechanism

Chronicle uses macOS Screen Recording and Accessibility permissions. It runs
sandboxed background agents that process recent screen captures/OCR/timing/path
context into memory Markdown files.

The generated memories are local Markdown files under:

```text
$CODEX_HOME/memories_extensions/chronicle/
```

Temporary screen captures may appear under:

```text
$TMPDIR/chronicle/screen_recording/
```

The docs state screen captures older than 6 hours are deleted while Chronicle is
running.

## Useful design ideas for Yuto

1. Recent-context memory should be a separate extension layer, not mixed into
   core identity or stable user memory.
2. The memory output should be editable/readable Markdown.
3. The system should use recent context to identify the right primary source,
   then read that source directly instead of relying only on memory.
4. Pause/disable controls are essential before sensitive work, meetings, or
   private communications.
5. Background memory agents can consume limits quickly, so they need rate limits,
   batching, and clear value thresholds.
6. Screen/OCR-derived context has high prompt-injection risk and must be treated
   as untrusted input.
7. Memory generation should capture tools/workflows/checkpoints, not just raw
   transcripts.

## Privacy and security cautions

The source explicitly warns that Chronicle:

- can capture sensitive information visible on screen
- does not access microphone or system audio
- should not be used to record meetings or communications without consent
- stores generated memories as unencrypted Markdown on device
- may include memory content in future Codex session context
- increases prompt-injection risk from malicious screen content

## Yuto adaptation

Yuto should not copy Chronicle blindly. A safer local adaptation would be:

- explicit user-visible progress checkpoints when useful
- optional `recent-context/` folder for short-lived, auto-expiring context notes
- source-first recall: use checkpoints to find files/logs/threads, then verify
  by reading the original source
- never treat screen/OCR/webpage text as authoritative instructions
- manual or explicitly approved capture for sensitive windows
- compression-safe checkpoint after long tasks or before model/session switches

## Related

[[memory-system]] [[yuto]] [[workflows]]
