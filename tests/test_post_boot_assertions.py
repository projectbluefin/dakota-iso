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
            "ro rd.luks.uuid=luks-1B4E28BA-2FA1-11D2-883F-B9A761BDE3FB",
        ]
        bad_cmdlines = [
            "root=/dev/vda2 ro quiet splash",
            "console=ttyS0 rd.luks=1",  # no uuid/name key, must not match
            "rd.luks.uuid=x ro",  # not a UUID
            "rd.luks.uuid=1b4e28ba-2fa1-11d2-883f ro",  # truncated UUID
            "rd.luks.uuid=1b4e28ba-2fa1-11d2-883f-b9a761bde3fbXX ro",  # trailing junk
            "rd.luks.name=root=luks-root ro",  # name form without UUID
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

    def test_flatpak_assertion_fails_on_command_error_or_leak(self):
        """flatpak list failing must not pass vacuously, and presence of
        org.bootcinstaller must fail."""
        text = VERIFY_SCRIPT.read_text()
        self.assertIn('FLATPAK_RC', text, "flatpak exclusion check must track exit code")
        self.assertIn('org.bootcinstaller', text)

    def _check_uefi(self, efibootmgr_out: str) -> int:
        return subprocess.run(
            ["bash", "-c", 'source "$0" && check_uefi_boot_entry "$1"',
             str(VERIFY_SCRIPT), efibootmgr_out],
            capture_output=True, text=True,
        ).returncode

    def test_uefi_check_accepts_installer_written_entry(self):
        for loader in (r"File(\EFI\fedora\shimx64.efi)", r"\EFI\fedora\shimx64.efi"):
            out = (
                "BootCurrent: 0004\n"
                "BootOrder: 0004,0001,0000\n"
                "Boot0000* UiApp\tFvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)\n"
                "Boot0001* UEFI Misc Device\tPciRoot(0x0)/Pci(0x4,0x0){auto_created_boot_option}\n"
                f"Boot0004* Fedora\tHD(1,GPT,0b1c7a3e-1111-2222-3333-444455556666,0x800,0x12c000)/{loader}\n"
            )
            self.assertEqual(self._check_uefi(out), 0, f"should accept: {loader}")

    def test_uefi_check_rejects_ovmf_auto_entries_and_fallback(self):
        """The fisherman #2 regression (no NVRAM entry written) must fail even
        though OVMF always lists BootCurrent + auto-enumerated Boot#### lines."""
        bad = {
            "ovmf auto-enumerated disk": (
                "BootCurrent: 0001\n"
                "Boot0000* UiApp\tFvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)\n"
                "Boot0001* UEFI Misc Device\tPciRoot(0x0)/Pci(0x4,0x0){auto_created_boot_option}\n"
            ),
            "removable-media fallback": (
                "BootCurrent: 0002\n"
                "Boot0002* UEFI\tHD(1,GPT,0b1c7a3e-1111-2222-3333-444455556666,0x800,0x12c000)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n"
            ),
            "uiapp": (
                "BootCurrent: 0000\n"
                "Boot0000* UiApp\tFvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)\n"
            ),
            "bootcurrent points at missing entry": (
                "BootCurrent: 0009\n"
                "Boot0004* Fedora\tHD(1,GPT,0b1c7a3e-1111-2222-3333-444455556666,0x800,0x12c000)/File(\\EFI\\fedora\\shimx64.efi)\n"
            ),
            "no bootcurrent": (
                "Boot0004* Fedora\tHD(1,GPT,0b1c7a3e-1111-2222-3333-444455556666,0x800,0x12c000)/File(\\EFI\\fedora\\shimx64.efi)\n"
            ),
        }
        for name, out in bad.items():
            self.assertNotEqual(self._check_uefi(out), 0, f"should reject: {name}")


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

        for marker in ("openssl passwd", "PermitRootLogin", 'enable ${SSH_UNIT}', "multi-user.target.wants"):
            self.assertIn(marker, guarded_slice, f"{marker!r} not found inside debug guard")

        before_guard = text[:guard_start]
        for marker in ("PermitRootLogin", "system-preset", "multi-user.target.wants"):
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
        import shutil
        if not shutil.which("just"):
            self.skipTest("`just` binary not available in this environment")
        result = subprocess.run(
            ["just", "--justfile", str(JUSTFILE), "--list"],
            capture_output=True, text=True,
        )
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


    def _recipe_body(self, header):
        text = JUSTFILE.read_text()
        rest = text[text.index(header) + len(header):]
        # Recipe body ends at the next non-indented line (comment or recipe).
        m = re.search(r"\n(?=[^\s])", rest)
        return rest if m is None else rest[:m.start()]

    def test_ssh_unreachable_uses_distinct_exit_code(self):
        text = VERIFY_SCRIPT.read_text()
        unreachable = text[text.index('if [[ "$READY" -ne 1 ]]'):]
        unreachable = unreachable[:unreachable.index("\nfi\n")]
        self.assertIn('"${STRICT_POST_BOOT:-0}" != "1"', unreachable)
        self.assertIn("::warning", unreachable)
        self.assertTrue(unreachable.rstrip().endswith("exit 2"))

    def test_nested_plain_runs_forward_installed_ssh_port(self):
        for header in ("plain-test-qemu target:", "gui-e2e target:"):
            body = self._recipe_body(header)
            self.assertEqual(
                body.count(
                    "plain-qemu-ssh-port-installed={{plain-qemu-ssh-port-installed}}"
                ),
                2,
                header,
            )

    def test_soft_fail_lives_in_script_and_only_covers_assertion_failures(self):
        text = VERIFY_SCRIPT.read_text()
        tail = text[text.rindex('if [[ "$FAIL" -eq 0 ]]'):]
        self.assertIn('"${STRICT_POST_BOOT:-0}" != "1"', tail)
        self.assertIn("NON-BLOCKING", tail)
        self.assertTrue(tail.rstrip().endswith("exit 1"))
        for header in ("luks-verify-qemu target:", "plain-verify-qemu target:"):
            body = self._recipe_body(header)
            self.assertNotIn("STRICT_POST_BOOT:-0", body, header)
            self.assertNotIn("exit 0", body, header)

    def test_installed_boot_can_reuse_live_nvram(self):
        # LUKS always carries the live NVRAM over; plain only behind
        # PLAIN_CARRY_LIVE_NVRAM=1 (see the pristine-default test below).
        for header, prefix in (
            ("luks-boot-qemu-installed target:", "dakota-qemu"),
            ("plain-boot-qemu-installed target:", "dakota-plain-qemu"),
        ):
            body = self._recipe_body(header)
            self.assertIn(
                f"cp /var/tmp/{prefix}-live-vars.fd \"$OVMF_VARS\"", body, header
            )

    def test_plain_installed_boot_defaults_to_pristine_nvram(self):
        # A carried-over live NVRAM left no valid BootOrder entry for the
        # installed disk, so OVMF dropped to "EFI Internal Shell" (every plain
        # E2E failed from 2026-10-25). The carry-over is opt-in only.
        body = self._recipe_body("plain-boot-qemu-installed target:")
        self.assertIn('"${PLAIN_CARRY_LIVE_NVRAM:-0}" == "1"', body)
        self.assertIn('cp "$OVMF_VARS_TEMPLATE" "$OVMF_VARS"', body)

    def test_installed_ssh_hostfwd_bound_to_loopback(self):
        text = JUSTFILE.read_text()
        for var in ("luks-qemu-ssh-port-installed", "plain-qemu-ssh-port-installed"):
            self.assertIn(f"hostfwd=tcp:127.0.0.1:{{{{{var}}}}}-:22", text)

if __name__ == "__main__":
    unittest.main()
