# Variants

How Dakota ISO variants work and how to add new ones.

## Current variants

| Variant | `payload_ref` | ISO output |
|---|---|---|
| `dakota` | `ghcr.io/projectbluefin/dakota:latest` | `dakota-live.iso` |
| `dakota-nvidia` | `ghcr.io/projectbluefin/dakota-nvidia:latest` | `dakota-nvidia-live.iso` |

## How variants work

Each variant is a directory containing a single file — `payload_ref` — with the OCI
image reference. Everything else (Containerfile, scripts, ISO assembly) is shared.

```
dakota/
  payload_ref    ← ghcr.io/projectbluefin/dakota:latest
dakota-nvidia/
  payload_ref    ← ghcr.io/projectbluefin/dakota-nvidia:latest
```

The justfile reads `<target>/payload_ref` and passes it as the `BASE_IMAGE` build-arg:

```makefile
container target:
    podman build \
        --build-arg BASE_IMAGE=$(cat {{target}}/payload_ref | tr -d '[:space:]') \
        -t {{target}}-installer -f ./dakota/Containerfile ./dakota
```

The installer configs inside the ISO (`images.json`, `recipe.json`) are patched at
build time to reference the correct image via `configure-live.sh`.

## Adding a new variant

```bash
mkdir my-variant
echo 'ghcr.io/projectbluefin/my-variant:latest' > my-variant/payload_ref
just iso-sd-boot my-variant
```

Output: `output/my-variant-live.iso`

That's it — no Containerfile changes needed unless the new variant requires
custom installer config or branding.

## Adding a variant to CI

To build a new variant in `build-iso.yml`, add it to the matrix:

```yaml
matrix:
  target: [dakota, dakota-nvidia, my-variant]
```

And add the matching R2 upload lines in the upload step if the variant
should publish to `projectbluefin.dev`.

## Installer branding per variant

The installer branding (`distro_name`, `distro_logo`, tour slides) is defined in
`dakota/src/etc/bootc-installer/recipe.json`. This is shared across all variants.

If a variant needs different branding:
1. Create `<variant>/recipe.json` with the custom content
2. Update `configure-live.sh` to copy `<variant>/recipe.json` if it exists,
   falling back to `dakota/src/etc/bootc-installer/recipe.json`

## `images.json` — catalog lock

`dakota/src/etc/bootc-installer/images.json` locks the installer to show only one
image choice. Key fields:

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
