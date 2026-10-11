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
description: Label taxonomy, queue states, and lifecycle rules for issues and pull requests in dakota-iso.
version: "2.0"
last_updated: "2026-10-11"
metadata:
  type: procedure
---

# Label Workflow — dakota-iso

dakota-iso uses [Prow](https://github.com/cncf/prow-github-actions) (cncf/prow-github-actions v3.0.1) and org-wide configuration from [`projectbluefin/.project/prow.yaml`](https://github.com/projectbluefin/.project/blob/main/prow.yaml).

Full org lifecycle documentation lives in the shared [Project Bluefin label-workflow guide](https://github.com/projectbluefin/common/blob/main/docs/skills/label-workflow.md).

---

## Issue lifecycle

1. **Filed.** New issues receive `needs-human` and their initial `kind/` label from the issue template.
2. **Triage.** Maintainers review the issue, refine the `kind/*` label, and set `/area` or `/priority`.
3. **Accepted.** When scope is clear and approved, comment `/triage accepted`.
4. **Agent pickup.** Maintainers remove `needs-human` when work is open for automation. Hive assigns work only when `triage/accepted` is present and neither `needs-human` nor `blocked` is set.
5. **Blocked.** Use `/label blocked` while waiting on dependencies outside the repo; `/remove-label blocked` when unblocked.

---

## PR lifecycle

1. **Opened.** Reviewers are requested from `OWNERS`.
2. **Review.** Reviewers comment `/lgtm` when satisfied. Any new push clears `lgtm`.
3. **Approve.** An `OWNERS` approver comments `/approve` (or approves via GitHub review).
4. **Merge.** When checks pass with `lgtm` and `approved`, Prow merges the PR via squash merge.
5. **Hold.** Comment `/hold` to pause merge; `/hold cancel` to resume.

---

## Active label taxonomy

| Category | Labels | Applied by |
|---|---|---|
| **Kind** | `kind/bug`, `kind/feature`, `kind/cleanup`, `kind/documentation`, `kind/regression`, `kind/security` | Template or `/kind <name>` |
| **Area** | `area/desktop`, `area/flatpak`, `area/gaming`, `area/hardware`, `area/installer`, `area/dx` | `/area <name>`, `/remove-area <name>` |
| **Triage** | `triage/accepted`, `needs-triage`, `needs-kind` | `/triage accepted`, Prow required-label checks |
| **Priority** | `priority/critical-urgent`, `priority/important-soon` | `/priority <level>` |
| **Gates** | `needs-human`, `hold`, `blocked`, `lgtm`, `approved` | Maintainers, `/hold`, `/label blocked`, `/lgtm`, `/approve` |

---

## PR comment policy

(Inherited from org-level model in [`common/docs/factory/agentic-model.md`](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md))

- **One comment per PR event, max.** Combine all findings into one comment.
- Never duplicate GitHub UI state (approvals, CI status, labels).
- `@` mentions only when asking someone to do something specific.
- When in doubt, post nothing. **Never post multiple comments on the same issue/PR.**
