"""Guard the ubuntu-26.04 LUKS E2E regression where `sudo just` broke the gate.

On ubuntu-26.04 the GitHub runner keeps the runner user's ``XDG_RUNTIME_DIR``
when running ``sudo``. A plain ``sudo just`` (the "Build debug ISO" step) then
wrote a root-owned ``just`` cache into the runner's runtime dir
(`/run/user/1001/just`). The very next step — the unprivileged
``just luks-test-qemu`` / ``just gui-e2e`` — failed with
"Permission denied (os error 13)" before the test ever started, taking both
LUKS legs down with it.

The fix runs the build step as ``sudo env -u XDG_RUNTIME_DIR just ...``, the
same ``sudo env VAR=... just`` convention ``build-iso.yml`` already uses. This
test asserts every E2E workflow that runs ``sudo just`` on 26.04 unsets that
variable, so a bare ``sudo just`` cannot slip back in and re-break the gate.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
LUKS_WORKFLOW = REPO / ".github/workflows/test-luks-install.yml"
GUI_WORKFLOW = REPO / ".github/workflows/scheduled-gui-installer.yml"


def _build_iso_run(workflow_path: Path) -> str:
    """Return the ``run`` block of the "Build debug ISO" step."""
    workflow = yaml.safe_load(workflow_path.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Build debug ISO":
                return step["run"]
    raise AssertionError(f"no 'Build debug ISO' step in {workflow_path}")


@pytest.mark.parametrize(
    "workflow_path",
    [LUKS_WORKFLOW, GUI_WORKFLOW],
    ids=["test-luks-install", "scheduled-gui-installer"],
)
def test_build_step_unsets_xdg_runtime_dir(workflow_path: Path):
    run = _build_iso_run(workflow_path)
    assert (
        "env -u XDG_RUNTIME_DIR just" in run
    ), (
        "the Build debug ISO step runs `sudo just` with the runner's "
        "XDG_RUNTIME_DIR inherited, so on ubuntu-26.04 root owns the just "
        "cache in the runtime dir and the next unprivileged `just` fails to "
        "write it (os error 13); unset XDG_RUNTIME_DIR for the sudo step"
    )
