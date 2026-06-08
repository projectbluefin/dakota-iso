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

---

## Before You Mark Work Complete

- [ ] Did I discover any workaround, non-obvious pattern, or convention?
- [ ] Is there a skill file for the area I worked in?
- [ ] If yes — did I update it?
- [ ] If no — did I create one in `docs/skills/`?
- [ ] Is the skill file committed in **this same PR**? (Not a follow-up. Same PR.)

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

**Do NOT write:**

| Category | Example |
|---|---|
| One-off task note | "Use commit message `fix(iso): revert squash logic` for this PR" |
| Obvious developer knowledge | "Run `git status` to see changed files" |
| Ephemeral state | "R2 uploads are currently paused due to credential rotation" |

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
