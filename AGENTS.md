# AGENTS.md

> This file tells AI coding agents (GitHub Copilot, Claude, Gemini, etc.) how to
> contribute safely and work in this repository. Human contributors follow the same steps.

Dakota ISO builds bootable UEFI live ISOs from [Dakota](https://github.com/projectbluefin/dakota)
images (GNOME OS / bootc / composefs). Two variants: `dakota` and `dakota-nvidia`.

---

## Find something to work on

| Time available | Link |
|---|---|
| 30 minutes | [XS issues](https://github.com/projectbluefin/dakota-iso/issues?q=is%3Aopen+label%3Aqueue%2Fagent-ready+label%3Asize%2Fxs+no%3Aassignee) |
| Half a day | [S issues](https://github.com/projectbluefin/dakota-iso/issues?q=is%3Aopen+label%3Aqueue%2Fagent-ready+label%3Asize%2Fs+no%3Aassignee) |
| Full day | [M issues](https://github.com/projectbluefin/dakota-iso/issues?q=is%3Aopen+label%3Aqueue%2Fagent-ready+label%3Asize%2Fm+no%3Aassignee) |
| All sizes | [Everything ready](https://github.com/projectbluefin/dakota-iso/issues?q=is%3Aopen+label%3Aqueue%2Fagent-ready+no%3Aassignee+sort%3Acreated-asc) |

---

## Mandatory Behavioral Gates

### 1. Read-First Gate

Read the relevant skill file in `docs/` **before making any changes**.
Do not assume you know the build system, disk space requirements, or CI constraints.

### 2. Verification Gate

Before submitting a PR:
- Run `just iso-sd-boot <target>` locally (or `just container <target>` for container-only changes)
- Confirm the ISO boots: `just boot-iso-serial <target>` or the CI smoke test
- PR description must state what you built and that it booted

### 3. Justfile Integrity Gate

The `justfile` is the canonical interface. All build tasks go through it.
If you identify a missing recipe, add it to the justfile in the same PR.

### 4. Operator Accountability Gate

The human operator is responsible for every AI-generated PR.
PRs must include: `[ ] I am using an agent and I take responsibility for this PR`

### 5. Upstream-First Gate

The `origin` remote is `projectbluefin/dakota-iso` (upstream). Pushes go directly upstream.
If working from a personal fork, add it as a separate remote — never push to origin from a fork context.

---

## In-repo skills — read these before working

All accumulated knowledge lives in `docs/`. These files are the source of truth
for this repo. When you fix a bug or discover a pattern, add a lesson here.

| Area | File | Load when... |
|---|---|---|
| **Build system** | [`docs/build.md`](docs/build.md) | Building ISOs locally, disk space, BTRFS/XFS, variants |
| **Architecture** | [`docs/architecture.md`](docs/architecture.md) | Understanding the two-container pipeline, boot flow, squashfs |
| **CI/CD** | [`docs/ci.md`](docs/ci.md) | `build-iso.yml`, `test-luks-install.yml`, R2 uploads |
| **LUKS testing** | [`docs/luks-testing.md`](docs/luks-testing.md) | LUKS E2E test (local QEMU, libvirt, CI-equivalent) |
| **R2 promotion** | [`docs/r2-promotion.md`](docs/r2-promotion.md) | Promoting ISOs to production, rclone, named releases |
| **Variants** | [`docs/variants.md`](docs/variants.md) | Adding new variants, `payload_ref` pattern |

---

## How to add a lesson

1. Open the relevant file in `docs/`
2. Add a section: `### <what you learned> (YYYY-MM-DD)`
3. What failed → why → the fix → code example if applicable
4. Commit it in the same PR as your change

This is the feedback loop: every agent that works here makes it easier for the next.
