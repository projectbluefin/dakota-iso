"""Exercise the live target binding against PCI/catalog fixtures, not source text."""

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "live/src/bind-media-target.py"
spec = importlib.util.spec_from_file_location("bind_media_target", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_gpu_selection_preserves_source_and_gui_update_target(tmp_path):
    for vendor, pci_class, expected in (
        ("0x8086", "0x030000", "base"),
        ("0x10de", "0x030200", "nvidia"),
        ("0x10de", "0x040300", "base"),
        ("0x10de", "unreadable", "base"),
    ):
        root = tmp_path / (vendor + pci_class)
        config = root / "etc/bootc-installer"
        config.mkdir(parents=True)
        base = "ghcr.io/projectbluefin/dakota:stable"
        nvidia = "ghcr.io/projectbluefin/dakota-nvidia:stable"
        recipe = dict(imgref=base, targetImgref=base, image=nvidia, local_imgref=nvidia)
        receipt = dict(image=nvidia, targetImgref=base, platformManifestDigest="sha256:" + "a" * 64,
                       ociLayoutPath="/run/rootfsbase/usr/share/bootc-installer/oci")
        (config / "recipe.json").write_text(json.dumps(recipe))
        (config / "media-input.json").write_text(json.dumps(receipt))
        (config / "images.json").write_text(json.dumps({"images": [dict(imgref=base, nvidia_imgref=nvidia)]}))
        device = root / "sys/bus/pci/devices/0000:01:00.0"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor)
        (device / "class").write_text(pci_class)
        module.bind_target(root)
        actual_recipe = json.loads((config / "recipe.json").read_text())
        actual_receipt = json.loads((config / "media-input.json").read_text())
        target = nvidia if expected == "nvidia" else base
        assert actual_recipe == recipe
        assert actual_receipt == {**receipt, "targetImgref": target}
