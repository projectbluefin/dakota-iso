"""Protect the Plain E2E merge-queue gate (#347)."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/test-plain-install.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_triggers(workflow):
    # PyYAML's YAML 1.1 parser reads the unquoted `on` key as True.
    triggers = workflow[True]
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert "merge_group" in triggers
    assert {"push", "schedule", "workflow_dispatch"} <= set(triggers)
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'push' }}"


def test_pull_requests_skip_the_e2e(workflow):
    jobs = workflow["jobs"]
    assert jobs["unit-tests"]["if"] == "github.event_name != 'pull_request'"
    assert jobs["plain-e2e"]["needs"] == "unit-tests"


def test_merge_queue_runs_only_the_stable_leg(workflow):
    channels = workflow["jobs"]["plain-e2e"]["strategy"]["matrix"]["installer_channel"]
    assert channels == (
        "${{ github.event_name == 'merge_group' && fromJSON('[\"stable\"]')"
        " || fromJSON('[\"dev\", \"stable\"]') }}"
    )


def test_gate_wiring(workflow):
    gate = workflow["jobs"]["plain-e2e-gate"]
    assert gate["name"] == "Plain E2E gate"
    assert gate["needs"] == ["unit-tests", "plain-e2e"]
    assert "pull_request" in gate["if"] and "merge_group" in gate["if"]
    assert gate["permissions"] == {}
    env = gate["steps"][0]["env"]
    assert env["UNIT_TESTS"] == "${{ needs.unit-tests.result }}"
    assert env["PLAIN_E2E"] == "${{ needs.plain-e2e.result }}"
    assert env["EVENT"] == "${{ github.event_name }}"


@pytest.mark.parametrize(
    ("event", "unit", "e2e", "passes"),
    [
        ("pull_request", "skipped", "skipped", True),
        ("pull_request", "failure", "skipped", False),
        ("merge_group", "success", "success", True),
        ("merge_group", "success", "skipped", False),
        ("merge_group", "skipped", "skipped", False),
        ("merge_group", "success", "failure", False),
        ("merge_group", "success", "cancelled", False),
        ("merge_group", "failure", "skipped", False),
    ],
)
def test_gate_script(workflow, event, unit, e2e, passes):
    script = workflow["jobs"]["plain-e2e-gate"]["steps"][0]["run"]
    env = os.environ | {"EVENT": event, "UNIT_TESTS": unit, "PLAIN_E2E": e2e}
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is passes, result.stderr
