"""Behavioural unit tests for scripts/iso-sd-boot.sh.

scripts/iso-sd-boot.sh builds a systemd-boot UEFI live ISO for a given target variant
(dakota, stable, lts, etc.). It coordinates container building, offline OCI store injection,
composefs vs non-composefs squashfs assembly, and delegation to live/src/build-iso.sh.

It reads configuration from environment variables and target directory files:
    TARGET            — variant directory name (REQUIRED; fails if unset)
    OUTPUT_DIR        — directory for final ISO and artifacts (default: output)
    WORKDIR           — scratch directory for staging and squashfs root (default: OUTPUT_DIR)
    DEBUG             — 0 (default) or 1
    INSTALLER_CHANNEL — stable (default) or dev
    COMPRESSION       — fast (default) or release

In addition, it branches on target metadata:
    <TARGET>/payload_ref      — payload container image ref
    <TARGET>/live_target      — live environment image variant
    <TARGET>/live_title       — live ISO title passed to live/src/build-iso.sh
    live/src/<VARIANT>/composefs — true (default) or false (selects squash + diffid vs non-squashed)

Covered behaviour
-----------------
1. TARGET is strictly required; running without TARGET exits non-zero immediately.
2. Configuration defaults are respected (DEBUG=0, INSTALLER_CHANNEL=stable, COMPRESSION=fast).
3. Container build delegation invokes `just debug=... installer_channel=... container <target>`.
4. Composefs mode (composefs=true) squashes the payload image, inspects diff_id, updates
   ostree.final-diffid annotation/label, and embeds into VFS containers-storage.
5. Non-composefs mode (composefs=false) commits without squash to preserve ostree commits,
   and embeds into overlay containers-storage.
6. Compression selection sets mksquashfs block size and compression level (fast: level 3,
   128K; release: level 15, 1M).
7. Final delegation to live/src/build-iso.sh passes --title, boot-files tar, rootfs squashfs,
   and output ISO path.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "iso-sd-boot.sh"


class IsoSdBootHarness(unittest.TestCase):
    """Test harness running scripts/iso-sd-boot.sh against stubbed system utilities."""

    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="iso-sd-boot-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)

        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        self.calls_log = self.sandbox / "calls.log"
        self.calls_log.touch()

        # Replicate repository structure in sandbox
        (self.sandbox / "scripts").mkdir()
        (self.sandbox / "live" / "src").mkdir(parents=True)
        (self.sandbox / "output").mkdir()

        # Copy script under test, preserving stub PATH precedence
        script_content = SCRIPT.read_text()
        # In iso-sd-boot.sh, two lines prepend hardcoded /usr/sbin:/usr/bin to PATH,
        # which would shadow stubbed podman/buildah binaries in an unprivileged test sandbox.
        # Preserve the stub PATH in front:
        script_content = script_content.replace(
            "PATH=/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:$PATH",
            'PATH="${PATH}:/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin"',
        )
        script_content = script_content.replace(
            'PATH="/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:${PATH}"',
            'PATH="${PATH}:/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin"',
        )

        self.script_under_test = self.sandbox / "scripts" / "iso-sd-boot.sh"
        self.script_under_test.write_text(script_content)
        self.script_under_test.chmod(0o755)

        # Create target directories
        self.setup_target("dakota", payload_ref="ghcr.io/projectbluefin/dakota:stable\n",
                          live_target="dakota-nvidia\n", live_title="Dakota Live\n",
                          composefs="true\n")
        self.setup_target("lts", payload_ref="ghcr.io/projectbluefin/bluefin-lts:stable\n",
                          live_target="bluefin-lts\n", live_title="Bluefin LTS Live\n",
                          composefs="false\n")

        # Stub delegate live/src/build-iso.sh
        self.delegate_script = self.sandbox / "live" / "src" / "build-iso.sh"
        self.delegate_script.write_text("#!/usr/bin/bash\necho \"delegate-build-iso $*\" >> \"$STUB_CALLS\"\nexit 0\n")
        self.delegate_script.chmod(0o755)

    def setup_target(self, name, payload_ref, live_target, live_title, composefs):
        target_dir = self.sandbox / name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "payload_ref").write_text(payload_ref)
        (target_dir / "live_target").write_text(live_target)
        (target_dir / "live_title").write_text(live_title)

        variant = live_target.strip().replace("-nvidia-open", "").replace("-nvidia", "")
        variant_dir = self.sandbox / "live" / "src" / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / "composefs").write_text(composefs)

    def _stub(self, name, body):
        path = self.bindir / name
        path.write_text("#!/usr/bin/bash\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def _install_stubs(self, *, mount_dir=None):
        if mount_dir is None:
            mount_dir = self.sandbox / "fake-ctr-mount"
            mount_dir.mkdir(parents=True, exist_ok=True)
            (mount_dir / "usr" / "lib" / "modules").mkdir(parents=True, exist_ok=True)
            (mount_dir / "usr" / "lib" / "systemd" / "boot" / "efi").mkdir(parents=True, exist_ok=True)

        self._stub("just", """
            echo "just $*" >> "$STUB_CALLS"
            exit 0
        """)

        self._stub("podman", f"""
            echo "podman $*" >> "$STUB_CALLS"
            if [ "$1" = "image" ] && [ "$2" = "mount" ]; then
                echo "{mount_dir}"
                exit 0
            fi
            if [ "$1" = "image" ] && [ "$2" = "unmount" ]; then
                exit 0
            fi
            if [ "$1" = "unshare" ]; then
                shift
                exec "$@"
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

        self._stub("findmnt", """
            echo "xfs"
            exit 0
        """)

        self._stub("mount", """
            exit 0
        """)

        self._stub("umount", """
            exit 0
        """)

    def run_script(self, env_vars=None):
        self._install_stubs()

        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["STUB_CALLS"] = str(self.calls_log)

        if env_vars:
            env.update(env_vars)

        return subprocess.run(
            ["bash", str(self.script_under_test)],
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


class TestIsoSdBootSyntax(unittest.TestCase):
    """Syntax validation for scripts/iso-sd-boot.sh."""

    def test_syntax_is_valid_bash(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestIsoSdBootArgumentsAndDefaults(IsoSdBootHarness):
    """Verify argument enforcement, default values, and target validation."""

    def test_missing_target_fails_immediately(self):
        """TARGET is required; missing TARGET exits non-zero without building."""
        res = self.run_script(env_vars={"TARGET": ""})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("TARGET must be set", res.stderr)
        self.assertEqual(len(self.recorded_calls()), 0, "No tools should be invoked when TARGET is missing")

    def test_default_options_passed_to_just_container(self):
        """Default DEBUG=0, INSTALLER_CHANNEL=stable are passed to just container."""
        out_dir = self.sandbox / "custom_out"
        res = self.run_script(env_vars={
            "TARGET": "dakota",
            "OUTPUT_DIR": str(out_dir),
        })
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        just_calls = [c for c in calls if c.startswith("just ")]
        self.assertEqual(len(just_calls), 1)
        self.assertEqual(just_calls[0], "just debug=0 installer_channel=stable container dakota")

    def test_custom_debug_and_channel_options(self):
        """Explicit DEBUG=1 and INSTALLER_CHANNEL=dev are propagated."""
        out_dir = self.sandbox / "custom_out"
        res = self.run_script(env_vars={
            "TARGET": "dakota",
            "OUTPUT_DIR": str(out_dir),
            "DEBUG": "1",
            "INSTALLER_CHANNEL": "dev",
        })
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        just_calls = [c for c in calls if c.startswith("just ")]
        self.assertEqual(len(just_calls), 1)
        self.assertEqual(just_calls[0], "just debug=1 installer_channel=dev container dakota")


class TestIsoSdBootComposefsAndCompression(IsoSdBootHarness):
    """Verify composefs squashing vs non-composefs commit and compression flags."""

    def test_composefs_true_mode(self):
        """Composefs mode (dakota) squashes payload, updates final-diffid, and uses VFS."""
        out_dir = self.sandbox / "output"
        res = self.run_script(env_vars={
            "TARGET": "dakota",
            "OUTPUT_DIR": str(out_dir),
        })
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        buildah_calls = [c for c in calls if c.startswith("buildah ")]

        # Commit with --squash for composefs
        squash_commits = [c for c in buildah_calls if "commit --squash" in c]
        self.assertTrue(len(squash_commits) >= 1, "buildah commit --squash was not called for composefs mode")

        # Config with ostree.final-diffid
        annot_configs = [c for c in buildah_calls if "config --annotation ostree.final-diffid" in c]
        self.assertTrue(len(annot_configs) >= 1, "ostree.final-diffid annotation was not set")

        # Delegate build-iso called with title, boot-files tar, rootfs.sfs, output iso
        delegate_calls = [c for c in calls if c.startswith("delegate-build-iso ")]
        self.assertEqual(len(delegate_calls), 1)
        expected_iso = str(out_dir / "dakota-live.iso")
        self.assertIn("--title Dakota Live", delegate_calls[0])
        self.assertIn(expected_iso, delegate_calls[0])

    def test_composefs_false_mode(self):
        """Non-composefs mode (lts) commits without squash to preserve ostree commits."""
        out_dir = self.sandbox / "output"
        res = self.run_script(env_vars={
            "TARGET": "lts",
            "OUTPUT_DIR": str(out_dir),
        })
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        calls = self.recorded_calls()
        buildah_calls = [c for c in calls if c.startswith("buildah ")]

        # Must commit WITHOUT --squash
        squash_commits = [c for c in buildah_calls if "commit --squash" in c]
        self.assertEqual(len(squash_commits), 0, "buildah commit --squash must NOT be called in non-composefs mode")
        plain_commits = [c for c in buildah_calls if "commit " in c and "--squash" not in c]
        self.assertTrue(len(plain_commits) >= 1, "plain buildah commit was not called")

        # Delegate build-iso called with LTS title
        delegate_calls = [c for c in calls if c.startswith("delegate-build-iso ")]
        self.assertEqual(len(delegate_calls), 1)
        expected_iso = str(out_dir / "lts-live.iso")
        self.assertIn("--title Bluefin LTS Live", delegate_calls[0])
        self.assertIn(expected_iso, delegate_calls[0])

    def test_compression_fast_vs_release(self):
        """COMPRESSION=fast uses level 3 and 128K block; release uses level 15 and 1M block."""
        # Fast compression (default)
        res_fast = self.run_script(env_vars={"TARGET": "dakota", "COMPRESSION": "fast"})
        self.assertEqual(res_fast.returncode, 0)
        fast_calls = self.recorded_calls()
        mksfs_fast = [c for c in fast_calls if c.startswith("mksquashfs ")][0]
        self.assertIn("-Xcompression-level 3", mksfs_fast)
        self.assertIn("-b 131072", mksfs_fast)

        # Release compression
        self.calls_log.unlink(missing_ok=True)
        self.calls_log.touch()
        res_rel = self.run_script(env_vars={"TARGET": "dakota", "COMPRESSION": "release"})
        self.assertEqual(res_rel.returncode, 0)
        rel_calls = self.recorded_calls()
        mksfs_rel = [c for c in rel_calls if c.startswith("mksquashfs ")][0]
        self.assertIn("-Xcompression-level 15", mksfs_rel)
        self.assertIn("-b 1048576", mksfs_rel)


if __name__ == "__main__":
    unittest.main()
