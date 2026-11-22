"""Contract tests proving the post-boot strictness gate (dakota#381) cannot be
waved through by continue-on-error or `if: always()`.

A post-boot assertion added to STRICT_IDS makes verify-post-install.sh exit 1.
That only fails the pipeline if the E2E step that runs the script forwards the
exit code and is not itself waved through. These tests pin that wiring so the
gate a graduated check relies on cannot quietly stop gating.
"""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
JUSTFILE = REPO / "justfile"
CRITERIA = REPO / ".github" / "post-boot-criteria.yml"
PLAIN_E2E_STEP = "Run plain install E2E"
LUKS_E2E_STEP = "Run LUKS end-to-end test"


def _load_workflow(name):
    return yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())


def _find_step(workflow, name):
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step
    raise AssertionError(f"step {name!r} not found in {workflow}")


class TestStrictPostBootContract(unittest.TestCase):
    def test_recipes_forward_the_verify_exit_code(self):
        # The plain recipe captures verify-post-install.sh's rc and exits with
        # it; the LUKS recipe runs it under `set -e`, so a strict failure also
        # aborts the recipe with that rc. Either way the lane sees the exit.
        just = JUSTFILE.read_text()
        self.assertIn('POST_BOOT_RC=0', just)
        self.assertIn('exit "$POST_BOOT_RC"', just)
        self.assertIn("scripts/verify-post-install.sh", just)

    def test_e2e_steps_are_not_waved_through(self):
        # A strict assertion can only gate the lane if the step running the
        # script lets a non-zero exit through. continue-on-error or
        # `if: always()` would swallow it and the gate would be a no-op.
        cases = (
            ("test-plain-install.yml", PLAIN_E2E_STEP),
            ("test-luks-install.yml", LUKS_E2E_STEP),
        )
        for wf, name in cases:
            step = _find_step(_load_workflow(wf), name)
            self.assertNotIn(
                "continue-on-error", step,
                f"{name} in {wf} is continue-on-error (gate waved through)",
            )
            self.assertFalse(
                str(step.get("if", "")).startswith("always()"),
                f"{name} in {wf} is `if: always()` (gate waved through)",
            )

    def test_graduation_reporter_is_not_waved_through(self):
        # The weekly reporter must itself fail (not be continue-on-error) so a
        # broken check-fetch surfaces instead of silently reporting nothing.
        wf = _load_workflow("graduation-check.yml")
        for job in wf.get("jobs", {}).values():
            for step in job.get("steps", []):
                if "graduation-check.py" in (step.get("run", "") or ""):
                    self.assertNotIn("continue-on-error", step)

    def test_graduation_reporter_is_reachable_from_a_schedule(self):
        # A gate that never fires is not a gate: the reporter must have an
        # active trigger, and it must actually run the script.
        wf = _load_workflow("graduation-check.yml")
        triggers = wf[True] if True in wf else wf.get("on", {})
        self.assertIn("schedule", triggers, "no schedule trigger")
        self.assertTrue(triggers["schedule"], "schedule has no cron")
        self.assertIn("graduation-check.py", yaml.dump(wf))

    def test_criteria_config_lists_the_three_checks_and_a_bar(self):
        cfg = yaml.safe_load(CRITERIA.read_text())
        for cid in ("uefi-bootcurrent", "no-installer-flatpak", "luks-uuid"):
            self.assertIn(cid, cfg["checks"])
        bar = cfg["graduation"]
        self.assertGreaterEqual(bar["required_consecutive_passes"], 1)
        self.assertLessEqual(bar["maximum_failure_rate"], 0.5)
        self.assertGreaterEqual(bar["lookback_runs"], 1)
