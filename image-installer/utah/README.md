# Utah gnome-setup medium (experimental)

The raw live medium that boots Utah and installs it by block copy with GNOME
STF's gnome-setup driving systemd-sysinstall. Moved from `projectbluefin/utah`;
the design is its ADR-0001, and the shared design with Dakota is under
discussion in #413.

- Paths below this directory mirror the Utah repository (`iso/raw/`,
  `iso/scripts/`, `scripts/`, `config/`), and the files are byte-identical to
  `projectbluefin/utah` `main` at
  `583fd3e414b23bc625c89ce762902c4fe9e4c894`, so the copy can be checked
  file by file against that commit (below).
- `raw_e2e.py` resolves its helpers from those Utah-relative paths. Besides
  the medium's own files, it runs on the host `iso/scripts/qemu_monitor.py`,
  `plymouth-prompt.py`, `luks-unlock.py`, `extension-state.sh`,
  `fastfetch-ocr-match.sh` and `scripts/flavors.py` (which reads
  `config/flavors.json` to tell the flavor under test), and copies into the
  installed system `iso/scripts/e2e-services.sh`,
  `verify-installed-flavor.py`, `verify-countme-runtime.sh` and
  `scripts/verify-desktop-contract.py`.
- Under CI (`GITHUB_ACTIONS`/`CI`) `raw_e2e.py` fails at VM start unless
  `/dev/kvm` is writable, rather than running the VMs under TCG (utah#1632).
  Every job that runs it grants KVM with `sudo chmod a+rw /dev/kvm`, not
  `setfacl`, whose grant vanished between VMs in utah#1631.
- `iso/raw/build-hybrid-iso.sh` (utah#1404) turns the raw medium into an El
  Torito hybrid ISO with shim -> systemd-boot on its ESP (Secure Boot off),
  reading only `iso/raw/medium.py`; `raw_e2e.py --iso --boot-as cdrom|usb`
  boots it. The publish builds it and gates the upload on its CD-ROM and USB
  legs (below).
- Renovate leaves this directory alone (`renovate.json`): Utah's Renovate
  bumps the digests there and a resync brings them here.
- gnome-setup is built from the `bluefin` branch of `hanthor/gnome-setup`, the
  one meant for Dakota too.
- `iso/scripts/luks-unlock.py` duplicates `live/src/luks-unlock.py` with a
  different monitor transport. Merge them after the cutover, when both lanes
  can be tested together.
- `.github/workflows/utah-image-installer.yml` builds it and runs the GUI E2E on
  pull requests that touch it, on dispatch, and when called. With its `iso`
  input on, two more legs turn the medium into the hybrid ISO
  (`iso/raw/build-hybrid-iso.sh`, projectbluefin/utah#1404) and boot the live
  stage from it as a CD-ROM and as a USB disk (`raw_e2e.py --iso --boot-as`).
- `.github/workflows/publish-utah-installer.yml` publishes it to
  `https://projectbluefin.dev/utah-installer-latest.raw.xz`, monthly and on
  dispatch: from the signature-verified `utah:stable` digest, only after the
  GUI E2E passed on that digest, with no test credentials, checksum signed
  keylessly. It is xz-compressed so Fedora Media Writer (and GNOME Disks or
  Impression) flashes it directly. It replaces the fisherman ISO
  (`build-iso-utah.yml`, now dispatch only).
- The publish also runs the ISO legs in its E2E, builds the hybrid ISO from
  the same clean medium and uploads it beside the raw.xz as
  `utah-installer-latest.iso`, uncompressed (Fedora Media Writer writes ISOs
  as is), with the same signed `-CHECKSUM` and three rotated backups
  (`utah-installer-backup-N.iso`). Secure Boot must be off until a signed boot
  chain lands (projectbluefin/utah#1380). Its `iso` input is on for schedules
  and by default; dispatch with `iso=false` to test and publish the raw.xz
  alone.

## Resync

Copy every file below this directory (except this README) from the Utah
commit, then check the copy:

```bash
sha=<utah commit>
cd image-installer/utah
git ls-files . ':!README.md' | while read -r p; do
  git -C /path/to/utah show "$sha:$p" | cmp -s - "$p" || echo "differs: $p"
done
```

A file `raw_e2e.py` starts to read must be added at its Utah path in the same
resync.

## Cutover

Until all of these hold, `projectbluefin/utah` keeps its copy, its nightly run
and the publishing of `ghcr.io/projectbluefin/utah-installer`:

1. The E2E passes here.
2. The first medium is published here; Utah's README then points to it and
   `utah-live-latest.iso` is retired.
3. Utah drops `iso/raw/`, `image-installer.yml` and `publish-installer.yml` in
   the same change window, so there is one copy again.

`dakota/` beside this directory is left for #394 or whatever #413 decides.
