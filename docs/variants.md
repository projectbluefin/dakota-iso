# Variants

How Dakota ISO variants work and how to add new ones.

## Current variants

| Variant | `payload_ref` | Role in unified ISO |
|---|---|---|
| `dakota` | `ghcr.io/projectbluefin/dakota:latest` | offline install (in `store.squashfs.img`) |
| `dakota-nvidia` | `ghcr.io/projectbluefin/dakota-nvidia:latest` | live boot environment |

CI produces **one unified ISO** (`dakota-live.iso`). The NVIDIA variant boots live; both
variants are available for installation via the embedded offline store.

## How variants work

Each variant is a directory containing a single file — `payload_ref` — with the OCI
image reference.

```
dakota/
  payload_ref    ← ghcr.io/projectbluefin/dakota:latest
dakota-nvidia/
  payload_ref    ← ghcr.io/projectbluefin/dakota-nvidia:latest
```

The justfile reads `<target>/payload_ref` and uses it for squashfs assembly. The live
container is built from `live/Containerfile` with `--build-arg TARGET=<target>`:

```makefile
container target:
    podman build \
        --build-arg TARGET={{target}} \
        -t {{target}}-installer -f ./live/Containerfile ./live
```

The installer configs inside the ISO (`images.json`, `recipe.json`) are patched at
build time to reference the correct image via `configure-live.sh`.

## Adding a new variant

For local testing, create a `<variant>/payload_ref` and run `just iso-sd-boot`:

```bash
mkdir my-variant
echo 'ghcr.io/projectbluefin/my-variant:latest' > my-variant/payload_ref
just iso-sd-boot my-variant
```

Output: `output/my-variant-live.iso`

To include the new variant in CI's unified ISO, add it to the `build-offline-store.sh`
invocation in `build-iso.yml` so the offline store carries the new image:

```yaml
- name: Build offline image store squashfs
  run: |
    sudo bash scripts/build-offline-store.sh \
      /var/iso-build/store.squashfs.img \
      ghcr.io/projectbluefin/dakota-nvidia:latest \
      ghcr.io/projectbluefin/dakota:latest \
      ghcr.io/projectbluefin/my-variant:latest   # ← add here
```

And if the new variant should boot live (not just be installable), update the
live container build step to use `--build-arg TARGET=my-variant`.

## Installer branding per variant

The installer branding (`distro_name`, `distro_logo`, tour slides) is defined in
`live/src/etc/bootc-installer/recipe.json`. This is shared across all variants.

If a variant needs different branding:
1. Create `<variant>/recipe.json` with the custom content
2. Update `configure-live.sh` to copy `<variant>/recipe.json` if it exists,
   falling back to `live/src/etc/bootc-installer/recipe.json`

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
