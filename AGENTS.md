# AGENTS.md

> This file tells AI coding agents (GitHub Copilot, Claude, Gemini, etc.) how to
> contribute safely and work in this repository. Human contributors follow the same steps.
> See the [org-wide agentic model](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md) for cross-repo rules.

**dakota-iso** builds a single bootable UEFI live ISO from [Dakota](https://github.com/projectbluefin/dakota)
images (GNOME OS / bootc / composefs). The live environment runs the NVIDIA variant; the offline
embedded OCI store lets the installer deploy to non-NVIDIA hardware without a network pull.

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

> **Before using any tool or library: look up its docs via Context7 first. Always.**
> bootc, xorriso, composefs, systemd-boot, skopeo, GitHub Actions — every tool has live, authoritative docs.
> Pattern: `resolve-library-id` → `get-library-docs` → implement → cite the section.
> Guessing, flag-hunting, and trial-and-error are banned. The docs exist. Read them.

```
1. docs/SKILL.md                       # find the skill for your task
2. Context7: resolve the tool's library ID, read its docs, then act
3. docs/factory/agentic-model.md       # cross-repo rules (in projectbluefin/common)
4. justfile                            # all build tasks go through here
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

### 0. Test Gate — nothing ships without a test that would have caught it

**This gate fires before writing any code.**

For every change to the installer pipeline, ask:
> "If this change is wrong, which test breaks?"

If the answer is "none", write the test first.

| Change type | Required test |
|---|---|
| Copying a binary into the live container | Unit test asserts binary AND all shared lib deps are present in Containerfile |
| Any change to filesystem formatting | CI smoke test runs `mkfs.<fs>` inside the container; E2E uses that filesystem |
| Any change to the installer recipe | E2E recipe matches what the interactive installer sends by default |
| Any change to boot parameters | Regression test asserts the parameter is present in generated boot entries |

**`pytest tests/` passing does NOT mean the application works.**
The Python unit tests (`test.yml`) are fast static checks — they verify source-file
text invariants and Python routing logic with mocked subprocesses.  They cannot catch
runtime failures: a broken ISO build, a LUKS unlock that fails in QEMU, or an installer
that cannot find the embedded VFS store.  The real functional gates are:

| Gate | Workflow | What it proves |
|---|---|---|
| Fast unit tests | `test.yml` — runs on every PR | Source-file invariants and Python logic. Necessary but not sufficient. |
| LUKS install E2E | `test-luks-install.yml` | Encrypted install completes and installed system boots. |
| Plain install E2E | `test-plain-install.yml` | Unencrypted XFS composefs install completes and installed system boots. |

Never say "tests pass" to mean "the application works". Say "unit tests pass" and specify
which E2E gate (if any) covers the change.

**The E2E must test the same code path users hit.** If the interactive installer defaults to XFS, the E2E must use XFS. A test that passes on btrfs while users install on XFS is not a test — it is a false signal.

**Never claim a fix is verified by CI if you have not confirmed what the CI test actually exercises.** Check the recipe, the flags, the filesystem — not just the green checkmark.



Read the relevant skill file in `docs/` **before making any changes**.
Do not assume you know the build system, disk space requirements, or CI constraints.

**Specific triggers — stop and read before acting:**

| Situation | Read first |
|---|---|
| Any QEMU boot issue (installed disk won't boot, UEFI shell loop) | [`docs/luks-testing.md`](docs/luks-testing.md) |
| ISO is unexpectedly large (>6 GB) | [`docs/ci.md`](docs/ci.md) — check for double-embedded store |
| Install fails: `does not resolve to an image ID` | [`docs/ci.md`](docs/ci.md) — VFS store not embedded |
| CI pipeline changes | [`docs/ci.md`](docs/ci.md) |
| R2 promotion / named releases | [`docs/r2-promotion.md`](docs/r2-promotion.md) |
| Variant image refs look wrong or reference `ublue-os` | [`docs/variants.md`](docs/variants.md) — verify via `execute-release.yml` in source repo |
| systemd-boot title is wrong | [`<variant>/live_title`](docs/variants.md) — edit the `live_title` file in the variant dir |

### 2. Verification Gate

**Read [`docs/skills/qa-policy.md`](docs/skills/qa-policy.md) before running any test or making any verification claim.**

Key rules (full policy in that file):
- **Always test fresh artifacts.** Kill stale QEMU processes and delete stale install disks before every run.
- **`debug=1` is required for E2E.** SSH is disabled in production ISOs. Use `just debug=1 plain-e2e dakota`.
- **"ISO booted" is not proof.** Only a completed install + installed-system boot proves the pipeline works.

Before submitting a PR:
- Run `just debug=1 iso-sd-boot <target>` locally (or `just container <target>` for container-only changes)
- Run `just plain-test-qemu <target>` — must exit with `✅ Installed system boot verified`
- Only trigger CI after local tests pass
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
| **QA policy** | [`docs/skills/qa-policy.md`](docs/skills/qa-policy.md) | Before running any test, E2E, or making a verification claim |
| **LUKS testing** | [`docs/luks-testing.md`](docs/luks-testing.md) | LUKS E2E test (local QEMU, libvirt, CI-equivalent) |
| **R2 promotion** | [`docs/r2-promotion.md`](docs/r2-promotion.md) | Promoting ISOs to production, rclone, named releases |
| **Variants** | [`docs/variants.md`](docs/variants.md) | Adding new variants, `payload_ref` pattern |
| **Label workflow** | [`docs/skills/label-workflow.md`](docs/skills/label-workflow.md) | Issue lifecycle, labels, PR queue |
| **Human gates** | [`docs/skills/human-gates.md`](docs/skills/human-gates.md) | When to stop and ask a human |
| **Skill improvement** | [`docs/skills/skill-improvement.md`](docs/skills/skill-improvement.md) | Writing skill updates in the same PR |
| **Skill drift CI** | [`docs/skills/skill-drift.md`](docs/skills/skill-drift.md) | Skill-drift check failing on a PR |

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
# Before every commit — both must pass:
just --list                    # verify justfile is parseable
pre-commit run --all-files     # lint, yaml/json hygiene, no floating action tags
```

The `skill-drift.yml` CI check warns when a PR changes implementation files without updating a matching skill doc. Treat warnings as hard requirements.

### Agent environment constraints

- **`sudo` is not available** in automated agent bash sessions (no TTY). Never call
  `sudo dd`, `sudo modprobe`, `sudo nbd-client`, etc. If an operation requires root,
  provide the exact command for the user to run in their terminal instead.
- **`just build-bg` fails in agent sessions** — it calls `setsid sudo just ...` which
  requires a TTY for `sudo`. Use direct backgrounding instead:
  ```bash
  LOG=output/build.log; mkdir -p output
  setsid bash -c "just output_dir=output iso-sd-boot <target> > '${LOG}' 2>&1" &
  disown $!
  ```
- **Block device writes** (USB burning, disk inspection via nbd) always require root.
  Use `udisksctl` for unmounting; provide `dd` / `qemu-nbd` commands for the user to run.

### ISO size invariant

| Variant | Expected size (fast) | Expected size (release) |
|---|---|---|
| `dakota` (composefs) | ~5.5 GB | ~4.5 GB |
| `bluefin` / `bluefin-lts-hwe` (non-composefs) | ~7 GB | ~6 GB |

- If a **bluefin/lts-hwe** ISO is **~12 GB**: the non-composefs OCI embedding is NOT
  squashing layers before writing to `oci-store`. bluefin-nvidia has ~120 OCI layers;
  without `--squash`, all ~120 layer blobs land in the squashfs → ~8 GB OCI store → 12 GB ISO.
  **Fix:** `buildah commit --squash --format oci` (not `--format oci` alone) in both
  `justfile` and `scripts/build-live-squashfs.sh` non-composefs paths.
  **This has been rediscovered multiple times — never remove `--squash` from this path.**
- If a **dakota** ISO is **~8 GB**: the VFS OCI store is being double-embedded.
  The live squashfs already contains the OCI baked in as VFS containers-storage.
  Do **not** build a separate `store.squashfs.img` or pass `--store` to `build-iso.sh`.
- If an ISO is **~4.4 GB** and installs fail with `does not resolve to an image ID`: the
  VFS store is missing. `scripts/build-live-squashfs.sh` must be called with
  `--oci-image <ref>`.

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

**`ublue-os` image names are legacy.** All active images have migrated to `ghcr.io/projectbluefin/`.
Never write `ublue-os` into a `payload_ref`, `base_imgref`, `nvidia_imgref`, or `images.json`.
If you see a `ublue-os` ref in source, it is a stale artifact — replace it with the correct `projectbluefin` image.
Verify the correct name before replacing: read `execute-release.yml` in the source repo or run `skopeo list-tags`. Do **not** guess.

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

⛔ **Never use `rclone copyto` to manually overwrite `dakota-live-latest.iso` on R2.** The `latest` pointer is the production artifact users download. Only CI may write to it — after the E2E gate passes. Manual uploads bypass the gate and ship broken ISOs. See [#85](https://github.com/projectbluefin/dakota-iso/issues/85).

See [`docs/skills/human-gates.md`](docs/skills/human-gates.md) for full evidence requirements.

## Verification Requirements

Do not request PR review without evidence:

- [ ] CI is passing (link the run in the PR description)
- [ ] **Full install completed and installed system boots** — "ISO booted" alone is NOT sufficient. Run `just plain-e2e <target>` or equivalent and paste the result. A live session boot proves the initramfs works; only a completed install proves fisherman, partitioning, and post-install steps work.
- [ ] If the change touches default filesystem, encryption, or bootloader: `plain-e2e` output is mandatory, not optional.
- [ ] ISO size is ~5.3 GB (release compression, no double-embedded store)
- [ ] Skill file update committed in **this same PR** (not a follow-up)
- [ ] PR title follows Conventional Commits format
- [ ] Both AI attribution trailers present on every AI-authored commit
