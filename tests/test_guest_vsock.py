"""scripts/guest-vsock.sh: the per-run key and host-key drop-in reach the guest (#380).

The installed system is booted with SMBIOS type 11 strings; systemd only acts
on them if the credential names and base64 payloads are exactly right, and a
mistake there shows up minutes later as an unreachable guest. These tests run
guest_vsock_prepare for real (ssh-keygen included) and decode what QEMU would
be given. Booting a guest is out of scope.
"""

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "scripts" / "guest-vsock.sh"

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None,
                                reason="ssh-keygen not installed")


def prepare(tmp_path, device_mode=0o666, cid="3225"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Vsock-capable socat for hosts without systemd-ssh-proxy.
    socat = bin_dir / "socat"
    socat.write_text("#!/bin/bash\necho '#define WITH_VSOCK 1'\n")
    socat.chmod(0o755)
    device = tmp_path / "vhost-vsock"
    device.write_text("")
    device.chmod(device_mode)
    state = tmp_path / "state"
    script = 'source "$0"; guest_vsock_prepare "$1" "$2" "" && printf "%s\\n" "${GUEST_VSOCK_QEMU_ARGS[@]}"'
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "GUEST_VSOCK_DEVICE": str(device)}
    result = subprocess.run(["bash", "-c", script, str(LIB), str(state), cid],
                            env=env, capture_output=True, text=True, timeout=30)
    return result, state


def credentials(args):
    creds = {}
    for value in args:
        prefix = "type=11,value=io.systemd.credential.binary:"
        if value.startswith(prefix):
            name, _, payload = value[len(prefix):].partition("=")
            creds[name] = base64.b64decode(payload).decode()
    return creds


def test_guest_gets_this_runs_public_key_and_host_key_generation(tmp_path):
    result, state = prepare(tmp_path)
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert "vhost-vsock-pci,guest-cid=3225" in args
    creds = credentials(args)
    assert creds["ssh.ephemeral-authorized_keys-all"] == (state / "key.pub").read_text()
    assert creds["ssh.ephemeral-authorized_keys-all"].startswith("ssh-ed25519 ")
    assert creds["systemd.unit-dropin.sshd-vsock.socket"] == (
        "[Socket]\nExecStartPre=/usr/bin/ssh-keygen -A\n"
    )
    assert (state / "cid").read_text().strip() == "3225"
    assert (state / "key").stat().st_mode & 0o077 == 0


def test_each_run_gets_a_fresh_key(tmp_path):
    first, state = prepare(tmp_path)
    assert first.returncode == 0, first.stderr
    old_key = (state / "key.pub").read_text()
    shutil.rmtree(tmp_path / "bin")
    second, _ = prepare(tmp_path)
    assert second.returncode == 0, second.stderr
    assert (state / "key.pub").read_text() != old_key


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a 0444 file")
def test_unusable_vhost_device_fails_before_boot(tmp_path):
    result, state = prepare(tmp_path, device_mode=0o444)
    assert result.returncode != 0
    assert "modprobe vhost_vsock" in result.stderr
    assert not state.exists()
