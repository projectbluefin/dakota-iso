---
id: skill-drift
name: Skill Drift
one_line_purpose: Legacy advisory workflow for documentation-update feedback.
entry_point: docs/skills/skill-drift.md
category: ci-ops
status: active
tags:
  - ci
  - skill-drift
  - documentation
  - validation
description: Use when interpreting the legacy advisory workflow for documentation-update feedback.
version: "1.0"
last_updated: "2026-08-01"
metadata:
  type: procedure
---

# Legacy Skill Drift Advisory — dakota-iso

`skill-drift.yml` is a legacy advisory workflow that warns when a PR changes
implementation files without updating matching documentation. The shared
factory policy has retired bespoke skill-drift checks as release gates:
documentation quality belongs in developer-time checks and review, not a
separate CI gate.

The durable-learning mandate remains in [`skill-improvement.md`](./skill-improvement.md).
Treat this workflow's feedback as useful context, not a merge-blocking policy.

---

## When to Use

Use this procedure when the legacy workflow reports missing documentation after
an implementation change. Do not use it to determine whether a release may
ship; follow the shared skill-improvement procedure and repository checks.

## When Not to Use

Do not use this advisory workflow to justify unrelated documentation changes,
waivers, or a release decision.

## How it works

```
PR opened
  └─ extract changed files
       ├─ match against code-paths
       └─ if code-paths hit and no skill-paths hit → WARN
```

This workflow is advisory only. Follow the shared skill-improvement procedure
and the repository's pre-commit checks rather than inventing a local waiver
process.

---

## Path mapping

| Changed path | Update this skill |
|---|---|
| `.github/workflows/build-iso.yml`, `build-iso-bluefin.yml` | `docs/ci.md` |
| `.github/workflows/test-*.yml` | `docs/skills/e2e-ci.md` or `docs/luks-testing.md` |
| `.github/workflows/skill-drift.yml` | `docs/skills/skill-drift.md` (this file) |
| `justfile` | whichever skill owns the changed recipe |
| `live/src/build-iso.sh` | `docs/architecture.md` or `docs/build.md` |
| `live/src/configure-live.sh` | `docs/architecture.md` |
| `live/src/install-flatpaks.sh` | `docs/build.md` |
| `scripts/build-live-squashfs.sh` | `docs/build.md` |
| `dakota/Containerfile` | `docs/architecture.md` |
| `<variant>/payload_ref` | `docs/variants.md` |

Not sure? Check `docs/skills/INDEX.md`.

---

## What counts as a satisfying update

A passing update must:
- Name the file, workflow, hook, command, or path that changed
- State the new rule, behavior, or expectation
- Explain what an agent should now do differently

**Passing:** "Added `--squash` flag to buildah commit in `scripts/build-live-squashfs.sh`; non-composefs ISOs must always squash before VFS import. Update `docs/build.md` ISO size invariant table."

**Failing:** rewrapping text, adding unrelated notes, or touching any markdown file without explaining the implementation change.

---

## Core Process

1. Identify the changed implementation path.
2. Use the mapping to locate the relevant local documentation.
3. Update the procedure only when the behavior or operator action changed.
4. Run the repository's developer-time checks and treat the workflow output as
   advisory feedback.

## Common failure modes

- Changing a workflow and forgetting to update `docs/ci.md`
- Updating the wrong skill file for the behavior that changed
- Adding a placeholder doc that does not explain the change
- Treating an advisory signal as an independent release gate

---

## Red Flags

- Adding a waiver instead of documenting a real behavior change
- Updating unrelated Markdown solely to satisfy the workflow
- Treating an advisory result as a substitute for review or verification

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Any Markdown change satisfies the warning." | Documentation must explain the changed operator behavior. |
| "The warning blocks the release." | The workflow is advisory; the real release gates remain authoritative. |

## Verification

- [ ] The documented behavior corresponds to the changed implementation.
- [ ] The relevant local skill, not an unrelated document, was updated.
- [ ] The workflow was not used as a release or merge gate.

## See Also

- [`docs/skills/skill-improvement.md`](./skill-improvement.md) — the full mandate for writing skill updates
