---
name: "source-command-triage-issues"
description: "Review all open GitHub issues, comment on each, and identify which are ready to fix"
---

# source-command-triage-issues

Use this skill when the user asks to run the migrated source command `triage-issues`.

## Command Template

# Triage Open Issues

Review all open issues on https://github.com/nesquena/hermes-webui and prepare a summary.

## Process

### 1. Fetch all open issues
```
gh issue list --state open --limit 30 --repo nesquena/hermes-webui --json number,title,author,createdAt
```

### 2. For each issue, gather details in parallel
- Fetch body and comments with `gh issue view <number> --json title,body,author,comments,state`
- Categorize: bug, feature request, question, or duplicate
- Check if there's already a PR addressing it
- Check if there are unanswered comments that need a response
- Check memory (project_key_contributors.md) for context on the reporter

### 3. Classify by actionability

**Ready to fix now** (clear root cause, small scope):
- List issue number, title, estimated complexity (low/medium/high)

**Needs investigation** (unclear root cause or scope):
- List what questions need answering

**Feature requests for roadmap** (good ideas, not urgent):
- Check if already in ROADMAP.md, add if missing

**Already addressed** (fixed but not closed):
- Close with a comment linking the fix PR

### 4. Respond to any unanswered issues
- Every open issue should have at least one response
- Thank reporters, acknowledge the issue, set expectations
- For bugs: confirm or ask for reproduction steps
- For features: note whether it's in the roadmap

### 5. Present summary to user
- Table of all open issues with category and recommendation
- Highlight which ones are ready for immediate /fix-issue treatment
- Note any that should be closed (already fixed, duplicate, etc.)
