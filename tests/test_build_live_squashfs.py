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
  must be squashed to a single layer with `podman build --squash-all`. Omitting the squash
  causes the ~120 layers of chunkified images to explode the VFS storage directory,
  ballooning ISO size from ~5 GB to ~12 GB (a recurring regression documented in AGENTS.md).
- When --oci-image is provided in non-composefs mode (composeFsBackend=false), the payload
  is built without squashing to preserve ostree commit objects, and imported into
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
        for helper in ("variant-config.sh", "payload-oci.sh"):
            shutil.copy2(REPO / "scripts" / helper, self.sandbox / "scripts" / helper)
            (self.sandbox / "scripts" / helper).chmod(0o755)

        # Stub the signature verifier: record the ref and echo a digest-pinned ref,
        # the contract build-live-squashfs.sh relies on in --target mode.
        self.verifier_stub = self.sandbox / "scripts" / "verify-image-signature.sh"
        self.verifier_stub.write_text(textwrap.dedent("""\
            #!/usr/bin/bash
            echo "verify-image-signature $*" >> "$STUB_CALLS"
            echo "${1%%[:@]*}@sha256:feedface"
        """))
        self.verifier_stub.chmod(0o755)

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

    def _install_stubs(self, *, composefs=True, probe_exit=None):
        if probe_exit is None:
            probe_exit = 0 if composefs else 1
        self._stub("podman", f"""
            echo "podman $*" >> "$STUB_CALLS"
            if [ "$1" = "build" ]; then
                _prev=""
                for _arg in "$@"; do
                    if [ "$_prev" = "-f" ] && [ -f "$_arg" ]; then
                        sed 's|^|containerfile |' "$_arg" >> "$STUB_CALLS"
                    fi
                    _prev="$_arg"
                done
                exit 0
            fi
            if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
                echo "sha256:11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
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
                        exit {probe_exit}
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

    def run_script(self, *args, composefs=True, probe_exit=None, env_vars=None):
        self._install_stubs(composefs=composefs, probe_exit=probe_exit)

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

    def payload_builds(self):
        """`podman build` calls that prepare the payload (not the live container)."""
        return [c for c in self.recorded_calls()
                if c.startswith("podman build") and "localhost/iso-payload-work:" in c]

    def recorded_calls(self):
        if not self.calls_log.exists():
            return []
        text = self.calls_log.read_text().strip()
        return [line for line in text.split("\n") if line]


class TestBuildLiveSquashfsExecution(BuildLiveSquashfsHarness):
    """Execution tests verifying composefs --squash behavior and argument handling."""

    def test_composefs_mode_squashes_payload_commit(self):
        """When --oci-image is given and composeFsBackend is true, every payload build squashes."""
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
        builds = self.payload_builds()
        self.assertEqual(len(builds), 2, f"expected inject+relabel payload builds: {calls!r}")
        for build in builds:
            self.assertIn("--pull=never", build)
            self.assertIn(
                "--format oci",
                build,
                "payload build must pin --format oci; Docker format drops --annotation "
                "(ostree.final-diffid)",
            )
            self.assertIn(
                "--squash-all",
                build,
                f"payload build in composefs mode is missing --squash-all: {build!r}. "
                "Omitting the squash inflates ISO sizes from ~5 GB to ~12 GB.",
            )

        # The payload is built FROM the source image with only the bootc install
        # config copied in, then relabelled with the squashed layer's diff_id.
        self.assertIn("containerfile FROM ghcr.io/projectbluefin/dakota-nvidia:stable", calls)
        self.assertIn(
            "containerfile COPY bootc-root-mount.toml /usr/lib/bootc/install/00-defaults.toml",
            calls,
        )
        diffid = "sha256:11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
        self.assertIn(f"--label ostree.final-diffid={diffid}", builds[1])
        self.assertIn(f"--annotation ostree.final-diffid={diffid}", builds[1])

        # Only the relabelled image is pushed (each push gzips the squashed
        # layer), under the payload's own name.
        pushes = [c for c in calls if c.startswith("podman push ")]
        self.assertEqual(len(pushes), 1, calls)
        self.assertIn("localhost/iso-payload-work:final-", pushes[0])
        self.assertTrue(
            pushes[0].endswith(":ghcr.io/projectbluefin/dakota-nvidia:stable"), pushes[0])
        self.assertIn("oci-archive:", pushes[0])

        self.assertFalse([c for c in calls if c.startswith("buildah")],
                         "build-live-squashfs.sh must not need buildah")

    def test_non_composefs_mode_commits_without_squash(self):
        """When --oci-image is given and composeFsBackend is false, the payload is not squashed."""
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
        builds = self.payload_builds()
        self.assertEqual(len(builds), 1, f"expected one payload build: {calls!r}")
        self.assertNotIn("--squash", builds[0],
                         f"payload build in non-composefs mode must not squash: {builds[0]!r}")
        self.assertIn(
            "containerfile COPY bootc-root-mount.toml /usr/lib/bootc/install/00-defaults.toml",
            calls,
        )
        self.assertFalse([c for c in calls if c.startswith("buildah")],
                         "build-live-squashfs.sh must not need buildah")

    def test_positional_probe_fails_closed_on_error(self):
        """A probe that errors (podman 125, grep 2) must abort, not fall back to non-composefs."""
        for probe_exit in (2, 125, 127):
            with self.subTest(probe_exit=probe_exit):
                self.calls_log.write_text("")
                res = self.run_script(
                    "--oci-image", "ghcr.io/projectbluefin/dakota-nvidia:stable",
                    "localhost/dakota-nvidia-live:latest",
                    str(self.sandbox / "output" / "dakota.sfs"),
                    str(self.sandbox / "output" / "dakota.tar"),
                    probe_exit=probe_exit,
                )
                self.assertNotEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
                self.assertIn("could not read composeFsBackend", res.stderr)
                self.assertNotIn("composeFsBackend=false", res.stdout)
                calls = self.recorded_calls()
                self.assertFalse(self.payload_builds(), calls)
                self.assertFalse([c for c in calls if c.startswith("mksquashfs")], calls)

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

    def _write_variant_composefs(self, bootloader_variant, value):
        vdir = self.sandbox / "live" / "src" / bootloader_variant
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "composefs").write_text(value)

    def run_target_mode(self, target, *, composefs_stub):
        """Run --target mode with the recipe.json probe stubbed to composefs_stub."""
        return self.run_script(
            "--target", target,
            "--oci-image", f"ghcr.io/projectbluefin/{target}-nvidia:stable",
            "--output-dir", str(self.sandbox / "output"),
            composefs=composefs_stub,
        )

    def test_target_mode_reads_composefs_from_variant_config(self):
        """--target mode resolves composefs via variant-config.sh, not the recipe.json probe."""
        self._write_variant_composefs("bluefin", "false")
        # Stub says composefs=true; variant-config.sh says false and must win.
        res = self.run_target_mode("bluefin", composefs_stub=True)
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("composeFsBackend=false", res.stdout)

        builds = self.payload_builds()
        self.assertTrue(builds, "payload was not built")
        for build in builds:
            self.assertNotIn(
                "--squash",
                build,
                f"composefs=false variant must not squash the payload: {build!r}",
            )

    def test_target_mode_composefs_true_squashes_payload(self):
        """--target mode with composefs=true squashes the payload even if the probe says false."""
        self._write_variant_composefs("dakota", "true")
        res = self.run_target_mode("dakota", composefs_stub=False)
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("composeFsBackend=true", res.stdout)

        builds = self.payload_builds()
        self.assertTrue(builds, "payload was not built")
        self.assertTrue(
            all("--squash-all" in c for c in builds),
            f"composefs=true variant must squash the payload: {builds!r}",
        )

    def test_target_mode_composefs_defaults_to_true(self):
        """--target mode with no live/src/<variant>/composefs file defaults to true."""
        res = self.run_target_mode("dakota", composefs_stub=False)
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("composeFsBackend=true", res.stdout)

    def test_target_mode_verifies_live_base_and_pins_digest(self):
        """--target mode verifies the live base image and passes the digest to podman build."""
        res = self.run_target_mode("dakota", composefs_stub=False)
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        calls = self.calls_log.read_text()
        self.assertIn("verify-image-signature ghcr.io/projectbluefin/dakota:stable", calls)
        build = next(l for l in calls.splitlines() if l.startswith("podman build"))
        self.assertIn("--build-arg BASE_DIGEST=@sha256:feedface", build)
        self.assertIn("--build-arg TAG=stable", build)
        self.assertIn("--build-arg REGISTRY=projectbluefin", build)

    def test_target_mode_fails_closed_when_verification_fails(self):
        """--target mode aborts before podman build when the verifier exits non-zero."""
        self.verifier_stub.write_text("#!/usr/bin/bash\necho 'ERROR: bad signature' >&2\nexit 1\n")
        res = self.run_target_mode("dakota", composefs_stub=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("podman build", self.calls_log.read_text())

    def test_target_mode_composefs_uses_live_target_derivation(self):
        """--target mode reads composefs from live_target with the -nvidia suffix stripped."""
        (self.sandbox / "bluefin").mkdir(parents=True, exist_ok=True)
        (self.sandbox / "bluefin" / "live_target").write_text("bluefin-nvidia\n")
        self._write_variant_composefs("bluefin", "false")
        res = self.run_target_mode("bluefin", composefs_stub=True)
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("composeFsBackend=false", res.stdout)

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
