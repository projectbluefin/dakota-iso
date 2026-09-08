"""Behavioural unit tests for scripts/build-iso.sh.

scripts/build-iso.sh is the named-flag wrapper that .github/workflows/
test-plain-install.yml calls to build the plain-install E2E ISO.  It translates

    scripts/build-iso.sh --squashfs <sfs> --boot-tar <tar> --output <iso>

into the *positional* interface of live/src/build-iso.sh

    live/src/build-iso.sh <boot-tar> <squashfs> <output-iso>

The argument order differs between the two interfaces, so a copy/paste slip in
the wrapper silently swaps the boot tar and the rootfs squashfs.  That produces
an ISO that builds successfully and then fails to boot, which only surfaces
hours later in the QEMU E2E lane.  These tests pin the translation and the
argument-validation behaviour.

The wrapper resolves its delegate as "$(dirname "$0")/../live/src/build-iso.sh",
so each test runs a copy of the real script inside a temporary
<root>/scripts + <root>/live/src tree whose delegate is a recording stub.  The
real live/src/build-iso.sh (which needs xorriso, mtools and root) is never run.

Covered behaviour
-----------------
1. Flags are translated to the documented positional order (boot-tar, squashfs,
   output) and the delegate is reached via exec.
2. Flag order on the command line does not matter.
3. Every flag is required; omitting any one prints usage and exits non-zero
   without invoking the delegate.
4. A flag given without a value fails instead of consuming the next flag.
5. Unknown/positional arguments are rejected rather than silently ignored.
6. Values containing spaces survive the translation as single arguments.
7. The wrapper propagates the delegate's exit status.
8. The flags used by .github/workflows/test-plain-install.yml are exactly the
   flags the wrapper accepts.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
WRAPPER = REPO / "scripts" / "build-iso.sh"
DELEGATE = REPO / "live" / "src" / "build-iso.sh"
PLAIN_INSTALL_WORKFLOW = REPO / ".github" / "workflows" / "test-plain-install.yml"

# Records every argument the delegate received, one per line, then exits with
# the status in STUB_EXIT_CODE (default 0).
STUB_DELEGATE = """#!/usr/bin/bash
: > "${ARGS_FILE}"
for a in "$@"; do
    printf '%s\\n' "$a" >> "${ARGS_FILE}"
done
exit "${STUB_EXIT_CODE:-0}"
"""


class BuildIsoWrapperHarness(unittest.TestCase):
    """Runs scripts/build-iso.sh against a recording stub delegate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="build-iso-args-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        root = Path(self.tmp)
        (root / "scripts").mkdir()
        (root / "live" / "src").mkdir(parents=True)

        self.wrapper = root / "scripts" / "build-iso.sh"
        shutil.copy2(WRAPPER, self.wrapper)

        self.stub = root / "live" / "src" / "build-iso.sh"
        self.stub.write_text(STUB_DELEGATE)
        self.stub.chmod(0o755)

        self.args_file = root / "delegate-args.txt"

    def run_wrapper(self, *args, stub_exit_code=0):
        env = dict(os.environ)
        env["ARGS_FILE"] = str(self.args_file)
        env["STUB_EXIT_CODE"] = str(stub_exit_code)
        return subprocess.run(
            ["bash", str(self.wrapper), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp,
        )

    def delegate_args(self):
        if not self.args_file.exists():
            return None
        text = self.args_file.read_text()
        return text.split("\n")[:-1] if text else []

    def assertDelegateNotCalled(self):
        self.assertIsNone(
            self.delegate_args(),
            "live/src/build-iso.sh must not run when argument validation fails",
        )


class TestArgumentTranslation(BuildIsoWrapperHarness):
    """The named flags map onto live/src/build-iso.sh's positional order."""

    def test_flags_translate_to_boot_tar_squashfs_output_order(self):
        result = self.run_wrapper(
            "--squashfs", "/build/dakota-live.squashfs",
            "--boot-tar", "/build/dakota-boot-files.tar",
            "--output", "/build/dakota-live.iso",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.delegate_args(),
            [
                "/build/dakota-boot-files.tar",
                "/build/dakota-live.squashfs",
                "/build/dakota-live.iso",
            ],
            "live/src/build-iso.sh takes <boot-tar> <squashfs> <output-iso>; "
            "swapping the first two builds an unbootable ISO",
        )

    def test_flag_order_does_not_change_positional_order(self):
        orderings = (
            ("--output", "/o.iso", "--boot-tar", "/b.tar", "--squashfs", "/s.sfs"),
            ("--boot-tar", "/b.tar", "--output", "/o.iso", "--squashfs", "/s.sfs"),
            ("--squashfs", "/s.sfs", "--output", "/o.iso", "--boot-tar", "/b.tar"),
        )
        for argv in orderings:
            with self.subTest(argv=argv):
                self.args_file.unlink(missing_ok=True)
                result = self.run_wrapper(*argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.delegate_args(), ["/b.tar", "/s.sfs", "/o.iso"]
                )

    def test_values_with_spaces_stay_single_arguments(self):
        result = self.run_wrapper(
            "--squashfs", "/var/iso build/live.squashfs",
            "--boot-tar", "/var/iso build/boot files.tar",
            "--output", "/var/iso build/out.iso",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.delegate_args(),
            [
                "/var/iso build/boot files.tar",
                "/var/iso build/live.squashfs",
                "/var/iso build/out.iso",
            ],
        )

    def test_last_occurrence_of_a_repeated_flag_wins(self):
        result = self.run_wrapper(
            "--squashfs", "/first.sfs",
            "--squashfs", "/second.sfs",
            "--boot-tar", "/b.tar",
            "--output", "/o.iso",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.delegate_args(), ["/b.tar", "/second.sfs", "/o.iso"])

    def test_delegate_exit_status_is_propagated(self):
        result = self.run_wrapper(
            "--squashfs", "/s.sfs",
            "--boot-tar", "/b.tar",
            "--output", "/o.iso",
            stub_exit_code=42,
        )
        self.assertEqual(
            result.returncode,
            42,
            "the wrapper execs the delegate, so a failed ISO build must not "
            "report success to the workflow step",
        )


class TestArgumentValidation(BuildIsoWrapperHarness):
    """Incomplete or malformed invocations fail loudly before any ISO work."""

    def test_no_arguments_prints_usage_and_fails(self):
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage: scripts/build-iso.sh", result.stderr)
        self.assertDelegateNotCalled()

    def test_each_required_flag_is_enforced(self):
        complete = {
            "--squashfs": "/s.sfs",
            "--boot-tar": "/b.tar",
            "--output": "/o.iso",
        }
        for omitted in complete:
            with self.subTest(omitted=omitted):
                self.args_file.unlink(missing_ok=True)
                argv = []
                for flag, value in complete.items():
                    if flag != omitted:
                        argv += [flag, value]
                result = self.run_wrapper(*argv)
                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"{omitted} missing must be an error, not an empty argument",
                )
                self.assertIn("Usage: scripts/build-iso.sh", result.stderr)
                self.assertDelegateNotCalled()

    def test_flag_without_a_value_fails(self):
        for flag in ("--squashfs", "--boot-tar", "--output"):
            with self.subTest(flag=flag):
                self.args_file.unlink(missing_ok=True)
                result = self.run_wrapper(flag)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires a path", result.stderr)
                self.assertDelegateNotCalled()

    def test_unknown_flag_is_rejected(self):
        result = self.run_wrapper(
            "--squashfs", "/s.sfs",
            "--boot-tar", "/b.tar",
            "--output", "/o.iso",
            "--oci-image", "ghcr.io/projectbluefin/dakota:latest",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument: --oci-image", result.stderr)
        self.assertDelegateNotCalled()

    def test_stray_positional_argument_is_rejected(self):
        result = self.run_wrapper(
            "/build/dakota-live.squashfs",
            "--boot-tar", "/b.tar",
            "--output", "/o.iso",
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "the wrapper takes flags only; a positional path is a caller bug",
        )
        self.assertIn("unknown argument:", result.stderr)
        self.assertDelegateNotCalled()


class TestWorkflowContract(unittest.TestCase):
    """The plain-install workflow and the wrapper agree on the flag set."""

    def test_wrapper_and_delegate_exist(self):
        self.assertTrue(WRAPPER.is_file(), f"{WRAPPER} is missing")
        self.assertTrue(DELEGATE.is_file(), f"{DELEGATE} is missing")

    def test_wrapper_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(WRAPPER)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plain_install_workflow_uses_only_supported_flags(self):
        workflow = PLAIN_INSTALL_WORKFLOW.read_text()
        # Capture the invocation only up to the end of its backslash
        # continuations, so unrelated later commands in the same step do not
        # leak flags into the comparison.
        invocation = re.search(
            r"bash scripts/build-iso\.sh((?:[^\n]*\\\n)*[^\n]*)",
            workflow,
        )
        self.assertIsNotNone(
            invocation,
            "test-plain-install.yml no longer calls scripts/build-iso.sh; "
            "update or drop this contract test",
        )
        used = set(re.findall(r"--[a-z-]+", invocation.group(1)))
        supported = set(re.findall(r"^\s*(--[a-z-]+)\)", WRAPPER.read_text(), re.M))
        self.assertEqual(
            supported,
            {"--squashfs", "--boot-tar", "--output"},
            "wrapper flag set changed; update the workflow call site too",
        )
        self.assertTrue(
            used <= supported,
            f"test-plain-install.yml passes unsupported flags: {sorted(used - supported)}",
        )
        self.assertEqual(
            used,
            supported,
            "test-plain-install.yml must pass every required flag",
        )


if __name__ == "__main__":
    unittest.main()
