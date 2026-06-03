# Contributing

## What this repo does
`dakota-iso` builds the bootable live ISO for Dakota/Bluefin, including the live environment and installer wiring. Changes here affect the bootable installer path, so test locally before pushing.

## Prerequisites
- `podman`
- `xorriso`
- `mtools`
- `cpio`
- enough free disk space for ISO artifacts (see `README.md`)

## Local build workflow
```bash
git clone https://github.com/projectbluefin/dakota-iso
cd dakota-iso
just iso-sd-boot dakota
# or
just iso-sd-boot dakota-nvidia
```

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
