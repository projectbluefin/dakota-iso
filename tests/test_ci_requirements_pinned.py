"""CI test dependencies must come from one reviewed, pinned requirements file.

The unit-test and E2E workflows install pytest, PyYAML and jsonschema with
`pip install`. A newly published version of one of those packages can run
installation and plugin code on a GitHub-hosted runner before any of this
repository's own code runs. Because actions/checkout leaves repository
credentials on the runner, an unpinned test dependency can silently change
security-sensitive checks.

This test enforces the fix: every CI workflow that needs Python tools loads
them from a single `.github/requirements-ci.txt` file, and that file pins each
package to one exact version. It fails on any future `pip install` that names
a package directly without an exact `==` version, so drift cannot come back
without a reviewed change.
"""
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
REQUIREMENTS = REPO / ".github" / "requirements-ci.txt"

# The run: text of a step, e.g. "pip install pytest PyYAML jsonschema".
PIP_INSTALL_RE = re.compile(r"pip\s+install\s+(.*)$")
# A reference to a requirements file: "-r path" or "--requirement path".
REQ_REF_RE = re.compile(r"(?:^|\s)(?:-r|--requirement)\s+(\S+)")
# A package name, e.g. PyYAML or jsonschema.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A package pinned to one exact version, e.g. pytest==9.2.0.
PINNED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^=]+$")


def _workflow_run_lines():
    """Yield the raw pip-install argument text from every workflow step."""
    for wf_file in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf_file.read_text()) or {}
        for job in doc.get("jobs", {}).values() or []:
            for step in job.get("steps", []) or []:
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                for line in run.splitlines():
                    match = PIP_INSTALL_RE.search(line.strip())
                    if match:
                        yield wf_file.name, match.group(1).strip()


def _uses_requirements_file(args):
    """True if this pip install loads its packages from a requirements file."""
    return bool(REQ_REF_RE.search(args))


def _unpinned_direct_packages(args):
    """Directly named packages that are not pinned to one exact version."""
    if _uses_requirements_file(args):
        return []
    unpinned = []
    for token in args.split():
        if token.startswith("-"):
            continue
        if not PINNED_RE.match(token):
            unpinned.append(token)
    return unpinned


class RequirementsFileTest(unittest.TestCase):
    def test_requirements_file_exists_and_is_fully_pinned(self):
        self.assertTrue(
            REQUIREMENTS.exists(),
            f"{REQUIREMENTS.relative_to(REPO)} is missing",
        )
        for line in REQUIREMENTS.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertTrue(
                PINNED_RE.match(stripped),
                f"{stripped!r} in {REQUIREMENTS.relative_to(REPO)} is not a "
                "single name==version requirement",
            )

    def test_requirements_file_pins_the_three_test_packages(self):
        pinned = {
            line.strip().split("==", 1)[0].lower(): line.strip()
            for line in REQUIREMENTS.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for pkg in ("pytest", "pyyaml", "jsonschema"):
            self.assertIn(pkg, pinned, f"{pkg} is not pinned in the file")


class WorkflowPinningTest(unittest.TestCase):
    def test_no_workflow_names_an_unpinned_package_directly(self):
        for wf, args in _workflow_run_lines():
            unpinned = _unpinned_direct_packages(args)
            self.assertEqual(
                unpinned,
                [],
                f"{wf} installs {unpinned} directly instead of loading "
                ".github/requirements-ci.txt",
            )

    def test_affected_workflows_load_the_shared_requirements_file(self):
        affected = (
            "test.yml",
            "smoke-test.yml",
            "test-luks-install.yml",
            "test-plain-install.yml",
        )
        by_file = {}
        for wf, args in _workflow_run_lines():
            by_file.setdefault(wf, []).append(args)
        for wf in affected:
            self.assertIn(wf, by_file, f"{wf} has no pip install step")
            for args in by_file[wf]:
                self.assertTrue(
                    _uses_requirements_file(args),
                    f"{wf} runs `pip install {args}` instead of loading "
                    ".github/requirements-ci.txt",
                )


if __name__ == "__main__":
    unittest.main()
