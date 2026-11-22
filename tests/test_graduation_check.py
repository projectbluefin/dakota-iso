"""Tests for scripts/graduation-check.py: the measured graduation bar that
recommends which post-boot assertions qualify to graduate to STRICT_IDS
(dakota#381).

The bar is measured offline against a run-history fixture (no `gh`, no
network), so it is deterministic and CI-runnable. The live path (fetching
TAP lines from the last 20 scheduled E2E runs) is covered by the same
evaluate() logic through the offline fixture.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "graduation-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("graduation_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["graduation_check"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class TestBar(unittest.TestCase):
    def setUp(self):
        self.bar = mod.Bar.from_config(mod.load_criteria())

    def test_all_pass_qualifies(self):
        ok, fails, total, _ = mod.evaluate(["ok"] * 20, self.bar)
        self.assertTrue(ok)
        self.assertEqual((fails, total), (0, 20))

    def test_old_failures_within_rate_qualify(self):
        # 2 of 20 failed but 18 consecutive recent passes: the bar is both
        # 3 consecutive AND <=10% failures, so this qualifies.
        ok, *_ = mod.evaluate(["not ok", "not ok"] + ["ok"] * 18, self.bar)
        self.assertTrue(ok)

    def test_too_many_failures_does_not_qualify(self):
        ok, *_ = mod.evaluate(["not ok"] * 3 + ["ok"] * 17, self.bar)
        self.assertFalse(ok)

    def test_recent_failure_breaks_the_streak(self):
        # 1 failure most-recent: rate is fine (5%) but the streak is 0 < 3.
        ok, *_ = mod.evaluate(["ok"] * 19 + ["not ok"], self.bar)
        self.assertFalse(ok)

    def test_two_recent_failures_still_short_of_streak(self):
        ok, *_ = mod.evaluate(["ok"] * 18 + ["not ok"] * 2, self.bar)
        self.assertFalse(ok)

    def test_empty_window_does_not_qualify(self):
        ok, *_ = mod.evaluate([], self.bar)
        self.assertFalse(ok)


class TestParseTap(unittest.TestCase):
    def test_last_occurrence_wins(self):
        self.assertEqual(mod.parse_tap("ok - a\nnot ok - b\nok - a"),
                         {"a": "ok", "b": "not ok"})

    def test_only_id_lines_parsed(self):
        self.assertEqual(mod.parse_tap("ok - a\nsome log\n::warning:: b\n"), {"a": "ok"})

    def test_timestamp_prefixed_lines_match(self):
        # Actions logs prefix each line with an ISO timestamp; the verdict must
        # still be parsed.
        log = (
            "2026-11-22T07:00:01.1234567Z ok - luks-uuid\n"
            "2026-11-22T07:00:02.6543210Z not ok - uefi-bootcurrent\n"
        )
        self.assertEqual(
            mod.parse_tap(log),
            {"luks-uuid": "ok", "uefi-bootcurrent": "not ok"},
        )


class TestOfflineRun(unittest.TestCase):
    def run_cli(self, fixture, *extra):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(fixture, fh)
            path = fh.name
        try:
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--runs", path, "--json", *extra],
                capture_output=True, text=True, cwd=REPO,
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def test_offline_json_reports_pass_rates(self):
        fixture = {
            "uefi-bootcurrent": ["ok"] * 20,
            "no-installer-flatpak": ["not ok", "not ok"] + ["ok"] * 18,
            "luks-uuid": ["not ok"] * 3 + ["ok"] * 17,
        }
        proc = self.run_cli(fixture)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)["checks"]
        self.assertTrue(report["uefi-bootcurrent"]["qualified"])
        self.assertTrue(report["no-installer-flatpak"]["qualified"])
        self.assertFalse(report["luks-uuid"]["qualified"])

    def test_offline_never_opens_issues(self):
        # Offline mode measures and reports only; it must not attempt to open an
        # issue (no repo / no gh in a unit test) nor complain about a missing
        # repository.
        proc = self.run_cli({"uefi-bootcurrent": ["ok"] * 20})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("no repository", proc.stderr)


class TestLiveGhCalls(unittest.TestCase):
    """The live gh path is not exercised by the offline fixture, so assert the
    exact argv here. ``gh api`` takes no ``--repo`` flag (unlike ``gh run list``
    / ``gh issue``), so the endpoint carries the repo and we call ``gh``
    directly instead of through ``run_gh`` (dakota#392)."""

    def _run_with_stub(self, fn):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        orig = subprocess.run
        subprocess.run = fake_run
        try:
            fn()
        finally:
            subprocess.run = orig
        return calls

    def test_fetch_run_log_calls_api_without_repo_flag(self):
        calls = self._run_with_stub(
            lambda: mod.fetch_run_log("projectbluefin/dakota-iso", 12345)
        )
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[0], "gh")
        self.assertNotIn("--repo", cmd)          # gh api has no --repo flag
        self.assertEqual(cmd[1], "api")
        self.assertTrue(cmd[2].endswith("/actions/runs/12345/logs"))

    def test_list_runs_keeps_repo_flag(self):
        calls = self._run_with_stub(
            lambda: mod.list_runs("projectbluefin/dakota-iso")
        )
        self.assertEqual(len(calls), 2)          # one per E2E workflow
        for cmd in calls:
            self.assertEqual(cmd[0], "gh")
            self.assertIn("--repo", cmd)          # gh run list accepts --repo
            self.assertIn("run", cmd)             # gh run list
            self.assertIn("--workflow", cmd)


if __name__ == "__main__":
    unittest.main()
