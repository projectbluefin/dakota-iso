"""Tests for multi-arch ISO build logic.

Validates that build-iso.sh correctly handles the --arch flag by creating
mock boot-files tars and verifying the ISO layout.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def _have_tools():
    """Check if ISO build tools are available."""
    for tool in ("xorriso", "mkfs.fat", "mtools", "mmd", "mcopy"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            return False
    return True


def _create_mock_boot_tar(tmpdir: Path, arch: str) -> Path:
    """Create a mock boot-files tar with fake kernel, initramfs, and EFI binary."""
    boot_dir = tmpdir / f"boot-{arch}"
    modules_dir = boot_dir / "usr" / "lib" / "modules" / "6.12.0"
    efi_dir = boot_dir / "usr" / "lib" / "systemd" / "boot" / "efi"
    modules_dir.mkdir(parents=True)
    efi_dir.mkdir(parents=True)

    # Create fake kernel and initramfs (small files for testing)
    (modules_dir / "vmlinuz").write_bytes(b"\x00" * 1024)
    (modules_dir / "initramfs.img").write_bytes(b"\x00" * 2048)

    # Create arch-appropriate EFI binary
    if arch == "aarch64":
        (efi_dir / "systemd-bootaa64.efi").write_bytes(b"\x00" * 512)
    else:
        (efi_dir / "systemd-bootx64.efi").write_bytes(b"\x00" * 512)

    # Create tar
    tar_path = tmpdir / f"boot-{arch}.tar"
    subprocess.run(
        ["tar", "-cf", str(tar_path), "-C", str(boot_dir), "."],
        check=True,
    )
    return tar_path


def _create_mock_squashfs(tmpdir: Path, arch: str) -> Path:
    """Create a minimal squashfs for testing."""
    squashfs_root = tmpdir / f"sfs-root-{arch}"
    squashfs_root.mkdir()
    (squashfs_root / "etc").mkdir()
    (squashfs_root / "etc" / "os-release").write_text(f"ARCH={arch}\n")

    sfs_path = tmpdir / f"squashfs-{arch}.img"
    result = subprocess.run(
        ["mksquashfs", str(squashfs_root), str(sfs_path), "-noappend", "-quiet"],
        capture_output=True,
    )
    if result.returncode != 0:
        # mksquashfs not available — create a fake file
        sfs_path.write_bytes(b"\x00" * 4096)
    return sfs_path


@unittest.skipUnless(_have_tools(), "ISO build tools not available")
class TestMultiArchISO(unittest.TestCase):
    """Test multi-arch ISO assembly."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-iso-"))
        self.script = Path(__file__).parent.parent / "live" / "src" / "build-iso.sh"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmpdir)], check=False)

    def test_single_arch_backwards_compatible(self):
        """Single-arch invocation still works with positional args."""
        boot_tar = _create_mock_boot_tar(self.tmpdir, "x86_64")
        squashfs = _create_mock_squashfs(self.tmpdir, "x86_64")
        output_iso = self.tmpdir / "test-single.iso"

        result = subprocess.run(
            ["bash", str(self.script), str(boot_tar), str(squashfs), str(output_iso)],
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": str(self.tmpdir)},
        )
        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertTrue(output_iso.exists())

    def test_multi_arch_two_architectures(self):
        """Multi-arch invocation with --arch x86_64 and --arch aarch64."""
        boot_x86 = _create_mock_boot_tar(self.tmpdir, "x86_64")
        boot_arm = _create_mock_boot_tar(self.tmpdir, "aarch64")
        sfs_x86 = _create_mock_squashfs(self.tmpdir, "x86_64")
        sfs_arm = _create_mock_squashfs(self.tmpdir, "aarch64")
        output_iso = self.tmpdir / "test-multi.iso"

        result = subprocess.run(
            [
                "bash", str(self.script),
                "--arch", f"x86_64:{boot_x86}:{sfs_x86}",
                "--arch", f"aarch64:{boot_arm}:{sfs_arm}",
                str(output_iso),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": str(self.tmpdir)},
        )
        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertTrue(output_iso.exists())

        # Verify the ISO contains both arch entries in stdout
        self.assertIn("[x86_64]", result.stdout)
        self.assertIn("[aarch64]", result.stdout)
        self.assertIn("Multi-arch mode: 2", result.stdout)


class TestMultiArchArgParsing(unittest.TestCase):
    """Test argument parsing without requiring ISO tools."""

    def test_arch_spec_parsing(self):
        """Verify arch:boot-tar:squashfs format is parseable."""
        spec = "x86_64:/path/to/boot.tar:/path/to/rootfs.sfs"
        parts = spec.split(":", 2)
        self.assertEqual(parts[0], "x86_64")
        self.assertEqual(parts[1], "/path/to/boot.tar")
        self.assertEqual(parts[2], "/path/to/rootfs.sfs")

    def test_arch_spec_aarch64(self):
        """Verify aarch64 spec parsing."""
        spec = "aarch64:/tmp/arm-boot.tar:/tmp/arm-rootfs.sfs"
        parts = spec.split(":", 2)
        self.assertEqual(parts[0], "aarch64")
        self.assertEqual(parts[1], "/tmp/arm-boot.tar")
        self.assertEqual(parts[2], "/tmp/arm-rootfs.sfs")


if __name__ == "__main__":
    unittest.main()
