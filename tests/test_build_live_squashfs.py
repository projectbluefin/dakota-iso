"""Behavioural unit tests for scripts/build-live-squashfs.sh.

scripts/build-live-squashfs.sh builds a live rootfs squashfs and companion boot tar
from a container image. It operates in two modes:

1. Target mode:
       build-live-squashfs.sh --target <variant> [--installer-channel dev|stable]
           [--oci-image <ref>] --output-dir <dir> [--debug 0|1]

2. Positional mode:
       build-live-squashfs.sh [--oci-image <ref>] <image> <output-squashfs> <output-boot-tar>

Critical invariants:
- When --oci-image is provided in composefs mode (composeFsBackend=true), the payload
  must be squashed to a single layer with `buildah commit --squash`. Omitting --squash
  causes the ~120 layers of chunkified images to explode the VFS storage directory,
  ballooning ISO size from ~5 GB to ~12 GB (a recurring regression documented in AGENTS.md).
- When --oci-image is provided in non-composefs mode (composeFsBackend=false), the payload
  is committed without --squash to preserve ostree commit objects, and imported into
  overlay containers-storage.
- Flag and argument parsing validation: missing positional arguments or missing required
  flags (--output-dir in target mode) must fail early.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "build-live-squashfs.sh"


class BuildLiveSquashfsHarness(unittest.TestCase):
    """Test harness running scripts/build-live-squashfs.sh against stubbed system utilities."""

    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="build-live-squashfs-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)

        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        self.calls_log = self.sandbox / "calls.log"
        self.calls_log.touch()

        # Replicate repository structure in sandbox
        (self.sandbox / "scripts").mkdir()
        (self.sandbox / "live" / "src").mkdir(parents=True)
        (self.sandbox / "output").mkdir()

        # Copy script under test
        self.script_under_test = self.sandbox / "scripts" / "build-live-squashfs.sh"
        shutil.copy2(SCRIPT, self.script_under_test)
        self.script_under_test.chmod(0o755)

        # Setup mock container mount
        self.mount_dir = self.sandbox / "mock-image-mount"
        self.mount_dir.mkdir(parents=True, exist_ok=True)
        (self.mount_dir / "usr" / "lib" / "modules").mkdir(parents=True, exist_ok=True)
        (self.mount_dir / "usr" / "lib" / "systemd" / "boot" / "efi").mkdir(parents=True, exist_ok=True)

    def _stub(self, name, body):
        path = self.bindir / name
        path.write_text("#!/usr/bin/bash\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def _install_stubs(self, *, composefs=True):
        self._stub("podman", f"""
            echo "podman $*" >> "$STUB_CALLS"
            if [ "$1" = "build" ]; then
                exit 0
            fi
            if [ "$1" = "image" ] && [ "$2" = "mount" ]; then
                echo "{self.mount_dir}"
                exit 0
            fi
            if [ "$1" = "image" ] && [ "$2" = "unmount" ]; then
                exit 0
            fi
            if [ "$1" = "run" ]; then
                # Check if this is the composeFsBackend detection call
                for arg in "$@"; do
                    if [[ "$arg" == *"composeFsBackend"* ]]; then
                        {"exit 0" if composefs else "exit 1"}
                    fi
                done
                exit 0
            fi
            exit 0
        """)

        self._stub("buildah", """
            echo "buildah $*" >> "$STUB_CALLS"
            if [ "$1" = "from" ]; then
                echo "fake-ctr-123"
                exit 0
            fi
            exit 0
        """)

        self._stub("skopeo", """
            echo "skopeo $*" >> "$STUB_CALLS"
            if [ "$1" = "inspect" ]; then
                printf '{"rootfs": {"diff_ids": ["sha256:11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"]}}\\n'
                exit 0
            fi
            exit 0
        """)

        self._stub("mksquashfs", """
            echo "mksquashfs $*" >> "$STUB_CALLS"
            touch "$2"
            exit 0
        """)

        # rsync is used in the non-composefs path to copy the overlay store while
        # skipping whiteout device nodes. Stub it so the suite stays hermetic on
        # hosts without rsync installed.
        self._stub("rsync", """
            echo "rsync $*" >> "$STUB_CALLS"
            src="${@: -2:1}"
            dst="${@: -1:1}"
            mkdir -p "$dst"
            if [ -d "$src" ]; then
                cp -a "$src." "$dst" 2>/dev/null || true
            fi
            exit 0
        """)

        self._stub("findmnt", """
            echo "unknown"
            exit 0
        """)

        self._stub("mount", """
            exit 0
        """)

        self._stub("umount", """
            exit 0
        """)

        # Stub id to report root (id -u returns 0)
        self._stub("id", """
            if [ "$1" = "-u" ]; then
                echo "0"
                exit 0
            fi
            exec /usr/bin/id "$@"
        """)

    def run_script(self, *args, composefs=True, env_vars=None):
        self._install_stubs(composefs=composefs)

        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["STUB_CALLS"] = str(self.calls_log)
        # Point scratch dir to sandbox
        env["SUPERISO_TMPDIR"] = str(self.sandbox / "tmp")
        (self.sandbox / "tmp").mkdir(exist_ok=True)

        if env_vars:
            env.update(env_vars)

        return subprocess.run(
            ["bash", str(self.script_under_test), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.sandbox,
        )

    def recorded_calls(self):
        if not self.calls_log.exists():
            return []
        text = self.calls_log.read_text().strip()
        return [line for line in text.split("\n") if line]


class TestBuildLiveSquashfsExecution(BuildLiveSquashfsHarness):
    """Execution tests verifying composefs --squash behavior and argument handling."""

    def test_composefs_mode_squashes_payload_commit(self):
        """When --oci-image is given and composeFsBackend is true, buildah commit MUST use --squash."""
        out_sfs = self.sandbox / "output" / "dakota.sfs"
        out_tar = self.sandbox / "output" / "dakota.tar"
        res = self.run_script(
            "--oci-image", "ghcr.io/projectbluefin/dakota-nvidia:stable",
            "localhost/dakota-nvidia-live:latest",
            str(out_sfs),
            str(out_tar),
            composefs=True,
        )
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        buildah_commits = [c for c in calls if c.startswith("buildah commit")]
        self.assertTrue(len(buildah_commits) >= 1, "buildah commit was not called")

        for commit_call in buildah_commits:
            self.assertIn(
                "--squash",
                commit_call,
                f"buildah commit in composefs mode is missing --squash: {commit_call!r}. "
                "Omitting --squash inflates ISO sizes from ~5 GB to ~12 GB.",
            )

        # Check that ostree.final-diffid annotation is set
        annot_calls = [c for c in calls if "ostree.final-diffid" in c]
        self.assertTrue(len(annot_calls) >= 1, "ostree.final-diffid annotation not updated")

    def test_non_composefs_mode_commits_without_squash(self):
        """When --oci-image is given and composeFsBackend is false, buildah commit must NOT squash."""
        out_sfs = self.sandbox / "output" / "bluefin.sfs"
        out_tar = self.sandbox / "output" / "bluefin.tar"
        res = self.run_script(
            "--oci-image", "ghcr.io/projectbluefin/bluefin-nvidia:stable",
            "localhost/bluefin-nvidia-live:latest",
            str(out_sfs),
            str(out_tar),
            composefs=False,
        )
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        buildah_commits = [c for c in calls if c.startswith("buildah commit")]
        self.assertTrue(len(buildah_commits) >= 1, "buildah commit was not called")

        for commit_call in buildah_commits:
            self.assertNotIn(
                "--squash",
                commit_call,
                f"buildah commit in non-composefs mode must not use --squash: {commit_call!r}",
            )

    def test_missing_positional_arguments_fails(self):
        """Positional mode requires image, output squashfs, and output boot tar."""
        # 0 arguments
        res0 = self.run_script()
        self.assertNotEqual(res0.returncode, 0)
        self.assertIn("Usage: build-live-squashfs.sh", res0.stderr)

        # 1 argument
        res1 = self.run_script("localhost/test-image:latest")
        self.assertNotEqual(res1.returncode, 0)

        # 2 arguments
        res2 = self.run_script("localhost/test-image:latest", "/out/test.sfs")
        self.assertNotEqual(res2.returncode, 0)

    def test_target_mode_requires_output_dir(self):
        """Target mode (--target) fails if --output-dir is not provided."""
        res = self.run_script("--target", "dakota")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ERROR: --target requires --output-dir", res.stderr)

    def test_compression_release_sets_high_compression_level(self):
        """SUPERISO_COMPRESSION=release sets zstd level 15 and 1M block size."""
        out_sfs = self.sandbox / "output" / "dakota.sfs"
        out_tar = self.sandbox / "output" / "dakota.tar"
        res = self.run_script(
            "localhost/dakota-nvidia-live:latest",
            str(out_sfs),
            str(out_tar),
            env_vars={"SUPERISO_COMPRESSION": "release"},
        )
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        mksfs_calls = [c for c in calls if c.startswith("mksquashfs ")]
        self.assertEqual(len(mksfs_calls), 1)
        self.assertIn("-Xcompression-level 15", mksfs_calls[0])
        self.assertIn("-b 1048576", mksfs_calls[0])


if __name__ == "__main__":
    unittest.main()
