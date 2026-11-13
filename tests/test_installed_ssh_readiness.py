"""Exercise the publication SSH gate with fake transport, not a booted guest."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-post-install.sh"


def run_gate(tmp_path, message, status, ready_after=999, mount="btrfs rw,compress=zstd:1",
             extra_env=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    transport = bin_dir / "sshpass"
    transport.write_text('''#!/bin/bash
case "${!#}" in
    true)
        n=0
        [[ ! -f "$COUNT" ]] || read -r n < "$COUNT"
        n=$((n + 1))
        echo "$n" > "$COUNT"
        if (( n >= READY_AFTER )); then exit 0; fi
        printf '%s\\n' "$ERROR_MESSAGE" >&2
        exit "$ERROR_STATUS"
        ;;
    *findmnt*) echo "$MOUNT" ;;
    *cmdline*) echo 'root=LABEL=root rootflags=compress=zstd:1' ;;
    *efibootmgr*) exit 1 ;;
    *flatpak*) exit 0 ;;
esac
''')
    transport.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/bash\nexit 0\n")
    sleep.chmod(0o755)
    env = {
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COUNT": str(tmp_path / "count"), "READY_AFTER": str(ready_after),
        "ERROR_MESSAGE": message, "ERROR_STATUS": str(status), "MOUNT": mount,
        "TMPDIR": str(tmp_path), "STRICT_POST_BOOT": "0",
    }
    env.update(extra_env or {})
    result = subprocess.run(["bash", str(SCRIPT), "2225", "none"],
                            env=env, capture_output=True, text=True, timeout=10)
    count = tmp_path / "count"
    return result, int(count.read_text()) if count.exists() else 0


@pytest.mark.parametrize(("message", "status"), [
    ("ssh: connect to host 127.0.0.1 port 2225: Connection refused", 255),
    ("Connection timed out during banner exchange", 255),
    ("Permission denied, please try again.", 5),
])
def test_unreachable_reports_last_error_and_fails_closed(tmp_path, message, status):
    result, attempts = run_gate(tmp_path, message, status)
    assert result.returncode == 2
    assert attempts == 60
    assert message in result.stderr
    assert f"exit status {status}" in result.stderr
    assert "60 attempts" in result.stderr
    assert "elapsed" in result.stderr
    assert "after 5 minutes" not in result.stderr
    assert "Installed /var mount:" not in result.stdout
    assert not list(tmp_path.glob("installed-ssh.*"))
    assert "sshpass -p" not in result.stderr


def test_transient_failure_still_runs_required_assertions(tmp_path):
    result, attempts = run_gate(tmp_path, "Connection refused", 255, ready_after=3)
    assert result.returncode == 0
    assert attempts == 3
    assert "compression and persistent rootflags verified" in result.stdout
    assert "Connection refused" not in result.stderr
    assert not list(tmp_path.glob("installed-ssh.*"))


def test_reachable_guest_without_compression_still_fails(tmp_path):
    result, _ = run_gate(tmp_path, "", 255, ready_after=1, mount="btrfs rw")
    assert result.returncode == 1
    assert "persistent rootflags are required" in result.stderr


def test_unwritable_tmpdir_fails_immediately_without_ssh(tmp_path):
    result, attempts = run_gate(tmp_path, "", 0, ready_after=1,
                                extra_env={"TMPDIR": str(tmp_path / "missing")})
    assert result.returncode == 1
    assert attempts == 0
    assert "Cannot create temporary file" in result.stderr
    assert "could not reach installed system" not in result.stderr
