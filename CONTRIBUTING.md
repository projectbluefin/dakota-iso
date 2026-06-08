# CONTRIBUTING

Thanks for helping out!

`dakota-iso` builds the bootable live ISO for [Dakota](https://github.com/projectbluefin/dakota), including the live environment and installer wiring. Changes here affect the bootable installer path — test locally before pushing.

## Before you start

Read `AGENTS.md` and the relevant skill file in `docs/` before making changes.
The docs are the source of truth for build quirks, disk space requirements, and CI constraints.

## Prerequisites

- `podman` (rootless)
- `xorriso` (via brew: `brew install xorriso`, or distro package)
- `mtools` (via brew or distro)
- ~25 GB free disk space on `/var` (not `/tmp` — tmpfs is too small)
- See [`docs/build.md`](docs/build.md) for full setup details

## Local build workflow

```bash
git clone https://github.com/projectbluefin/dakota-iso
cd dakota-iso
just iso-sd-boot dakota         # full ISO build
just iso-sd-boot dakota-nvidia  # nvidia variant
just boot-iso-serial dakota     # boot + smoke test in QEMU
```

## PR workflow

All PRs target `main`. PRs require:
- A description of what changed and why
- Evidence that the ISO built and booted (or explicit statement it's a docs/ci-only change)
- Both AI attribution trailers if AI-assisted (see `AGENTS.md`)

```bash
gh pr create --repo projectbluefin/dakota-iso --base main
```

## CI

Two workflows run on PRs:
- `lint.yml` — shell and YAML hygiene
- `test.yml` — container build smoke test

Full ISO builds and LUKS E2E tests run on push to `main` and weekly. See [`docs/ci.md`](docs/ci.md).

## Related repos

- [`projectbluefin/dakota`](https://github.com/projectbluefin/dakota) — source images this repo packages into ISOs
- [`projectbluefin/common`](https://github.com/projectbluefin/common) — shared OCI layer, org-level factory docs
- [`projectbluefin/bootc-installer`](https://github.com/projectbluefin/bootc-installer) — the Flatpak installer bundled in the live ISO

Useful validation steps:
```bash
just container dakota
just boot-iso-serial dakota
```
`just boot-iso-serial <target>` is the quickest local smoke test for a bootable ISO.

## Repo notes
- Key scripts: `dakota/src/build-iso.sh` and `dakota/src/configure-live.sh`
- The `justfile` is the canonical interface for local work
- Prefer small, surgical changes: this repo feeds the Dakota installer media

## Pull requests
- Open PRs against `main`
- Use Conventional Commits (`docs:`, `fix:`, `feat:`, etc.)
- In the PR description, say what you built locally and how you verified boot/install behavior
- If you touch build logic or installer behavior, test the resulting ISO before pushing
