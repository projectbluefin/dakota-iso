# Variants

How the Dakota ISO build target works.

## Current build

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
```bash
cat <variant>/payload_ref | tr -d '[:space:]'
```
