---
name: label-workflow
description: "Label taxonomy, issue lifecycle (filed→triage→queued→claimed→done), and the agent/human handoff model for projectbluefin/dakota-iso. Use when understanding the issue lifecycle, triaging work, or finding agent-ready issues."
metadata:
  type: procedure
---

# Label Workflow — dakota-iso

## The one-line model

**Humans decide what gets built. Agents build it.**

Humans file, triage, and approve work. Agents claim, implement, and ship it.
Labels are the handoff signal between the two.

---

## Issue lifecycle

```
filed → triage → queue/agent-ready → queue/claimed → done (closed)
```

| Label | Actor | Next action |
|---|---|---|
| *(no queue label)* | **Human** triager | Review, set `kind/` label, then add `queue/agent-ready` when ready |
| `queue/agent-ready` | **Agent** / contributor | Self-assign and start work; change to `queue/claimed` |
| `queue/claimed` | **Agent** | Implement → open PR with `Closes #NNN` |
| `queue/hold` | *nobody* | Intentionally paused — read comments for reason |
| `needs-human` | **Human** | Agent is blocked — read the issue comment, unblock |

## PR lifecycle

```
opened ──▶ review ──▶ lgtm + CI green ──▶ merged
           [needs-human]   [human approves]
```

- PRs need a human `lgtm` or review approval before merge
- Add `queue/hold` at any time to pause automation
- Close stale PRs after 30 days of inactivity

---

## Label reference

| Label | Meaning |
|---|---|
| `bug` | Something is broken |
| `enhancement` | New feature or improvement |
| `documentation` | Docs-only change |
| `queue/agent-ready` | Approved and ready for agent pickup |
| `queue/claimed` | An agent or contributor is actively working this |
| `queue/hold` | Intentionally paused |
| `needs-human` | Agent is blocked; human input required |
| `source:agent` | Filed or changed by an AI agent |
| `source:gha` | Filed by GitHub Actions / automation |
| `source:manual` | Filed by a human |
| `good first issue` | Good for first-time contributors |

---

## Finding work

```bash
# Agent-ready issues in this repo
gh issue list --repo projectbluefin/dakota-iso \
  --label "queue/agent-ready" --assignee ""

# P0 blockers across the org
gh search issues --label "hive/p0" --owner projectbluefin --state open \
  --json number,title,repository
```

---

## Filing an issue

When you discover a bug or gap:

1. Check for duplicates first
2. Use a descriptive title (Conventional Commits style: `type: description`)
3. Include reproduction steps or context
4. Add `source:agent` if filed by an AI agent
5. **Do not** self-apply `queue/agent-ready` — that's a human triage decision

---

## PR comment policy

(Inherited from org-level model in [`common/docs/factory/agentic-model.md`](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md))

- **One comment per PR event, max.** Combine all findings into one comment.
- Never duplicate GitHub UI state (approvals, CI status, labels).
- `@` mentions only when asking someone to do something specific.
- When in doubt, post nothing. **Never post multiple comments on the same issue/PR.**
