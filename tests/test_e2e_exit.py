"""Coverage for scripts/e2e-exit.sh and the E2E workflows that rely on it.

The helper gives the plain and LUKS E2E harnesses numbered phase exit codes
and a wall-clock deadline that fires before GitHub's timeout-minutes kill, so
a hung lane exits 75 while the serial-log and screenshot uploads still run.
"""

import re
import subprocess
import time
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
HELPER = REPO / "scripts" / "e2e-exit.sh"
WORKFLOWS = REPO / ".github" / "workflows"
JUSTFILE = REPO / "justfile"


def run_harness(body, env=None, timeout=30):
    """Run a bash harness that sources the helper, return the CompletedProcess."""
    script = f"set -euo pipefail\nsource '{HELPER}'\n{body}\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


HARNESS_TRAP = "trap 'rc=$(e2e_status $?); exit \"$rc\"' EXIT\n"


class PhaseExitCodes(unittest.TestCase):
    def test_success_exits_zero(self):
        r = run_harness(HARNESS_TRAP + "E2E_PHASE_EXIT=12\ntrue")
        self.assertEqual(r.returncode, 0)

    def test_set_e_trip_reports_phase_code(self):
        r = run_harness(HARNESS_TRAP + "E2E_PHASE_EXIT=13\nfalse")
        self.assertEqual(r.returncode, 13)

    def test_explicit_exit_code_wins_over_phase(self):
        r = run_harness(HARNESS_TRAP + "E2E_PHASE_EXIT=12\ne2e_exit 77 missing qemu")
        self.assertEqual(r.returncode, 77)
        self.assertIn("FAIL: missing qemu", r.stderr)

    def test_failure_before_first_phase_is_one(self):
        r = run_harness(HARNESS_TRAP + "false")
        self.assertEqual(r.returncode, 1)

    def test_missing_dependency_exits_77(self):
        r = run_harness(HARNESS_TRAP + "e2e_require definitely-not-a-real-command-xyz")
        self.assertEqual(r.returncode, 77)

    def test_every_phase_code_has_a_name(self):
        for code, name in {
            "0": "passed",
            "11": "live ISO boot",
            "12": "install",
            "13": "installed-disk boot",
            "14": "LUKS unlock",
            "15": "post-install verification",
            "75": "wall-clock deadline reached",
            "77": "missing host dependency",
        }.items():
            r = run_harness(f"e2e_phase_name {code}")
            self.assertEqual(r.stdout.strip(), name, code)


class WallClockDeadline(unittest.TestCase):
    HANDLER = (
        "on_deadline() { echo evidence > \"$OUT\"; "
        "e2e_exit \"$E2E_EXIT_DEADLINE\" \"deadline in phase $E2E_PHASE_EXIT\"; }\n"
    )

    def test_no_deadline_is_a_noop(self):
        r = run_harness(HARNESS_TRAP + self.HANDLER + "e2e_deadline_start on_deadline\nE2E_PHASE_EXIT=11")
        self.assertEqual(r.returncode, 0)

    def test_blocked_harness_exits_75_and_runs_handler(self):
        out = Path(self.id().replace(".", "_") + ".evidence")
        out = Path("/tmp") / out.name
        out.unlink(missing_ok=True)
        deadline = int(time.time()) + 2
        start = time.monotonic()
        # `sleep 600` blocks the trap; the watcher must kill it after the grace.
        r = run_harness(
            HARNESS_TRAP + self.HANDLER
            + "e2e_deadline_start on_deadline\nE2E_PHASE_EXIT=12\nsleep 600",
            env={
                "E2E_WALL_CLOCK_DEADLINE": str(deadline),
                "E2E_DEADLINE_GRACE": "1",
                "OUT": str(out),
            },
        )
        self.assertEqual(r.returncode, 75, r.stderr)
        self.assertIn("deadline in phase 12", r.stderr)
        self.assertEqual(out.read_text().strip(), "evidence")
        self.assertLess(time.monotonic() - start, 20)
        out.unlink()

    def test_deadline_already_passed_exits_75(self):
        r = run_harness(
            HARNESS_TRAP + self.HANDLER + "e2e_deadline_start on_deadline",
            env={"E2E_WALL_CLOCK_DEADLINE": "1", "OUT": "/dev/null"},
        )
        self.assertEqual(r.returncode, 75)

    def test_malformed_deadline_is_usage_error(self):
        r = run_harness(
            HARNESS_TRAP + self.HANDLER + "e2e_deadline_start on_deadline",
            env={"E2E_WALL_CLOCK_DEADLINE": "soon", "OUT": "/dev/null"},
        )
        self.assertEqual(r.returncode, 1)


class WorkflowWiring(unittest.TestCase):
    """The deadline must track the real timeouts, or it fires after the kill."""

    def _steps(self, workflow, job):
        data = yaml.safe_load((WORKFLOWS / workflow).read_text())
        return data["jobs"][job], data["jobs"][job]["steps"]

    def _check(self, workflow, job, step_id):
        job_def, steps = self._steps(workflow, job)
        deadline = [s for s in steps if s.get("name") == "Set the job deadline"]
        self.assertEqual(len(deadline), 1, workflow)
        self.assertEqual(
            int(deadline[0]["env"]["TIMEOUT_MINUTES"]), int(job_def["timeout-minutes"]), workflow
        )
        names = [s.get("id") or s.get("name") for s in steps]
        self.assertLess(names.index("Set the job deadline"), names.index(step_id), workflow)
        e2e = next(s for s in steps if s.get("id") == step_id)
        self.assertIn("exit_code=$rc", e2e["run"])
        if "timeout-minutes" in e2e:
            self.assertEqual(
                int(e2e["env"]["STEP_TIMEOUT_MINUTES"]), int(e2e["timeout-minutes"]), workflow
            )

    def test_plain_workflow(self):
        self._check("test-plain-install.yml", "plain-e2e", "plain_test")

    def test_luks_workflow(self):
        self._check("test-luks-install.yml", "luks-e2e", "luks_test")


class HarnessPhases(unittest.TestCase):
    def _recipe(self, name):
        text = JUSTFILE.read_text()
        m = re.search(rf"^{re.escape(name)} .*?:\n(.*?)(?=^\S)", text, re.M | re.S)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def test_luks_harness_sets_phases_in_order(self):
        body = self._recipe("luks-test-qemu")
        self.assertIn("source scripts/e2e-exit.sh", body)
        self.assertIn("e2e_deadline_start on_deadline", body)
        self.assertEqual(re.findall(r"E2E_PHASE_EXIT=(\d+)", body), ["11", "12", "13", "14", "15"])

    def test_plain_harness_sets_phases_in_order(self):
        body = self._recipe("plain-test-qemu")
        self.assertIn("source scripts/e2e-exit.sh", body)
        self.assertIn("e2e_deadline_start on_deadline", body)
        self.assertEqual(re.findall(r"E2E_PHASE_EXIT=(\d+)", body), ["11", "12", "13", "15"])


if __name__ == "__main__":
    unittest.main()
