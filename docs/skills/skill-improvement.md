---
name: skill-improvement
description: "The skill-improvement mandate — every agent session must produce a skill file update alongside the work. Use when completing a task and deciding whether to write a skill update, or when creating or updating a skill file."
metadata:
  type: procedure
---

# Skill Improvement Mandate — dakota-iso

Every agent session produces two outputs:

1. **The work** — the PR, fix, or improvement
2. **The learning** — what a future agent should know

Output 1 without Output 2 leaves the factory no smarter. **The loop only compounds if agents write back.**

## Contents
- [Before You Mark Work Complete](#before-you-mark-work-complete)
- [What Counts as a Learning Worth Writing Back](#what-counts-as-a-learning-worth-writing-back)
- [Where to Write It](#where-to-write-it)
- [Format](#format)
- [Committing](#committing)
- [See Also](#see-also)

---

## Before You Mark Work Complete

Run this checklist before opening a PR for review or marking an issue done:

- [ ] Did I discover any workaround, non-obvious pattern, or convention?
- [ ] Is there a skill file for the area I worked in?
- [ ] If yes — did I update it?
- [ ] If no — did I create one in `docs/skills/`?
- [ ] Is the skill file committed in **this same PR**? (Not a follow-up. Same PR.)

If all five are checked, you're done. If any are unchecked, finish them first.

---

## What Counts as a Learning Worth Writing Back

**Write it:**

| Category | Example |
|---|---|
| Upstream bug workaround | "`installer_channel=dev` regressed fisherman overlay storage — use `stable` until tuna-os/fisherman#38 resolves" |
| Non-obvious correctness requirement | "Use `buildah commit --squash` not `podman create --entrypoint /bin/sh && podman commit` — the latter corrupts Entrypoint and breaks bootc install" |
| Convention not obvious from code | "skopeo copy must run inside the installer container, not the build host — otherwise tar-split metadata format mismatches" |
| Trial-and-error discovery | "dmsquash-live requires `CDLABEL=DAKOTA_LIVE` exactly — changing the label breaks live boot without any helpful error message" |
| CI/environment quirk | "BTRFS hosts: even with squash-to-1-layer, VFS import is slow — use XFS loopback for local builds" |
| **Project-internal fact correction** | "The `dakota` image tag is `:latest` from `ghcr.io/projectbluefin/dakota` — verify via `payload_ref` file, not from memory." |

**Project-internal fact drift is a first-class failure mode.** When an agent writes documentation about image names, tags, workflow outputs, registry paths, R2 bucket layout, or any other project-internal fact — and gets it wrong because it used training data instead of reading the source — that is a skill failure. The fix is always the same: read the source file (`payload_ref`, `execute-release.yml`, `build-iso.yml`), update the skill, add verification commands so the next agent can self-check.

**The rule:** Any skill file containing project-internal facts (image names, tag schemas, R2 paths, workflow matrix values) **must** include the exact source to re-derive those facts (e.g., `cat <variant>/payload_ref` or `gh workflow view build-iso.yml`).

**Do NOT write:**

| Category | Example |
|---|---|
| One-off task note | "Use commit message `fix(iso): revert squash logic` for this PR" |
| Obvious developer knowledge | "Run `git status` to see changed files" |
| Ephemeral state | "R2 uploads are currently paused due to credential rotation" |
| Contradiction of another skill | If a skill says X and you want to say not-X, update the existing skill to say not-X — don't add a new doc |

---

## Where to Write It

| Working in... | Write to |
|---|---|
| ISO build system, justfile, squashfs | `docs/build.md` |
| Architecture, boot flow, VFS storage | `docs/architecture.md` |
| CI workflows, R2 uploads, smoke tests | `docs/ci.md` |
| LUKS testing, fisherman, QEMU | `docs/luks-testing.md` |
| R2 promotion, named releases | `docs/r2-promotion.md` |
| Variants, `payload_ref` pattern | `docs/variants.md` |
| Issue lifecycle, labels, PR policy | `docs/skills/label-workflow.md` |
| Human gates, evidence requirements | `docs/skills/human-gates.md` |
| Onboarding, dev environment | `docs/skills/onboarding.md` |
| Skill-drift CI check behavior | `docs/skills/skill-drift.md` |
| New process or procedure (no existing file) | Create `docs/skills/<area>.md` |

If a pattern affects 2+ projectbluefin repos, write it locally first, then open a propagation issue in `projectbluefin/common`.

---

## Format

Add a dated section at the bottom of the relevant file:

```markdown
### <what you learned> (YYYY-MM-DD)

**What failed:** <describe the symptom>
**Why:** <root cause>
**Fix:** <what to do instead>

```bash
# code example if applicable
```
```

Keep it concise. Future agents need to understand it at a glance.

---

## Committing

Skill file updates must be committed in the **same PR** as the change that generated the learning. Never a follow-up PR.

```bash
git add docs/build.md     # or whichever file you updated
git commit --amend        # add to the existing commit, or
git commit -m "docs(build): add lesson on buildah squash entrypoint bug"
```

---

## See Also

- [`docs/skills/skill-drift.md`](./skill-drift.md) — how the CI enforcement works (warns on PRs that change code without updating docs)
- [Org-level skill-improvement mandate](https://github.com/projectbluefin/common/blob/main/docs/skills/skill-improvement.md) — cross-repo version of this document
