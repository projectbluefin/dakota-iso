# R2 Promotion

Managing Dakota ISOs in Cloudflare R2: promoting builds, creating named releases,
and maintaining the `latest` pointers.

## ⛔ Critical rule: never manually overwrite `latest`

**`dakota-live-latest.iso` is the production artifact users download. Only CI
may write to it — after the E2E gate passes.**

Manual `rclone copyto` to the `latest` pointer is prohibited. It bypasses
the CI E2E test gate (`plain-e2e`) and ships untested ISOs to users.
This caused a production outage in June 2026 (broken XFS install, issue [#85](https://github.com/projectbluefin/dakota-iso/issues/85)).

✅ Allowed: `rclone copyto` from current latest objects to create/update **named releases** (`alpha3`) after CI E2E passed
❌ Prohibited: overwriting `dakota-live-latest.iso` from a local build
❌ Prohibited: overwriting `dakota-live-latest.iso-CHECKSUM` from a local build

## Bucket layout

| Bucket | Purpose |
|---|---|
| `testing` | All builds — CI uploads here, all ISOs are public via projectbluefin.dev |

**Endpoint:** `https://2a4147f637f7d9e6a67ca185357d3b0a.r2.cloudflarestorage.com`
**Account ID:** `2a4147f637f7d9e6a67ca185357d3b0a`

Latest pointers are long-lived. Dated build objects are no longer retained by policy.

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

# Create a named release (e.g., alpha2)
rclone copyto -v \
  R2:testing/dakota-live-latest.iso \
  R2:testing/dakota-live-alpha2.iso
rclone copyto -v \
  R2:testing/dakota-live-latest.iso-CHECKSUM \
  R2:testing/dakota-live-alpha2.iso-CHECKSUM
```

## Backup rotation

Each CI build automatically rotates 3 backup slots before uploading the new latest:

```
backup-2 → backup-3
backup-1 → backup-2
latest   → backup-1
new ISO  → latest
```

Slots beyond 3 are auto-pruned by the workflow. You never need to manage backup rotation manually.

## One-time cleanup (historical — June 2026)

The transition from dated publish (YYYYMMDD-SHA objects) to latest-only was performed in
June 2026. All dated objects were deleted at that time. The bucket no longer contains dated
objects by policy. If dated objects reappear (e.g. from an old workflow branch), delete them:

```bash
# Preview dated objects first (safe read-only)
rclone lsf R2:testing | grep -E '^[a-z0-9-]+-live-[0-9]{8}-[0-9a-f]{7}\.iso(-CHECKSUM)?$' | sort

# Delete them
rclone lsf R2:testing | grep -E '^[a-z0-9-]+-live-[0-9]{8}-[0-9a-f]{7}\.iso(-CHECKSUM)?$' | \
while read -r f; do
  rclone deletefile "R2:testing/${f}"
done
```

## Named ISOs

| Name | Source | Notes |
|---|---|---|
| `dakota-live-alpha2.iso` | `20260614-7ef17bd` | Rebuilt with bootc-installer v2.7.4 (ENOSPC + composefs hostname fixes) |
| `dakota-live-alpha3.iso` | `20260614-9939dd7` | First build with fixed installer (fisherman v0.2.1, bootc-installer v2.7.3) |
| `dakota-live-alpha4.iso` | `20260618-f095551` | composefs installed-boot fix; root-mount-spec injection in build-live-squashfs.sh |
| `dakota-live-alpha5.iso` | `20260701-7ee8e14` | New alpha5 release from the triggered build |
| `dakota-live-alpha6.iso` | _to be confirmed_ | Planned: not yet promoted. `build-iso.yml` rotates `dakota-live-latest.iso` daily, so the source build ID is whichever build is latest when `promote-release.yml` is dispatched from `main`; record it from the run summary afterwards |
| `dakota-live-latest.iso` | Latest CI build | Auto-updated by daily `build-iso.yml` |

## Public URLs

```
https://projectbluefin.dev/dakota-live-latest.iso
https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM
https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM.sigstore.json
https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM.sig
https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM.pem
https://projectbluefin.dev/dakota-live-backup-1.iso
https://projectbluefin.dev/dakota-live-backup-2.iso
https://projectbluefin.dev/dakota-live-backup-3.iso

https://projectbluefin.dev/utah-live-latest.iso
https://projectbluefin.dev/utah-live-latest.iso-CHECKSUM
https://projectbluefin.dev/utah-live-latest.iso-CHECKSUM.sigstore.json
https://projectbluefin.dev/utah-live-latest.iso-CHECKSUM.sig
https://projectbluefin.dev/utah-live-latest.iso-CHECKSUM.pem
https://projectbluefin.dev/utah-live-backup-1.iso
https://projectbluefin.dev/utah-live-backup-2.iso
https://projectbluefin.dev/utah-live-backup-3.iso
```

The `.sigstore.json` bundle is the keyless cosign signature of the `latest` checksum
(the primary artifact since cosign v3); see
[Verifying a downloaded ISO](ci.md#verifying-a-downloaded-iso) for the check. The `.sig`/`.pem`
pair is the same keyless signature in the older separate-file format, still published
alongside for now. Named releases carry the same signature, copied unchanged by
`promote-release.yml`; backup slots carry no signature.

The `bluefin-live-*` and `bluefin-lts-hwe-live-*` URLs are retired (#228): those matrix
entries are commented out in `build-iso-utah.yml` and are not `promote-release.yml`
choices, so nothing refreshes them. Any objects still in the bucket are stale.

Named releases follow the same pattern:
```
https://projectbluefin.dev/dakota-live-alpha5.iso
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

CI uploads latest objects only:
1. `dakota-live-latest.iso`
2. `dakota-live-latest.iso-CHECKSUM`
3. `dakota-live-latest.iso-CHECKSUM.sigstore.json` / `.sig` / `.pem` — keyless cosign signature of the checksum (the `.sigstore.json` bundle is the primary artifact; `.sig`/`.pem` kept alongside)

Named releases are manual snapshots copied from latest after the CI gate passes.
`promote-release.yml` copies the signed `-CHECKSUM` byte-for-byte together with its
`.sigstore.json` bundle (and `.sig`/`.pem` when present), then runs `cosign verify-blob`
on what landed, so a named release verifies with the same
[procedure](ci.md#verifying-a-downloaded-iso) as `latest`. The signed checksum names
the `latest` object; a separate, unsigned `<name>.iso-SHA256SUMS` carries the same hash
with the promoted filename for `sha256sum -c`.

## Credential scope: protected deployment environments

The R2 write credentials can publish or replace the live ISO objects, so they
are the most valuable secret in this repo. Four workflows read them or the
download statistics, and every one of them takes a `workflow_dispatch`. Since
#404 each reader sits behind a deployment environment whose deployment branch
rule allows `main` only.

| Environment | Reader | Secret | Deployment branch rule |
|---|---|---|---|
| `r2-production` | `promote` job in `promote-release.yml` | `RCLONE_CONFIG_R2_ACCESS_KEY_ID`, `RCLONE_CONFIG_R2_SECRET_ACCESS_KEY`, `RCLONE_CONFIG_R2_ENDPOINT` | `main` only |
| `r2-production` | `build-and-publish` job in `build-iso.yml` (nightly Dakota publish) | the same three | `main` only |
| `r2-production` | `build-and-publish` job in `build-iso-utah.yml` (monthly Utah publish) | the same three | `main` only |
| `r2-observability` | `count` job in `r2-download-stats.yml` | `CF_OBSERVABILITY_TOKEN` | `main` only |

The two publish lanes carry the environment on their `build-and-publish`
job. `environment:` is a job-level key — GitHub Actions has no step-level
form, and a step-level key is rejected as invalid workflow syntax, which
stopped both ISO publish workflows from running at all (#407). The
environment therefore gates the whole job: a dispatch from any other branch
is refused the environment and the job does not start, rather than starting
and skipping only the upload.

**Why an environment:** a `workflow_dispatch` needs the workflow file to exist on
the default branch, but the run executes the copy of the workflow on the branch
that was picked. With repository-scoped secrets, a collaborator with write access
could put altered workflow code on a side branch, dispatch it, and read the
credentials — the reviewed copy on `main` never runs. At environment scope, with
the deployment branch rule set to `main` only, that dispatch receives no
credentials.

A repository-scoped copy of the same secret is the fallback a step reads when
it declares no environment, so the copies have to go (step 4 below). While they
exist, the environment is the only thing standing between a side branch and
the key, and a side branch that drops the `environment:` line still gets it.

**Repository settings a maintainer applies:**

The order matters at every step. The environments must exist and hold the
secrets before the workflows that name them are merged: GitHub creates a
missing environment on first use with no protection rules, so merging first
would leave the credentials unprotected under the environment's name. The
repository-scoped copies may only be deleted once every reader has moved to
the environment — the nightly publish lanes included — and after a publish
from `main` has proved that lane works on the environment's copy.

```bash
# 1. Create the two environments and restrict deployment to main only.
#    (gh api -F takes typed scalars; nested objects use key[subkey]=value.)
gh api -X PUT repos/projectbluefin/dakota-iso/environments/r2-production \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X PUT repos/projectbluefin/dakota-iso/environments/r2-observability \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/projectbluefin/dakota-iso/environments/r2-production/deployment-branch-policies \
  -F name='main' -F type='branch'
gh api -X POST repos/projectbluefin/dakota-iso/environments/r2-observability/deployment-branch-policies \
  -F name='main' -F type='branch'

# 2. Copy the secrets to environment scope (values come from the password
#    manager; the CLI prompts for each value). The repository-scoped copies
#    stay for now: the publish lanes still read them until this change is
#    merged, and deleting them earlier would break the nightly upload.
gh secret set RCLONE_CONFIG_R2_ACCESS_KEY_ID     --env r2-production     --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_SECRET_ACCESS_KEY --env r2-production     --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_ENDPOINT          --env r2-production     --repo projectbluefin/dakota-iso
gh secret set CF_OBSERVABILITY_TOKEN             --env r2-observability  --repo projectbluefin/dakota-iso

# 3. Merge this change. Every reader now declares an environment, so the
#    environment is what hands out the credentials.

# 4. Confirm a publish from main still works, then delete the
#    repository-scoped copies. Until this step they are live credentials that
#    a workflow reading them without an environment still receives.
gh workflow run build-iso.yml --ref main
#    let the run finish and check the upload step succeeded (the
#    r2-production environment lists the deployment), then:
gh secret delete RCLONE_CONFIG_R2_ACCESS_KEY_ID     --repo projectbluefin/dakota-iso
gh secret delete RCLONE_CONFIG_R2_SECRET_ACCESS_KEY --repo projectbluefin/dakota-iso
gh secret delete RCLONE_CONFIG_R2_ENDPOINT          --repo projectbluefin/dakota-iso
gh secret delete CF_OBSERVABILITY_TOKEN             --repo projectbluefin/dakota-iso
```

`r2-production` must not require an approving reviewer: the nightly publish
lanes share it, they run on a schedule, and a scheduled run cannot collect a
human approval. `r2-observability` must not either, because the stats job also
runs on a daily schedule. If a reviewer is wanted for the manual promotion
tool, give that job an environment of its own.

A new job or step that reads one of these credentials needs an environment that
gates it. `tests/test_workflow_protected_environments.py` fails when a reader
in any of these workflows has none, and when a workflow outside that list reads
a protected credential at all.

Put that environment on the **job**, never on the step. GitHub Actions defines
`environment:` only under `jobs.<job_id>`; a step-level key is invalid
workflow syntax and GitHub rejects the whole file, so the upload step of both
ISO publish workflows once took the lane down with it (#407).

## Rotating R2 credentials

Use this procedure when credentials are compromised or as routine rotation.

The rule the whole procedure exists to keep: **no reader may still hold the
old key when it is revoked.** A scope that keeps a copy of the old value while
the new key sits in the environment keeps working until the old token is
revoked, and then fails at the next nightly publish.

**Step 1 — Create new token (Cloudflare dashboard, ~60 seconds):**
1. `dash.cloudflare.com → R2 → Manage R2 API Tokens → Create token`
2. Set permissions: `Object Read & Write` on the `testing` bucket
3. Copy the new `access_key_id` and `secret_access_key`

**Step 2 — Find every scope that holds a copy:**
```bash
gh secret list --repo projectbluefin/dakota-iso
gh secret list --env r2-production --repo projectbluefin/dakota-iso
```
Every scope listed above holds a copy of the key and must be updated in step 3.
After the setup above, the only scope is the `r2-production` environment; a
repository-scoped copy means that setup was not finished.

**Step 3 — Update every scope that holds a copy:**
```bash
gh secret set RCLONE_CONFIG_R2_ACCESS_KEY_ID     --env r2-production --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_SECRET_ACCESS_KEY --env r2-production --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_ENDPOINT          --env r2-production --repo projectbluefin/dakota-iso
# each prompts for the value — paste, Enter, done
# the credentials live in the r2-production environment (see above)
#
# If step 2 still listed a repository-scoped copy, update or delete it too:
gh secret set RCLONE_CONFIG_R2_ACCESS_KEY_ID     --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_SECRET_ACCESS_KEY --repo projectbluefin/dakota-iso
gh secret set RCLONE_CONFIG_R2_ENDPOINT          --repo projectbluefin/dakota-iso
```

**Step 4 — Verify every scope now holds the new key, then publish once:**
```bash
gh secret list --env r2-production --repo projectbluefin/dakota-iso
gh secret list --repo projectbluefin/dakota-iso
# RCLONE_CONFIG_R2_ACCESS_KEY_ID and RCLONE_CONFIG_R2_SECRET_ACCESS_KEY
# should show today's timestamp
```
Then publish once from `main` (`gh workflow run build-iso.yml --ref main`) and
confirm the upload step succeeded, so the nightly lane is known to work on the
new key before the old one disappears.

**Step 5 — Revoke old token** in the Cloudflare dashboard. Only now: revoking
it while a reader still holds the old value breaks that reader.

> Note: `wrangler` cannot create or revoke R2 API tokens — the OAuth token from
> `wrangler login` does not carry the `api_tokens:edit` scope. Steps 1 and 5
> require the Cloudflare dashboard. Only steps 2 to 4 are CLI-automatable.

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

### Overwriting a named release — copy ISO and checksum together (2026-06)

When a named release (e.g. alpha2) needs to be replaced, always copy both
the ISO and checksum in the same session so they stay paired.

```bash
rclone copyto -v R2:testing/dakota-live-latest.iso R2:testing/dakota-live-alpha2.iso
rclone copyto -v R2:testing/dakota-live-latest.iso-CHECKSUM R2:testing/dakota-live-alpha2.iso-CHECKSUM
```

### GitHub-native named release promotion via promote-release.yml (2026-09-21)

Named releases historically required a manual `rclone copyto` command run by an
operator with local R2 credentials. `.github/workflows/promote-release.yml` codifies
this as a GitHub Actions workflow using repository R2 secrets.

Key invariants codified in the workflow:
1. **Workflow dispatch restriction**: the workflow file must exist on the default
   branch (`main`) before it can be dispatched at all. That is *not* the same as
   "the dispatch runs the reviewed copy": a dispatch can select another branch, and
   the run then executes that branch's copy of the workflow file. Credential scope
   is what protects the run — see the deployment-environment section above and the
   2026-11-25 lesson below.
2. **Signed checksum is never rewritten**: the `-CHECKSUM` is signed by the build
   workflow, so rewriting it (as this workflow originally did, for the promoted filename)
   breaks its Sigstore bundle and left named releases unverifiable (#383). The checksum and
   its bundle are copied unchanged and checked with `cosign verify-blob`; the renamed
   `sha256sum -c` line goes into a separate unsigned `-SHA256SUMS` file.
3. **Source presence and size parity**: `.iso`, `-CHECKSUM` and `-CHECKSUM.sigstore.json` must exist at the source,
   and byte sizes are validated after copying to ensure no partial promotion occurs.
4. **Publish race (known gap, not yet enforced)**: `build-iso.yml` rewrites
   `*-live-latest.iso` daily and the ISO and checksum copies are not atomic. A publish
   landing between them promotes a mixed pair that current verification cannot detect,
   because it compares the two checksum files rather than hashing the promoted ISO.
   Until that is fixed, dispatch promotions well clear of the daily build window.

### A dispatch runs the branch's workflow code, not main's (2026-11-25)

The `promote-release.yml` and `r2-download-stats.yml` jobs read long-lived
Cloudflare credentials. A `workflow_dispatch` requires the workflow file to exist
on the default branch, but the run executes the workflow file **from the branch
that was dispatched** — so a collaborator with write access could push altered
workflow code to a side branch, dispatch it, and read the credentials that the
reviewed `main` copy never touches.

Fix (#404): each job declares a deployment environment — `r2-production` for the
promotion job, `r2-observability` for the stats job — the credentials live at
environment scope, and the environment's deployment branch rule allows `main`
only. A dispatch from any other branch then gets no credentials.

What an agent or maintainer should do differently:

- A job that reads a credential gets an `environment:`. `tests/test_workflow_protected_environments.py`
  enforces this for all four workflows that read one.
- The environment must exist in the repository settings **before** the workflow
  that names it is merged. GitHub creates a missing environment on first use with
  no protection rules, so the order is settings first, merge second.
- A scheduled job cannot wait for a human approval, so an environment that a
  scheduled workflow uses must not require reviewers. Both environments stay
  reviewer-free: `r2-observability` carries the daily stats run, and
  `r2-production` now carries the nightly and monthly publish lanes as well,
  which run on a schedule too. A required reviewer on `r2-production` would
  hang the nightly publish job forever, so a reviewer for the manual promotion
  tool needs an environment of its own.

### `environment:` is job-level only — a step-level key takes the whole lane down (2026-11-25)

**What failed:** the protected-environment change (#404) put
`environment: r2-production` on the `Upload ISO to Cloudflare R2` **step** of
`build-iso.yml` and `build-iso-utah.yml`, to gate only the step that reads the
R2 credentials. GitHub Actions has no step-level `environment:` key — it exists
only under `jobs.<job_id>` — so GitHub rejected both workflow files outright
("Unexpected value 'environment'"). Both ISO publish lanes stopped running, and
the nightly upload of the live ISO stopped with them (#407).

**Why:** a narrower gate looked like a better design, but the Actions workflow
schema has no place to put it. The workflow file is validated as a whole, so an
unknown key anywhere in it takes the entire workflow down, not just the step.

**Fix:** put `environment: r2-production` on the `build-and-publish` job of both
publish workflows, and leave the upload step with no `environment:` of its own.
The job is then the smallest unit that can be gated, and a dispatch from a
branch the environment does not allow never starts the job.

What an agent or maintainer should do differently:

- Put the environment on the job, never on a step.
- `tests/test_workflow_protected_environments.py` now asserts that no step in
  any workflow carries an `environment:` key, and that the job that declares one
  is the job that reads the credential.
- A workflow that reads a protected secret but declares no environment fails
  the same test — deleting the `environment:` line is not a way out.
- `actionlint` (via `pre-commit run --all-files`) catches the step-level key as
  a workflow syntax error; run it before committing a workflow change.
