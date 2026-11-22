"""e2e-infra-rerun.py may re-run a failed E2E run only when every failure is infra.

A false "infra" re-runs a product defect and hides it for another two hours; a
false "product" just leaves a run red. So the tests pin both directions with
real job logs (tests/fixtures/e2e-infra, trimmed from the runs they name), and
drive the log download against a local HTTP server.
"""
import contextlib
import http.server
import importlib.util
import io
import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/e2e-infra"
WORKFLOWS = ROOT / ".github/workflows"
spec = importlib.util.spec_from_file_location("e2e_infra_rerun", ROOT / "scripts/e2e-infra-rerun.py")
rerun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rerun)

E2E_WORKFLOW_FILES = ("build-iso.yml", "build-iso-utah.yml", "test-luks-install.yml", "test-plain-install.yml")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def failing(*lines: str) -> str:
    """A job log whose one step fails after printing `lines`."""
    return "\n".join([
        "2026-11-19T14:00:00Z ##[group]Run set -euo pipefail",
        *(f"2026-11-19T14:00:01Z {line}" for line in lines),
        "2026-11-19T14:00:02Z ##[error]Process completed with exit code 1.",
    ])


class ClassifyTests(unittest.TestCase):
    def test_a_ghcr_blob_500_during_an_image_pull_is_infra(self):
        # projectbluefin/utah run 44538634534, the run the utah script was written for.
        name, evidence = rerun.classify(fixture("ghcr-blob-500.log"))
        self.assertEqual(name, "registry-5xx")
        self.assertIn("fetching blob: received unexpected HTTP status: 500", evidence)

    def test_dakota_iso_product_failures_are_not_infra(self):
        # Real failing steps: Build and Publish Dakota Live ISO run 43550102030
        # (installed system never reachable over SSH) and LUKS Install
        # End-to-End Test run 44904654448 (bootc install ran out of space).
        for name in ("plain-verify-ssh-timeout.log", "luks-install-enospc.log"):
            with self.subTest(name):
                self.assertEqual(rerun.classify(fixture(name)), (None, ""))

    def test_a_job_with_no_log_is_infra(self):
        self.assertEqual(rerun.classify(None)[0], rerun.NO_LOGS)

    def test_each_signature_matches_its_recorded_wording(self):
        samples = {
            "runner-shutdown": "##[error]The runner has received a shutdown signal. This can happen when "
                               "the runner service is stopped, or a manually started runner is canceled.",
            "registry-5xx": 'Error: logging into "ghcr.io": invalid status code from registry '
                            "503 (Service Unavailable)",
            "sigstore-5xx": "Error: signing: POST https://rekor.sigstore.dev/api/v1/log/entries: "
                            "502 Bad Gateway",
        }
        self.assertEqual(set(samples), {s.name for s in rerun.SIGNATURES})
        for name, line in samples.items():
            with self.subTest(name):
                self.assertEqual(rerun.classify(failing(line)), (name, f"2026-11-19T14:00:01Z {line}"))

    def test_near_misses_stay_unexplained(self):
        for line in (
            "ghcr.io/projectbluefin/dakota-nvidia@sha256:5003abc pulled",  # digits in a digest
            "received unexpected HTTP status: 404 Not Found",              # a missing image is not transient
            "Error: no matching signatures: rekor.sigstore.dev entry not found",  # a bad signature is a product fault
            "fisherman: fatal: required local source is absent or unreadable",
        ):
            with self.subTest(line):
                self.assertIsNone(rerun.classify(failing(line))[0])

    def test_only_the_first_failing_step_is_judged(self):
        # A step that recovered from a 500 must not excuse a later product failure.
        log = "\n".join([
            "##[group]Run sudo podman pull",
            "Error: fetching blob: received unexpected HTTP status: 500 Internal Server Error",
            "Trying again... done",
            "##[group]Run sudo env PATH=\"$PATH\" just plain-enospc-gate dakota",
            "Bound OCI media preflight FAILED",
            "##[error]Process completed with exit code 1.",
            "##[group]Run upload diagnostics",
            "received unexpected HTTP status: 503",
            "##[error]upload failed",
        ])
        self.assertIsNone(rerun.classify(log)[0])

    def test_a_log_without_an_error_line_is_judged_whole(self):
        log = "##[group]Run x\nThe runner has received a shutdown signal\n"
        self.assertEqual(rerun.classify(log)[0], "runner-shutdown")


class DecideTests(unittest.TestCase):
    def logs(self, mapping):
        return lambda job_id: mapping[job_id]

    def test_every_failure_infra_means_re_run(self):
        jobs = [{"id": 1, "name": "LUKS E2E dakota (dev)"}, {"id": 2, "name": "LUKS E2E dakota (stable)"}]
        go, verdicts = rerun.decide(jobs, self.logs({1: fixture("ghcr-blob-500.log"), 2: None}))
        self.assertTrue(go)
        self.assertEqual([v.signature for v in verdicts], ["registry-5xx", "no-logs"])

    def test_one_unexplained_failure_leaves_the_run_red(self):
        jobs = [{"id": 1, "name": "LUKS E2E dakota (dev)"}, {"id": 2, "name": "LUKS E2E dakota (stable)"}]
        go, verdicts = rerun.decide(jobs, self.logs({1: fixture("ghcr-blob-500.log"),
                                                     2: fixture("luks-install-enospc.log")}))
        self.assertFalse(go)
        self.assertIsNone(verdicts[1].signature)
        self.assertIn("unexplained", rerun.summary("u", go, verdicts))

    def test_no_failed_jobs_means_no_re_run(self):
        self.assertEqual(rerun.decide([], self.logs({})), (False, []))


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []
    blob = b"log body\n"

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith("/jobs/1/logs"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/blob/1")
            self.end_headers()
        elif self.path == "/blob/1":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(type(self).blob)
        elif self.path.endswith("/jobs/2/logs"):
            self.send_error(404)
        elif self.path.endswith("/jobs/3/logs"):
            self.send_error(500)
        elif "/actions/runs/7/jobs" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"jobs": [{"id": 1, "name": "a", "conclusion": "failure"},'
                             b' {"id": 2, "name": "b", "conclusion": "success"},'
                             b' {"id": 3, "name": "c", "conclusion": "skipped"},'
                             b' {"id": 4, "name": "d", "conclusion": "cancelled"}]}')
        elif "/actions/runs/8/jobs" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"jobs": [{"id": 1, "name": "a", "conclusion": "failure"},'
                             b' {"id": 2, "name": "b", "conclusion": "failure"}]}')

    def log_message(self, *args):
        pass


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.original_api = rerun.API
        rerun.API = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(setattr, rerun, "API", self.original_api)
        _Handler.seen = []
        _Handler.blob = b"log body\n"

    def test_the_log_redirect_is_followed_without_the_token(self):
        self.assertEqual(rerun.job_log("o/r", 1, "secret"), "log body\n")
        self.assertEqual(_Handler.seen, [("/repos/o/r/actions/jobs/1/logs", "Bearer secret"),
                                         ("/blob/1", None)])

    def test_a_missing_log_is_none_and_any_other_error_raises(self):
        self.assertIsNone(rerun.job_log("o/r", 2, "t"))
        with self.assertRaises(Exception):
            rerun.job_log("o/r", 3, "t")

    def test_only_failed_and_cancelled_jobs_are_classified(self):
        self.assertEqual([j["id"] for j in rerun.failed_jobs("o/r", "7", "t")], [1, 4])

    def test_main_writes_the_decision_to_github_output(self):
        # Run 8 has two failed jobs: job 1's log is the blob below, job 2 has no log.
        for blob, expected in (("ghcr-blob-500.log", "infra=true\n"),
                               ("plain-verify-ssh-timeout.log", "infra=false\n")):
            with self.subTest(blob), tempfile.TemporaryDirectory() as tmp:
                _Handler.blob = fixture(blob).encode()
                output = Path(tmp) / "output"
                env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t", "GITHUB_OUTPUT": str(output)}
                with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                    self.assertEqual(rerun.main(["e2e-infra-rerun.py", "8"]), 0)
                self.assertEqual(output.read_text(), expected)


class WorkflowTests(unittest.TestCase):
    workflow = yaml.safe_load((WORKFLOWS / "rerun-e2e-infra-failures.yml").read_text())
    alert = yaml.safe_load((WORKFLOWS / "ci-alert.yml").read_text())

    def test_it_follows_every_e2e_lane_by_its_real_name(self):
        # A renamed workflow would silently stop being re-run.
        names = [yaml.safe_load((WORKFLOWS / f).read_text())["name"] for f in E2E_WORKFLOW_FILES]
        self.assertEqual(sorted(self.workflow[True]["workflow_run"]["workflows"]), sorted(names))

    def test_only_a_first_attempt_of_a_main_line_event_is_classified(self):
        condition = self.workflow["jobs"]["classify"]["if"]
        self.assertIn("github.event.workflow_run.run_attempt == 1", condition)
        self.assertIn("github.event.workflow_run.conclusion == 'failure'", condition)
        self.assertNotIn("merge_group", condition)
        self.assertNotIn("pull_request", condition)

    def test_the_re_run_waits_on_an_environment_and_checks_for_a_newer_run(self):
        job = self.workflow["jobs"]["rerun"]
        self.assertEqual(job["environment"], "e2e-infra-retry-wait")
        self.assertIn("needs.classify.outputs.infra == 'true'", job["if"])
        script = job["steps"][0]["run"]
        self.assertNotIn("sleep", script)
        self.assertIn('gh run rerun "$RUN_ID" --failed', script)
        self.assertIn('"completed 1"', script)
        # Any event counts as newer: an older ISO build must not publish over a newer one.
        self.assertIn('-f branch="$BRANCH"', script)
        self.assertNotIn("event=", script)

    def test_only_the_re_run_job_can_write(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["jobs"]["classify"]["permissions"],
                         {"contents": "read", "actions": "read"})
        self.assertEqual(self.workflow["jobs"]["rerun"]["permissions"], {"actions": "write"})

    def test_ci_alert_holds_back_only_for_runs_the_re_run_will_replace(self):
        steps = self.alert["jobs"]["alert"]["steps"]
        classify = next(s for s in steps if s.get("id") == "infra")
        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        # A classifier failure must fall back to alerting, never fail the alert job.
        self.assertTrue(classify["continue-on-error"])
        self.assertTrue(checkout["continue-on-error"])
        for step in (classify, checkout):
            self.assertIn("github.event.workflow_run.run_attempt == 1", step["if"])
            for name in self.workflow[True]["workflow_run"]["workflows"]:
                self.assertIn(f'"{name}"', step["if"])
            # Lint and Test are never re-run, so their alerts are never held back.
            self.assertNotIn('"Lint"', step["if"])
            self.assertNotIn('"Test"', step["if"])
        create = next(s for s in steps if s["name"] == "Create or reopen alert issue")
        self.assertIn("steps.infra.outputs.infra != 'true'", create["if"])


if __name__ == "__main__":
    unittest.main()
