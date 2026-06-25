---
name: skill-drift
description: "Covers how the skill-drift CI check works — when it fires, what it validates, how to write a satisfying skill update, and how to request a waiver. Load when the skill-drift check is failing on a PR or when deciding whether a change requires a skill file update."
metadata:
  type: procedure
---

# Skill Drift — dakota-iso

`skill-drift.yml` warns when a PR changes implementation files without updating the matching skill documentation. The goal: keep agent-facing docs in sync with real repo behavior while the implementation context is still fresh.

The mandate for *why* you must write skill updates is in [`skill-improvement.md`](./skill-improvement.md).

---

## How it works

```
PR opened
  └─ extract changed files
       ├─ match against code-paths
       └─ if code-paths hit and no skill-paths hit → WARN
```

Currently advisory (warns but does not block merge). Treat warnings as hard requirements.

---

## Path mapping

| Changed path | Update this skill |
|---|---|
| `.github/workflows/build-iso.yml`, `build-iso-bluefin.yml` | `docs/ci.md` |
| `.github/workflows/test-*.yml` | `docs/skills/e2e-ci.md` or `docs/luks-testing.md` |
| `.github/workflows/skill-drift.yml` | `docs/skills/skill-drift.md` (this file) |
| `justfile` | whichever skill owns the changed recipe |
| `dakota/src/build-iso.sh`, `live/src/build-iso.sh` | `docs/architecture.md` or `docs/build.md` |
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

## Waiver process

For refactoring changes with no functional impact:

1. Add to your PR description:
   ```markdown
   ## Skill drift waiver
   Changed: `live/src/configure-live.sh`
   Reason: Internal variable rename only — no behavior change, no operator impact.
   ```
2. A maintainer can override the check. Do not self-waive.

---

## Common failure modes

- Changing a workflow and forgetting to update `docs/ci.md`
- Updating the wrong skill file for the behavior that changed
- Adding a placeholder doc that does not explain the change
- Assuming advisory = optional

---

## See Also

- [`docs/skills/skill-improvement.md`](./skill-improvement.md) — the full mandate for writing skill updates
