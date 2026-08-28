"""Unit tests for scripts/fisherman-install.sh.

``scripts/fisherman-install.sh`` is the wrapper that both E2E install gates
(``scripts/plain-install-qemu.sh`` and ``scripts/luks-install-qemu.sh``) invoke
instead of calling ``fisherman`` directly. It owns three decisions that decide
whether an install job goes green or red:

1. **Failure triage.** fisherman exits non-zero on composefs sysroots when it
   writes ``/etc/hostname`` even though the OS install itself succeeded. The
   wrapper must swallow *only* that failure and must propagate every other
   non-zero exit — if the match ever widens, a genuinely broken install is
   reported as a pass.
2. **Target discovery.** It picks the installed root from ``lsblk`` output,
   unlocking LUKS with the passphrase read out of the recipe JSON when the disk
   is encrypted, and it must close the mapper again afterwards.
3. **Post-install patching.** It locates ``etc`` under either the composefs
   (``ostree/bootc/deploy``) or classic (``ostree/deploy``) layout and writes
   the hostname plus the ``rechunker-group-fix.service`` ordering-cycle
   override that keeps LTS/stable images bootable.

None of this was covered by a test. The script is only ever exercised inside a
QEMU VM at the end of a multi-hour ISO build, so a regression in the triage
logic surfaces as a mis-reported install result, not as a test failure.

These tests run the real script under ``bash`` with a stub ``PATH`` in front of
it — the fisherman binary, ``lsblk``, ``cryptsetup``, ``mount`` and ``umount``
are replaced by recording shims, so the control flow is exercised for real
without touching a block device or requiring root.
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
FISHERMAN_INSTALL = REPO / "scripts" / "fisherman-install.sh"

# lsblk -nrpo NAME,FSTYPE output fragments for the three disk shapes the
# wrapper distinguishes.
LSBLK_PLAIN = "/dev/vda\n/dev/vda1 vfat\n/dev/vda2 btrfs\n"
LSBLK_LUKS = "/dev/vda\n/dev/vda1 vfat\n/dev/vda2 crypto_LUKS\n"
LSBLK_UNKNOWN = "/dev/vda\n/dev/vda1 vfat\n/dev/vda2 ext4\n"

# The two log shapes that mean "the install worked, only the hostname write
# blew up" — old fisherman and new fisherman respectively.
LOG_HOSTNAME_OSTREE_ADMIN = (
    "installing bootc payload...\n"
    "error writing hostname: ostree admin --print-current-dir failed\n"
)
LOG_HOSTNAME_COMPOSEFS = (
    "installing bootc payload...\n"
    "error writing hostname: reading composefs deploy base: "
    "no such file or directory\n"
)


class FishermanInstallHarness(unittest.TestCase):
    """Runs scripts/fisherman-install.sh against stubbed system binaries."""

    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="fisherman-install-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        self.calls = self.sandbox / "calls.log"
        self.calls.touch()
        # The script creates its own mount point with `mktemp -d /tmp/...`, so
        # the stub `mount` reports where it landed and we clean it up here.
        self.mnt_record = self.sandbox / "mountpoint"
        self.addCleanup(self._cleanup_mountpoint)

    def _cleanup_mountpoint(self):
        if self.mnt_record.exists():
            target = self.mnt_record.read_text().strip()
            if target.startswith("/tmp/post-install-fix-"):
                shutil.rmtree(target, ignore_errors=True)

    def _stub(self, name, body):
        path = self.bindir / name
        path.write_text("#!/usr/bin/bash\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def _write_recipe(self, **recipe):
        path = self.sandbox / "recipe.json"
        path.write_text(json.dumps(recipe))
        return path

    def _install_stubs(self, *, fish_rc=0, fish_log="install complete\n",
                       lsblk=LSBLK_PLAIN, layout="ostree/bootc/deploy/default/abcdef/etc",
                       mount_rc=0, luks_open_rc=0):
        """Install recording shims for every external command the script calls.

        ``layout`` is the directory tree the stub ``mount`` materialises under
        the mount point, standing in for the deployment on the installed disk.
        Pass ``None`` to mount an empty filesystem (no deployment found).
        """
        self._stub("fisherman", f"""
            printf '%s' {shlex.quote(fish_log)}
            echo "fisherman $*" >> "$STUB_CALLS"
            exit {fish_rc}
        """)
        self._stub("lsblk", f"""
            echo "lsblk $*" >> "$STUB_CALLS"
            printf '%s' {shlex.quote(lsblk)}
        """)
        self._stub("mount", f"""
            echo "mount $*" >> "$STUB_CALLS"
            [ {mount_rc} -eq 0 ] || exit {mount_rc}
            target="${{@: -1}}"
            echo "$target" > "$STUB_MNT"
            {'mkdir -p "$target"/' + shlex.quote(layout) if layout else ':'}
        """)
        self._stub("umount", """
            echo "umount $*" >> "$STUB_CALLS"
        """)
        self._stub("cryptsetup", f"""
            action="$1"
            if [ "$action" = "luksOpen" ]; then
                passphrase="$(cat)"
                echo "cryptsetup $* key=$passphrase" >> "$STUB_CALLS"
                exit {luks_open_rc}
            fi
            echo "cryptsetup $*" >> "$STUB_CALLS"
        """)

    def run_script(self, recipe_path, **stub_kwargs):
        self._install_stubs(**stub_kwargs)
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["FISHERMAN_BIN"] = str(self.bindir / "fisherman")
        env["STUB_CALLS"] = str(self.calls)
        env["STUB_MNT"] = str(self.mnt_record)
        env["TMPDIR"] = str(self.sandbox)
        proc = subprocess.run(
            ["bash", str(FISHERMAN_INSTALL), str(recipe_path)],
            capture_output=True, text=True, env=env, cwd=self.sandbox,
        )
        proc.calls = self.calls.read_text()
        return proc

    def deployed_etc(self):
        """Return the stubbed deployment's etc/ dir, or None if never created."""
        if not self.mnt_record.exists():
            return None
        root = Path(self.mnt_record.read_text().strip())
        matches = sorted(p for p in root.glob("ostree/**/etc") if p.is_dir())
        return matches[0] if matches else None


class TestFailureTriage(FishermanInstallHarness):
    """Only the composefs hostname-write failure may be swallowed."""

    def test_script_syntax_is_valid_bash(self):
        proc = subprocess.run(["bash", "-n", str(FISHERMAN_INSTALL)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_old_fisherman_hostname_failure_is_swallowed_and_patched(self):
        recipe = self._write_recipe(hostname="dakota-test")
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_OSTREE_ADMIN)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("patching manually", proc.stdout)
        etc = self.deployed_etc()
        self.assertIsNotNone(etc, "deployment etc/ was never mounted")
        self.assertEqual((etc / "hostname").read_text().strip(), "dakota-test")

    def test_new_fisherman_composefs_failure_is_swallowed_and_patched(self):
        recipe = self._write_recipe(hostname="lts-box")
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_COMPOSEFS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("patching manually", proc.stdout)
        self.assertEqual((self.deployed_etc() / "hostname").read_text().strip(),
                         "lts-box")

    def test_unrelated_failure_propagates_exit_code(self):
        """A real install failure must stay red — this is the whole gate."""
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(
            recipe, fish_rc=7,
            fish_log="error: bootc install to-filesystem: no space left on device\n",
        )
        self.assertEqual(proc.returncode, 7)
        self.assertIn("non-hostname reason", proc.stdout)
        self.assertEqual(proc.calls.strip().splitlines(), ["fisherman " + str(recipe)],
                         "wrapper kept going after an unrelated failure")

    def test_hostname_mention_without_composefs_marker_propagates(self):
        """"writing hostname" alone is not the known bug — both halves required.

        Guards the ``&&`` in the triage condition: if it ever degrades to a
        single ``grep -q "writing hostname"``, any failure whose log merely
        mentions the hostname step would be reported as a successful install.
        """
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(
            recipe, fish_rc=3,
            fish_log="writing hostname\nerror: disk full while writing rootfs\n",
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("non-hostname reason", proc.stdout)

    def test_success_path_skips_hostname_but_still_patches_override(self):
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(recipe, fish_rc=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fisherman succeeded", proc.stdout)
        etc = self.deployed_etc()
        self.assertFalse((etc / "hostname").exists(),
                         "hostname rewritten even though fisherman succeeded")
        self.assertTrue(
            (etc / "systemd/system/rechunker-group-fix.service.d/override.conf").exists()
        )


class TestTargetDiscovery(FishermanInstallHarness):
    """LUKS vs plain root selection and mapper lifecycle."""

    def test_luks_passphrase_is_read_from_recipe_and_mapper_closed(self):
        recipe = self._write_recipe(hostname="crypt-box",
                                    encryption={"passphrase": "testpassphrase"})
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_COMPOSEFS,
                               lsblk=LSBLK_LUKS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("luksOpen --key-file=- --batch-mode /dev/vda2",
                      proc.calls)
        self.assertIn("key=testpassphrase", proc.calls)
        self.assertIn("cryptsetup luksClose", proc.calls,
                      "LUKS mapper left open after patching")
        self.assertEqual((self.deployed_etc() / "hostname").read_text().strip(),
                         "crypt-box")

    def test_luks_without_passphrase_reports_error_and_skips_patch(self):
        recipe = self._write_recipe(hostname="crypt-box")
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_COMPOSEFS,
                               lsblk=LSBLK_LUKS)
        self.assertIn("could not extract LUKS passphrase", proc.stdout)
        self.assertNotIn("cryptsetup luksOpen", proc.calls)
        self.assertIsNone(self.deployed_etc())

    def test_luks_open_failure_does_not_mount(self):
        recipe = self._write_recipe(hostname="crypt-box",
                                    encryption={"passphrase": "wrong"})
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_COMPOSEFS,
                               lsblk=LSBLK_LUKS, luks_open_rc=1)
        self.assertIn("cryptsetup luksOpen /dev/vda2 failed", proc.stdout)
        self.assertNotIn("mount /dev/mapper/", proc.calls)

    def test_no_recognised_filesystem_reports_error(self):
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(recipe, fish_rc=0, lsblk=LSBLK_UNKNOWN)
        self.assertIn("no btrfs, xfs, or crypto_LUKS partition found",
                      proc.stdout)
        self.assertIsNone(self.deployed_etc())

    def test_mount_failure_reports_error_and_skips_patch(self):
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(recipe, fish_rc=0, mount_rc=32)
        self.assertIn("post-install patch not applied", proc.stdout)
        self.assertIsNone(self.deployed_etc())


class TestPostInstallPatching(FishermanInstallHarness):
    """Deployment discovery and the contents of the patches applied."""

    def test_classic_ostree_layout_is_found_as_fallback(self):
        """Non-composefs deployments live under ostree/deploy/<sr>/deploy/<cs>."""
        recipe = self._write_recipe(hostname="classic-box")
        proc = self.run_script(
            recipe, fish_rc=1, fish_log=LOG_HOSTNAME_OSTREE_ADMIN,
            layout="ostree/deploy/default/deploy/abcdef.0/etc",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        etc = self.deployed_etc()
        self.assertIsNotNone(etc, "classic ostree layout was not discovered")
        self.assertEqual((etc / "hostname").read_text().strip(), "classic-box")

    def test_missing_deployment_etc_warns_without_failing(self):
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_COMPOSEFS, layout=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("deployment etc/ not found", proc.stdout)

    def test_rechunker_override_breaks_the_ordering_cycle(self):
        """DefaultDependencies=no is what stops the boot-time deadlock."""
        recipe = self._write_recipe(hostname="dakota")
        proc = self.run_script(recipe, fish_rc=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        override = (self.deployed_etc()
                    / "systemd/system/rechunker-group-fix.service.d/override.conf")
        self.assertTrue(override.exists(), "rechunker override was not written")
        self.assertIn("[Unit]", override.read_text())
        self.assertIn("DefaultDependencies=no", override.read_text())

    def test_hostname_with_surrounding_recipe_keys_is_extracted_exactly(self):
        """The grep-based extraction must not pick up a neighbouring value."""
        recipe = self.sandbox / "recipe.json"
        recipe.write_text(json.dumps({
            "target": "/dev/vda",
            "hostname": "dakota-e2e",
            "username": "not-the-hostname",
        }))
        proc = self.run_script(recipe, fish_rc=1,
                               fish_log=LOG_HOSTNAME_OSTREE_ADMIN)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.deployed_etc() / "hostname").read_text().strip(),
                         "dakota-e2e")


if __name__ == "__main__":
    unittest.main()
