#!/usr/bin/env python3
"""Bind the live receipt to the GUI's hardware-selected day-2 update ref."""

import json
from pathlib import Path


def bind_target(root=Path("/")):
    config = root / "etc/bootc-installer"
    recipe_path = config / "recipe.json"
    receipt_path = config / "media-input.json"
    recipe = json.loads(recipe_path.read_text())
    receipt = json.loads(receipt_path.read_text())
    # Match bootc-installer's _detect_display_devices: PCI display class 0x03.
    nvidia = False
    for class_file in (root / "sys/bus/pci/devices").glob("*/class"):
        try:
            if int(class_file.read_text().strip(), 16) >> 16 == 0x03:
                nvidia |= (class_file.parent / "vendor").read_text().strip() == "0x10de"
        except (OSError, ValueError):
            continue
    target = recipe["imgref"]
    if nvidia:
        catalog = json.loads((config / "images.json").read_text())
        for image in catalog["images"]:
            if image.get("imgref") == recipe["imgref"] and image.get("nvidia_imgref"):
                target = image["nvidia_imgref"]
                break
    receipt["targetImgref"] = target
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    bind_target()
