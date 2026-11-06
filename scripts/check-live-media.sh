#!/usr/bin/bash
# Exercise the shipped consumer's strict media gate without a writable target.
set -euo pipefail
DISK=/never-a-device
[[ ! -e "${DISK}" ]] || { echo "preflight sentinel unexpectedly exists" >&2; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT
python3 - "${WORK}/recipe.json" <<'PY'
import json
from pathlib import Path
import sys
config = Path("/etc/bootc-installer")
media = json.loads((config / "media-input.json").read_text())
live = json.loads((config / "recipe.json").read_text())
recipe = dict(disk="/never-a-device", filesystem=live["filesystem"],
              image=media["image"], targetImgref=media["targetImgref"],
              composeFsBackend=live["composeFsBackend"], bootloader=live["bootloader"],
              hostname="media-preflight", encryption={"type": "none"}, flatpaks=[])
Path(sys.argv[1]).write_text(json.dumps(recipe) + "\n")
PY
rc=0
/usr/local/bin/fisherman "${WORK}/recipe.json" > "${WORK}/stdout" 2> "${WORK}/stderr" || rc=$?
cat "${WORK}/stdout" "${WORK}/stderr"
# In pinned v0.5.3 strict receipt/hash validation precedes disk validation.
if [[ "${rc}" -ne 1 ]] || ! grep -Fxq \
    'fisherman: fatal: invalid recipe: disk /never-a-device: stat /never-a-device: no such file or directory' \
    "${WORK}/stderr"; then
    echo 'Bound OCI media preflight FAILED' >&2
    exit 1
fi
echo 'Bound OCI media preflight PASSED (all blobs verified; no runtime OCI export)'
