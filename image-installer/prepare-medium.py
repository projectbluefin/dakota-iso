#!/usr/bin/env python3
import pathlib
import sys


def prepare_esp(esp_path, root_uuid, initrd_name):
    esp = pathlib.Path(esp_path)
    entries_dir = esp / "loader" / "entries"
    live_path = entries_dir / "dakota-live.conf"
    entries = [path for path in entries_dir.glob("*.conf") if path != live_path]
    if len(entries) != 1:
        raise SystemExit(f"expected one installed BLS entry in {entries_dir}, got {len(entries)}")

    fields = [line.split(maxsplit=1) for line in entries[0].read_text().splitlines()]
    options = [value for key, *values in filter(None, fields) if key == "options" for value in values]
    if (len(options) != 1 or not any(row[:1] == ["linux"] and len(row) == 2 for row in fields)
            or not any(row[:1] == ["initrd"] and len(row) == 2 for row in fields)):
        raise SystemExit("expected a Dakota Type #1 BLS entry with linux, initrd and options")
    args = [
        arg for arg in options[0].split()
        if arg not in {"ro", "rw", "dakota.live"}
        and not arg.startswith(("root=", "dakota.live.installer=", "systemd.volatile="))
    ]
    args += [
        f"root=UUID={root_uuid}", "ro", "dakota.live", "dakota.live.installer=gnome-setup",
        "systemd.volatile=state", "console=tty0", "console=ttyS0,115200n8",
    ]
    live_entry = [
        "title Dakota Live",
        *(" ".join(row) for row in fields if row[:1] in (["linux"], ["initrd"])),
        f"initrd /{initrd_name}",
        "options " + " ".join(args),
    ]
    live_path.write_text("\n".join(live_entry) + "\n")
    config = esp / "loader" / "loader.conf"
    keep = [line for line in config.read_text().splitlines()
            if line.split()[:1] not in (["timeout"], ["default"])] if config.exists() else []
    config.write_text("\n".join([*keep, "timeout 5", "default dakota-live.conf"]) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: prepare-medium.py <esp-path> <root-uuid> <live-initrd-name>")
    prepare_esp(sys.argv[1], sys.argv[2], sys.argv[3])
