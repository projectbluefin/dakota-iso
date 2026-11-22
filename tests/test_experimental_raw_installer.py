"""Contract tests for the opt-in gnome-setup raw-medium experiment."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).parent.parent
TRACK = REPO / "image-installer"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ExperimentalRawInstallerContractTest(unittest.TestCase):
    def test_bootc_loop_inventory_uses_gpt_only_for_missing_types(self):
        module = load_module("lsblk_bootc", TRACK / "lsblk-bootc.py")
        disk = "/dev/loop5"
        device = {"path": disk, "children": [
            {"path": disk + "p2", "type": "part", "parttype": "reported-esp"},
            {"path": disk + "p3", "type": "part", "parttype": None},
        ]}
        table = {"label": "gpt", "device": disk, "partitions": [
            {"node": disk + "p3", "type": "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"},
            {"node": disk + "p2", "type": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"},
        ]}
        with self.assertRaisesRegex(ValueError, "requested loop device"):
            module.complete_partition_types(device, dict(table, device="/dev/loop6"))
        module.complete_partition_types(device, table)
        self.assertEqual(device["children"][0]["parttype"], "reported-esp")
        self.assertEqual(device["children"][1]["parttype"], "4f68bce3-e8cd-4db1-96e7-fbcaf984b709")

    def test_builder_inputs_are_commit_pinned_and_self_checking(self):
        text = (TRACK / "Containerfile").read_text()
        for ref in ("SYSTEMD_REF", "GNOME_SETUP_REF"):
            self.assertIn(f"ARG {ref}=", text)
            self.assertIn(f'test "$(git -C /src rev-parse HEAD)" = "${{{ref}}}"', text)
        self.assertIn("COPY --from=sysinstall /out/usr/ /usr/", text)
        self.assertIn("COPY --from=gnome-setup /out/usr/ /usr/", text)

    def test_live_services_are_scoped_to_opt_in_kernel_argument(self):
        units = TRACK / "src" / "usr" / "lib" / "systemd" / "system"
        for name in (
            "dakota-sysinstall.socket",
            "dakota-cryptenroll.socket",
            "dakota-live-setup.service",
            "dakota-live-ready.service",
        ):
            text = (units / name).read_text()
            self.assertIn("ConditionKernelCommandLine=dakota.live.installer=gnome-setup", text)
        self.assertTrue((units / "dakota-sysinstall-post@.service").exists())
        pages = (TRACK / "src" / "usr" / "share" / "dakota" / "gnome-setup" / "pages.conf").read_text()
        self.assertIn("post-install-unit=dakota-sysinstall-post@.service", pages)

    def test_live_setup_creates_liveuser_and_gdm_autologin(self):
        setup_script = (TRACK / "src" / "usr" / "libexec" / "dakota-live-setup").read_text()
        self.assertIn("useradd", setup_script)
        self.assertIn("liveuser", setup_script)
        self.assertIn("AutomaticLoginEnable", setup_script)
        self.assertIn("AutomaticLogin", setup_script)

    def test_prepare_medium_adds_live_entry_without_mutating_installed_entry(self):
        module = load_module("prepare_medium", TRACK / "prepare-medium.py")
        with tempfile.TemporaryDirectory() as tmp:
            esp = Path(tmp)
            (esp / "loader" / "entries").mkdir(parents=True)
            installed = ("title Dakota\nlinux /vmlinuz\ninitrd /initramfs.img\n"
                         "options root=gpt-auto rw quiet composefs.digest=v1-sha512-12:example lockdown=integrity\n")
            (esp / "loader" / "entries" / "dakota.conf").write_text(installed)
            module.prepare_esp(esp, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "dakota-live-initrd.img")

            self.assertEqual((esp / "loader" / "entries" / "dakota.conf").read_text(), installed)
            live = (esp / "loader" / "entries" / "dakota-live.conf").read_text()
            self.assertIn("root=UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", live)
            self.assertIn("dakota.live", live)
            self.assertIn("dakota.live.installer=gnome-setup", live)
            self.assertIn("systemd.volatile=state", live)
            options = next(line.split()[1:] for line in live.splitlines() if line.startswith("options "))
            self.assertIn("composefs.digest=v1-sha512-12:example", options)
            self.assertIn("lockdown=integrity", options)
            self.assertNotIn("root=gpt-auto", options)
            self.assertNotIn("rw", options)
            self.assertIn("initrd /dakota-live-initrd.img", live)
            self.assertEqual((esp / "loader" / "loader.conf").read_text(), "timeout 5\ndefault dakota-live.conf\n")

    def test_setup_root_conf_is_in_usr_lib_composefs(self):
        build_script = (TRACK / "build-raw-image.sh").read_text()
        self.assertIn("usr/lib/composefs", build_script)
        self.assertNotIn("usr/lib/bootc/setup-root-conf.toml", build_script)

    def test_repart_definitions_copy_esp_and_encrypt_root(self):
        module = load_module("live_definitions", TRACK / "src/usr/libexec/dakota-live-definitions.py")
        table = {
            "partitiontable": {
                "sectorsize": 512,
                "partitions": [
                    {"node": "/dev/loop0p1", "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "uuid": "ESP-UUID", "size": 2048, "name": "ESP"},
                    {"node": "/dev/loop0p2", "type": "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709", "uuid": "ROOT-UUID", "size": 4096, "name": "root"},
                ],
            }
        }
        rendered = module.render_definitions(table, "/dev/loop0p2", tpm2=True)
        self.assertTrue(rendered["01-copy.conf"].endswith("\n"))
        self.assertIn("CopyBlocks=/dev/disk/by-partuuid/esp-uuid", rendered["01-copy.conf"])
        self.assertIn("SizeMaxBytes=1048576", rendered["01-copy.conf"])
        self.assertIn("Encrypt=key-file+tpm2", rendered["02-copy.conf"])
        self.assertIn("TPM2PCRs=7", rendered["02-copy.conf"])
        with self.assertRaisesRegex(ValueError, "one distinct root"):
            module.render_definitions(table, "/dev/unknown", tpm2=True)

    def test_copied_disk_stages_boot_before_publishing_an_independent_entry(self):
        module = load_module("finish_boot", TRACK / "src/usr/libexec/dakota-finish-boot.py")
        with tempfile.TemporaryDirectory() as tmp:
            esp, root = Path(tmp) / "esp", Path(tmp) / "root"
            entries = esp / "loader/entries"
            entries.mkdir(parents=True)
            (entries / "dakota-live.conf").write_text("title Dakota Live\n")
            (esp / "dakota-live-initrd.img").touch()
            entry = entries / "bootc.conf"
            entry.write_text("title Dakota\noptions rw root=gpt-auto composefs.digest=v1-sha512-12:example\n")
            loader = esp / "loader/loader.conf"
            loader.write_text("timeout 5\ndefault dakota-live.conf\n")
            etc = root / "state/deploy/one/etc"
            etc.mkdir(parents=True)
            (etc / "fstab").write_text("UUID=ABCD-1234 /boot vfat defaults 0 2\n")

            module.finish(esp, root, "luks-id", "discard", ("old-root", "new-root", "abcd-1234", "EEFF-6789"))
            self.assertEqual(list(entries.iterdir()), [entries / "bootc.conf.pending"])
            self.assertFalse((esp / "dakota-live-initrd.img").exists())

            module.finish(esp, root, "luks-id", "discard", ("old-root", "new-root", "abcd-1234", "EEFF-6789"))
            module.publish(esp)
            module.publish(esp)

            self.assertEqual(list(entries.iterdir()), [entry])
            self.assertFalse((entries / "bootc.conf.pending").exists())
            self.assertEqual(loader.read_text(), "timeout 5\ndefault bootc.conf\n")
            self.assertIn("composefs.digest=v1-sha512-12:example root=UUID=new-root rd.luks.uuid=luks-id", entry.read_text())
            self.assertNotIn("root=gpt-auto", entry.read_text())
            self.assertEqual((etc / "fstab").read_text(), "UUID=EEFF-6789 /boot vfat defaults 0 2\n")
            self.assertEqual((etc / "crypttab").read_text(), "luks-luks-id UUID=luks-id none discard\n")

    def test_new_track_scripts_are_valid_shell(self):
        for path in (
            TRACK / "build-raw-image.sh",
            TRACK / "src" / "usr" / "libexec" / "dakota-live-setup",
            TRACK / "src" / "usr" / "libexec" / "dakota-live-definitions",
            TRACK / "src" / "usr" / "libexec" / "dakota-sysinstall-post",
        ):
            result = subprocess.run(["bash", "-n", path], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_opt_in_recipe_exists_without_replacing_release_iso_recipe(self):
        text = (REPO / "justfile").read_text()
        self.assertIn("experimental-raw-medium", text)
        self.assertIn("image-installer/Containerfile", text)
        self.assertIn("image-installer/build-raw-image.sh", text)
        self.assertIn("iso-sd-boot target:", text)


if __name__ == "__main__":
    unittest.main()
