"""Regression tests for the R2 download-stats workflow error handling.

The scheduled ``r2-download-stats.yml`` workflow counts ISO downloads from
Cloudflare R2. Its "Query R2 download counts" step runs under
``set -euo pipefail`` and assigns the curl response to a variable:

    result=$(curl -sS --fail-with-body ...)

With ``set -e`` a nonzero curl exit (an HTTP 400 from ``--fail-with-body``
exits 22) aborts the step *before* the response body is printed. The only
evidence a run ever gave was ``curl: (22) ... error: 400`` with no body, so a
rejected query was invisible.

These tests run the real workflow ``run`` block with a stub ``curl`` on ``PATH``
and assert that:

1. On an HTTP error the captured response body is printed to stderr and the
   step still exits with curl's nonzero status (22).
2. On success the response is processed normally and the step exits 0.
3. On a transport failure (e.g. curl exit 6, "couldn't resolve host") the body
   is printed and that distinct status is preserved.

The stub writes the error body to stdout (the way ``--fail-with-body`` does)
and exits with the status under test, so the assignment captures it exactly as
the real curl would.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/r2-download-stats.yml"


def _step_run():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["count"]["steps"]
                if s.get("name") == "Query R2 download counts")
    return step["run"]


def _run_with_curl(tmp_path, status, body):
    """Run the step with a stub curl that prints ``body`` and exits ``status``.

    The stub reads the body from ``CURL_BODY`` so the JSON is passed verbatim,
    with no shell-escaping of the quotes inside it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s" "$CURL_BODY"\n'
        f"exit {status}\n"
    )
    curl.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CURL_BODY": body,
        "CF_OBSERVABILITY_TOKEN": "test-token",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "step.md"),
    }
    return subprocess.run(
        ["bash", "-c", _step_run()],
        env=env, text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize(
    ("status", "body", "description"),
    [
        pytest.param(22,
                     '{"success":false,"errors":[{"code":9106,"message":"rejected query: unknown dataset r2"}]}',
                     "HTTP 400 body is printed and status 22 preserved",
                     id="http-400"),
        pytest.param(6,
                     '{"success":false,"errors":[{"code":0,"message":"Temporary failure in name resolution"}]}',
                     "transport failure body is printed and status 6 preserved",
                     id="transport-failure"),
    ],
)
def test_error_body_is_printed_with_status(tmp_path, status, body, description):
    result = _run_with_curl(tmp_path, status, body)
    assert result.returncode == status, description
    assert body in result.stderr, f"{description}: body should be on stderr"


def test_success_path_unchanged(tmp_path):
    body = '{"success":true,"result":[{"resource.key":"dakota.iso","count":1234}]}'
    result = _run_with_curl(tmp_path, 0, body)
    assert result.returncode == 0
    # The body is processed (pretty-printed by jq) on stdout, not discarded.
    assert "dakota.iso" in result.stdout
    # The failure branch must not have run.
    assert "rejected query" not in result.stderr
