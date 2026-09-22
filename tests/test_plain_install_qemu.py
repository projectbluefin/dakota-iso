"""Executed black-box coverage for scripts/plain-install-qemu.sh.

scripts/plain-install-qemu.sh drives the plain (no-encryption) install lane of
the QEMU E2E matrix (.github/workflows/test-plain-install.yml, `just
test-plain-install`). Every decision it makes is pure shell that runs on the
host before anything reaches the VM, yet none of it had a test:

* argument-count validation,
* offline vs. network image selection from `podman image exists`,
* live_target -> bootloader-variant derivation (``-nvidia``/``-nvidia-open``
  suffix stripping) and the ``grub`` -> ``grub2`` rename,
* composefs vs. bootcDirect recipe shape (``image`` vs. ``targetImgref``,
  ``composeFsBackend``), and the constant recipe fields the installer
  contract depends on (disk, filesystem, hostname, ``encryption.type``),
* defaults when a variant ships no ``composefs``/``bootloader`` file,
* failure diagnostics + non-zero exit on the bootcDirect path,
* BLS serial-console patching and the socat powerdown handshake.

The script is executed for real. ``sshpass`` (which fronts both ``ssh`` and
``scp``), ``socat`` and ``go`` are replaced by recording stubs on PATH, so the
tests observe exactly the commands and the recipe JSON the script would have
sent to the VM.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "plain-install-qemu.sh"

SSHPASS_STUB = r"""#!/usr/bin/env bash
# Stub for sshpass: `sshpass -p live <ssh|scp> ...`
shift 2
prog="$1"
shift
printf '%s %s\n' "$prog" "$*" >> "$STUB_LOG"

if [[ "$prog" == "scp" ]]; then
    # Last arg is liveuser@host:/remote/path, second to last is the local file.
    remote="${!#}"
    local_file=""
    n=0
    for a in "$@"; do n=$((n+1)); done
    i=0
    for a in "$@"; do
        i=$((i+1))
        [[ $i -eq $((n-1)) ]] && local_file="$a"
    done
    cp "$local_file" "$STUB_CAPTURE/$(basename "${remote##*:}")"
    exit 0
fi

remote_cmd="${!#}"
# Real ssh consumes stdin; drain it so the script's `printf ... | $SSH ...`
# pipelines never die on SIGPIPE under `set -o pipefail`.
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
# `go build -o <path> ./cmd/fisherman/` — materialize the output binary.
prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && printf 'fake-fisherman\n' > "$a"
    prev="$a"
done
exit 0
"""


class PlainInstallHarness(unittest.TestCase):
    """Run plain-install-qemu.sh against a synthetic repo tree and stub PATH."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-plain-install-"))
        self.workdir = self.tmpdir / "repo"
        (self.workdir / "scripts").mkdir(parents=True)
        (self.workdir / "live" / "src").mkdir(parents=True)
        shutil.copy(SCRIPT, self.workdir / "scripts" / "plain-install-qemu.sh")
        # The script scp's this into the VM; only its existence matters here.
        (self.workdir / "scripts" / "fisherman-install.sh").write_text("#!/usr/bin/bash\n")

        self.bindir = self.tmpdir / "bin"
        self.bindir.mkdir()
        for name, body in (
            ("sshpass", SSHPASS_STUB),
            ("socat", SOCAT_STUB),
            ("go", GO_STUB),
        ):
            path = self.bindir / name
            path.write_text(body)
            path.chmod(0o755)

        self.log = self.tmpdir / "stub.log"
        self.log.write_text("")
        self.capture = self.tmpdir / "capture"
        self.capture.mkdir()

        # A regular writable file, so the script takes the unprivileged socat
        # path and the "monitor socket is gone" wait loop exits immediately.
        self.monitor = self.tmpdir / "monitor.sock"
        self.monitor.write_text("")

        self.fisher_repo = self.tmpdir / "fisherman"
        self.fisher_repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

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

    def run_script(self, target, image_exists=False, fisherman_rc=0, args=None):
        env = {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_CAPTURE": str(self.capture),
            "STUB_IMAGE_EXISTS": "0" if image_exists else "1",
            "STUB_FISHERMAN_RC": str(fisherman_rc),
        }
        if args is None:
            args = [str(target), "2222", str(self.monitor), str(self.fisher_repo)]
        return subprocess.run(
            ["bash", "scripts/plain-install-qemu.sh", *args],
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
        path = self.capture / "plain-recipe.json"
        self.assertTrue(path.exists(), f"no recipe uploaded; stub log:\n{self.stub_log()}")
        return json.loads(path.read_text())

    def assert_recipe_contract(self, recipe):
        """Constants fisherman must receive on BOTH recipe templates.

        plain-install-qemu.sh emits two separate printf templates — composefs
        (line 56) and bootcDirect (line 68). Asserting these only on one left
        the other free to drift; a bootcDirect recipe asking for LUKS passed
        the whole suite, in a lane whose entire identity is "no encryption".
        """
        self.assertEqual(recipe["disk"], "/dev/vda")
        self.assertEqual(recipe["filesystem"], "btrfs")
        self.assertEqual(recipe["hostname"], "dakota-plain-test")
        self.assertEqual(recipe["encryption"], {"type": "none"})
        self.assertEqual(recipe["flatpaks"], [])


class TestArgumentValidation(PlainInstallHarness):
    def test_too_few_arguments_exits_1_with_usage(self):
        result = self.run_script(None, args=["target", "2222", "/tmp/mon"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage:", result.stderr)

    def test_no_arguments_exits_1(self):
        result = self.run_script(None, args=[])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage:", result.stderr)


class TestComposefsRecipe(PlainInstallHarness):
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
        self.assert_recipe_contract(recipe)
        self.assertNotIn("targetImgref", recipe)

    def test_composefs_path_does_not_build_fisherman(self):
        """The podman path uses the shipped fisherman — no local go build."""
        self.run_script(self.target, image_exists=True)
        self.assertNotIn("go build", self.stub_log())

    def test_scratch_disk_is_mounted_over_var_tmp(self):
        self.run_script(self.target, image_exists=True)
        log = self.stub_log()
        self.assertIn("mkfs.ext4 -F /dev/vdb", log)
        self.assertIn("mount /dev/vdb /var/tmp", log)

    def test_installer_and_recipe_are_uploaded(self):
        self.run_script(self.target, image_exists=True)
        self.assertTrue((self.capture / "plain-recipe.json").exists())
        self.assertTrue((self.capture / "fisherman-install.sh").exists())

    def test_bls_entries_are_patched_for_serial_console(self):
        self.run_script(self.target, image_exists=True)
        log = self.stub_log()
        self.assertIn("console=ttyS0", log)
        self.assertIn("loader/entries", log)

    def test_powerdown_is_sent_to_the_qemu_monitor(self):
        result = self.run_script(self.target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.stub_log()
        self.assertIn(f"socat - UNIX-CONNECT:{self.monitor}", log)
        self.assertIn("system_powerdown", log)


class TestBootloaderDerivation(PlainInstallHarness):
    def test_grub_is_renamed_to_grub2(self):
        self.make_variant("stable", composefs="true", bootloader="grub")
        target = self.make_target("ghcr.io/example/stable:latest", "stable")
        self.run_script(target, image_exists=True)
        self.assertEqual(self.recipe()["bootloader"], "grub2")

    def test_systemd_bootloader_is_passed_through(self):
        self.make_variant("dakota", composefs="true", bootloader="systemd")
        target = self.make_target("ghcr.io/example/dakota:latest", "dakota")
        self.run_script(target, image_exists=True)
        self.assertEqual(self.recipe()["bootloader"], "systemd")

    def test_nvidia_suffix_is_stripped_from_live_target(self):
        """bluefin-nvidia reads live/src/bluefin/, which has no sibling dir."""
        self.make_variant("bluefin", composefs="true", bootloader="grub")
        target = self.make_target("ghcr.io/example/bluefin:latest", "bluefin-nvidia")
        self.run_script(target, image_exists=True)
        self.assertEqual(self.recipe()["bootloader"], "grub2")

    def test_nvidia_open_suffix_is_stripped_from_live_target(self):
        self.make_variant("bluefin", composefs="true", bootloader="grub")
        target = self.make_target("ghcr.io/example/bluefin:latest", "bluefin-nvidia-open")
        self.run_script(target, image_exists=True)
        self.assertEqual(self.recipe()["bootloader"], "grub2")

    def test_missing_variant_config_falls_back_to_composefs_and_systemd(self):
        """No live/src/<variant>/ at all: composefs=true, bootloader=systemd."""
        target = self.make_target("ghcr.io/example/unknown:latest", "unknown")
        result = self.run_script(target, image_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        recipe = self.recipe()
        self.assertTrue(recipe["composeFsBackend"])
        self.assertEqual(recipe["bootloader"], "systemd")


class TestBootcDirectRecipe(PlainInstallHarness):
    """composefs=false variants (stable, lts) install via fisherman bootcDirect."""

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
        """Same installer contract as the composefs recipe — separate template."""
        self.run_script(self.target, image_exists=True)
        recipe = self.recipe()
        self.assertFalse(recipe["composeFsBackend"])
        self.assert_recipe_contract(recipe)

    def test_target_imgref_is_the_bare_ref_not_a_transport_url(self):
        """targetImgref is a day-2 rebase ref: no containers-storage:/docker:// prefix."""
        self.run_script(self.target, image_exists=False)
        target_imgref = self.recipe()["targetImgref"]
        self.assertNotIn("containers-storage:", target_imgref)
        self.assertNotIn("docker://", target_imgref)

    def test_patched_fisherman_is_built_and_uploaded(self):
        self.run_script(self.target, image_exists=True)
        log = self.stub_log()
        self.assertIn("go build", log)
        self.assertIn("./cmd/fisherman/", log)
        self.assertTrue((self.capture / "fisherman").exists())

    def test_fisherman_bin_override_is_exported_to_the_installer(self):
        self.run_script(self.target, image_exists=True)
        self.assertIn("FISHERMAN_BIN=/tmp/fisherman", self.stub_log())

    def test_install_failure_exits_1_and_dumps_diagnostics(self):
        result = self.run_script(self.target, image_exists=True, fisherman_rc=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("INSTALL FAILURE DIAGNOSTICS", result.stdout)
        log = self.stub_log()
        self.assertIn("dmesg", log)
        self.assertIn("journalctl", log)

    def test_install_failure_skips_bls_patch_and_powerdown(self):
        self.run_script(self.target, image_exists=True, fisherman_rc=1)
        log = self.stub_log()
        self.assertNotIn("console=ttyS0", log)
        self.assertNotIn("system_powerdown", log)
        self.assertNotIn("socat", log)


if __name__ == "__main__":
    unittest.main()
