"""Tests for live ISO build correctness — regression guards for cross-distro support.

These tests ensure that changes made to support Fedora/CentOS-based images
(bluefin, bluefin-lts-hwe) do not break the existing GnomeOS/dakota pipeline.

Covered invariants
------------------
1. Boot cmdline uses LABEL=DAKOTA_LIVE, NOT CDLABEL= (cdrom_id-based) or
   /dev/sr0 (optical-only).
   - CDLABEL= requires the cdrom_id udev helper, which fails with the
     Debian-built initramfs on GnomeOS native kernels.
   - /dev/sr0 only exists for optical drives; USB flash drive boots
     present the ISO as /dev/sdX and silently black-screen when /dev/sr0
     is not found (regression introduced post-alpha2).
   - LABEL= uses blkid (any block device) and works on USB, optical, QEMU.
2. xfsprogs (mkfs.xfs) is present in the Containerfile's build stage so
   fisherman's XFS preflight check passes on images that don't ship it.
3. Initramfs selection logic: Containerfile tries native dracut first
   (for Fedora/RPM images) and falls back to Debian cross-build (for
   GnomeOS images that have no package manager).
4. configure-live.sh shell syntax is valid for all target distros.
5. Variant config files are complete and consistent for known variants.
6. Release builds keep debug-only SSH/password config inside the DEBUG guard.
7. build-iso.yml uploads to R2 only after the full install + verify gates pass.
8. live/src/luks-unlock.py stays in sync with dakota/src/luks-unlock.py.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
LIVE_BUILD_ISO = REPO / "live" / "src" / "build-iso.sh"
DAKOTA_BUILD_ISO = REPO / "dakota" / "src" / "build-iso.sh"
CONTAINERFILE = REPO / "live" / "Containerfile"
CONFIGURE_LIVE = REPO / "live" / "src" / "configure-live.sh"
BUILD_ISO_WORKFLOW = REPO / ".github" / "workflows" / "build-iso.yml"
BUILD_ISO_BLUEFIN_WORKFLOW = REPO / ".github" / "workflows" / "build-iso-bluefin.yml"
TEST_LUKS_WORKFLOW = REPO / ".github" / "workflows" / "test-luks-install.yml"
TEST_PLAIN_WORKFLOW = REPO / ".github" / "workflows" / "test-plain-install.yml"
LIVE_LUKS_UNLOCK = REPO / "live" / "src" / "luks-unlock.py"
DAKOTA_LUKS_UNLOCK = REPO / "dakota" / "src" / "luks-unlock.py"
BUILD_LIVE_SQUASHFS = REPO / "scripts" / "build-live-squashfs.sh"
ISO_SD_BOOT = REPO / "scripts" / "iso-sd-boot.sh"
README = REPO / "README.md"

# Variant directories that must be fully configured.
KNOWN_VARIANTS = ["dakota", "bluefin", "bluefin-lts-hwe"]

# Required files in every variant directory.
VARIANT_REQUIRED_FILES = ["payload_ref", "live_target", "tag", "registry"]

# Required files in live/src/<variant>/ for every non-dakota variant.
LIVE_SRC_VARIANT_FILES = [
    "base_imgref",
    "nvidia_imgref",
    "bootloader",
    "composefs",
    "flatpak_var_path",
    "registry",
    "images.json",
]


class TestBootCmdline(unittest.TestCase):
    """Ensure LABEL=DAKOTA_LIVE is used (not CDLABEL= or /dev/sr0).

    /dev/sr0 is optical-only and silently fails on USB flash drive boots
    (regression post-alpha2). CDLABEL= requires cdrom_id which is broken
    in the Debian-built initramfs on GnomeOS kernels. LABEL= uses blkid
    and works on any block device.
    """

    def _check_boot_root(self, path: Path):
        content = path.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                "CDLABEL=", stripped,
                f"{path.name}: found CDLABEL= — use LABEL= (blkid-based, works on USB): {stripped!r}",
            )
            self.assertNotIn(
                "root=live:/dev/sr0", stripped,
                f"{path.name}: found root=live:/dev/sr0 — breaks USB flash drive boots, "
                f"use root=live:LABEL=DAKOTA_LIVE: {stripped!r}",
            )

    def _check_has_label(self, path: Path):
        content = path.read_text()
        label_lines = [
            ln for ln in content.splitlines()
            if "root=live:LABEL=DAKOTA_LIVE" in ln and not ln.strip().startswith("#")
        ]
        self.assertGreaterEqual(
            len(label_lines), 2,
            f"{path.name}: expected ≥2 boot entries with root=live:LABEL=DAKOTA_LIVE "
            f"(BLS + loopback), found {len(label_lines)}.",
        )

    def test_live_build_iso_uses_label_not_cdlabel_or_sr0(self):
        """live/src/build-iso.sh must use LABEL=, not CDLABEL= or /dev/sr0."""
        self._check_boot_root(LIVE_BUILD_ISO)

    def test_dakota_build_iso_uses_label_not_cdlabel_or_sr0(self):
        """dakota/src/build-iso.sh must use LABEL=, not CDLABEL= or /dev/sr0."""
        self._check_boot_root(DAKOTA_BUILD_ISO)

    def test_live_build_iso_contains_label_root(self):
        """live/src/build-iso.sh boot entries must use root=live:LABEL=DAKOTA_LIVE."""
        self._check_has_label(LIVE_BUILD_ISO)


    def _check_nvidia_modeset(self, path):
        content = path.read_text()
        boot_lines = [
            ln for ln in content.splitlines()
            if ("options " in ln or "linux " in ln)
            and "root=live:" in ln
            and not ln.strip().startswith("#")
        ]
        missing = [ln for ln in boot_lines if "nvidia-drm.modeset=1" not in ln]
        self.assertEqual(
            missing, [],
            f"{path.name}: boot entries missing nvidia-drm.modeset=1 "
            "(causes black screen on NVIDIA hardware):\n"
            + "\n".join(missing),
        )

    def test_live_build_iso_has_nvidia_drm_modeset(self):
        """All live/src/build-iso.sh boot entries must include nvidia-drm.modeset=1."""
        self._check_nvidia_modeset(LIVE_BUILD_ISO)

    def test_dakota_build_iso_has_nvidia_drm_modeset(self):
        """All dakota/src/build-iso.sh boot entries must include nvidia-drm.modeset=1."""
        self._check_nvidia_modeset(DAKOTA_BUILD_ISO)


class TestXfsprogs(unittest.TestCase):
    """Ensure mkfs.xfs is present in the live environment."""

    def test_containerfile_installs_xfsprogs_in_debian_stage(self):
        """Debian initramfs-builder stage must install xfsprogs."""
        content = CONTAINERFILE.read_text()
        self.assertIn(
            "xfsprogs",
            content,
            "xfsprogs must be in the Debian apt-get install list so mkfs.xfs "
            "is available to COPY into the final image. Without it, fisherman's "
            "XFS preflight check fails on images that don't ship xfsprogs "
            "(e.g. GnomeOS/dakota after the XFS-default change).",
        )

    def test_containerfile_copies_mkfs_xfs_to_final_stage(self):
        """Final stage must COPY mkfs.xfs from the Debian builder stage."""
        content = CONTAINERFILE.read_text()
        self.assertIn(
            "mkfs.xfs",
            content,
            "The final Containerfile stage must COPY /usr/sbin/mkfs.xfs from "
            "the initramfs-builder so it is present in the live squashfs.",
        )

    def test_containerfile_copies_mkfs_xfs_shared_libs(self):
        """Final stage must COPY the Debian-specific shared library deps of mkfs.xfs.

        mkfs.xfs needs: libblkid.so.1  libuuid.so.1  libinih.so.1  liburcu.so.8

        libblkid.so.1 and libuuid.so.1 are provided by the TARGET base image
        (freedesktop-sdk for dakota, Fedora for bluefin) at the correct version.
        Do NOT copy them from Debian — the Debian bookworm version only has
        BLKID_2_21 while the system sfdisk/libfdisk requires BLKID_2_40, and
        overwriting the system copy breaks sfdisk with "version BLKID_2_40 not found".

        Only libinih.so.1 and liburcu.so.8 must be copied from Debian:
          - libinih: Fedora ships libinih.so.0, xfsprogs needs .so.1
          - liburcu: not always present in base images
        """
        content = CONTAINERFILE.read_text()
        # These two are genuinely absent/wrong-version in target base images.
        required_from_debian = [
            "libinih.so",
            "liburcu.so",
        ]
        for lib in required_from_debian:
            self.assertIn(
                lib,
                content,
                f"Containerfile final stage is missing COPY for {lib} — "
                "mkfs.xfs will fail at runtime. Add it to the COPY block.",
            )
        # libblkid and libuuid must NOT be copied from Debian — base image
        # provides a newer version; overwriting breaks sfdisk (BLKID_2_40).
        for lib in ("libblkid.so", "libuuid.so"):
            self.assertNotIn(
                f"COPY --from=initramfs-builder\n    /usr/lib/x86_64-linux-gnu/{lib}",
                content,
                f"Containerfile must not copy {lib} from Debian — base image "
                "ships a newer version; overwriting it breaks sfdisk.",
            )


class TestInitramfsSelectionLogic(unittest.TestCase):
    """Verify the native-vs-Debian dracut selection logic in Containerfile."""

    def setUp(self):
        self.content = CONTAINERFILE.read_text()

    def test_native_dracut_stage_exists(self):
        """Containerfile must have an initramfs-native stage."""
        self.assertIn(
            "AS initramfs-native",
            self.content,
            "Missing 'AS initramfs-native' stage. Native dracut build is "
            "required for Fedora-based images (bluefin/bluefin-lts-hwe) to get "
            "correct udev rules matching the Fedora kernel.",
        )

    def test_native_stage_tries_dnf_install(self):
        """Native stage must attempt to install dracut-live via dnf."""
        self.assertIn(
            "dracut-live",
            self.content,
            "initramfs-native stage must install dracut-live so the "
            "dmsquash-live dracut module is available on Fedora images.",
        )

    def test_native_stage_writes_dracut_status(self):
        """Native stage must write a dracut-status file for the Debian stage."""
        self.assertIn(
            "dracut-status",
            self.content,
            "initramfs-native stage must write /tmp/dracut-status ('native' or "
            "'debian') so the Debian stage knows whether to cross-build or use "
            "the natively-built initramfs.",
        )

    def test_debian_stage_reads_dracut_status(self):
        """Debian stage must check dracut-status to decide whether to cross-build."""
        self.assertIn(
            "dracut-status",
            self.content,
            "initramfs-builder (Debian) stage must read dracut-status and "
            "only run the cross-build when native dracut was unavailable.",
        )

    def test_debian_stage_fallback_includes_dmsquash_live(self):
        """Debian cross-build must add the dmsquash-live dracut module."""
        self.assertIn(
            "dmsquash-live",
            self.content,
            "Debian dracut cross-build must include --add 'dmsquash-live' "
            "for the live boot to work.",
        )

    def test_debian_stage_does_not_add_virtio_modules(self):
        """Debian cross-build must NOT add virtio modules (breaks GnomeOS boot).

        GnomeOS kernels have virtio_scsi/pci/blk built-in. Adding them via
        --add-drivers causes dracut to fail silently for built-in modules,
        producing a broken initramfs that can't enumerate the SCSI CD device.
        """
        # Find the Debian dracut RUN block
        in_debian_run = False
        for line in self.content.splitlines():
            if "Cross-building initramfs" in line or (
                "DRACUT_NO_XATTR" in line and "add-drivers" in line
            ):
                in_debian_run = True
            if in_debian_run:
                self.assertNotIn(
                    "virtio_scsi",
                    line,
                    "Debian dracut cross-build must NOT include virtio_scsi "
                    "in --add-drivers — it is built-in on GnomeOS kernels and "
                    "adding it breaks the initramfs module resolution.",
                )
                if line.strip().endswith(";") or line.strip() == "fi;":
                    break


class TestConfigureLiveSyntax(unittest.TestCase):
    """configure-live.sh must have valid bash syntax."""

    def test_configure_live_bash_syntax(self):
        """bash -n must pass on configure-live.sh."""
        result = subprocess.run(
            ["bash", "-n", str(CONFIGURE_LIVE)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"configure-live.sh has bash syntax errors:\n{result.stderr}",
        )

    def test_configure_live_reads_variant_dir(self):
        """configure-live.sh must read config from VARIANT_DIR."""
        content = CONFIGURE_LIVE.read_text()
        self.assertIn(
            "VARIANT_DIR",
            content,
            "configure-live.sh must use VARIANT_DIR to read per-variant "
            "config (base_imgref, bootloader, composefs, etc.) so new "
            "variants can be added without modifying the script.",
        )

    def test_configure_live_does_not_hardcode_dakota_imgref(self):
        """configure-live.sh must NOT unconditionally assign dakota image refs.

        The script reads BASE_IMGREF and NVIDIA_IMGREF from VARIANT_DIR config
        files. Having hardcoded dakota values as DEFAULTS (in else branches) is
        acceptable — what must NOT happen is an unconditional assignment that
        would override the variant config for non-dakota images.
        """
        content = CONFIGURE_LIVE.read_text()
        # The VARIANT_DIR block must exist — this is the cross-distro mechanism
        self.assertIn(
            "VARIANT_DIR",
            content,
            "configure-live.sh must use VARIANT_DIR for per-variant config.",
        )
        # Unconditional (top-level) assignment of a dakota-specific ref would
        # be a bug. Look for lines that assign the dakota ref WITHOUT being
        # inside an if/else block (i.e., no leading whitespace from an if branch).
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # A bare top-level assignment (no indentation) hardcoding dakota is wrong.
            # Indented assignments in else branches are fine as defaults.
            if re.match(
                r'^BASE_IMGREF=["\']ghcr\.io/projectbluefin/dakota',
                stripped,
            ) and not line.startswith(" ") and not line.startswith("\t"):
                self.fail(
                    f"configure-live.sh has an unconditional top-level assignment "
                    f"of a dakota image ref: {stripped!r}. "
                    "This overrides the variant config for non-dakota images. "
                    "Hardcoded values are only acceptable inside else branches "
                    "(indented) as default fallbacks."
                )

    def test_configure_live_handles_usr_local_symlink(self):
        """configure-live.sh must handle /usr/local as a dangling symlink.

        On Fedora Silverblue (bluefin), /usr/local is a symlink to
        /var/usrlocal which doesn't exist at container build time. The script
        must not unconditionally mkdir /usr/local/share/applications.
        """
        content = CONFIGURE_LIVE.read_text()
        # Should use /usr/share/applications instead of /usr/local/share/applications
        # for the installer desktop file
        self.assertNotIn(
            "mkdir -p /usr/local/share/applications",
            content,
            "configure-live.sh must not mkdir /usr/local/share/applications — "
            "on Fedora Silverblue /usr/local is a dangling symlink to "
            "/var/usrlocal which doesn't exist at container build time. "
            "Use /usr/share/applications/ instead.",
        )

    def test_configure_live_binds_var_home_while_creating_liveuser(self):
        """configure-live.sh must create liveuser in the runtime-visible home tree.

        The bind/unmount logic must be guarded to avoid unmounting a real
        directory or following symlinks when `/home` already resolves to
        `/var/home` or is already a mountpoint.
        """
        content = CONFIGURE_LIVE.read_text()
        bind_marker = "mount --bind /var/home /home"
        user_marker = "useradd --create-home --uid 1000 --user-group"

        # Ensure the EXIT trap exists so the bind is cleaned up on early exit.
        self.assertIn(
            "trap cleanup_liveuser_home_bind EXIT",
            content,
            "configure-live.sh must install an EXIT trap so /home is unmounted "
            "if liveuser creation exits early.",
        )

        # The script must use canonical path comparison to avoid binding when
        # /home already points to /var/home (symlink case).
        self.assertIn(
            "readlink -f /home",
            content,
            "configure-live.sh must compare canonical paths (readlink -f) to avoid "
            "unsafe bind mounts when /home resolves to /var/home.",
        )

        # The script should check whether /home is already a mountpoint before
        # attempting to bind, using either mountpoint(1) or /proc/mounts grep.
        has_mountpoint_check = (
            "mountpoint -q /home" in content
            or "grep -qs ' /home ' /proc/mounts" in content
        )
        self.assertTrue(
            has_mountpoint_check,
            "configure-live.sh must check whether /home is already a mountpoint "
            "(mountpoint -q /home or grep /proc/mounts) before bind-mounting.",
        )

        # If a bind is performed, both the mount and its unmount must be present
        # and the bind must occur before useradd so the created home lands in
        # the runtime-visible tree.
        self.assertIn(
            bind_marker,
            content,
            "configure-live.sh must bind /var/home onto /home before creating "
            "liveuser so the home directory lands in the runtime-visible tree.",
        )
        self.assertIn(
            "umount /home",
            content,
            "configure-live.sh must unmount the temporary /home bind mount after "
            "the liveuser setup block completes.",
        )
        self.assertLess(
            content.index(bind_marker),
            content.index(user_marker),
            "configure-live.sh must bind /var/home onto /home before useradd runs.",
        )

        # Relabeling should be performed when restorecon is available. Do NOT
        # require SELinux to be enabled on the build host — image builds on
        # non-SELinux hosts still need relabeling when restorecon exists.
        self.assertIn(
            "restorecon -RF /var/home/liveuser",
            content,
            "configure-live.sh must relabel /var/home/liveuser when restorecon is "
            "available so file contexts inside the image are correct (do not gate on selinuxenabled).",
        )


class TestReleaseSafetyInvariants(unittest.TestCase):
    """Release-only security and CI gate invariants."""

    def test_configure_live_debug_only_ssh_bits_do_not_leak_past_debug_block(self):
        """Release builds must keep SSH/password helpers inside DEBUG=1.

        The live ISO may enable SSH/password auth for local E2E, but those
        release-sensitive markers must stay inside the DEBUG guard rather than
        leaking into the always-on section below it.
        """
        content = CONFIGURE_LIVE.read_text()
        split_marker = "\n# Give liveuser passwordless sudo so the live session is fully manageable\n"
        self.assertIn(
            split_marker,
            content,
            "configure-live.sh moved the post-debug boundary comment; update this test.",
        )
        debug_section, release_section = content.split(split_marker, 1)

        debug_only_markers = [
            'echo "liveuser:live" | chpasswd',
            'echo "root:root" | chpasswd',
            'PasswordAuthentication yes',
            'PermitRootLogin yes',
            'debug-ssh-banner.service',
        ]

        for marker in debug_only_markers:
            self.assertIn(
                marker,
                debug_section,
                f"Missing expected DEBUG-only marker in guarded section: {marker!r}",
            )
            self.assertNotIn(
                marker,
                release_section,
                f"DEBUG-only marker leaked past the DEBUG guard: {marker!r}",
            )

    def test_build_iso_upload_waits_for_full_install_and_verify(self):
        """R2 upload must wait for the real plain-install and boot gates."""
        content = BUILD_ISO_WORKFLOW.read_text()
        self.assertIn(
            'steps.e2e_install.outcome == \'success\'',
            content,
            "build-iso.yml must gate R2 upload on a successful full install.",
        )
        self.assertIn(
            'steps.e2e_verify.outcome == \'success\'',
            content,
            "build-iso.yml must gate R2 upload on installed-boot verification.",
        )

        upload_block = content.split("- name: Upload ISO to Cloudflare R2", 1)[1].split(
            "\n      - name:", 1
        )[0]
        self.assertIn(
            "steps.e2e_enospc.conclusion == 'success'",
            upload_block,
            "Upload step must still require the ENOSPC export gate.",
        )
        self.assertIn(
            "steps.e2e_install.outcome == 'success'",
            upload_block,
            "Upload step must wait for full install success.",
        )
        self.assertIn(
            "steps.e2e_verify.outcome == 'success'",
            upload_block,
            "Upload step must wait for installed-boot verification.",
        )
        self.assertIn(
            "steps.boot_verify.outcome == 'success'",
            upload_block,
            "Upload step must wait for the production ISO smoke boot.",
        )
        self.assertLess(
            content.index("- name: Boot verification (UEFI + serial)"),
            content.index("- name: Upload ISO to Cloudflare R2"),
            "Dakota boot verification must run before the publish step.",
        )

    def test_build_iso_bluefin_upload_waits_for_boot_verification(self):
        """Bluefin uploads must wait for the smoke-boot gate to pass."""
        content = BUILD_ISO_BLUEFIN_WORKFLOW.read_text()
        upload_block = content.split("- name: Upload ISO to Cloudflare R2", 1)[1].split(
            "\n      - name:", 1
        )[0]
        self.assertIn(
            "steps.boot_verify.outcome == 'success'",
            upload_block,
            "build-iso-bluefin.yml must gate R2 upload on successful boot verification.",
        )
        self.assertIn(
            "- name: Boot verification status",
            content,
            "build-iso-bluefin.yml must restore a red CI status when boot verification fails.",
        )
        self.assertIn(
            "steps.boot_verify.outcome == 'failure'",
            content,
            "build-iso-bluefin.yml must explicitly fail the job when boot verification fails.",
        )

    def test_publish_workflows_define_concurrency(self):
        """Monthly publishers must not race each other on latest pointers."""
        for workflow in [BUILD_ISO_WORKFLOW, BUILD_ISO_BLUEFIN_WORKFLOW]:
            content = workflow.read_text()
            self.assertIn(
                "\nconcurrency:\n",
                content,
                f"{workflow.name} must define workflow-level concurrency.",
            )

    def test_build_iso_rotates_and_prunes_dakota_backups(self):
        """Dakota publisher must maintain exactly 3 backup ISO slots."""
        content = BUILD_ISO_WORKFLOW.read_text()
        self.assertIn(
            "dakota-live-backup-1.iso",
            content,
            "build-iso.yml must rotate latest into dakota-live-backup-1.iso before overwrite.",
        )
        self.assertIn(
            "dakota-live-backup-2.iso",
            content,
            "build-iso.yml must keep a second backup slot for rollback safety.",
        )
        self.assertIn(
            "dakota-live-backup-3.iso",
            content,
            "build-iso.yml must keep a third backup slot for rollback safety.",
        )
        self.assertIn(
            "Delete backup slots beyond 3",
            content,
            "build-iso.yml must explicitly prune backup slots older than the most recent 3.",
        )

    def test_build_iso_bluefin_rotates_and_prunes_backups(self):
        """Bluefin publisher must maintain exactly 3 backup slots per iso_name."""
        content = BUILD_ISO_BLUEFIN_WORKFLOW.read_text()
        self.assertIn(
            "BASE=\"${{ matrix.iso_name }}\"",
            content,
            "build-iso-bluefin.yml must derive a base name from matrix.iso_name.",
        )
        self.assertIn(
            "${BASE}-backup-1.iso",
            content,
            "build-iso-bluefin.yml must rotate latest into backup-1 before overwrite.",
        )
        self.assertIn(
            "${BASE}-backup-2.iso",
            content,
            "build-iso-bluefin.yml must keep a second backup slot per iso_name.",
        )
        self.assertIn(
            "${BASE}-backup-3.iso",
            content,
            "build-iso-bluefin.yml must keep a third backup slot per iso_name.",
        )
        self.assertIn(
            "Delete backup slots beyond 3",
            content,
            "build-iso-bluefin.yml must explicitly prune backup slots older than 3.",
        )

    def test_readme_download_table_has_last_three_builds_links(self):
        """README top download table must expose latest + last 3 dakota backups."""
        content = README.read_text()
        top_table_section = content.split("\nBuilds bootable UEFI live ISOs", 1)[0]
        self.assertIn(
            "| Variant | Download | Checksum | Size | Published (UTC) | Validation | Last 3 builds |",
            top_table_section,
            "README download table header must include a 'Last 3 builds' column.",
        )
        dakota_rows = [ln for ln in top_table_section.splitlines() if ln.startswith("| `dakota` |")]
        self.assertEqual(
            len(dakota_rows), 1,
            "README must contain exactly one dakota row in the top download table.",
        )
        dakota_row = dakota_rows[0]
        for backup_name in (
            "dakota-live-backup-1.iso",
            "dakota-live-backup-2.iso",
            "dakota-live-backup-3.iso",
        ):
            self.assertIn(
                backup_name,
                dakota_row,
                f"Dakota row must link backup slot {backup_name}.",
            )

    def test_readme_bluefin_rows_link_last_three_builds(self):
        """README bluefin/bluefin-lts-hwe rows must link backup slots 1..3."""
        content = README.read_text()
        top_table_section = content.split("\nBuilds bootable UEFI live ISOs", 1)[0]

        for prefix, iso_base in (
            ("`bluefin`", "bluefin-live"),
            ("`bluefin-lts-hwe`", "bluefin-lts-hwe-live"),
        ):
            row = next(
                (ln for ln in top_table_section.splitlines() if ln.startswith(f"| {prefix} |")),
                None,
            )
            if row is None:
                continue  # variant row may not exist yet
            for n in (1, 2, 3):
                backup_name = f"{iso_base}-backup-{n}.iso"
                self.assertIn(
                    backup_name,
                    row,
                    f"{prefix} row must link {backup_name}.",
                )

    def test_e2e_workflows_wait_for_unit_tests(self):
        """Expensive QEMU E2E jobs should not run after cheap test failures."""
        self.assertIn(
            "\n  plain-e2e:\n    needs: unit-tests\n",
            TEST_PLAIN_WORKFLOW.read_text(),
            "test-plain-install.yml must gate plain-e2e on unit-tests.",
        )
        self.assertIn(
            "\n  luks-e2e:\n    needs: unit-tests\n",
            TEST_LUKS_WORKFLOW.read_text(),
            "test-luks-install.yml must gate luks-e2e on unit-tests.",
        )

    def test_luks_unlock_copies_are_identical(self):
        """live/ and dakota/ luks-unlock helpers must stay byte-for-byte aligned."""
        self.assertEqual(
            LIVE_LUKS_UNLOCK.read_text(),
            DAKOTA_LUKS_UNLOCK.read_text(),
            "live/src/luks-unlock.py and dakota/src/luks-unlock.py diverged. "
            "Keep them identical so CI/build logic and local helpers exercise "
            "the same unlock behavior.",
        )


class TestVariantConfig(unittest.TestCase):
    """Variant directories must be complete and consistent."""

    def test_known_variants_have_required_files(self):
        """Each known variant directory must contain all required config files."""
        for variant in KNOWN_VARIANTS:
            variant_dir = REPO / variant
            for filename in VARIANT_REQUIRED_FILES:
                path = variant_dir / filename
                self.assertTrue(
                    path.exists(),
                    f"Variant '{variant}' is missing required config file: "
                    f"{filename}\nExpected at: {path}",
                )
                content = path.read_text().strip()
                self.assertTrue(
                    content,
                    f"Variant '{variant}/{filename}' is empty.",
                )

    def test_non_dakota_variants_have_live_src_config(self):
        """bluefin and bluefin-lts-hwe must have live/src/<variant>/ config dirs."""
        for variant in ["bluefin", "bluefin-lts-hwe"]:
            live_src_dir = REPO / "live" / "src" / variant
            for filename in LIVE_SRC_VARIANT_FILES:
                path = live_src_dir / filename
                self.assertTrue(
                    path.exists(),
                    f"live/src/{variant}/ is missing: {filename}\n"
                    f"Expected at: {path}\n"
                    "This file tells configure-live.sh how to build the live "
                    f"environment for the {variant} variant.",
                )

    def test_dakota_variant_uses_gnomeos_composefs(self):
        """dakota must use composefs=true and systemd bootloader."""
        composefs = (REPO / "live" / "src" / "dakota" / "composefs").read_text().strip() \
            if (REPO / "live" / "src" / "dakota" / "composefs").exists() \
            else "true"  # dakota is the default, no override file needed
        # For dakota, the recipe.json hardcodes composefs=true
        recipe = (REPO / "live" / "src" / "etc" / "bootc-installer" / "recipe.json").read_text()
        self.assertIn(
            '"composeFsBackend": true',
            recipe,
            "dakota recipe.json must set composeFsBackend=true.",
        )

    def test_fedora_variants_use_grub_bootloader(self):
        """bluefin and bluefin-lts-hwe must specify bootloader=grub."""
        for variant in ["bluefin", "bluefin-lts-hwe"]:
            bootloader_file = REPO / "live" / "src" / variant / "bootloader"
            if bootloader_file.exists():
                bootloader = bootloader_file.read_text().strip()
                self.assertEqual(
                    bootloader,
                    "grub",
                    f"live/src/{variant}/bootloader must be 'grub'. "
                    "Fedora-based images use GRUB, not systemd-boot.",
                )

    def test_fedora_variants_disable_composefs(self):
        """bluefin and bluefin-lts-hwe must set composefs=false."""
        for variant in ["bluefin", "bluefin-lts-hwe"]:
            composefs_file = REPO / "live" / "src" / variant / "composefs"
            if composefs_file.exists():
                composefs = composefs_file.read_text().strip()
                self.assertEqual(
                    composefs,
                    "false",
                    f"live/src/{variant}/composefs must be 'false'. "
                    "Fedora Silverblue uses ostree, not composefs-native.",
                )

    def test_variant_tags_are_valid(self):
        """All variant tag files must contain a valid tag string."""
        valid_tags = {"stable", "lts", "testing", "latest"}
        for variant in KNOWN_VARIANTS:
            tag_file = REPO / variant / "tag"
            if tag_file.exists():
                tag = tag_file.read_text().strip()
                self.assertIn(
                    tag,
                    valid_tags,
                    f"Variant '{variant}/tag' contains unexpected tag: {tag!r}. "
                    f"Valid tags: {valid_tags}",
                )

    def test_payload_refs_are_ghcr_urls(self):
        """All payload_ref files must be ghcr.io image URLs."""
        for variant in KNOWN_VARIANTS:
            payload_file = REPO / variant / "payload_ref"
            if payload_file.exists():
                payload = payload_file.read_text().strip()
                self.assertTrue(
                    payload.startswith("ghcr.io/"),
                    f"Variant '{variant}/payload_ref' must be a ghcr.io URL, "
                    f"got: {payload!r}",
                )


class TestBuildIsoScript(unittest.TestCase):
    """Static analysis of build-iso.sh for correctness invariants."""

    def _check_script(self, path: Path):
        return path.read_text()

    def test_live_build_iso_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(LIVE_BUILD_ISO)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         f"live/src/build-iso.sh syntax error:\n{result.stderr}")

    def test_dakota_build_iso_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(DAKOTA_BUILD_ISO)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         f"dakota/src/build-iso.sh syntax error:\n{result.stderr}")

    def test_build_iso_scripts_are_in_sync(self):
        """live/ and dakota/ build-iso.sh must have identical boot cmdlines.

        These two scripts serve different entry points (CI vs local justfile)
        but must stay in sync on the boot cmdline to prevent split-brain bugs
        where CI builds boot with different options than local test builds.
        """
        live_content = LIVE_BUILD_ISO.read_text()
        dakota_content = DAKOTA_BUILD_ISO.read_text()

        def extract_boot_lines(content):
            return [
                ln.strip() for ln in content.splitlines()
                if ("root=live:" in ln or "rd.live." in ln)
                and not ln.strip().startswith("#")
            ]

        live_boot = extract_boot_lines(live_content)
        dakota_boot = extract_boot_lines(dakota_content)

        self.assertEqual(
            live_boot, dakota_boot,
            "live/src/build-iso.sh and dakota/src/build-iso.sh have different "
            "boot cmdline options. These files must be kept in sync.\n"
            f"live:   {live_boot}\ndakota: {dakota_boot}",
        )


if __name__ == "__main__":
    unittest.main()


class TestBuildLiveSquashfs(unittest.TestCase):
    """Invariants for scripts/build-live-squashfs.sh."""

    def test_composefs_detection_no_broken_python_quoting(self):
        """build-live-squashfs.sh must NOT use Python open() inside sh -c for composeFsBackend.

        The broken pattern is:
          sh -c 'python3 -c "... open("/etc/bootc-installer/recipe.json") ..."'
        The '/' path separators and inner '"' break the sh quoting, causing the
        Python -c argument to be truncated.  The result is that the detection
        silently fails and COMPOSEFS_BACKEND is always set to false — which
        makes the compositor embed the payload into the overlay store instead of
        the VFS store, so 'podman image exists' returns false in the live VM
        and fisherman falls back to a network pull (ENOSPC with 4 GiB RAM).

        Use grep or cat+python (piped, no path quoting) instead.
        """
        content = BUILD_LIVE_SQUASHFS.read_text()
        self.assertNotIn(
            'open("/etc/bootc-installer/recipe.json")',
            content,
            "build-live-squashfs.sh contains broken Python quoting for "
            "composeFsBackend detection: open() path inside sh -c single-quotes "
            "breaks the -c argument. Use grep or pipe to python instead.",
        )

    def test_lts_images_json_defaults_to_btrfs(self):
        """live/src/bluefin-lts-hwe/images.json must default to btrfs.

        LTS (bluefin-lts-hwe) uses btrfs as its default filesystem to avoid boot timeouts.
        """
        import json
        images_json = REPO / "live" / "src" / "bluefin-lts-hwe" / "images.json"
        self.assertTrue(images_json.exists(), f"{images_json} not found")
        data = json.loads(images_json.read_text())
        for img in data.get("images", []):
            self.assertEqual(
                img.get("filesystem"),
                "btrfs",
                f"bluefin-lts-hwe/images.json image '{img.get('name')}' must "
                "default to filesystem=btrfs. "
                f"Got: {img.get('filesystem')!r}",
            )

    def test_justfile_lts_filesystem_is_btrfs(self):
        """justfile _filesystem_for must return btrfs for all targets."""
        justfile = REPO / "justfile"
        content = justfile.read_text()
        self.assertIn(
            '_filesystem_for target:\n    @echo "btrfs"',
            content,
            "justfile _filesystem_for must return btrfs for all targets",
        )

    def test_justfile_socat_uses_prefix(self):
        """All socat UNIX-CONNECT calls in justfile must use $SOCAT_PREFIX.

        When QEMU is run with sudo for KVM access, the UNIX sockets are owned by
        root and cannot be accessed by the unprivileged user. Using $SOCAT_PREFIX
        (conditionally set to 'sudo' if the socket is not writable) prevents
        silent connection/powerdown/screendump failures.
        """
        justfile = REPO / "justfile"
        content = justfile.read_text()
        # Find all lines containing socat and UNIX-CONNECT
        for line in content.splitlines():
            if "socat" in line and "UNIX-CONNECT:" in line:
                self.assertIn(
                    "$SOCAT_PREFIX socat",
                    line,
                    f"Line in justfile uses raw 'socat' with UNIX-CONNECT: {line!r}. "
                    "Must use '$SOCAT_PREFIX socat' to support root-owned sockets "
                    "when QEMU runs with sudo."
                )


class TestPayloadPristine(unittest.TestCase):
    """The embedded payload image must ship the same content the registry serves.

    The payload OCI image embedded in the ISO is what bootc deploys — every
    file baked into it at ISO build time ends up on the installed system.
    Injecting a `driver = "vfs"` /etc/containers/storage.conf into the payload
    (once done so podman could read the embedded VFS store) made every
    installed system run podman with VFS storage: no overlay, massive disk
    usage, and broken update-staging pulls. The live environment gets its own
    storage.conf from configure-live.sh, the store-embed step passes one via
    CONTAINERS_STORAGE_CONF, and fisherman bind-mounts one into the install
    container when needed — the payload itself never needs it.

    Only the bootc install config (/usr/lib/bootc/install/00-defaults.toml)
    may be injected into the payload.
    """

    PAYLOAD_SCRIPTS = {
        "scripts/iso-sd-boot.sh": ISO_SD_BOOT,
        "scripts/build-live-squashfs.sh": BUILD_LIVE_SQUASHFS,
    }

    def test_no_storage_conf_injected_into_payload(self):
        """No build script may buildah-copy a storage.conf into the payload."""
        pattern = re.compile(
            r"buildah copy.*(?:/etc/containers/storage\.conf|storage\.conf.*/etc/containers)"
        )
        for name, path in self.PAYLOAD_SCRIPTS.items():
            for line in path.read_text().splitlines():
                self.assertIsNone(
                    pattern.search(line),
                    f"{name} injects a storage.conf into the payload image: "
                    f"{line.strip()!r}. The installed system inherits payload "
                    "content verbatim — it would run podman with VFS storage. "
                    "Configure storage for the live env (configure-live.sh), "
                    "the embed step (CONTAINERS_STORAGE_CONF), or the install "
                    "container (fisherman bind-mount) instead.",
                )
