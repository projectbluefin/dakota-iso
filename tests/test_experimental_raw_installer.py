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
        self.assertIn("zz-dakota-live", setup_script)

    def test_prepare_medium_adds_live_entry_without_mutating_installed_entry(self):
        module = load_module("prepare_medium", TRACK / "prepare-medium.py")
        with tempfile.TemporaryDirectory() as tmp:
            esp = Path(tmp)
            (esp / "loader" / "entries").mkdir(parents=True)
            installed = "title Dakota\nlinux /vmlinuz\ninitrd /initramfs.img\noptions root=gpt-auto rw quiet\n"
            (esp / "loader" / "entries" / "dakota.conf").write_text(installed)
            module.prepare_esp(esp, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "dakota-live-initrd.img")

            self.assertEqual((esp / "loader" / "entries" / "dakota.conf").read_text(), installed)
            live = (esp / "loader" / "entries" / "dakota-live.conf").read_text()
            self.assertIn("root=UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", live)
            self.assertIn("dakota.live", live)
            self.assertIn("dakota.live.installer=gnome-setup", live)
            self.assertIn("systemd.volatile=state", live)
            self.assertIn("initrd /dakota-live-initrd.img", live)
            self.assertEqual((esp / "loader" / "loader.conf").read_text(), "timeout 5\ndefault dakota-live.conf\n")
    def test_setup_root_conf_is_in_usr_lib_composefs(self):
        build_script = (TRACK / "build-raw-image.sh").read_text()
        self.assertIn("usr/lib/composefs", build_script)
        self.assertNotIn("usr/lib/bootc/setup-root-conf.toml", build_script)

    def test_build_raw_image_canonicalizes_out_dir(self):
        build_script = (TRACK / "build-raw-image.sh").read_text()
        self.assertIn('OUT_DIR="$(mkdir -p "$(dirname "${OUT}")" && cd "$(dirname "${OUT}")" && pwd)"', build_script)
        self.assertIn('-v "${OUT_DIR}":/out', build_script)


    def test_repart_definitions_copy_esp_and_encrypt_root(self):
        module = load_module("live_definitions", TRACK / "live-definitions.py")
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

    def test_post_install_handles_composefs_and_gpt_auto_without_root_karg(self):
        text = (TRACK / "src" / "usr" / "libexec" / "dakota-sysinstall-post").read_text()
        self.assertIn("/state/deploy/*", text)
        self.assertIn("/sysroot", text)
        self.assertIn("rd.luks.uuid", text)
        self.assertIn("resize2fs", text)
        self.assertIn("dakota.live.installer=gnome-setup", text)
        self.assertNotIn("root=UUID=", text)

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
