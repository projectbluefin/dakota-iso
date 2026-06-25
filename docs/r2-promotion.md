# R2 Promotion

Managing Dakota ISOs in Cloudflare R2: promoting builds, creating named releases,
and maintaining the `latest` pointers.

## ⛔ Critical rule: never manually overwrite `latest`

**`dakota-live-latest.iso` is the production artifact users download. Only CI
may write to it — after the E2E gate passes.**

Manual `rclone copyto` to the `latest` pointer is prohibited. It bypasses
the CI E2E test gate (`plain-e2e`) and ships untested ISOs to users.
This caused a production outage in June 2026 (broken XFS install, issue [#85](https://github.com/projectbluefin/dakota-iso/issues/85)).

✅ Allowed: `rclone copyto` to **dated slots** (`dakota-live-20260615-abc.iso`) for archival
✅ Allowed: `rclone copyto` between dated slots to create **named releases** (`alpha3`) after CI E2E passed
❌ Prohibited: overwriting `dakota-live-latest.iso` from a local build
❌ Prohibited: overwriting `dakota-live-latest.iso-CHECKSUM` from a local build

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
| `dakota-live-alpha2.iso` | `20260614-7ef17bd` | Rebuilt with bootc-installer v2.7.4 (ENOSPC + composefs hostname fixes) |
| `dakota-live-alpha3.iso` | `20260614-9939dd7` | First build with fixed installer (fisherman v0.2.1, bootc-installer v2.7.3) |
| `dakota-live-alpha4.iso` | `20260618-f095551` | composefs installed-boot fix; root-mount-spec injection in build-live-squashfs.sh |
| `dakota-live-latest.iso` | Latest CI build | Auto-updated by monthly `build-iso.yml` |

## Public URLs

```
https://projectbluefin.dev/dakota-live-latest.iso
https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM
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
2. Latest: `dakota-live-latest.iso` — overwritten on every successful monthly build

The dated copy is the source of truth for promotions. Always promote from a dated
ISO, never from latest (latest may change).

## Rotating R2 credentials

Use this procedure when credentials are compromised or as routine rotation.

**Step 1 — Create new token (Cloudflare dashboard, ~60 seconds):**
1. `dash.cloudflare.com → R2 → Manage R2 API Tokens → Create token`
2. Set permissions: `Object Read & Write` on the `testing` bucket
3. Copy the new `access_key_id` and `secret_access_key`

**Step 2 — Update GitHub secrets:**
```bash
gh secret set RCLONE_CONFIG_R2_ACCESS_KEY_ID --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_SECRET_ACCESS_KEY --repo projectbluefin/dakota-iso
# each prompts for the value — paste, Enter, done
```

**Step 3 — Revoke old token** in the Cloudflare dashboard.

**Step 4 — Verify:**
```bash
gh secret list --repo projectbluefin/dakota-iso
# RCLONE_CONFIG_R2_ACCESS_KEY_ID and RCLONE_CONFIG_R2_SECRET_ACCESS_KEY
# should show today's timestamp
```

> Note: `wrangler` cannot create or revoke R2 API tokens — the OAuth token from
> `wrangler login` does not carry the `api_tokens:edit` scope. Steps 1 and 3
> require the Cloudflare dashboard. Only step 2 is CLI-automatable.

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

### rclone endpoint uses account-ID hostname — keep it configurable (2026-06)

The R2 endpoint `https://<account-id>.r2.cloudflarestorage.com` embeds the
account ID as a hostname. Do not hardcode it in documentation — the format is
required by rclone's S3 provider and cannot be shortened to a path-based form.
When setting up a new rclone config, prompt the user to fill in their account ID
rather than copying a pre-filled value from docs.

### There is only one ISO — delete ghost nvidia-live-* files if they reappear (2026-06)

The CI produces a single unified ISO (`dakota-live.iso`) built from the
`dakota-nvidia:stable` image. There is no separate nvidia variant.

At one point an older CI layout uploaded `dakota-nvidia-live-*.iso` files.
Those were never updated after the unified ISO landed and became permanently
stale ghost files on R2. They were deleted in June 2026.

If `dakota-nvidia-live-*` files reappear in R2 (e.g. from an old workflow
branch being re-run), delete them:
```bash
for f in dakota-nvidia-live-latest.iso dakota-nvidia-live-latest.iso-CHECKSUM \
         dakota-nvidia-live-alpha2.iso  dakota-nvidia-live-alpha2.iso-CHECKSUM; do
  rclone deletefile R2:testing/$f
done
```
Do not promote them or reference them in docs.

### Overwriting a named release — promote from a dated ISO, not from latest (2026-06)

When a named release (e.g. alpha2) needs to be replaced, always copy from the
dated ISO — never from `dakota-live-latest.iso`. Latest may be updated by CI
between your copy commands, producing an inconsistent ISO/checksum pair.

```bash
# Correct: source is pinned dated build
rclone copyto -v R2:testing/dakota-live-YYYYMMDD-SHA.iso R2:testing/dakota-live-alpha2.iso
rclone copyto -v R2:testing/dakota-live-YYYYMMDD-SHA.iso-CHECKSUM R2:testing/dakota-live-alpha2.iso-CHECKSUM
```
