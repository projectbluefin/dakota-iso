"""Regression tests for the protected deployment environments of the release
workflows.

Every credential that can write to the published ISO bucket, or read the
download statistics, must sit behind a GitHub deployment environment whose
deployment branch rule allows `main` only. Without one, a `workflow_dispatch`
of a side branch runs that branch's copy of the workflow with the
repository-scoped secret, and a collaborator with write access can read it.

`environment` is a job-level key — GitHub Actions has no step-level form — so
the environment sits on the job that reads the credential, and a step-level
`environment:` is invalid workflow syntax that GitHub rejects outright.
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
    # The nightly publish lanes write the live ISO to the release bucket, so
    # they read the same R2 write credentials as the manual promotion tool.
    "build-iso-utah.yml": {"build-and-publish": "r2-production"},
    "build-iso.yml": {"build-and-publish": "r2-production"},
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


def _readers(workflow):
    """Every job or step that reads a protected credential.

    Each entry says which secrets it reads and which deployment environment
    gates it. The gate is the job's environment: GitHub Actions has no
    step-level ``environment`` key, so a step inherits the environment of the
    job it runs in and nothing else. A reader in a job with no environment is
    a credential a side-branch dispatch can still reach.
    """
    readers = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        job_environment = job.get("environment")
        step_secrets = set()
        for index, step in enumerate(job.get("steps") or []):
            secrets = _secret_names(step)
            if not secrets:
                continue
            step_secrets |= secrets
            readers.append({
                "location": f"{job_name} / {step.get('name') or index}",
                "job": job_name,
                "secrets": secrets,
                "environment": job_environment,
            })
        # A credential read by the job itself (a job-level `env:` block, a
        # matrix expansion) is a reader too.
        outside = _secret_names(job) - step_secrets
        if outside:
            readers.append({
                "location": job_name,
                "job": job_name,
                "secrets": outside,
                "environment": job_environment,
            })
    return readers


@pytest.mark.parametrize("workflow_name", sorted(EXPECTED_ENVIRONMENTS))
def test_every_credential_reader_is_gated_by_the_environment(workflow_name):
    # Every job or step that reads a protected credential must sit behind a
    # deployment environment. A reader added later without one fails here.
    workflow = _load(workflow_name)
    readers = _readers(workflow)
    assert readers, f"{workflow_name} reads no protected secret any more — " \
                     "re-check whether it still needs the environment"

    for reader in readers:
        assert reader["environment"], (
            f"{reader['location']} in {workflow_name} reads {sorted(reader['secrets'])} "
            f"but declares no environment, so a dispatch from an unreviewed branch "
            f"would still receive the credentials"
        )
        assert reader["environment"] == EXPECTED_ENVIRONMENTS[workflow_name][reader["job"]], (
            f"{reader['location']} in {workflow_name} must be gated by the "
            f"{EXPECTED_ENVIRONMENTS[workflow_name][reader['job']]!r} environment, "
            f"got {reader['environment']!r}"
        )


@pytest.mark.parametrize("workflow_name", sorted(EXPECTED_ENVIRONMENTS))
def test_environment_names_match_the_repository_settings(workflow_name):
    # The environment named in the workflow must be the one the maintainer
    # created with the "main only" deployment branch rule.
    workflow = _load(workflow_name)
    expected = EXPECTED_ENVIRONMENTS[workflow_name]

    for job_name, environment_name in expected.items():
        job = workflow["jobs"][job_name]
        declared = job.get("environment")
        assert declared == environment_name, (
            f"job {job_name!r} in {workflow_name} must use the "
            f"{environment_name!r} environment, got {declared!r}"
        )


@pytest.mark.parametrize("workflow_name", sorted(EXPECTED_ENVIRONMENTS))
def test_the_environment_sits_on_the_reader(workflow_name):
    # The environment must sit on the job that reads the credential, not on a
    # neighbour that reads nothing: an environment on a neighbour gates the
    # wrong code.
    workflow = _load(workflow_name)
    readers = _readers(workflow)
    reading_jobs = {reader["job"] for reader in readers}

    for job_name, job in workflow["jobs"].items():
        if job.get("environment"):
            assert job_name in reading_jobs, (
                f"job {job_name!r} in {workflow_name} declares an environment but "
                f"reads no protected credential; the environment must sit on the "
                f"job that reads it"
            )


def test_no_workflow_declares_a_step_level_environment():
    # GitHub Actions defines `environment` only under `jobs.<job_id>`. A
    # step-level key is not a narrower gate — it is invalid workflow syntax,
    # and GitHub rejects the whole workflow file ("Unexpected value
    # 'environment'"). Both ISO publish workflows carried it on their upload
    # step once and stopped running altogether (#407).
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                assert "environment" not in step, (
                    f"step {step.get('name')!r} in job {job_name!r} of "
                    f"{path.name} declares a step-level environment:, which "
                    f"GitHub Actions rejects as invalid workflow syntax; put "
                    f"the environment on the job"
                )


def test_no_other_workflow_reads_a_protected_credential():
    # A new workflow that reads one of these credentials inherits the same
    # exposure: it must declare an environment of its own, and be named in
    # EXPECTED_ENVIRONMENTS so this check covers it.
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        if not _secret_names(workflow):
            continue
        assert path.name in EXPECTED_ENVIRONMENTS, (
            f"{path.name} reads a protected credential but is not covered by the "
            f"environment contract; add it to EXPECTED_ENVIRONMENTS"
        )
