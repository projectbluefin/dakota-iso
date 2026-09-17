---
id: human-gates
name: Human Decision Gates
one_line_purpose: Define the four mandatory human gates (Design, Security, Breakage, Merge) for agents.
entry_point: docs/skills/human-gates.md
category: meta
status: active
tags:
  - governance
  - human-gates
  - process
description: Use when work may cross a design, security, breakage, or merge decision gate.
version: "1.0"
last_updated: "2026-08-01"
metadata:
  type: procedure
---

# Human Decision Gates — dakota-iso

Agents implement autonomously **except** at these four gates. At each gate,
stop and request human input explicitly. Never guess past a gate or create a
PR, issue, or comment autonomously to obtain that input.

---

## When to Use

Use this procedure before changing architecture, security-sensitive paths,
cross-repository contracts, or merge state. Do not use it for a small,
source-backed correction with no behavior or compatibility impact.

## When Not to Use

Do not invoke a decision gate to avoid a routine, reversible implementation
choice that is already specified by the repository's source and policy.

## Core Process: The Four Gates

### 1. Design Gate

**Stop when:** You are about to make an architecture change, introduce a new subsystem, or change behavior that is visible to users.

Examples for this repo:
- Changing the ISO boot flow (El Torito, systemd-boot, initramfs)
- Changing how the live environment is configured (`configure-live.sh`)
- Modifying the squashfs or VFS containers-storage layout
- Changing how variants are defined or how `payload_ref` works
- Adding a new Flatpak to the live environment bundle

**Action:** Describe the proposed design and the decision required to the human
operator. Wait for explicit direction before creating an external artifact.

---

### 2. Security Gate

**Stop when:** Your change touches signing, supply chain, secrets, or third-party package sources.

Examples for this repo:
- Changing cosign verification logic
- Adding a new Flatpak source or remote
- Modifying R2 credentials or upload logic
- Adding or changing GitHub Actions secrets usage

**Action:** State exactly which security property is affected and what the
approach preserves or changes. Wait for explicit human direction.

---

### 3. Breakage Gate

**Stop when:** Your change could break downstream consumers or other projectbluefin repos.

Examples for this repo:
- Changing the ISO filename or R2 upload path that other tooling expects
- Modifying the fisherman recipe format or `images.json` schema
- Changing a justfile variable that CI workflows depend on

**Action:** Identify all affected consumers first, then present that impact to
the human operator before proceeding.

---

### 4. Merge Gate

**Stop when:** Your PR is ready for final review and merge.

This gate is always human. A human reviewer must approve before merge. Auto-merge does not fire in this repo without human approval.

Agents never self-merge, never bypass branch protection, and never force-push to `main`.

---

## How to Signal a Gate

When you hit a gate:

1. State which gate you've hit and why.
2. Present the options, recommendation, and affected consumers or security
   properties as applicable.
3. Stop. Do not continue implementation until a human responds.

Only create or update a PR, issue, or label after the human explicitly directs
that action. Verify any required label exists before using it.

---

## Verification Evidence Requirement

Before requesting PR review, provide:

- [ ] CI run link (must be green or explain any failing steps)
- [ ] For installer or ISO behavior changes: full install completed and the
      installed system booted (`just debug=1 plain-e2e dakota` or equivalent)
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

## Red Flags

- Continuing implementation after identifying a gate
- Opening a PR, issue, or comment to request approval without explicit direction
- Treating a successful smoke boot as install verification

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The design is obvious." | User-visible architecture still requires human direction. |
| "I can open a draft PR to ask." | External writes require explicit human direction first. |

## Verification

- [ ] The applicable gate was identified before the affected action.
- [ ] The human decision and any constraints are recorded before work resumes.
- [ ] Verification evidence matches the change type.
