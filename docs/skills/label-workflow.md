---
id: label-workflow
name: Label Workflow
one_line_purpose: Issue lifecycle, label taxonomy, and human-agent handoff signals.
entry_point: docs/skills/label-workflow.md
category: meta
status: active
tags:
  - workflow
  - labels
  - issue-tracking
  - governance
description: Use when finding, claiming, labeling, or commenting on projectbluefin work items.
version: "1.0"
last_updated: "2026-08-01"
metadata:
  type: procedure
---

# Label Workflow — dakota-iso

## Canonical policy

The shared [Project Bluefin label workflow](https://github.com/projectbluefin/common/blob/main/docs/skills/label-workflow.md)
defines the lifecycle and label taxonomy. Do not maintain a local fork of those
definitions.

Humans triage and approve work. Agents work only on human-approved tasks and
must not self-apply priority, queue, or claim labels.

Before relying on a shared label, confirm that the repository's labels have
been synchronized:

```bash
gh label list --repo projectbluefin/dakota-iso --limit 200 --json name
gh search issues --label "status/queued" --owner projectbluefin --state open
```

If a required shared label is absent, do not replace it with a legacy local
label. Report the synchronization gap to a maintainer and wait for direction.

---

## When Not to Use

Do not use this procedure to bypass human triage, claim an unapproved issue,
or create labels locally. For repository-specific implementation guidance, use
the relevant local skill instead.

## Core Process

1. Read the shared label workflow for the canonical lifecycle.
2. Verify the target repository exposes the required shared labels.
3. Work only after human approval and follow the shared claim procedure.
4. If labels are out of sync, report the gap and wait rather than using a
   legacy substitute.

## PR comment policy

Inherited from the [org-level agentic model](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md):

- **One comment per PR event, max.** Combine all findings into one comment.
- Never duplicate GitHub UI state (approvals, CI status, labels).
- `@` mentions only when asking someone to do something specific.
- When in doubt, post nothing. **Never post multiple comments on the same issue/PR.**

## Red Flags

- Using `queue/*` labels as substitutes for the shared `status/*` lifecycle
- Self-applying priority, queue, or claim labels
- Posting a second comment that repeats GitHub UI state

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The old label is close enough." | A legacy label can bypass or misrepresent the shared lifecycle. |
| "I can claim it now and sort out labels later." | Claiming follows human approval and verified label availability. |

## Verification

- [ ] Required labels were verified with `gh label list`.
- [ ] The work item follows the shared lifecycle.
- [ ] Any PR or issue comment is necessary, consolidated, and human-directed.
