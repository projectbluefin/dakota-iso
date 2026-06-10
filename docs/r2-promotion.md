# R2 Promotion

Managing Dakota ISOs in Cloudflare R2: promoting builds, creating named releases,
and maintaining the `latest` pointers.

## Bucket layout

| Bucket | Purpose |
|---|---|
| `testing` | All builds — CI uploads here, all ISOs are public via projectbluefin.dev |

**Endpoint:** `https://2a4147f637f7d9e6a67ca185357d3b0a.r2.cloudflarestorage.com`  
**Account ID:** `2a4147f637f7d9e6a67ca185357d3b0a`

ISOs are permanent — no expiry. Full history from 2026-04-10.

## rclone config (`~/.config/rclone/rclone.conf`)

> Credentials are not stored in this repo. Request them from a project maintainer
> or retrieve them from the shared password manager.

```ini
[R2]
type = s3
provider = Cloudflare
region = auto
access_key_id = <your-r2-access-key-id>
secret_access_key = <your-r2-secret-access-key>
endpoint = https://2a4147f637f7d9e6a67ca185357d3b0a.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
```

⚠️ `no_check_bucket = true` is **required** — without it, `CopyObject` hangs on large files.  
⚠️ `acl = private` is required per Cloudflare docs for object-level permission tokens.

## Common operations

```bash
# List bucket contents
rclone ls R2:testing | grep dakota | sort -k2

# Promote a dated ISO to latest (server-side copy — takes 2–5 min for 4–5 GB)
rclone copyto -v \
  R2:testing/dakota-live-YYYYMMDD-<sha>.iso \
  R2:testing/dakota-live-latest.iso

# Always update the checksum too
rclone copyto -v \
  R2:testing/dakota-live-YYYYMMDD-<sha>.iso-CHECKSUM \
  R2:testing/dakota-live-latest.iso-CHECKSUM

# Create a named release (e.g., alpha2)
rclone copyto -v \
  R2:testing/dakota-live-YYYYMMDD-<sha>.iso \
  R2:testing/dakota-live-alpha2.iso
```

## Named ISOs

| Name | Source | Notes |
|---|---|---|
| `dakota-live-alpha2.iso` | `20260508-3059a71` | Last build with fisherman v0.1.0 before v0.2.0 regression |
| `dakota-nvidia-live-alpha2.iso` | `20260508-3059a71` | Nvidia variant, same build |
| `dakota-live-latest.iso` | Latest CI build | Auto-updated by `build-iso.yml` |
| `dakota-nvidia-live-latest.iso` | Latest CI build | Auto-updated by `build-iso.yml` |

## Public URLs

```
https://projectbluefin.dev/dakota-live-latest.iso
https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM
https://projectbluefin.dev/dakota-nvidia-live-latest.iso
https://projectbluefin.dev/dakota-nvidia-live-latest.iso-CHECKSUM
```

Named releases follow the same pattern:
```
https://projectbluefin.dev/dakota-live-alpha2.iso
```

## Verifying an ISO without downloading it

```bash
# Fetch just the first 2 KB (GPT headers) and check partition type
curl --range 0-2047 https://projectbluefin.dev/dakota-live-latest.iso -o /var/tmp/head.bin
fdisk -l /var/tmp/head.bin

# Check MBR type byte (0xEE = protective/good, 0x00 = missing)
printf "MBR type: 0x%02x\n" "$(od -An -tx1 -j450 -N1 /var/tmp/head.bin | tr -d ' ')"
```

Expected: `Disklabel type: gpt` from gdisk/parted. Note that `fdisk` shows `dos`
for hybrid layouts — this is normal. What matters is the GPT EFI System Partition type.

## Inspect GPT without downloading (xorriso in container)

```bash
podman run --rm \
    -v ./output:/iso:ro \
    debian:sid \
    bash -c "
        apt-get update -qq >/dev/null
        apt-get install -y -qq xorriso >/dev/null 2>&1
        xorriso -indev /iso/dakota-live.iso -report_system_area plain 2>/dev/null
    "
# Must show: GPT type GUID: 28732ac1... (EFI System Partition OK)
```

## CI upload (build-iso.yml)

CI uploads two copies of every ISO automatically:
1. Dated: `dakota-live-YYYYMMDD-<sha>.iso` — permanent, never overwritten
2. Latest: `dakota-live-latest.iso` — overwritten on every successful build

The dated copy is the source of truth for promotions. Always promote from a dated
ISO, never from latest (latest may change).

---

## Lessons

### Direct uploads from local host hang/fail (2026-05)

Uploading multi-GB ISOs directly from this host to R2 hangs indefinitely.
Root cause: likely a routing/MTU issue specific to this network.

Fix: always use R2→R2 server-side copies (`rclone copyto R2:testing/src R2:testing/dst`).
Server-side copies take 2–5 min for 4–5 GB files — this is normal, do not assume failure.

### no_check_bucket = true required in rclone config (2026-05)

Without `no_check_bucket = true`, rclone's `CopyObject` call to the Cloudflare R2 API
hangs indefinitely on large files. This is a known Cloudflare R2 behavior.
Always include this in the rclone R2 config.
