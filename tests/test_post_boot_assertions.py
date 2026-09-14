"""Tests for scripts/verify-post-install.sh and the --enable-debug-ssh path
added to scripts/fisherman-install.sh for projectbluefin/dakota#651
("[fisherman] e2e: add post-boot assertions for UEFI entries, Flatpak
exclusion, and LUKS cmdline format").

These are static/logic tests only — they do not boot QEMU. The real
end-to-end proof is `just plain-test-qemu <target>` / `just luks-test-qemu
<target>` reporting "All post-boot assertions passed" (see docs/luks-testing.md
and docs/skills/e2e-ci.md).
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
VERIFY_SCRIPT = REPO / "scripts" / "verify-post-install.sh"
FISHERMAN_INSTALL = REPO / "scripts" / "fisherman-install.sh"
PLAIN_INSTALL_QEMU = REPO / "scripts" / "plain-install-qemu.sh"
LUKS_INSTALL_QEMU = REPO / "scripts" / "luks-install-qemu.sh"
JUSTFILE = REPO / "justfile"


def _extract_luks_cmdline_regex(script_text: str) -> str:
    """Pull the rd.luks grep -E pattern straight out of verify-post-install.sh
    so the test exercises the actual pattern shipped, not a hand-copied one."""
    match = re.search(
        r"grep -qE '([^']*rd\\\.luks\\\.[^']+)'", script_text
    )
    assert match, "could not find the rd.luks grep -qE pattern in verify-post-install.sh"
    return match.group(1)


class TestVerifyPostInstallScript(unittest.TestCase):
    """Static checks on the new post-boot assertion script."""

    def test_script_exists_and_is_executable(self):
        self.assertTrue(VERIFY_SCRIPT.exists(), f"Missing {VERIFY_SCRIPT}")
        self.assertTrue(
            VERIFY_SCRIPT.stat().st_mode & 0o111,
            "scripts/verify-post-install.sh must be executable",
        )

    def test_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(VERIFY_SCRIPT)], capture_output=True, text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"scripts/verify-post-install.sh syntax error:\n{result.stderr}",
        )

    def test_checks_all_three_dakota_651_assertions(self):
        text = VERIFY_SCRIPT.read_text()
        self.assertIn("efibootmgr", text, "missing UEFI boot entry assertion (fisherman #2)")
        self.assertIn("org.bootcinstaller", text, "missing installer Flatpak exclusion assertion (fisherman #1)")
        self.assertIn("/proc/cmdline", text, "missing LUKS cmdline assertion (common#385)")

    def test_luks_cmdline_regex_matches_uuid_and_name_forms(self):
        """The regex must accept both rd.luks.uuid= and rd.luks.name= forms,
        and reject cmdlines that carry neither (the common#385 regression)."""
        pattern = _extract_luks_cmdline_regex(VERIFY_SCRIPT.read_text())

        good_cmdlines = [
            "root=/dev/mapper/luks-root rd.luks.uuid=1b4e28ba-2fa1-11d2-883f-b9a761bde3fb ro",
            "rd.luks.name=1b4e28ba-2fa1-11d2-883f-b9a761bde3fb=root ro quiet",
        ]
        bad_cmdlines = [
            "root=/dev/vda2 ro quiet splash",
            "console=ttyS0 rd.luks=1",  # no uuid/name key, must not match
        ]

        for cmdline in good_cmdlines:
            result = subprocess.run(
                ["bash", "-c", f"echo '{cmdline}' | grep -qE '{pattern}'"],
            )
            self.assertEqual(
                result.returncode, 0,
                f"expected LUKS cmdline regex to match: {cmdline!r}",
            )

        for cmdline in bad_cmdlines:
            result = subprocess.run(
                ["bash", "-c", f"echo '{cmdline}' | grep -qE '{pattern}'"],
            )
            self.assertNotEqual(
                result.returncode, 0,
                f"expected LUKS cmdline regex to NOT match: {cmdline!r}",
            )

    def test_flatpak_assertion_treats_empty_output_as_pass(self):
        """flatpak list | grep org.bootcinstaller producing no output must be
        treated as a PASS (installer flatpak correctly excluded)."""
        text = VERIFY_SCRIPT.read_text()
        self.assertIn(
            'if [[ -z "$FLATPAK_OUT" ]]', text,
            "flatpak exclusion check must treat empty grep output as success",
        )

    def test_efibootmgr_assertion_requires_both_bootcurrent_and_boot_entry(self):
        text = VERIFY_SCRIPT.read_text()
        self.assertIn("BootCurrent", text)
        self.assertIn(r"Boot[0-9A-Fa-f]{4}", text)


class TestFishermanInstallDebugSSH(unittest.TestCase):
    """The --enable-debug-ssh path must stay opt-in, never unconditional."""

    def test_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(FISHERMAN_INSTALL)], capture_output=True, text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"scripts/fisherman-install.sh syntax error:\n{result.stderr}",
        )

    def test_debug_ssh_flag_is_opt_in(self):
        text = FISHERMAN_INSTALL.read_text()
        self.assertIn('ENABLE_DEBUG_SSH=0', text)
        self.assertIn('--enable-debug-ssh', text)

    def test_root_password_and_sshd_enable_are_inside_debug_guard(self):
        """root:root / PermitRootLogin / sshd preset must only be *written to
        disk* when ENABLE_DEBUG_SSH=1 — never unconditionally on every install.
        (Comment/docstring mentions of these terms don't count as a leak.)"""
        lines = FISHERMAN_INSTALL.read_text().splitlines()
        code_lines = [ln for ln in lines if not ln.strip().startswith("#")]
        text = "\n".join(code_lines)

        guard_start = text.find('if [[ "$ENABLE_DEBUG_SSH" -eq 1 ]]')
        self.assertNotEqual(guard_start, -1, "missing ENABLE_DEBUG_SSH guard block")

        guarded_slice = text[guard_start:]
        end = guarded_slice.find("\n    else\n")
        guarded_slice = guarded_slice[:end] if end != -1 else guarded_slice

        for marker in ("openssl passwd", "PermitRootLogin", "enable sshd.service"):
            self.assertIn(marker, guarded_slice, f"{marker!r} not found inside debug guard")

        before_guard = text[:guard_start]
        for marker in ("PermitRootLogin", "enable sshd.service"):
            self.assertNotIn(
                marker, before_guard,
                f"{marker!r} appears (in code) before the ENABLE_DEBUG_SSH guard — must not leak unconditionally",
            )

    def test_install_qemu_scripts_pass_enable_debug_ssh_flag(self):
        for path in (PLAIN_INSTALL_QEMU, LUKS_INSTALL_QEMU):
            text = path.read_text()
            self.assertIn(
                "--enable-debug-ssh", text,
                f"{path.name} must invoke fisherman-install.sh with --enable-debug-ssh "
                "so post-boot SSH assertions (dakota#651) can reach the installed system",
            )


class TestJustfileWiring(unittest.TestCase):
    """Verify the new recipes/variables are correctly wired into the justfile."""

    def test_justfile_syntax(self):
        result = subprocess.run(
            ["just", "--justfile", str(JUSTFILE), "--list"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and "No such file or directory" in (result.stderr or ""):
            self.skipTest("`just` binary not available in this environment")
        self.assertEqual(
            result.returncode, 0,
            f"justfile failed to parse:\n{result.stderr}",
        )

    def test_installed_ssh_port_variables_defined(self):
        text = JUSTFILE.read_text()
        self.assertIn("luks-qemu-ssh-port-installed", text)
        self.assertIn("plain-qemu-ssh-port-installed", text)

    def test_luks_verify_qemu_recipe_exists_and_is_wired_into_luks_test_qemu(self):
        text = JUSTFILE.read_text()
        self.assertIn("luks-verify-qemu target:", text)
        # luks-test-qemu must call luks-verify-qemu as its final step.
        luks_test_start = text.index("luks-test-qemu target installer_channel=")
        rest = text[luks_test_start:]
        end = rest.find("\n\n")
        luks_test_body = rest if end == -1 else rest[:end]
        self.assertIn("luks-verify-qemu", luks_test_body)

    def test_plain_verify_qemu_calls_verify_post_install_script(self):
        text = JUSTFILE.read_text()
        verify_start = text.index("plain-verify-qemu target:")
        rest = text[verify_start:]
        end = rest.find("\n\n")
        verify_body = rest if end == -1 else rest[:end]
        self.assertIn("verify-post-install.sh", verify_body)


if __name__ == "__main__":
    unittest.main()
