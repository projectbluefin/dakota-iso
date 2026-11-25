"""Regression tests for the protected deployment environments of the manual
release tools (#404).

``r2-download-stats.yml`` and ``promote-release.yml`` are ``workflow_dispatch``
workflows that read long-lived Cloudflare and R2 credentials. GitHub lets a
collaborator dispatch a workflow from any branch that holds the workflow file,
and the code that runs is the code on that branch — the reviewed copy on ``main``
is not what executes. With repository-scoped secrets, altered workflow code on a
side branch can therefore read or exfiltrate those credentials.

A deployment environment fixes that: the job declares ``environment:``, the
credentials live at environment scope, and the environment's deployment branch
rule allows ``main`` only. A dispatch from another branch then gets no
credentials.

These tests pin that wiring, so an edit that drops the ``environment:`` key or
adds a second secret-reading job without one fails here instead of silently
reopening the hole.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

# The environments a maintainer creates in the repository settings, with the
# deployment branch rule "main" only. The workflow and the repository setting
# must name the same environment, otherwise the job runs with no credentials.
EXPECTED_ENVIRONMENTS = {
    "r2-download-stats.yml": {"count": "r2-observability"},
    "promote-release.yml": {"promote": "r2-production"},
}

# Credentials that must never be readable by workflow code from an unreviewed
# branch.
PROTECTED_SECRETS = (
    "CF_OBSERVABILITY_TOKEN",
    "RCLONE_CONFIG_R2_ACCESS_KEY_ID",
    "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY",
    "RCLONE_CONFIG_R2_ENDPOINT",
)


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _secret_names(node):
    """Collect every ``secrets.NAME`` reference in a job definition."""
    names = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.startswith("secrets."):
                names.add(key[len("secrets."):])
            names |= _secret_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _secret_names(item)
    elif isinstance(node, str):
        for secret in PROTECTED_SECRETS:
            if f"secrets.{secret}" in node:
                names.add(secret)
    return names


def _secret_reading_jobs(workflow):
    """Return ``{job_name: {secret_name, ...}}`` for jobs that read a secret."""
    reading = {}
    for job_name, job in (workflow.get("jobs") or {}).items():
        secrets = _secret_names(job)
        if secrets:
            reading[job_name] = secrets
    return reading


@pytest.mark.parametrize("workflow_name", sorted(EXPECTED_ENVIRONMENTS))
def test_secret_reading_jobs_declare_their_environment(workflow_name):
    # Every job that reads a protected credential must sit behind a deployment
    # environment. A job added later without one fails here.
    workflow = _load(workflow_name)
    reading = _secret_reading_jobs(workflow)
    assert reading, f"{workflow_name} reads no protected secret any more — " \
                     "re-check whether it still needs the environment"

    for job_name in reading:
        job = workflow["jobs"][job_name]
        environment = job.get("environment")
        assert environment, (
            f"job {job_name!r} in {workflow_name} reads {sorted(reading[job_name])} "
            f"but declares no environment, so a dispatch from an unreviewed branch "
            f"would still receive the credentials"
        )


@pytest.mark.parametrize("workflow_name", sorted(EXPECTED_ENVIRONMENTS))
def test_environment_names_match_the_repository_settings(workflow_name):
    # The environment named in the workflow must be the one the maintainer
    # created with the "main only" deployment branch rule.
    workflow = _load(workflow_name)
    expected = EXPECTED_ENVIRONMENTS[workflow_name]

    for job_name, environment_name in expected.items():
        job = workflow["jobs"][job_name]
        assert job.get("environment") == environment_name, (
            f"job {job_name!r} in {workflow_name} must use the "
            f"{environment_name!r} environment, got {job.get('environment')!r}"
        )


@pytest.mark.parametrize("workflow_name", sorted(EXPECTED_ENVIRONMENTS))
def test_the_protected_job_is_the_one_that_reads_the_secret(workflow_name):
    # Pin which job carries the environment: the environment must sit on the job
    # that reads the credential, not on a neighbour that reads nothing.
    workflow = _load(workflow_name)
    reading = _secret_reading_jobs(workflow)
    expected = EXPECTED_ENVIRONMENTS[workflow_name]

    for job_name, environment_name in expected.items():
        assert job_name in reading, (
            f"job {job_name!r} in {workflow_name} no longer reads a protected "
            f"secret — the {environment_name!r} environment may have been moved "
            f"off the job that needs it"
        )
