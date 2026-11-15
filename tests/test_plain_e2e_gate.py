"""Guard the merge queue's plain-install check and its result handling."""

import itertools
import os
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/test-plain-install.yml"


def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_events_and_gate_wiring():
    workflow = load_workflow()
    triggers = workflow[True]  # PyYAML treats the YAML 1.1 key "on" as a boolean.
    assert triggers["pull_request"]["branches"] == ["main"]
    assert "merge_group" in triggers
    assert {"push", "schedule", "workflow_dispatch"} <= triggers.keys()
    jobs = workflow["jobs"]
    for name in ("unit-tests", "plain-e2e"):
        assert jobs[name]["if"] == "github.event_name != 'pull_request'"
    assert jobs["plain-e2e"]["strategy"]["matrix"]["installer_channel"] == (
        '${{ github.event_name == \'merge_group\' && fromJSON(\'["stable"]\') '
        '|| fromJSON(\'["dev","stable"]\') }}'
    )
    gate = jobs["plain-e2e-gate"]
    assert gate["name"] == "Plain E2E gate"
    assert gate["needs"] == ["unit-tests", "plain-e2e"]
    assert gate["if"] == "always()"
    assert gate["permissions"] == {}
    assert gate["timeout-minutes"] == 5
    assert gate["steps"][0]["env"] == {
        "UNIT_RESULT": "${{ needs.unit-tests.result }}",
        "E2E_RESULT": "${{ needs.plain-e2e.result }}",
        "EVENT_NAME": "${{ github.event_name }}",
    }


@pytest.mark.parametrize(
    ("event", "unit", "e2e"),
    itertools.product(
        ("pull_request", "merge_group", "push", "schedule", "workflow_dispatch"),
        ("success", "failure", "cancelled", "skipped"),
        ("success", "failure", "cancelled", "skipped"),
    ),
)
def test_gate_results(event, unit, e2e):
    script = load_workflow()["jobs"]["plain-e2e-gate"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env=os.environ | {"EVENT_NAME": event, "UNIT_RESULT": unit, "E2E_RESULT": e2e},
        capture_output=True,
        text=True,
    )
    failed = (unit in {"failure", "cancelled"} or e2e in {"failure", "cancelled"}
              or (event == "merge_group" and e2e == "skipped"))
    assert (result.returncode != 0) == failed, result.stdout + result.stderr
