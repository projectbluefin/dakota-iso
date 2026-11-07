"""Debug gates must retain the production payload and change only live SSH setup."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).parent.parent


@pytest.mark.skipif(not shutil.which("mksquashfs") or not shutil.which("unsquashfs"),
                    reason="requires squashfs tools")
@pytest.mark.parametrize("ssh_unit", ["sshd.service", "ssh.service"])
def test_debug_copy_preserves_media_and_configures_boot_password(tmp_path, ssh_unit):
    root = tmp_path / "production"
    (root / "usr/lib/systemd/system").mkdir(parents=True)
    (root / "usr/lib/systemd/system" / ssh_unit).touch()
    media = root / "usr/share/bootc-installer/oci/blobs/sha256"
    media.mkdir(parents=True)
    (media / "fixture").write_bytes(b"production payload")
    config = root / "etc/bootc-installer"
    config.mkdir(parents=True)
    (config / "media-input.json").write_text('{"fixture":"production receipt"}')
    (root / "etc/ssh").mkdir()
    for name in ("run", "tmp"):
        (root / name).mkdir()
        (root / name / "runtime-secret").write_text("must not ship")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "live/src").mkdir(parents=True)
    script = repo / "scripts/build-debug-iso.sh"
    shutil.copy(REPO / "scripts/build-debug-iso.sh", script)
    (repo / "live/src/build-iso.sh").write_text('printf "%s\\n" "$@" > "$ASSEMBLY_ARGS"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stubs = {
        "unsquashfs": 'cp -a "$PRODUCTION" "$2"\n',
        "mksquashfs": '"$REAL_MKSQUASHFS" "$@"; "$REAL_UNSQUASHFS" -d "$CAPTURE" "$2"\n',
    }
    for name, body in stubs.items():
        path = bindir / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
        path.chmod(0o755)
    capture = tmp_path / "captured"
    args = tmp_path / "assembly-args"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PRODUCTION": str(root),
           "CAPTURE": str(capture), "ASSEMBLY_ARGS": str(args),
           "REAL_MKSQUASHFS": shutil.which("mksquashfs"),
           "REAL_UNSQUASHFS": shutil.which("unsquashfs")}
    result = subprocess.run(["bash", str(script), str(tmp_path / "production.sfs"), "boot.tar",
                             "debug.iso", "Utah Live"], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (capture / "usr/share/bootc-installer/oci/blobs/sha256/fixture").read_bytes() == b"production payload"
    assert (capture / "etc/bootc-installer/media-input.json").read_bytes() == (config / "media-input.json").read_bytes()
    for name in ("run", "tmp"):
        assert (capture / name).is_dir()
        assert not list((capture / name).iterdir())
    assert "liveuser:live" in (capture / "usr/lib/systemd/system/live-debug-passwords.service").read_text()
    assert f"enable {ssh_unit}" in (capture / "etc/systemd/system-preset/90-live-debug.preset").read_text()
    assert (capture / "etc/systemd/system/multi-user.target.wants" / ssh_unit).is_symlink()
    assert args.read_text().splitlines()[:3] == ["--title", "Utah Live", "boot.tar"]
    assert not list(tmp_path.glob("debug-iso.*"))
    assert not (root / "etc/ssh/sshd_config.d").exists()
