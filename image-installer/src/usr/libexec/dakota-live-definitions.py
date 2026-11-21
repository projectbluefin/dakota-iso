#!/usr/bin/env python3
import json
import pathlib
import sys


def render_definitions(table, root_node, tpm2=False, bios_type="21686148-6449-6e6f-744e-656564454649"):
    data = table.get("partitiontable", table)
    esp_type = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    parts = [part for part in data["partitions"] if part["type"].lower() != bios_type]
    roots = [part for part in parts if part["node"] == root_node]
    esps = [part for part in parts if part["type"].lower() == esp_type]
    if len(parts) != 2 or len(roots) != 1 or len(esps) != 1 or roots == esps:
        raise ValueError("expected exactly one source ESP and one distinct root partition")
    sector = data.get("sectorsize", 512)
    definitions = {}
    for index, part in enumerate(parts, start=1):
        ptype = part["type"].lower()
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
