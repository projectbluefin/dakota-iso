"""Exercise the CI alert's supersession lookup with paginated API responses."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci-alert.yml"


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([{"workflow_runs": [{"id": 43629903745, "run_number": 418, "event": "push"}]},
          {"workflow_runs": [{"id": 43630000000, "run_number": 419, "event": "push"}]}], 43630000000),
        ([{"workflow_runs": [{"id": 43629903745, "run_number": 418, "event": "push"}]}], None),
        ([{"workflow_runs": [{"id": 99, "run_number": 419, "event": "schedule"}]}], None),
    ],
)
def test_supersession_lookup(tmp_path, pages, expected):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["alert"]["steps"] if s.get("id") == "superseded")
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s" "$API_RESPONSE"\n')
    gh.chmod(0o755)
    output = tmp_path / "output"
    output.touch()
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "API_RESPONSE": json.dumps(pages),
        "GITHUB_OUTPUT": str(output),
        "REPO": "projectbluefin/dakota-iso",
        "WORKFLOW_ID": "123",
        "BRANCH": "main",
        "RUN_NUMBER": "418",
    }
    result = subprocess.run(["bash", "-c", step["run"]], env=env, text=True, capture_output=True, check=True)
    if expected is None:
        assert output.read_text() == ""
        assert "superseded" not in result.stdout
    else:
        assert output.read_text() == "found=true\n"
        assert f"superseded by run {expected}" in result.stdout
