# docs/ — In-Repo Knowledge Base

Accumulated lessons from real work on this repo.
Every agent working here should read the relevant file before starting.

When you fix a bug or discover a pattern, add a lesson here in the same PR as your change.
This is the feedback loop: lessons help every future agent and contributor.

## Skills

| File | Load when... |
|---|---|
| [`build.md`](build.md) | Building ISOs locally, disk space, BTRFS/XFS quirks, `just` variables |
| [`architecture.md`](architecture.md) | Two-container pipeline, boot flow, squashfs, VFS containers-storage |
| [`ci.md`](ci.md) | `build-iso.yml`, `test-luks-install.yml`, R2 uploads, smoke test |
| [`luks-testing.md`](luks-testing.md) | LUKS E2E test — local QEMU, libvirt, CI-equivalent flow |
| [`r2-promotion.md`](r2-promotion.md) | Promoting ISOs to production, rclone, named releases |
| [`variants.md`](variants.md) | Adding/modifying variants, `payload_ref` pattern |

## How to add a lesson

1. Open the relevant file (or create a new one if no file covers the area)
2. Add a section at the bottom: `### <what you learned> (YYYY-MM-DD)`
3. What failed → why → the fix → code example
4. Commit it in the same PR as your change
