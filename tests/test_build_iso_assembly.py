"""Integration tests for live/src/build-iso.sh ISO assembly.

Runs the real script against a mock boot-files tar and squashfs and checks
that it produces an ISO. Skipped when xorriso/mtools are absent.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest


def _missing_tools():
    """Return a list of missing ISO build tools, or empty list if all present."""
    missing = []
    for tool in ("xorriso", "mkfs.fat", "mmd", "mcopy"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            missing.append(tool)
    return missing


def _create_mock_boot_tar(tmpdir: Path) -> Path:
    """Create a mock boot-files tar with fake kernel, initramfs, and EFI binary."""
    boot_dir = tmpdir / "boot"
    modules_dir = boot_dir / "usr" / "lib" / "modules" / "6.12.0"
    efi_dir = boot_dir / "usr" / "lib" / "systemd" / "boot" / "efi"
    modules_dir.mkdir(parents=True)
    efi_dir.mkdir(parents=True)

    # Create fake kernel and initramfs (small files for testing)
    (modules_dir / "vmlinuz").write_bytes(b"\x00" * 1024)
    (modules_dir / "initramfs.img").write_bytes(b"\x00" * 2048)

    (efi_dir / "systemd-bootx64.efi").write_bytes(b"\x00" * 512)

    # Create tar
    tar_path = tmpdir / "boot.tar"
    subprocess.run(
        ["tar", "-cf", str(tar_path), "-C", str(boot_dir), "."],
        check=True,
    )
    return tar_path


def _create_mock_squashfs(tmpdir: Path) -> Path:
    """Create a minimal squashfs for testing."""
    squashfs_root = tmpdir / "sfs-root"
    squashfs_root.mkdir()
    (squashfs_root / "etc").mkdir()
    (squashfs_root / "etc" / "os-release").write_text("ID=dakota\n")

    sfs_path = tmpdir / "squashfs.img"
    result = subprocess.run(
        ["mksquashfs", str(squashfs_root), str(sfs_path), "-noappend", "-quiet"],
        capture_output=True,
    )
    if result.returncode != 0:
        # mksquashfs not available — create a fake file
        sfs_path.write_bytes(b"\x00" * 4096)
    return sfs_path


_MISSING = _missing_tools()


@pytest.mark.skipif(
    bool(_MISSING),
    reason=(
        f"ISO build tools not installed: {_MISSING}. "
        "Install with: sudo apt-get install -y xorriso dosfstools mtools"
    ),
)
class TestBuildIsoAssembly(unittest.TestCase):
    """Test ISO assembly from boot files and a squashfs."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-iso-"))
        self.script = Path(__file__).parent.parent / "live" / "src" / "build-iso.sh"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmpdir)], check=False)

    def _run(self, *args):
        return subprocess.run(
            ["bash", str(self.script), *map(str, args)],
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": str(self.tmpdir)},
        )

    def test_builds_iso_from_positional_args(self):
        """<boot-tar> <squashfs> <output-iso> produces an ISO."""
        boot_tar = _create_mock_boot_tar(self.tmpdir)
        squashfs = _create_mock_squashfs(self.tmpdir)
        output_iso = self.tmpdir / "test.iso"

        result = self._run(boot_tar, squashfs, output_iso)
        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertTrue(output_iso.exists())

    def test_unknown_option_is_rejected(self):
        """Removed flags such as --arch fail loudly instead of being read as a path."""
        boot_tar = _create_mock_boot_tar(self.tmpdir)
        squashfs = _create_mock_squashfs(self.tmpdir)
        output_iso = self.tmpdir / "test.iso"

        result = self._run("--arch", f"x86_64:{boot_tar}:{squashfs}", output_iso)
        self.assertEqual(result.returncode, 2, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("unknown option: --arch", result.stderr)
        self.assertFalse(output_iso.exists())


if __name__ == "__main__":
    unittest.main()
