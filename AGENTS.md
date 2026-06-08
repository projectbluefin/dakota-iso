# AGENTS.md

> This file tells AI coding agents (GitHub Copilot, Claude, Gemini, etc.) how to
> contribute safely and work in this repository. Human contributors follow the same steps.
> See the [org-wide agentic model](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md) for cross-repo rules.

**dakota-iso** builds bootable UEFI live ISOs from [Dakota](https://github.com/projectbluefin/dakota)
images (GNOME OS / bootc / composefs). Two variants: `dakota` and `dakota-nvidia`.

Home repo: [projectbluefin/dakota-iso](https://github.com/projectbluefin/dakota-iso)

## The System You Are Part Of

```
┌──────────────────────────────────────────────────────────┐
│  KubeStellar Hive  https://kubestellar.io/live/hive/     │
│  AI-native Continuous Maturity Model (ACMM) orchestration│
└──────────────────┬───────────────────────────────────────┘
                   │
      ┌────────────┴────────────┐
      ▼                         ▼
common ──────────────────────────┐
(shared OCI layer)               │
                                 ▼
bluefin / bluefin-lts / dakota ──┤──→ testsuite ──→ dakota-iso
                                 │               (this repo)
                          bootc-installer
```

You are an agent in this loop. Your work compounds.

## Agent fast path

```
1. docs/SKILL.md                       # find the skill for your task
2. docs/factory/agentic-model.md       # cross-repo rules (in projectbluefin/common)
3. justfile                            # all build tasks go through here
```

---

## Find something to work on

| Time available | Link |
|---|---|
| All sizes | [Everything agent-ready](https://github.com/projectbluefin/dakota-iso/issues?q=is%3Aopen+label%3Aqueue%2Fagent-ready+no%3Aassignee+sort%3Acreated-asc) |

```bash
# P0 blockers — start here every session
gh search issues --label "hive/p0" --owner projectbluefin --state open \
  --json number,title,repository

# Agent-ready issues in this repo
gh issue list --repo projectbluefin/dakota-iso --label "queue/agent-ready" --assignee ""
```

---

## Mandatory Behavioral Gates

### 1. Read-First Gate

Read the relevant skill file in `docs/` **before making any changes**.
Do not assume you know the build system, disk space requirements, or CI constraints.

**Specific triggers — stop and read before acting:**

| Situation | Read first |
|---|---|
| Any QEMU boot issue (installed disk won't boot, UEFI shell loop) | [`docs/luks-testing.md`](docs/luks-testing.md) |
| ISO is unexpectedly large (>6 GB) | [`docs/ci.md`](docs/ci.md) — check for double-embedded store |
| CI pipeline changes | [`docs/ci.md`](docs/ci.md) |
| R2 promotion / named releases | [`docs/r2-promotion.md`](docs/r2-promotion.md) |

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
| **Skill router** | [`docs/SKILL.md`](docs/SKILL.md) | **Start here** — find the right skill for your task |
| **Build system** | [`docs/build.md`](docs/build.md) | Building ISOs locally, disk space, BTRFS/XFS, variants |
| **Architecture** | [`docs/architecture.md`](docs/architecture.md) | Understanding the two-container pipeline, boot flow, squashfs |
| **CI/CD** | [`docs/ci.md`](docs/ci.md) | `build-iso.yml`, `test-luks-install.yml`, R2 uploads |
| **LUKS testing** | [`docs/luks-testing.md`](docs/luks-testing.md) | LUKS E2E test (local QEMU, libvirt, CI-equivalent) |
| **R2 promotion** | [`docs/r2-promotion.md`](docs/r2-promotion.md) | Promoting ISOs to production, rclone, named releases |
| **Variants** | [`docs/variants.md`](docs/variants.md) | Adding new variants, `payload_ref` pattern |
| **Label workflow** | [`docs/skills/label-workflow.md`](docs/skills/label-workflow.md) | Issue lifecycle, labels, PR queue |
| **Human gates** | [`docs/skills/human-gates.md`](docs/skills/human-gates.md) | When to stop and ask a human |
| **Skill improvement** | [`docs/skills/skill-improvement.md`](docs/skills/skill-improvement.md) | Writing skill updates in the same PR |

---

## Development Standards

### Commit format

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <description>`

Common types: `feat` `fix` `docs` `ci` `refactor` `chore` `build` `perf` `test` `revert`

### AI attribution

Every AI-authored commit **must** include both trailers:

```
feat(iso): add loopback.cfg for Ventoy support

Adds boot/grub/loopback.cfg to the ISO layout so the image is
bootable via Ventoy without re-extraction.

Assisted-by: Claude Sonnet 4.6 via GitHub Copilot
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

### Mandatory pre-commit checks

```bash
# Before every commit — these must pass:
just --list        # verify justfile is parseable (no just check target yet)
```

### Agent environment constraints

- **`sudo` is not available** in automated agent bash sessions (no TTY). Never call
  `sudo dd`, `sudo modprobe`, `sudo nbd-client`, etc. If an operation requires root,
  provide the exact command for the user to run in their terminal instead.
- **Block device writes** (USB burning, disk inspection via nbd) always require root.
  Use `udisksctl` for unmounting; provide `dd` / `qemu-nbd` commands for the user to run.

### ISO size invariant

The unified dakota ISO must be **~5.3 GB** with `SUPERISO_COMPRESSION=release`.

- If an ISO is **~8 GB**: the offline OCI store squashfs is being double-embedded.
  The live container already has the OCI baked in as VFS containers-storage.
  Do **not** build a separate `store.squashfs.img` or pass `--store` to `build-iso.sh`.
  See [`docs/ci.md`](docs/ci.md) lessons.
- If an ISO is **~6–7 GB**: compression is set to `fast` (zstd-3). Use `release` for R2.

### Sensitive paths (require maintainer review)

- `.github/workflows/` — CI pipeline changes
- `justfile` — canonical build interface
- `live/src/build-iso.sh` — ISO assembly logic (canonical; `dakota/src/build-iso.sh` is the local-only copy)
- `live/src/configure-live.sh` — live environment setup
- `live/src/install-flatpaks.sh` — Flatpak baking into squashfs

---

## 🚫 ABSOLUTE PROHIBITION — ublue-os org

**NEVER create issues, pull requests, comments, forks, webhook calls, API writes, automated reports, or any other programmatic action targeting any `ublue-os/*` repository.**

Read-only `gh api` calls to inspect `ublue-os` repos are permitted. No writes of any kind.

---

## Self-Improvement Loop

Every agent session produces two outputs:

1. **The work** — the PR, fix, or improvement
2. **The learning** — what a future agent should know

Output 1 without Output 2 leaves the factory no smarter. **The loop only compounds if agents write back.**

```
Agent works on task
  └─ discovers pattern / workaround / convention
       └─ writes it to the relevant skill file in docs/ or docs/skills/
            └─ commits in the same PR (never a follow-up)
                 └─ next agent starts smarter → loop
```

### How to add a lesson

1. Open the relevant file in `docs/` (or `docs/skills/` for process/procedure learnings)
2. Add a section: `### <what you learned> (YYYY-MM-DD)`
3. What failed → why → the fix → code example if applicable
4. Commit it in the **same PR** as your change — never a follow-up

### Before marking work complete — checklist

- [ ] Did I discover any workaround, non-obvious pattern, or convention?
- [ ] Is there a skill file for the area I worked in?
- [ ] If yes — did I update it?
- [ ] If no — did I create one in `docs/skills/`?
- [ ] Is the skill file committed in **this same PR**?

---

## Human Decision Gates

Stop and request human input at these four gates. Never guess past them.

| Gate | Stop when |
|---|---|
| **Design** | Architecture change, new subsystem, user-visible behavior change |
| **Security** | Signing, supply chain, secrets, COPR/third-party sources |
| **Breakage** | Cross-repo breaking change — removing/renaming inputs, changing defaults |
| **R2 promotion** | Promoting any ISO to a named release (e.g. `alpha3`) — requires explicit user "go ahead" after local boot verification |
| **CI trigger on release branch** | Triggering a CI build that will upload to R2 — confirm with user first |
| **Merge** | PR ready for final review — always requires human `lgtm` |

See [`docs/skills/human-gates.md`](docs/skills/human-gates.md) for full evidence requirements.

## Verification Requirements

Do not request PR review without evidence:

- [ ] CI is passing (link the run in the PR description)
- [ ] ISO built and booted (or container-only change — state this explicitly)
- [ ] ISO size is ~5.3 GB (release compression, no double-embedded store)
- [ ] Skill file update committed in **this same PR** (not a follow-up)
- [ ] PR title follows Conventional Commits format
- [ ] Both AI attribution trailers present on every AI-authored commit
