"""Behavioural tests for .github/scripts/publish_screenshots.sh.

The LUKS E2E workflow publishes its screenshots to the ``ci-screenshots``
branch so PR comments can embed them. Committing one file per screenshot per
run grew that branch without bound (#382). The script instead replaces the
branch with a single parentless commit holding only the newest run.

These tests run the real script against a local bare repository and check:

1. Every publish leaves exactly one parentless commit on the branch.
2. A new run drops older runs' files; legs of the same run keep each other.
3. A branch that already has history is replaced, not appended to.
4. A leg that loses a push race re-reads the tip and keeps the winner's files
   instead of overwriting them.
5. An empty screenshot directory pushes nothing.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / ".github" / "scripts" / "publish_screenshots.sh"
BRANCH = "ci-screenshots"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class PublishScreenshotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="publish-screenshots-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {
            **os.environ,
            **GIT_ENV,
            "HOME": str(self.tmp),
            "PUBLISH_RETRY_DELAY": "0",
        }
        self.remote_dir = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", str(self.remote_dir))
        self.remote = self.remote_dir.as_uri()

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def remote_git(self, *args):
        return self.git("--git-dir", str(self.remote_dir), *args)

    def shots(self, name, files):
        d = self.tmp / name
        d.mkdir()
        for filename, content in files.items():
            (d / filename).write_bytes(content)
        return d

    def publish(self, run, leg, src, env=None):
        run_dir = f"luks-e2e/{run}"
        return subprocess.run(
            ["bash", str(SCRIPT), self.remote, BRANCH, run_dir, f"{run_dir}/{leg}", str(src)],
            env=env or self.env,
            capture_output=True,
            text=True,
        )

    def branch_files(self):
        out = self.remote_git("ls-tree", "-r", "--name-only", BRANCH)
        return sorted(out.splitlines())

    def assert_single_parentless_commit(self):
        self.assertEqual(self.remote_git("rev-list", "--count", BRANCH), "1")
        parents = self.remote_git("rev-list", "--parents", "-n", "1", BRANCH).split()
        self.assertEqual(len(parents), 1, f"tip has parents: {parents[1:]}")

    def test_first_publish_creates_branch_with_one_commit(self):
        src = self.shots("s1", {"live-boot.png": b"live", "final-boot.png": b"final"})
        result = self.publish("100", "dakota/dev", src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.remote_git("rev-parse", BRANCH))
        self.assert_single_parentless_commit()
        self.assertEqual(
            self.branch_files(),
            ["luks-e2e/100/dakota/dev/final-boot.png", "luks-e2e/100/dakota/dev/live-boot.png"],
        )

    def test_legs_of_one_run_are_kept_and_older_runs_dropped(self):
        self.publish("100", "dakota/dev", self.shots("a", {"live-boot.png": b"old"}))
        self.publish("200", "dakota/dev", self.shots("b", {"live-boot.png": b"dev"}))
        result = self.publish("200", "dakota/stable", self.shots("c", {"plymouth.png": b"st"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_single_parentless_commit()
        self.assertEqual(
            self.branch_files(),
            ["luks-e2e/200/dakota/dev/live-boot.png", "luks-e2e/200/dakota/stable/plymouth.png"],
        )

    def test_republishing_a_leg_replaces_its_files(self):
        self.publish("100", "dakota/dev", self.shots("a", {"live-boot.png": b"1", "plymouth.png": b"1"}))
        result = self.publish("100", "dakota/dev", self.shots("b", {"final-boot.png": b"2"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.branch_files(), ["luks-e2e/100/dakota/dev/final-boot.png"])

    def test_existing_history_is_replaced(self):
        seed = self.tmp / "seed"
        self.git("init", "-q", str(seed))
        for i in range(3):
            (seed / f"old-{i}.png").write_bytes(bytes([i]))
            self.git("add", ".", cwd=seed)
            self.git("commit", "-q", "-m", f"old {i}", cwd=seed)
        self.git("push", "-q", self.remote, f"HEAD:refs/heads/{BRANCH}", cwd=seed)

        result = self.publish("300", "dakota/dev", self.shots("s", {"live-boot.png": b"new"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_single_parentless_commit()
        self.assertEqual(self.branch_files(), ["luks-e2e/300/dakota/dev/live-boot.png"])

    def test_losing_a_push_race_keeps_the_winning_legs_files(self):
        # Prepare the commit the other leg of the same run pushes first.
        rival = self.tmp / "rival"
        self.git("init", "-q", str(rival))
        (rival / "luks-e2e/400/dakota/stable").mkdir(parents=True)
        (rival / "luks-e2e/400/dakota/stable/final-boot.png").write_bytes(b"stable")
        self.git("add", ".", cwd=rival)
        self.git("commit", "-q", "-m", "rival", cwd=rival)

        # A git wrapper that lands the rival commit just before this leg's
        # first push, after this leg has already read the (absent) tip.
        real_git = shutil.which("git")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        marker = self.tmp / "raced"
        wrapper = bin_dir / "git"
        wrapper.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                if [[ "$1" == push && ! -e {marker} ]]; then
                  touch {marker}
                  {real_git} -C {rival} push -q --force {self.remote} HEAD:refs/heads/{BRANCH}
                fi
                exec {real_git} "$@"
                """
            )
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        env = {**self.env, "PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}"}

        result = self.publish("400", "dakota/dev", self.shots("s", {"live-boot.png": b"dev"}), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "race wrapper never ran")
        self.assertIn("rejected", result.stderr)
        self.assert_single_parentless_commit()
        self.assertEqual(
            self.branch_files(),
            ["luks-e2e/400/dakota/dev/live-boot.png", "luks-e2e/400/dakota/stable/final-boot.png"],
        )

    def test_empty_source_publishes_nothing(self):
        result = self.publish("500", "dakota/dev", self.shots("empty", {}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")
        self.assertEqual(self.remote_git("for-each-ref", f"refs/heads/{BRANCH}"), "")


if __name__ == "__main__":
    unittest.main()
