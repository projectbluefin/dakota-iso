#!/usr/bin/env python3
"""Finalize the copied Type #1 boot entry and the composefs deployment's mount config."""

import re
import sys
from pathlib import Path


def write(path, text):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    temporary.replace(path)


def finish(esp, root, luks_uuid, options, uuids):
    entries = esp / "loader/entries"
    live = entries / "dakota-live.conf"
    installed = [path for path in entries.glob("*.conf") if path != live]
    pending = list(entries.glob("*.conf.pending"))
    deployments = list((root / "state/deploy").glob("*/etc"))
    if len(installed + pending) != 1 or len(deployments) != 1:
        raise ValueError("expected one installed entry and one deployment")
    entry = installed[0] if installed else pending[0].with_suffix("")
    staged = entry.with_suffix(".conf.pending")
    lines = (staged if pending else entry).read_text().splitlines()
    indexes = [i for i, line in enumerate(lines) if line.split()[:1] == ["options"]]
    if len(indexes) != 1:
        raise ValueError("expected one options line in the installed BLS entry")

    old_root, new_root, old_esp, new_esp = uuids
    if not all(uuids) or new_root == old_root or new_esp == old_esp:
        raise ValueError("copied filesystems must have new UUIDs")
    ids = re.compile("|".join(map(re.escape, (old_root, old_esp))), re.IGNORECASE)
    replacements = {old_root.lower(): new_root, old_esp.lower(): new_esp}

    def rewrite(text):
        return ids.sub(lambda match: replacements[match[0].lower()], text)

    index = indexes[0]
    args = [arg for arg in lines[index].split()[1:]
            if arg not in {"ro", "dakota.live"}
            and not arg.startswith(("root=", "rd.luks.", "dakota.live.installer=", "systemd.volatile="))]
    if not any(arg.startswith(("composefs=", "composefs.digest=")) for arg in args):
        raise ValueError("installed BLS entry does not identify its composefs deployment")
    args += [f"root=UUID={new_root}", f"rd.luks.uuid={luks_uuid}", f"rd.luks.options={luks_uuid}={options}"]
    lines[index] = "options " + " ".join(args)

    loader = esp / "loader/loader.conf"
    loader_lines = [line for line in loader.read_text().splitlines() if line.split()[:1] != ["default"]]
    if not pending:
        entry.rename(staged)
    etc = deployments[0]
    fstab = etc / "fstab"
    if fstab.is_file():
        write(fstab, rewrite(fstab.read_text()))
    write(etc / "crypttab", f"luks-{luks_uuid} UUID={luks_uuid} none {options}\n")
    write(staged, rewrite("\n".join(lines) + "\n"))
    write(loader, "\n".join([*loader_lines, f"default {entry.name}"]) + "\n")
    live.unlink(missing_ok=True)
    (esp / "dakota-live-initrd.img").unlink(missing_ok=True)


def publish(esp):
    entries = esp / "loader/entries"
    if (entries / "dakota-live.conf").exists():
        raise ValueError("live entry still present on installed ESP")
    pending = list(entries.glob("*.conf.pending"))
    installed = list(entries.glob("*.conf"))
    if len(pending + installed) != 1:
        raise ValueError("expected one installed boot entry")
    if pending:
        pending[0].rename(pending[0].with_suffix(""))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--publish":
        publish(Path(sys.argv[2]))
    else:
        finish(Path(sys.argv[1]), Path(sys.argv[2]), *sys.argv[3:5], sys.argv[5:])
