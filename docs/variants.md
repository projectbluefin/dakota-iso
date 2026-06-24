# Variants

How the Dakota ISO build target works.

## Current build

| Variant | Live env image | Payload (offline store) | Bootloader | Composefs | Filesystem |
|---|---|---|---|---|---|
| `dakota` | `projectbluefin/dakota-nvidia:stable` | same | systemd-boot | yes | btrfs |
| `bluefin` | `projectbluefin/bluefin-nvidia:stable` | same | grub2 | no | btrfs |
| `bluefin-lts-hwe` | `projectbluefin/bluefin-lts-hwe-nvidia:stable` | same | grub2 | no | btrfs |
| `stable` | `projectbluefin/bluefin-nvidia:stable` | same | grub2 | no | btrfs |
| `lts` | `projectbluefin/bluefin-lts-hwe-nvidia:stable` | same | grub2 | no | btrfs |

**All variants default to btrfs. XFS is available as a user-selectable option in the installer UI only.**

There is one unified ISO: `dakota-live.iso`.

| Build target | `payload_ref` | Live boot | Offline install |
|---|---|---|---|
| `dakota` | `ghcr.io/projectbluefin/dakota-nvidia:stable` | NVIDIA live env | NVIDIA VFS store (auto-rebases to non-NVIDIA on first upgrade) |

CI and local builds both use `just iso-sd-boot dakota`. There is no separate `dakota-nvidia`
build target — the `dakota/live_target` file points to `dakota-nvidia` to select the right
container image.

## How the build target works

The `dakota/` directory contains two files:

```
dakota/
  payload_ref    ← ghcr.io/projectbluefin/dakota-nvidia:stable
  live_target    ← dakota-nvidia
```

The justfile reads `<target>/payload_ref` for the OCI image to embed and `<target>/live_target`
for the container build arg. The live container is built from `live/Containerfile` with
`--build-arg TARGET=<live_target>`:

```makefile
container target:
    podman build \
        --build-arg TARGET={{live_target}} \
        -t {{target}}-installer -f ./live/Containerfile ./live
```

The installer configs inside the ISO (`images.json`, `recipe.json`) are patched at
build time to reference the correct image via `configure-live.sh`.

## Adding a custom build target

For local testing, create a directory with `payload_ref` and optionally `live_target`:

```bash
mkdir my-variant
echo 'ghcr.io/projectbluefin/my-variant:latest' > my-variant/payload_ref
just iso-sd-boot my-variant
```

Output: `output/my-variant-live.iso`

## `images.json` — catalog lock

`live/src/etc/bootc-installer/images.json` locks the installer to show only the
available image choices. Key fields:

```json
{
  "name": "Dakota",
  "imgref": "ghcr.io/projectbluefin/dakota:latest",
  "bootloader": "systemd",
  "filesystem": "btrfs",
  "composefs": true,
  "needs_user_creation": false,
  "flatpak_var_path": "state/os/default/var"
}
```

- `bootloader: "systemd"` — installs systemd-boot, not GRUB
- `composefs: true` — enables composefs backend
- `needs_user_creation: false` — GNOME Initial Setup handles user creation at first boot
- `flatpak_var_path` — where installer places Flatpak data on the installed system

---

## Lessons

### payload_ref must not have trailing whitespace (2026-05)

The justfile strips whitespace with `tr -d '[:space:]'`, but if a script reads
`payload_ref` directly without stripping, trailing newlines cause `podman pull`
to fail with an invalid reference error. Always strip when reading payload_ref:

### Templating a variant for an unpublished image (2026-06)

Create all variant files and commit them, but add the CI matrix entry **commented out**
in `build-iso-bluefin.yml`. This keeps files reviewable without breaking CI.
When the image is published, enabling it is a single-line uncomment.

### Fedora/Silverblue-based variants: grub2 + no composefs (2026-06)

Bluefin and bluefin-lts-hwe are Fedora Silverblue bootc images. Key differences from Dakota:
- `bootloader: grub2` — fisherman skips `--bootloader` flag (grub2 is the default; only `systemd` needs explicit flag)
- `composefs: false` — ostree-native path; `ostree.final-diffid` annotation must be **removed** after squash (not updated)
- `filesystem: btrfs` for ALL variants (bluefin, bluefin-lts-hwe, dakota)
- `flatpak_var_path: var/lib/flatpak` — Silverblue stores flatpaks at the root, not in a deployment subdir
- `/usr/local` is a dangling symlink on Silverblue at build time — always write installer files to `/usr/share/`, not `/usr/local/`
```bash
cat <variant>/payload_ref | tr -d '[:space:]'
```

### Dead image refs — ublue-os org (2026-06)

`ublue-os` image names are legacy. All active images live under `ghcr.io/projectbluefin/`.
`bluefin-gdx` does not exist in `projectbluefin` — never use it.
`projectbluefin/bluefin-lts` publishes no standalone `-nvidia` image; the nvidia/HWE
variant is `bluefin-lts-hwe-nvidia:stable`. Always verify with:
```bash
skopeo list-tags docker://ghcr.io/projectbluefin/<image>
```
or read `execute-release.yml` in the source repo.

### systemd-boot title is controlled by live_title file (2026-06)

Each variant directory contains a `live_title` file whose contents appear verbatim
as the boot menu entry in systemd-boot and loopback.cfg:

```
bluefin/live_title          → Bluefin Live
bluefin-lts-hwe/live_title  → Bluefin LTS HWE Live
dakota/live_title            → Dakota Live
stable/live_title            → Bluefin Stable Live
lts/live_title               → Bluefin LTS Live
```

`build-iso.sh` accepts `--title <string>`; the justfile reads `<target>/live_title`
and passes it. To customise the title for a new variant, create a `live_title` file
in the variant directory.

### Non-composefs (ostree) offline storage: overlay additional store vs VFS size explosion (2026-06-24)

When building ISOs for non-composefs (ostree/bootcDirect) targets:
1. **Squashing is prohibited**: Squashing the payload image flattens the filesystem layer and breaks bootc's ostree unencapsulation (corrupts hardlinks, leading to `Expected commit object, not File` and missing `ostree.final-diffid` annotations).
2. **VFS size explosion**: Storing the image unsquashed in a VFS-format containers-storage additional store replicates and inflates all ~120 layers, causing `no space left on device` and OOM errors during build or installation.
3. **The Solution**: Build an unsquashed `oci-archive` payload and import it into an `overlay`-driver additional store (`/usr/lib/containers/storage`) inside the live squashfs root. To boot and run natively on EL10/LTS host kernels (which lack native overlay-on-overlay support), configure the live ISO's `/etc/containers/storage.conf` to use `fuse-overlayfs` as the mount program:
   ```toml
   [storage]
   driver = "overlay"
   additionalimagestores = ["/usr/lib/containers/storage"]

   [storage.options.overlay]
   mount_program = "/usr/bin/fuse-overlayfs"
   ```
4. **Fisherman targetImgref**: Ensure that `targetImgref` is populated in the fisherman recipe JSON. If omitted, fisherman's direct-mode path runs bootc without the `--source-imgref` argument, failing with `Either --source-imgref must be defined or this command must be executed inside a podman container`.

