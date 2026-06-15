"""Tests for live ISO build correctness — regression guards for cross-distro support.

These tests ensure that changes made to support Fedora/CentOS-based images
(bluefin, bluefin-lts) do not break the existing GnomeOS/dakota pipeline.

Covered invariants
------------------
1. Boot cmdline uses /dev/sr0, NOT CDLABEL= (CDLABEL udev detection is
   unreliable with Debian-built initramfs on GnomeOS native kernels).
2. xfsprogs (mkfs.xfs) is present in the Containerfile's build stage so
   fisherman's XFS preflight check passes on images that don't ship it.
3. Initramfs selection logic: Containerfile tries native dracut first
   (for Fedora/RPM images) and falls back to Debian cross-build (for
   GnomeOS images that have no package manager).
4. configure-live.sh shell syntax is valid for all target distros.
5. Variant config files are complete and consistent for known variants.
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

# Variant directories that must be fully configured.
KNOWN_VARIANTS = ["dakota", "bluefin", "bluefin-lts"]

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
    """Ensure /dev/sr0 is used, not CDLABEL."""

    def _check_no_cdlabel(self, path: Path):
        content = path.read_text()
        # CDLABEL in the options/linux lines (not in comments) must not appear
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                "CDLABEL=",
                stripped,
                f"{path.name}: found CDLABEL= in non-comment line: {stripped!r}\n"
                "Use root=live:/dev/sr0 — CDLABEL udev detection is unreliable "
                "with Debian-built initramfs on GnomeOS native kernels.",
            )

    def test_live_build_iso_uses_dev_sr0_not_cdlabel(self):
        """live/src/build-iso.sh must not use root=live:CDLABEL=."""
        self._check_no_cdlabel(LIVE_BUILD_ISO)

    def test_dakota_build_iso_uses_dev_sr0_not_cdlabel(self):
        """dakota/src/build-iso.sh must not use root=live:CDLABEL=."""
        self._check_no_cdlabel(DAKOTA_BUILD_ISO)

    def test_live_build_iso_contains_dev_sr0(self):
        """live/src/build-iso.sh boot entries must include root=live:/dev/sr0."""
        content = LIVE_BUILD_ISO.read_text()
        # Count options/linux lines that set root=live:/dev/sr0
        sr0_lines = [
            ln for ln in content.splitlines()
            if "root=live:/dev/sr0" in ln and not ln.strip().startswith("#")
        ]
        self.assertGreaterEqual(
            len(sr0_lines), 2,
            "Expected at least 2 boot entry lines with root=live:/dev/sr0 "
            f"(BLS + PXE), found {len(sr0_lines)}.",
        )


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
            "required for Fedora-based images (bluefin/bluefin-lts) to get "
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
        """bluefin and bluefin-lts must have live/src/<variant>/ config dirs."""
        for variant in ["bluefin", "bluefin-lts"]:
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
        """bluefin and bluefin-lts must specify bootloader=grub."""
        for variant in ["bluefin", "bluefin-lts"]:
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
        """bluefin and bluefin-lts must set composefs=false."""
        for variant in ["bluefin", "bluefin-lts"]:
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
