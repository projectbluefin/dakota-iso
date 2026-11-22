"""Regression tests for signature handling in ``promote-release.yml`` (#383).

The build workflows sign ``*-live-latest.iso-CHECKSUM`` with keyless cosign. A
named release must carry that signature and keep the signed checksum
byte-identical, or users cannot verify it. These tests run the real ``run``
blocks against a fake R2 bucket (a directory) through stub ``rclone``/``cosign``.
"""

import os
import subprocess
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/promote-release.yml"
SRC = "dakota-live-latest.iso"
DST = "dakota-live-alpha6.iso"
SUM = "a" * 64

RCLONE = r"""#!/usr/bin/env bash
set -euo pipefail
B="$BUCKET"; p() { echo "$B/${1#R2:testing/}"; }
args=(); for a in "$@"; do [[ $a == --* || $a == INFO ]] || args+=("$a"); done
case "${args[0]}" in
  lsf) ls -1 "$B" ;;
  copyto) src="${args[1]}"; [[ $src == R2:* ]] && src="$(p "$src")"; cp "$src" "$(p "${args[2]}")" ;;
  cat) cat "$(p "${args[1]}")" ;;
  size) printf '{"count":1,"bytes":%s}\n' "$(stat -c%s "$(p "${args[1]}")")" ;;
esac
"""


def _steps():
    wf = yaml.safe_load(WORKFLOW.read_text())
    return {s["name"]: s for s in wf["jobs"]["promote"]["steps"]}


def _run(tmp_path, name, bucket, extra_env=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool, body in {
        "rclone": RCLONE,
        "sudo": "#!/bin/sh\nexit 0\n",
        "cosign": '#!/bin/sh\necho "$@" >> "$COSIGN_LOG"\n',
    }.items():
        (bindir / tool).write_text(body)
        (bindir / tool).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BUCKET": str(bucket),
        "SRC": SRC,
        "DST": DST,
        "OVERWRITE": "false",
        "SIGNER_RE": r"build-iso\.yml",
        "COSIGN_LOG": str(tmp_path / "cosign.log"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        **(extra_env or {}),
    }
    return subprocess.run(["bash", "-c", _steps()[name]["run"]], env=env,
                          capture_output=True, text=True)


def _bucket(tmp_path, with_bundle=True):
    b = tmp_path / "r2"
    b.mkdir()
    (b / SRC).write_bytes(b"iso")
    (b / f"{SRC}-CHECKSUM").write_text(f"{SUM}  dakota-live-20261122-abc1234.iso\n")
    if with_bundle:
        (b / f"{SRC}-CHECKSUM.sigstore.json").write_text("{}")
    (b / f"{SRC}-CHECKSUM.pem").write_text("pem")
    return b


def test_promotion_copies_signature_and_keeps_signed_checksum(tmp_path):
    b = _bucket(tmp_path)
    r = _run(tmp_path, "Promote objects on R2", b)
    assert r.returncode == 0, r.stderr
    r = _run(tmp_path, "Verify promoted pair", b)
    assert r.returncode == 0, r.stderr

    assert (b / f"{DST}-CHECKSUM.sigstore.json").read_text() == "{}"
    assert (b / f"{DST}-CHECKSUM.pem").exists()
    assert not (b / f"{DST}-CHECKSUM.sig").exists()
    assert (b / f"{DST}-CHECKSUM").read_bytes() == (b / f"{SRC}-CHECKSUM").read_bytes()
    assert (b / f"{DST}-SHA256SUMS").read_text() == f"{SUM}  {DST}\n"

    log = (tmp_path / "cosign.log").read_text()
    assert "verify-blob" in log
    assert r"workflows/build-iso\.yml@" in log


def test_promotion_refuses_unsigned_source(tmp_path):
    b = _bucket(tmp_path, with_bundle=False)
    r = _run(tmp_path, "Promote objects on R2", b)
    assert r.returncode != 0
    assert "sigstore.json not found" in r.stdout + r.stderr
    assert not (b / DST).exists()
