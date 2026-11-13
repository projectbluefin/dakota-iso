"""Regression tests for the serial-log boot check in the `plain-verify-qemu`
justfile recipe (#313).

The recipe runs under `set -euo pipefail` and used to test the serial log with
`echo "$LOG" | grep -q PATTERN`. Once the log is large, `grep -q` exits at the
first match, `echo` is killed by SIGPIPE, the pipeline returns 141, and `if`
treats a real match as "no match" — so a boot that printed `login:` was
reported as a 5-minute timeout.

These tests run the exact `if` conditions shipped in the recipe against a
multi-megabyte log with the match near the start.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
JUSTFILE = REPO / "justfile"
RECIPE_HEADER = "plain-verify-qemu target:"

# Bigger than any pipe buffer, so a piped `echo` is still writing when
# `grep -q` matches and exits.
FILLER = "[   12.345678] systemd[1]: Started some-unit.service - Some Unit.\n" * 40000


def _recipe_body() -> str:
    text = JUSTFILE.read_text()
    rest = text[text.index(RECIPE_HEADER) + len(RECIPE_HEADER):]
    # Recipe body ends at the next non-indented line (comment or recipe).
    m = re.search(r"\n(?=[^\s])", rest)
    return rest if m is None else rest[:m.start()]


def _grep_conditions(body: str) -> list[str]:
    """Return the `if <cond>; then` conditions that grep the serial log."""
    return [
        m.group(1)
        for m in re.finditer(r"^\s*if (.*grep -q.*); then$", body, re.M)
        if '"$LOG"' in m.group(1)
    ]


def _condition_matching(conditions: list[str], needle: str) -> str:
    for cond in conditions:
        if needle in cond:
            return cond
    raise AssertionError(f"no serial-log check mentioning {needle!r} in {RECIPE_HEADER}")


def _run_condition(cond: str, log_text: str) -> str:
    """Run the recipe's condition the way the recipe does: LOG read from the
    serial file under `set -euo pipefail`."""
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        fh.write(log_text)
        serial = fh.name
    try:
        script = (
            "set -euo pipefail\n"
            'LOG=$(cat "$1" 2>/dev/null || true)\n'
            f"if {cond}; then echo MATCH; else echo NOMATCH; fi\n"
        )
        result = subprocess.run(
            ["bash", "-c", script, "_", serial],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        Path(serial).unlink()
    return result.stdout.strip()


class TestPlainVerifyBootCheck(unittest.TestCase):
    def setUp(self):
        self.conditions = _grep_conditions(_recipe_body())
        self.assertEqual(
            len(self.conditions), 2,
            f"expected the boot-target and emergency checks, got {self.conditions}",
        )

    def test_large_log_with_login_prompt_is_a_match(self):
        cond = _condition_matching(self.conditions, "login:")
        log = "\ndakota-plain-test login: \n" + FILLER
        self.assertGreaterEqual(len(log), 2 * 1024 * 1024)
        self.assertEqual(_run_condition(cond, log), "MATCH")

    def test_large_log_with_kernel_panic_is_a_match(self):
        cond = _condition_matching(self.conditions, "Kernel panic")
        log = "Kernel panic - not syncing: VFS: Unable to mount root fs\n" + FILLER
        self.assertEqual(_run_condition(cond, log), "MATCH")

    def test_large_log_without_markers_is_not_a_match(self):
        for cond in self.conditions:
            self.assertEqual(_run_condition(cond, FILLER), "NOMATCH", cond)

    def test_no_echo_var_pipe_into_grep_q_in_justfile(self):
        # Every recipe in the justfile runs under pipefail; piping a variable
        # into `grep -q` is a false negative once the variable is large.
        offenders = [
            line.strip()
            for line in JUSTFILE.read_text().splitlines()
            if re.search(r'echo\s+"\$\{?\w+\}?"\s*\|\s*grep\s+-\w*q', line)
        ]
        self.assertEqual(offenders, [], "use grep -q PATTERN <<<\"$VAR\" instead")


if __name__ == "__main__":
    unittest.main()
