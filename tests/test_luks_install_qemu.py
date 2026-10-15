"""Executed black-box coverage for scripts/luks-install-qemu.sh.

scripts/luks-install-qemu.sh drives the LUKS install lane of the QEMU E2E
matrix (.github/workflows/test-luks-install.yml, `just luks-test-qemu`). It is
the encrypted twin of scripts/plain-install-qemu.sh, which has executed
coverage in tests/test_plain_install_qemu.py; this script had none. Its
LUKS-specific behaviour is:

* a five-argument contract (passphrase between target and SSH port),
* the ``luks-passphrase`` encryption block in BOTH recipe templates
  (composefs and bootcDirect),
* the remote BLS patch that appends ``rd.luks.name=<UUID>=root`` so the
  installed disk can be unlocked, with a 2- vs 3-partition layout switch and
  a fallback when ``cryptsetup luksUUID`` fails,
* an extra ``systemd-oomd`` diagnostic on bootcDirect install failure.

The script is executed for real. ``sshpass`` (fronting ``ssh``/``scp``),
``socat`` and ``go`` are recording stubs on PATH. For the BLS tests the stub
additionally runs the remote command locally, with ``sudo``, ``mount``,
``umount``, ``ls`` and ``cryptsetup`` stubbed so the patch edits fixture
loader entries instead of a real boot partition.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "luks-install-qemu.sh"

PASSPHRASE = "test-passphrase"
LUKS_UUID = "0f6c3a1e-5b2d-4c7e-9a8f-1d2e3f4a5b6c"

SSHPASS_STUB = r"""#!/usr/bin/env bash
# Stub for sshpass: `sshpass -p live <ssh|scp> ...`
shift 2
prog="$1"
shift
printf '%s %s\n' "$prog" "$*" >> "$STUB_LOG"

if [[ "$prog" == "scp" ]]; then
    remote="${!#}"
    local_file="${@:$#-1:1}"
    cp "$local_file" "$STUB_CAPTURE/$(basename "${remote##*:}")"
    exit 0
fi

remote_cmd="${!#}"
if [[ "$remote_cmd" == *"loader/entries"* && -n "${STUB_EXEC_BLS:-}" ]]; then
    # Emulate the remote login shell: it receives the command string and
    # evaluates it. sudo/mount/ls/cryptsetup are stubbed on PATH.
    exec bash -c "$remote_cmd"
fi
# Real ssh consumes stdin; drain it so `printf ... | $SSH ...` pipelines
# never die on SIGPIPE under `set -o pipefail`.
cat > /dev/null 2>&1 || true
if [[ "$remote_cmd" == *"podman image exists"* ]]; then
    exit "${STUB_IMAGE_EXISTS:-1}"
fi
if [[ "$remote_cmd" == *"fisherman-install.sh"* ]]; then
    exit "${STUB_FISHERMAN_RC:-0}"
fi
exit 0
"""

SOCAT_STUB = r"""#!/usr/bin/env bash
payload="$(cat)"
printf 'socat %s <<< %s\n' "$*" "$payload" >> "$STUB_LOG"
exit 0
"""

GO_STUB = r"""#!/usr/bin/env bash
printf 'go %s\n' "$*" >> "$STUB_LOG"
prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && printf 'fake-fisherman\n' > "$a"
    prev="$a"
done
exit 0
"""

# --- stubs used only when the remote BLS patch is executed locally --------

SUDO_STUB = r"""#!/usr/bin/env bash
# `sudo -S -p "" bash -c "..."`: drop sudo's own options, run the rest.
cat > /dev/null 2>&1 || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        -S) shift ;;
        -p) shift 2 ;;
        *) break ;;
    esac
done
exec "$@"
"""

MOUNT_STUB = r"""#!/usr/bin/env bash
# mount <dev> <dir>: populate <dir> with the fixture boot partition.
printf 'mount %s\n' "$*" >> "$STUB_LOG"
cp -a "$STUB_BOOT/." "$2/"
printf '%s\n' "$2" > "$STUB_BOOT.mountpoint"
"""

UMOUNT_STUB = r"""#!/usr/bin/env bash
# umount <dir>: write the (possibly patched) entries back to the fixture.
printf 'umount %s\n' "$*" >> "$STUB_LOG"
rm -rf "$STUB_BOOT"
mkdir -p "$STUB_BOOT"
cp -a "$1/." "$STUB_BOOT/"
find "$1" -mindepth 1 -delete
"""

LS_STUB = r"""#!/usr/bin/env bash
# Only `ls /dev/vda3` is asked; answer from STUB_PARTITIONS.
if [[ "$1" == "/dev/vda3" ]]; then
    [[ "${STUB_PARTITIONS:-2}" == "3" ]] && exit 0
    exit 2
fi
exec /bin/ls "$@"
"""

CRYPTSETUP_STUB = r"""#!/usr/bin/env bash
printf 'cryptsetup %s\n' "$*" >> "$STUB_LOG"
if [[ "$1" == "luksUUID" && -n "${STUB_LUKS_UUID:-}" ]]; then
    printf '%s\n' "$STUB_LUKS_UUID"
    exit 0
fi
exit 1
"""


class LuksInstallHarness(unittest.TestCase):
    """Run luks-install-qemu.sh against a synthetic repo tree and stub PATH."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-luks-install-"))
        self.workdir = self.tmpdir / "repo"
        (self.workdir / "scripts").mkdir(parents=True)
        (self.workdir / "live" / "src").mkdir(parents=True)
        shutil.copy(SCRIPT, self.workdir / "scripts" / "luks-install-qemu.sh")
        (self.workdir / "scripts" / "fisherman-install.sh").write_text("#!/usr/bin/bash\n")
        shutil.copy(REPO / "scripts" / "variant-config.sh", self.workdir / "scripts" / "variant-config.sh")

        self.bindir = self.tmpdir / "bin"
        self.bindir.mkdir()
        self._write_stubs(
            ("sshpass", SSHPASS_STUB),
            ("socat", SOCAT_STUB),
            ("go", GO_STUB),
        )

        self.log = self.tmpdir / "stub.log"
        self.log.write_text("")
        self.capture = self.tmpdir / "capture"
        self.capture.mkdir()

        # Regular writable file: unprivileged socat path, wait loop exits at once.
        self.monitor = self.tmpdir / "monitor.sock"
        self.monitor.write_text("")

        self.fisher_repo = self.tmpdir / "fisherman"
        self.fisher_repo.mkdir()

        self.boot = self.tmpdir / "boot"
        self.extra_env = {}

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _write_stubs(self, *stubs):
        for name, body in stubs:
            path = self.bindir / name
            path.write_text(body)
            path.chmod(0o755)

    def make_target(self, payload_ref, live_target=None):
        target = self.tmpdir / "target"
        target.mkdir(exist_ok=True)
        (target / "payload_ref").write_text(payload_ref + "\n")
        if live_target is not None:
            (target / "live_target").write_text(live_target + "\n")
        return target

    def make_variant(self, name, composefs=None, bootloader=None):
        variant = self.workdir / "live" / "src" / name
        variant.mkdir(parents=True, exist_ok=True)
        if composefs is not None:
            (variant / "composefs").write_text(composefs + "\n")
        if bootloader is not None:
            (variant / "bootloader").write_text(bootloader + "\n")
        return variant

    def enable_bls_execution(self, entries, luks_uuid=LUKS_UUID, partitions=2):
        """Run the remote BLS patch locally against fixture loader entries.

        ``entries`` maps a path relative to the boot partition root
        (e.g. ``loader/entries/a.conf``) to its file content.
        """
        self._write_stubs(
            ("sudo", SUDO_STUB),
            ("mount", MOUNT_STUB),
            ("umount", UMOUNT_STUB),
            ("ls", LS_STUB),
            ("cryptsetup", CRYPTSETUP_STUB),
        )
        for rel, content in entries.items():
            path = self.boot / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.extra_env.update(
            {
                "STUB_EXEC_BLS": "1",
                "STUB_BOOT": str(self.boot),
                "STUB_LUKS_UUID": luks_uuid or "",
                "STUB_PARTITIONS": str(partitions),
            }
        )

    def run_script(self, target, image_exists=False, fisherman_rc=0, args=None):
        env = {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_CAPTURE": str(self.capture),
            "STUB_IMAGE_EXISTS": "0" if image_exists else "1",
            "STUB_FISHERMAN_RC": str(fisherman_rc),
            **self.extra_env,
        }
        if args is None:
            args = [str(target), PASSPHRASE, "2222", str(self.monitor), str(self.fisher_repo)]
        return subprocess.run(
            ["bash", "scripts/luks-install-qemu.sh", *args],
            cwd=self.workdir,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def stub_log(self):
        return self.log.read_text()

    def recipe(self):
        path = self.capture / "luks-recipe.json"
        self.assertTrue(path.exists(), f"no recipe uploaded; stub log:\n{self.stub_log()}")
        return json.loads(path.read_text())

    def assert_recipe_contract(self, recipe):
        """Constants fisherman must receive on BOTH recipe templates.

        The composefs and bootcDirect recipes are separate printf templates;
        asserting the encryption block on only one would let the other drift
        to an unencrypted install in the lane whose purpose is LUKS.
        """
        self.assertEqual(recipe["disk"], "/dev/vda")
        self.assertEqual(recipe["filesystem"], "btrfs")
        self.assertEqual(recipe["hostname"], "dakota-luks-test")
        self.assertEqual(
            recipe["encryption"], {"type": "luks-passphrase", "passphrase": PASSPHRASE}
        )
        self.assertEqual(recipe["flatpaks"], [])


class TestArgumentValidation(LuksInstallHarness):
    def test_four_arguments_is_one_short_and_exits_1(self):
        """The plain lane's 4-argument call shape must not be accepted here."""
        result = self.run_script(
            None, args=["target", "2222", str(self.monitor), str(self.fisher_repo)]
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage:", result.stderr)
        self.assertIn("<luks_passphrase>", result.stderr)
        self.assertEqual(self.stub_log(), "", "nothing may reach the VM on a usage error")

    def test_no_arguments_exits_1(self):
        result = self.run_script(None, args=[])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage:", result.stderr)


class TestComposefsRecipe(LuksInstallHarness):
    """composefs=true variants (dakota) install via podman/containers-storage."""

    def setUp(self):
        super().setUp()
        self.make_variant("dakota", composefs="true", bootloader="systemd")
        self.target = self.make_target("ghcr.io/example/dakota:latest", "dakota")

    def test_cached_image_uses_containers_storage_offline(self):
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using offline install", result.stdout)
        self.assertEqual(
            self.recipe()["image"], "containers-storage:ghcr.io/example/dakota:latest"
        )

    def test_uncached_image_falls_back_to_docker_transport(self):
        result = self.run_script(self.target, image_exists=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fisherman will pull from network", result.stdout)
        self.assertEqual(self.recipe()["image"], "docker://ghcr.io/example/dakota:latest")

    def test_recipe_contract_fields(self):
        self.run_script(self.target, image_exists=True)
        recipe = self.recipe()
        self.assertTrue(recipe["composeFsBackend"])
        self.assertEqual(recipe["bootloader"], "systemd")
        self.assert_recipe_contract(recipe)
        self.assertNotIn("targetImgref", recipe)

    def test_passphrase_argument_reaches_the_recipe(self):
        args = [str(self.target), "other-secret", "2222", str(self.monitor), str(self.fisher_repo)]
        result = self.run_script(self.target, image_exists=True, args=args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recipe()["encryption"]["passphrase"], "other-secret")

    def test_composefs_path_does_not_build_fisherman(self):
        self.run_script(self.target, image_exists=True)
        self.assertNotIn("go build", self.stub_log())
        self.assertNotIn("FISHERMAN_BIN", self.stub_log())

    def test_installer_runs_the_uploaded_recipe(self):
        self.run_script(self.target, image_exists=True)
        self.assertTrue((self.capture / "fisherman-install.sh").exists())
        self.assertIn("bash /tmp/fisherman-install.sh /tmp/luks-recipe.json", self.stub_log())

    def test_scratch_disk_is_mounted_over_var_tmp(self):
        self.run_script(self.target, image_exists=True)
        log = self.stub_log()
        self.assertIn("mkfs.ext4 -F /dev/vdb", log)
        self.assertIn("mount /dev/vdb /var/tmp", log)

    def test_powerdown_is_sent_to_the_qemu_monitor(self):
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.stub_log()
        self.assertIn(f"socat - UNIX-CONNECT:{self.monitor}", log)
        self.assertIn("system_powerdown", log)


class TestBootcDirectRecipe(LuksInstallHarness):
    """composefs=false variants (stable, lts) install via fisherman bootcDirect.

    These legs are dormant in test-luks-install.yml (dakota-only matrix), so
    nothing else exercises this branch.
    """

    def setUp(self):
        super().setUp()
        self.make_variant("lts", composefs="false", bootloader="grub")
        self.target = self.make_target("ghcr.io/example/lts:latest", "lts")

    def test_empty_image_and_target_imgref_trigger_bootc_direct(self):
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        recipe = self.recipe()
        self.assertEqual(recipe["image"], "")
        self.assertEqual(recipe["targetImgref"], "ghcr.io/example/lts:latest")
        self.assertFalse(recipe["composeFsBackend"])

    def test_recipe_contract_fields(self):
        self.run_script(self.target, image_exists=True)
        recipe = self.recipe()
        self.assertEqual(recipe["bootloader"], "grub2")
        self.assert_recipe_contract(recipe)

    def test_patched_fisherman_is_built_and_used(self):
        self.run_script(self.target, image_exists=True)
        log = self.stub_log()
        self.assertIn("./cmd/fisherman/", log)
        self.assertTrue((self.capture / "fisherman").exists())
        self.assertIn("FISHERMAN_BIN=/tmp/fisherman", log)

    def test_install_failure_exits_1_and_dumps_diagnostics(self):
        result = self.run_script(self.target, image_exists=True, fisherman_rc=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("INSTALL FAILURE DIAGNOSTICS", result.stdout)
        log = self.stub_log()
        self.assertIn("dmesg", log)
        self.assertIn("journalctl", log)
        self.assertIn("systemctl status systemd-oomd", log)

    def test_install_failure_skips_bls_patch_and_powerdown(self):
        self.run_script(self.target, image_exists=True, fisherman_rc=1)
        log = self.stub_log()
        self.assertNotIn("rd.luks.name", log)
        self.assertNotIn("socat", log)


class TestBlsLuksPatch(LuksInstallHarness):
    """The remote BLS patch, executed locally against fixture loader entries."""

    ENTRY = "title Dakota\nlinux /vmlinuz\noptions root=UUID=abc rw quiet\n"

    def setUp(self):
        super().setUp()
        self.make_variant("dakota", composefs="true", bootloader="systemd")
        self.target = self.make_target("ghcr.io/example/dakota:latest", "dakota")

    def options_line(self, rel):
        for line in (self.boot / rel).read_text().splitlines():
            if line.startswith("options "):
                return line
        self.fail(f"no options line in {rel}")

    def test_luks_uuid_and_serial_console_are_appended(self):
        self.enable_bls_execution({"loader/entries/a.conf": self.ENTRY})
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.options_line("loader/entries/a.conf"),
            "options root=UUID=abc rw quiet console=tty0 console=ttyS0 "
            f"rd.luks.name={LUKS_UUID}=root",
        )
        self.assertIn("BLS patch: 1 entries updated", result.stdout)

    def test_two_partition_layout_reads_vda1_and_vda2(self):
        self.enable_bls_execution({"loader/entries/a.conf": self.ENTRY}, partitions=2)
        self.run_script(self.target, image_exists=True)
        log = self.stub_log()
        self.assertIn("cryptsetup luksUUID /dev/vda2", log)
        self.assertIn("mount /dev/vda1 ", log)

    def test_three_partition_layout_shifts_to_vda2_and_vda3(self):
        """GRUB layouts add a separate boot partition."""
        self.enable_bls_execution({"loader/entries/a.conf": self.ENTRY}, partitions=3)
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Detected 3 partitions layout", result.stdout)
        log = self.stub_log()
        self.assertIn("cryptsetup luksUUID /dev/vda3", log)
        self.assertIn("mount /dev/vda2 ", log)

    def test_both_entry_directories_are_patched(self):
        self.enable_bls_execution(
            {
                "loader/entries/a.conf": self.ENTRY,
                "EFI/loader/entries/b.conf": self.ENTRY,
            }
        )
        result = self.run_script(self.target, image_exists=True)
        self.assertIn("BLS patch: 2 entries updated", result.stdout)
        for rel in ("loader/entries/a.conf", "EFI/loader/entries/b.conf"):
            self.assertIn(f"rd.luks.name={LUKS_UUID}=root", self.options_line(rel))

    def test_already_patched_entry_is_left_alone(self):
        patched = "options root=UUID=abc console=tty0 console=ttyS0\n"
        self.enable_bls_execution({"loader/entries/a.conf": patched})
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.boot / "loader/entries/a.conf").read_text(), patched)
        self.assertIn("BLS patch: 0 entries updated", result.stdout)

    def test_missing_luks_uuid_still_adds_serial_console(self):
        """cryptsetup failing must not abort the patch or emit an empty rd.luks.name."""
        self.enable_bls_execution({"loader/entries/a.conf": self.ENTRY}, luks_uuid=None)
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        options = self.options_line("loader/entries/a.conf")
        self.assertTrue(options.endswith("console=tty0 console=ttyS0"), options)
        self.assertNotIn("rd.luks.name", options)


if __name__ == "__main__":
    unittest.main()
