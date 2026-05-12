# Chamin Team Prototype

Created: 2026-05-11 JST
Status: prototype v0.1

Purpose:
- Provide the first reusable Chamin employee-team prototype.
- Start with one proven workflow: `lawlabo-pr-review`.
- Keep Chamin as the accountable lead while specialist lanes provide bounded evidence.

Source playbook:
- [[chamin-employee-team-prototype-playbook]]

Rules:
- Chamin is the only user-facing synthesizer.
- Use the smallest sufficient lane set.
- Reviewer lanes are read-only.
- Only one writer may exist in a workflow; this prototype has no writer lane.
- Worker output is a receipt, not truth, until Chamin verifies evidence refs.
- High-risk security/auth/payment/data findings require explicit residual-risk reporting.
- No worker-to-worker handoff unless allowlisted and routed by Chamin.

Files:
- [[chamin-team/employee]]: canonical Chamin employee definition.
- [[chamin-team/cookbooks/lawlabo-pr-review]]: first workflow cookbook.
- `lanes/*.yaml`: bounded reviewer and QA lane contracts.
- `steering-examples.yaml`: examples that should trigger this workflow.
- `receipt-schema.yaml`: common worker receipt contract.
- `handoff-schema.yaml`: common handoff request contract.

Validation:

```bash
cd /Users/kei/kei-jarvis
python tools/validate_chamin_team.py --json
python -m pytest tests/test_chamin_team.py -q
```

Runtime CLI:

```bash
cd /Users/kei/kei-jarvis

# 1. Check whether the team prototype is usable.
python tools/chamin_team.py status --json

# 2. Route a real task into the smallest useful lane set.
python tools/chamin_team.py route "review this Next auth/payment PR" --json

# 3. Generate a work packet for Chamin to execute.
python tools/chamin_team.py packet \
  "review this Next auth/payment PR" \
  --target-ref "PR #123" \
  --scope next

# 4. Record whether team mode helped after the run.
python tools/chamin_team.py receipt \
  --task-id "lawlabo-pr-123-review" \
  --lanes "correctness-reviewer,data-auth-reviewer,security-reviewer" \
  --status partial \
  --notes "Found one auth boundary gap; needs follow-up." \
  --quality-gain medium \
  --safety-gain high \
  --reuse keep
```

Current prototype workflow:
- `lawlabo-pr-review`

Current lanes:
- `correctness-reviewer`
- `security-reviewer`
- `data-auth-reviewer`
- `frontend-ui-a11y-reviewer`
- `content-claims-seo-reviewer`
- `qa-critic`

Promotion rule:
- Use the prototype on 3-5 prospective LawLabo review tasks.
- Record whether it reduced missed issues, rework, or scope drift.
- Promote only if the receipts show real quality, safety, or speed gain.
