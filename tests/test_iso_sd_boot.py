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
        script_content = self._rewrite_path_line(
            script_content,
            "PATH=/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:$PATH",
            'PATH="${PATH}:/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin"',
        )
        script_content = self._rewrite_path_line(
            script_content,
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

        # Stub delegate live/src/build-iso.sh.
        # Log one argument per line (plus the argument count) instead of "$*": joining
        # argv with spaces erases argument boundaries, so a quoting regression that
        # splits --title "Dakota Live" into two words would be indistinguishable.
        self.delegate_script = self.sandbox / "live" / "src" / "build-iso.sh"
        self.delegate_script.write_text(
            "#!/usr/bin/bash\n"
            "{\n"
            '  echo "delegate-build-iso argc=$#"\n'
            '  for _arg in "$@"; do printf \'delegate-build-iso-arg %s\\n\' "$_arg"; done\n'
            '} >> "$STUB_CALLS"\n'
            "exit 0\n"
        )
        self.delegate_script.chmod(0o755)

    def _rewrite_path_line(self, content, original, replacement):
        """Replace a PATH-prepend line, failing loudly if the source line moved or changed.

        Without this assertion a reworded line in scripts/iso-sd-boot.sh would silently
        no-op the rewrite, letting the real mount/umount/mksquashfs/rsync shadow the stubs.
        """
        self.assertIn(
            original,
            content,
            f"scripts/iso-sd-boot.sh no longer contains the expected PATH line: {original!r}. "
            "Update this test's PATH rewrite, otherwise the stubs are shadowed by real binaries.",
        )
        return content.replace(original, replacement)

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

        # `podman run ... skopeo copy` is the step that embeds the payload OCI archive
        # into the staged containers-storage. The storage driver it uses is only
        # observable through the storage.conf bind-mounted at /tmp/st.conf, which the
        # script deletes right after the run — so the stub dumps that file's contents
        # into the call log while it still exists.
        self._stub("podman", f"""
            echo "podman $*" >> "$STUB_CALLS"
            if [ "$1" = "run" ]; then
                for _arg in "$@"; do
                    case "$_arg" in
                        *:/tmp/st.conf:ro)
                            _conf=$(echo "$_arg" | sed 's|:/tmp/st.conf:ro$||')
                            sed 's|^|storage-conf |' "$_conf" >> "$STUB_CALLS"
                            ;;
                    esac
                done
                exit 0
            fi
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

        # scripts/iso-sd-boot.sh execs rsync to copy the overlay containers-storage
        # into the squashfs root. Stub it so the run stays hermetic instead of
        # depending on (or failing on) a host rsync.
        self._stub("rsync", """
            echo "rsync $*" >> "$STUB_CALLS"
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

    # Variables the script reads from the environment. Cleared before every run so an
    # ambient caller environment (e.g. DEBUG=1 INSTALLER_CHANNEL=dev) cannot leak into
    # the script under test and change the behaviour being asserted.
    SCRIPT_ENV_VARS = ("TARGET", "OUTPUT_DIR", "WORKDIR", "DEBUG", "INSTALLER_CHANNEL", "COMPRESSION")

    def run_script(self, env_vars=None):
        self._install_stubs()

        env = dict(os.environ)
        for name in self.SCRIPT_ENV_VARS:
            env.pop(name, None)
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

    ARG_PREFIX = "delegate-build-iso-arg "

    def delegate_invocations(self):
        """Return one argv list per recorded live/src/build-iso.sh invocation.

        The stub records each argument on its own line, so argument boundaries are
        preserved and both the ordering of the positional boot-tar/squashfs/ISO
        operands and the quoting of --title are observable.
        """
        invocations = []
        expected_counts = []
        for line in self.recorded_calls():
            if line.startswith("delegate-build-iso argc="):
                invocations.append([])
                expected_counts.append(int(line.split("=", 1)[1]))
            elif line.startswith(self.ARG_PREFIX):
                self.assertTrue(invocations, "delegate argument logged without an invocation header")
                invocations[-1].append(line[len(self.ARG_PREFIX):])
        for argv, count in zip(invocations, expected_counts):
            self.assertEqual(
                len(argv), count,
                f"delegate argv {argv!r} does not match logged argument count {count}; "
                "an argument was empty or contained a newline",
            )
        return invocations

    def expected_delegate_argv(self, target, title, out_dir):
        """Expected argv for the live/src/build-iso.sh delegation (see script tail)."""
        out = Path(os.path.realpath(out_dir))
        return [
            "--title", title,
            str(out / f"{target}-boot-files.tar"),
            str(out / f"{target}-rootfs.sfs"),
            str(out / f"{target}-live.iso"),
        ]

    def embed_run_calls(self):
        """Return the logged `podman run` lines that perform the OCI store embed."""
        return [
            c for c in self.recorded_calls()
            if c.startswith("podman run ") and "/payload.oci.tar" in c
        ]

    def storage_conf_lines(self):
        """Return the storage.conf contents captured from the embed `podman run`."""
        prefix = "storage-conf "
        return [c[len(prefix):].strip() for c in self.recorded_calls() if c.startswith(prefix)]


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

        # Delegate build-iso receives --title as ONE argument, then the boot-files tar,
        # the rootfs squashfs and the output ISO path, in that order. Asserting the full
        # argv catches both a quoting regression and a BOOT_TAR/SQUASHFS positional swap.
        invocations = self.delegate_invocations()
        self.assertEqual(len(invocations), 1)
        self.assertEqual(
            invocations[0],
            self.expected_delegate_argv("dakota", "Dakota Live", out_dir),
        )

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

        # Delegate build-iso receives the LTS title plus boot tar, squashfs, ISO in order
        invocations = self.delegate_invocations()
        self.assertEqual(len(invocations), 1)
        self.assertEqual(
            invocations[0],
            self.expected_delegate_argv("lts", "Bluefin LTS Live", out_dir),
        )

    def test_composefs_true_embeds_payload_into_vfs_containers_storage(self):
        """Composefs mode embeds the payload OCI into a VFS store under var/lib."""
        out_dir = self.sandbox / "output"
        res = self.run_script(env_vars={
            "TARGET": "dakota",
            "OUTPUT_DIR": str(out_dir),
        })
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        work = Path(os.path.realpath(out_dir))
        embeds = self.embed_run_calls()
        self.assertEqual(len(embeds), 1, f"expected exactly one embed run, got {embeds!r}")
        embed = embeds[0]

        # The staged store must be the composefs location (var/lib), bind-mounted at
        # the graphroot the storage.conf points at.
        self.assertIn(f"-v {work}/dakota-cs-staging/var/lib/containers/storage:/vfs-storage", embed)
        self.assertNotIn("/usr/lib/containers/storage:/vfs-storage", embed)
        self.assertIn(f"-v {work}/dakota-payload.oci.tar:/payload.oci.tar:ro", embed)
        self.assertIn("localhost/dakota-installer", embed)
        self.assertIn(
            "skopeo copy oci-archive:/payload.oci.tar:ghcr.io/projectbluefin/dakota:stable "
            "containers-storage:ghcr.io/projectbluefin/dakota:stable",
            embed,
        )

        # Driver is only visible in the storage.conf the run consumes.
        self.assertIn('driver = "vfs"', self.storage_conf_lines())

    def test_composefs_false_embeds_payload_into_overlay_containers_storage(self):
        """Non-composefs mode embeds the payload OCI into an overlay store under usr/lib."""
        out_dir = self.sandbox / "output"
        res = self.run_script(env_vars={
            "TARGET": "lts",
            "OUTPUT_DIR": str(out_dir),
        })
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")

        work = Path(os.path.realpath(out_dir))
        embeds = self.embed_run_calls()
        self.assertEqual(len(embeds), 1, f"expected exactly one embed run, got {embeds!r}")
        embed = embeds[0]

        self.assertIn(f"-v {work}/lts-cs-staging/usr/lib/containers/storage:/vfs-storage", embed)
        self.assertNotIn("/var/lib/containers/storage:/vfs-storage", embed)
        self.assertIn(f"-v {work}/lts-payload.oci.tar:/payload.oci.tar:ro", embed)
        self.assertIn("localhost/lts-installer", embed)
        self.assertIn(
            "skopeo copy oci-archive:/payload.oci.tar:ghcr.io/projectbluefin/bluefin-lts:stable "
            "containers-storage:ghcr.io/projectbluefin/bluefin-lts:stable",
            embed,
        )

        self.assertIn('driver = "overlay"', self.storage_conf_lines())

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
