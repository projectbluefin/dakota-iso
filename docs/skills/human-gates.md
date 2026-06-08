---
name: human-gates
description: "The four human decision gates — Design, Security, Breakage, and Merge — when an agent must stop and request human input. Use when uncertain whether a change requires human review, or to verify evidence requirements before opening a PR."
metadata:
  type: procedure
---

# Human Decision Gates — dakota-iso

Agents implement autonomously **except** at these four gates. At each gate, stop work, open a draft PR or issue comment, and request human input explicitly. Never guess past a gate.

---

## The Four Gates

### 1. Design Gate

**Stop when:** You are about to make an architecture change, introduce a new subsystem, or change behavior that is visible to users.

Examples for this repo:
- Changing the ISO boot flow (El Torito, systemd-boot, initramfs)
- Changing how the live environment is configured (`configure-live.sh`)
- Modifying the squashfs or VFS containers-storage layout
- Changing how variants are defined or how `payload_ref` works
- Adding a new Flatpak to the live environment bundle

**Action:** Open a draft PR with your proposed design. Describe what you're changing and why. Tag with `needs-human` and state you are at a design gate.

---

### 2. Security Gate

**Stop when:** Your change touches signing, supply chain, secrets, or third-party package sources.

Examples for this repo:
- Changing cosign verification logic
- Adding a new Flatpak source or remote
- Modifying R2 credentials or upload logic
- Adding or changing GitHub Actions secrets usage

**Action:** Open a draft PR. State exactly which security property is affected and what your approach preserves or changes.

---

### 3. Breakage Gate

**Stop when:** Your change could break downstream consumers or other projectbluefin repos.

Examples for this repo:
- Changing the ISO filename or R2 upload path that other tooling expects
- Modifying the fisherman recipe format or `images.json` schema
- Changing a justfile variable that CI workflows depend on

**Action:** Identify all affected consumers first. List them in the PR description.

---

### 4. Merge Gate

**Stop when:** Your PR is ready for final review and merge.

This gate is always human. A human reviewer must approve before merge. Auto-merge does not fire in this repo without human approval.

Agents never self-merge, never bypass branch protection, and never force-push to `main`.

---

## How to Signal a Gate

When you hit a gate:

1. **Open a draft PR** (or comment on the issue if no code is ready yet)
2. State which gate you've hit and why
3. Add label `needs-human`
4. Stop. Do not continue implementation until a human responds.

```bash
# Open a draft PR
gh pr create --repo projectbluefin/dakota-iso --base main --draft \
  --title "feat: <your change>" \
  --body "At Design Gate: <describe the decision needed>"

# Add needs-human label
gh pr edit <number> --repo projectbluefin/dakota-iso --add-label "needs-human"
```

---

## Verification Evidence Requirement

Before requesting PR review, provide:

- [ ] CI run link (must be green or explain any failing steps)
- [ ] For ISO changes: statement that ISO built and booted (`just boot-iso-serial dakota` output or QEMU screenshot)
- [ ] For container-only changes: statement this is container-only (no ISO boot required)
- [ ] Skill file update committed in **this same PR**
- [ ] PR title follows Conventional Commits format
- [ ] Both AI attribution trailers on every AI-authored commit

---

## When in Doubt

If you're unsure whether you've hit a gate:
- For architecture or security questions → **always stop and ask**
- For small bug fixes with clear scope → proceed, but document in the PR description
- For anything touching `.github/workflows/` → stop, that's sensitive path territory
