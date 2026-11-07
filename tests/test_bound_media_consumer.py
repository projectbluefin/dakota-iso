"""Opt-in release-binary proof: FISHERMAN_TEST_BINARY must name pinned v0.5.3.

No disks are exposed: a private user/mount namespace supplies only /dev/null etc.
The invalid disk refusal is reachable only after the actual strict media gate.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.oci_fixture import make_oci_archive


REPO = Path(__file__).parent.parent
BINARY = os.environ.get("FISHERMAN_TEST_BINARY")
pytestmark = pytest.mark.skipif(not BINARY or not shutil.which("bwrap"),
                                reason="requires pinned fisherman release binary and bwrap user namespaces")


@pytest.mark.parametrize("case,expected", [
    ("valid", "invalid recipe: disk /never-a-device"),
    ("missing", "live media requires /etc/bootc-installer/media-input.json"),
    ("corrupt", "bound blob hash mismatch"),
    ("writable", "not on a read-only filesystem"),
    ("target", "does not match media target"),
    ("source", "does not match embedded source"),
    ("unmounted", "opening bound path"),
])
def test_produced_media_against_shipped_consumer(tmp_path, case, expected):
    root = tmp_path / "root"
    config = root / "etc/bootc-installer"
    config.mkdir(parents=True)
    source = "ghcr.io/projectbluefin/dakota-nvidia:stable"
    target = "ghcr.io/projectbluefin/dakota:stable"
    (config / "recipe.json").write_text(json.dumps(dict(imgref=target, local_imgref=source)))
    (config / "live-iso-mode").touch()
    archive = tmp_path / "payload.tar"
    _, manifest, _ = make_oci_archive(archive)
    subprocess.run(["bash", "-c", 'set -euo pipefail; source "$1"; payload_embed_oci_archive "$2" "$3"',
                    "bash", str(REPO / "scripts/payload-oci.sh"), str(archive), str(root)], check=True)
    recipe = dict(disk="/never-a-device", filesystem="btrfs", image=source,
                  targetImgref=target, hostname="media-preflight", bootloader="systemd",
                  composeFsBackend=True, encryption={"type": "none"}, flatpaks=[])
    if case == "missing":
        (config / "media-input.json").unlink()
    elif case == "corrupt":
        layout = root / "usr/share/bootc-installer/oci"
        raw = json.loads((layout / "blobs/sha256" / manifest["digest"].split(":")[1]).read_text())
        path = layout / "blobs/sha256" / raw["layers"][0]["digest"].split(":")[1]
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])
    elif case == "target":
        recipe["targetImgref"] = source
    elif case == "source":
        recipe["image"] = "ghcr.io/example/unbound:stable"
    command = ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/",
               "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/etc", "--tmpfs", "/run",
               "--dir", "/run/bootcmedia", "--ro-bind", str(config), "/etc/bootc-installer"]
    if case != "unmounted":
        command += ["--bind" if case == "writable" else "--ro-bind", str(root), "/run/bootcmedia"]
    command += [str(Path(BINARY).resolve()), "-"]
    result = subprocess.run(command, input=json.dumps(recipe), text=True, capture_output=True, timeout=30)
    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stderr, result.stdout + result.stderr
