#!/usr/bin/env python3
import json
import pathlib
import sys


def render_definitions(table, root_node, tpm2=False, bios_type="21686148-6449-6e6f-744e-656564454649"):
    data = table.get("partitiontable", table)
    sector = data.get("sectorsize", 512)
    definitions = {}
    for index, part in enumerate(data["partitions"], start=1):
        ptype = part["type"].lower()
        if ptype == bios_type.lower():
            continue
        lines = ["[Partition]", f"Type={ptype}", f"CopyBlocks=/dev/disk/by-partuuid/{part['uuid'].lower()}"]
        if part.get("name"):
            lines.append(f"Label={part['name']}")
        if part["node"] == root_node:
            lines.append("Encrypt=key-file+tpm2" if tpm2 else "Encrypt=key-file")
            if tpm2:
                lines.append("TPM2PCRs=7")
        else:
            size = part["size"] * sector
            lines += [f"SizeMinBytes={size}", f"SizeMaxBytes={size}"]
        definitions[f"{index:02d}-copy.conf"] = "\n".join(lines) + "\n"
    return definitions


def write_definitions(out_dir, disk_table_json, root_node, tpm2):
    table = json.loads(disk_table_json)["partitiontable"]
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rendered = render_definitions(table, root_node, tpm2)
    for name, content in rendered.items():
        (out / name).write_text(content)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: live-definitions.py <out-dir> <root-node> <tpm2-flag>")
    write_definitions(sys.argv[1], sys.stdin.read(), sys.argv[2], sys.argv[3] == "1")
