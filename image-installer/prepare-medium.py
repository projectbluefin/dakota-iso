#!/usr/bin/env python3
import pathlib
import sys


def prepare_esp(esp_path, root_uuid, initrd_name):
    esp = pathlib.Path(esp_path)
    entries_dir = esp / "loader" / "entries"
    entries = sorted(entries_dir.glob("*.conf"))
    if not entries:
        raise SystemExit(f"no BLS entries found in {entries_dir}")

    installed_entry = entries[0].read_text().splitlines()
    linux = next(line for line in installed_entry if line.startswith("linux "))
    initrds = [line for line in installed_entry if line.startswith("initrd ")]

    live_entry = [
        "title Dakota Live",
        linux,
        *initrds,
        f"initrd /{initrd_name}",
        f"options root=UUID={root_uuid} ro dakota.live dakota.live.installer=gnome-setup systemd.volatile=state console=tty0 console=ttyS0,115200n8",
    ]
    (entries_dir / "dakota-live.conf").write_text("\n".join(live_entry) + "\n")
    (esp / "loader" / "loader.conf").write_text("timeout 5\ndefault dakota-live.conf\n")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: prepare-medium.py <esp-path> <root-uuid> <live-initrd-name>")
    prepare_esp(sys.argv[1], sys.argv[2], sys.argv[3])
