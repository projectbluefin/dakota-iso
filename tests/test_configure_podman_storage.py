"""Behavioural unit tests for .github/scripts/configure_podman_storage.sh.

The plain-install, LUKS-install and scheduled GUI-installer workflows all run
this script before pulling the multi-gigabyte payload image. It wipes podman
state and writes ``/etc/containers/storage.conf`` with a storage driver chosen
from the filesystem under ``/var/lib/containers``. A wrong choice does not fail
the step: it shows up later as an out-of-space or copy-on-write failure in the
middle of an install run. Each run only exercises the branch for whatever
filesystem that runner happens to have, so the other branches were never run.

These tests run the real script with ``sudo``, ``podman``, ``findmnt`` and
``rm`` replaced by recording stubs, and with the two host paths it checks or
writes redirected under a per-test sandbox. They cover:

1. **Driver choice.** btrfs -> btrfs, zfs -> zfs, ext2/3/4 and xfs ->
   overlay, anything else (including a failed ``findmnt``) -> vfs.
2. **Probe target.** ``findmnt`` is asked about ``/var/lib/containers`` when
   it exists (the BTRFS loopback mount point) and ``/var/lib`` otherwise.
3. **storage.conf contents.** driver, graphroot and runroot.
4. **Reset.** containers and images are removed before ``podman system
   reset``; if the reset fails, only the two lock files are removed and the
   step continues.
"""

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / ".github" / "scripts" / "configure_podman_storage.sh"

# Host paths the script tests or writes, rewritten under ${SANDBOX_ROOT}.
# GRAPHROOT and the lock paths are left alone: they are only written into
# storage.conf or passed to the stubbed rm, and the tests assert their values.
SANDBOXED = {
    "[ -d /var/lib/containers ]": '[ -d "${SANDBOX_ROOT}/var/lib/containers" ]',
    'cat > /etc/containers/storage.conf"': 'cat > ${SANDBOX_ROOT}/etc/containers/storage.conf"',
}

HOST_TOOLS = ("bash", "cat", "grep", "head")


def _sandboxed_script_text():
    text = SCRIPT.read_text(encoding="utf-8")
    for original, replacement in SANDBOXED.items():
        if original not in text:
            raise AssertionError(
                f"{SCRIPT.name} no longer contains {original!r}; update SANDBOXED"
            )
        text = text.replace(original, replacement)
    return text


class ConfigurePodmanStorageHarness(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="podman-storage-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.root = self.sandbox / "root"
        (self.root / "etc" / "containers").mkdir(parents=True)
        (self.root / "var" / "lib").mkdir(parents=True)
        self.calls = self.sandbox / "calls.log"
        self.calls.touch()

        self.script = self.sandbox / "configure_podman_storage.sh"
        self.script.write_text(_sandboxed_script_text())

        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        for tool in HOST_TOOLS:
            real = shutil.which(tool)
            self.assertIsNotNone(real, f"host has no {tool}")
            (self.bindir / tool).symlink_to(real)
        self._install_stubs()

    def _stub(self, name, body):
        path = self.bindir / name
        path.write_text(f"#!{shutil.which('bash')}\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def _install_stubs(self):
        self._stub("sudo", 'echo "sudo $*" >>"$STUB_CALLS"\nexec "$@"\n')
        self._stub("rm", 'echo "rm $*" >>"$STUB_CALLS"\n')
        # findmnt prints $STUB_FSTYPE, or fails when it is unset.
        self._stub("findmnt", """
            echo "findmnt $*" >>"$STUB_CALLS"
            [[ -n "${STUB_FSTYPE:-}" ]] || exit 1
            echo "$STUB_FSTYPE"
        """)
        self._stub("podman", """
            echo "podman $*" >>"$STUB_CALLS"
            case "$1 ${2:-}" in
                "system reset") [[ -z "${STUB_RESET_FAIL:-}" ]] || exit 125 ;;
                "info ") printf 'host:\\n  arch: amd64\\nstore:\\n  graphDriverName: x\\n' ;;
            esac
            exit 0
        """)

    def run_script(self, fstype="ext4", containers_dir=True, **env):
        if containers_dir:
            (self.root / "var" / "lib" / "containers").mkdir(exist_ok=True)
        full_env = {
            "PATH": str(self.bindir),
            "HOME": str(self.sandbox),
            "SANDBOX_ROOT": str(self.root),
            "STUB_CALLS": str(self.calls),
        }
        if fstype is not None:
            full_env["STUB_FSTYPE"] = fstype
        full_env.update(env)
        result = subprocess.run(
            [shutil.which("bash"), str(self.script)],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        return result

    def calls_for(self, tool):
        prefix = tool + " "
        return [
            ln[len(prefix):] for ln in self.calls.read_text().splitlines() if ln.startswith(prefix)
        ]

    def storage_conf(self):
        return (self.root / "etc" / "containers" / "storage.conf").read_text()

    def driver(self):
        for line in self.storage_conf().splitlines():
            if line.startswith("driver"):
                return line.split("=", 1)[1].strip().strip('"')
        self.fail(f"no driver line in storage.conf:\n{self.storage_conf()}")


class TestDriverSelection(ConfigurePodmanStorageHarness):
    def test_filesystem_to_driver(self):
        cases = {
            "btrfs": "btrfs",
            "zfs": "zfs",
            "ext4": "overlay",
            "ext3": "overlay",
            "ext2": "overlay",
            "xfs": "overlay",
            "tmpfs": "vfs",
            "overlay": "vfs",
        }
        for fstype, expected in cases.items():
            with self.subTest(fstype=fstype):
                result = self.run_script(fstype=fstype)
                self.assertEqual(self.driver(), expected)
                self.assertIn(f"Detected filesystem for /var/lib/containers: {fstype}", result.stdout)
                self.assertIn(f"Configuring podman storage driver: {expected}", result.stdout)

    def test_findmnt_failure_falls_back_to_vfs(self):
        result = self.run_script(fstype=None)
        self.assertEqual(self.driver(), "vfs")
        self.assertIn("Detected filesystem for /var/lib/containers: unknown", result.stdout)

    def test_storage_conf_contents(self):
        self.run_script(fstype="btrfs")
        self.assertEqual(
            self.storage_conf(),
            '[storage]\n'
            'driver = "btrfs"\n'
            'graphroot = "/var/lib/containers/storage"\n'
            'runroot = "/run/containers/storage"\n',
        )


class TestProbeTarget(ConfigurePodmanStorageHarness):
    def test_probes_containers_dir_when_present(self):
        self.run_script(containers_dir=True)
        self.assertEqual(self.calls_for("findmnt"), ["-n -o FSTYPE -T /var/lib/containers"])

    def test_probes_var_lib_when_containers_dir_missing(self):
        self.run_script(containers_dir=False)
        self.assertEqual(self.calls_for("findmnt"), ["-n -o FSTYPE -T /var/lib"])


class TestReset(ConfigurePodmanStorageHarness):
    def test_removes_containers_and_images_before_reset(self):
        self.run_script()
        podman = self.calls_for("podman")
        self.assertEqual(podman[:3], ["rm -fa", "rmi -a", "system reset -f"])
        self.assertTrue(all(c.startswith("podman") for c in self.calls_for("sudo")[:3]))
        self.assertEqual(self.calls_for("rm"), [])

    def test_failed_reset_clears_lock_files_and_continues(self):
        result = self.run_script(fstype="xfs", STUB_RESET_FAIL="1")
        self.assertEqual(
            self.calls_for("rm"),
            [
                "-rf /var/lib/containers/storage.lock",
                "-rf /run/containers/storage.lock",
            ],
        )
        self.assertIn("Warning: podman system reset not available", result.stdout)
        self.assertEqual(self.driver(), "overlay")

    def test_podman_info_without_storage_key_falls_back_to_head(self):
        # podman info names its storage section "store:", so the grep for
        # "storage:" misses and the head -20 fallback must keep the step green.
        result = self.run_script()
        self.assertIn("=== Podman storage configuration ===", result.stdout)
        self.assertIn("graphDriverName", result.stdout)


if __name__ == "__main__":
    unittest.main()
