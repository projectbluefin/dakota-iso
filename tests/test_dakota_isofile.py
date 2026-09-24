"""Executed unit coverage for ``live/src/dracut/95dakota-isofile/dakota-isofile.sh``.

That hook is the Ventoy/file-backed boot path: when the live ISO is a *file*
sitting on a USB partition rather than a burned optical/USB volume, nothing
publishes ``/dev/disk/by-label/DAKOTA_LIVE``, so dracut's ``dmsquash-live``
CDLABEL path never fires.  The hook scans candidate partitions, loop-mounts the
ISO it finds, republishes the label symlink and hand-queues
``dmsquash-live-root``.  If it regresses the ISO does not boot at all — and the
only signal is a black screen in an emergency shell.

Before this file the script had **zero** coverage of any kind: no test executed
it, and unlike the rest of ``live/src`` it was not even referenced by the static
assertions in ``tests/test_live_build_invariants.py``.

How these tests reach the code
------------------------------
The script is a dracut ``initqueue`` hook: dracut *sources* it, and it signals
"not ready yet, call me again" with a top-level ``return 1``.  Sourcing is
exactly what the harness does, which has a useful consequence — after the
top-level body returns, ``debug``/``try_iso``/``scan_mount`` and the
``LABEL``/``RUN_DIR``/``FOUND_MARKER``/``iso_hint`` variables remain defined in
the sourcing shell.  So the real functions, verbatim from the real file, can be
called directly.  No copy of the logic is kept here.

Two substitutions make that possible without root:

* every absolute path the script touches (``/dev/...``, ``/run/...``,
  ``/proc/cmdline``, ``/sbin/...``) is re-rooted into a sandbox directory.
  ``test_every_absolute_path_is_sandboxed`` is a drift gate: if the script grows
  an absolute path outside that table the test fails rather than silently
  escaping the sandbox.
* ``losetup``/``blkid``/``mount``/``umount``/``udevadm`` are replaced by
  recording shims on ``PATH``.

Known limitation: the final ``for dev in /dev/disk/by-label/* ...`` enumeration
loop is gated on ``[ -b "$dev" ]``, and creating a block device needs ``mknod``
(root).  The loop's *outcome* is covered (it returns 1 when no candidate
yields an ISO), and the per-device work it delegates to ``scan_mount`` is
covered directly, but the enumeration itself is not executed.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "live" / "src" / "dracut" / "95dakota-isofile" / "dakota-isofile.sh"

# Absolute path roots the script is allowed to touch.  Each is re-rooted into
# the sandbox; test_every_absolute_path_is_sandboxed asserts the list is total.
SANDBOXED_ROOTS = ("/dev/", "/run/", "/proc/", "/sbin/")

# Recording shims.  Each appends its argv to $STUB_LOG and then behaves the way
# the initramfs tool would.
STUBS = {
    # losetup --find --show --read-only <iso>  ->  allocate a fake loop device
    # and remember which file it is backed by, so blkid can answer for it.
    # losetup -d <loopdev>  ->  release it.
    "losetup": r"""#!/bin/bash
echo "losetup $*" >> "$STUB_LOG"
if [ "$1" = "-d" ]; then
    rm -f "$STUB_DIR/loop/$(basename "$2")"
    exit 0
fi
iso="${@: -1}"
if grep -Fxq "$iso" "$STUB_DIR/losetup-fail" 2>/dev/null; then
    exit 1
fi
n=$(cat "$STUB_DIR/loop-seq" 2>/dev/null || echo 0)
echo $((n + 1)) > "$STUB_DIR/loop-seq"
dev="$SANDBOX/dev/loop$n"
mkdir -p "$STUB_DIR/loop"
printf '%s' "$iso" > "$STUB_DIR/loop/loop$n"
echo "$dev"
""",
    # blkid -s LABEL -o value <loopdev>.  The fixture ISO files contain their
    # own ISO9660 volume label as their entire content.
    "blkid": r"""#!/bin/bash
echo "blkid $*" >> "$STUB_LOG"
dev="${@: -1}"
backing="$STUB_DIR/loop/$(basename "$dev")"
[ -f "$backing" ] || exit 2
iso=$(cat "$backing")
[ -f "$iso" ] || exit 2
cat "$iso"
""",
    # mount -o ro -t auto <dev> <mountpoint>.  Publishes whatever the fixture
    # staged for that device so the ISO globs have something to match.
    "mount": r"""#!/bin/bash
echo "mount $*" >> "$STUB_LOG"
dev="${@: -2:1}"
mp="${@: -1}"
base=$(basename "$dev")
if grep -Fxq "$base" "$STUB_DIR/mount-fail" 2>/dev/null; then
    exit 32
fi
if [ -d "$STUB_DIR/media/$base" ]; then
    cp -a "$STUB_DIR/media/$base/." "$mp/"
fi
echo "$mp" >> "$STUB_DIR/mounted"
exit 0
""",
    "umount": r"""#!/bin/bash
echo "umount $*" >> "$STUB_LOG"
exit 0
""",
    "udevadm": r"""#!/bin/bash
echo "udevadm $*" >> "$STUB_LOG"
exit 0
""",
}

# /sbin/initqueue is invoked by absolute path, so it lands in the sandbox
# rather than on PATH.
INITQUEUE_STUB = r"""#!/bin/bash
echo "initqueue $*" >> "$STUB_LOG"
exit 0
"""


class Sandbox:
    """A re-rooted filesystem plus stub PATH for one hook invocation."""

    def __init__(self, root: Path):
        self.root = root
        self.stub_dir = root / "stub"
        self.log = self.stub_dir / "log"
        self.bin = root / "bin"
        for path in (
            root / "dev" / "disk" / "by-label",
            root / "dev" / "disk" / "by-id",
            root / "proc",
            root / "sbin",
            root / "run",
            self.stub_dir / "media",
            self.bin,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.log.touch()
        (root / "dev" / "kmsg").touch()
        self.set_cmdline("root=live:CDLABEL=DAKOTA_LIVE rd.live.image")
        for name, body in STUBS.items():
            self._write_exec(self.bin / name, body)
        self._write_exec(root / "sbin" / "initqueue", INITQUEUE_STUB)
        self._write_exec(root / "sbin" / "dmsquash-live-root", "#!/bin/bash\nexit 0\n")

        # The re-rooted copy of the hook.  Only path prefixes are rewritten;
        # every statement is otherwise verbatim.
        self.script = root / "dakota-isofile.sh"
        text = HOOK.read_text()
        for prefix in SANDBOXED_ROOTS:
            text = text.replace(prefix, f"{root}{prefix}")
        self.script.write_text(text)

    @staticmethod
    def _write_exec(path: Path, body: str) -> None:
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # -- fixture helpers ------------------------------------------------
    def set_cmdline(self, cmdline: str) -> None:
        (self.root / "proc" / "cmdline").write_text(cmdline + "\n")

    def stage_iso(self, device: str, relpath: str, label: str = "DAKOTA_LIVE") -> Path:
        """Put an ISO file at *relpath* on the media of *device*."""
        target = self.stub_dir / "media" / device / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(label)
        return target

    def fail_mount(self, device: str) -> None:
        with (self.stub_dir / "mount-fail").open("a") as handle:
            handle.write(device + "\n")

    def fail_losetup(self, iso: Path) -> None:
        with (self.stub_dir / "losetup-fail").open("a") as handle:
            handle.write(str(iso) + "\n")

    def write_iso_file(self, relpath: str, label: str = "DAKOTA_LIVE") -> Path:
        """Write a standalone ISO file (not behind a mount) inside the sandbox."""
        target = self.root / relpath.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(label)
        return target

    # -- execution ------------------------------------------------------
    def run(self, extra: str = "") -> subprocess.CompletedProcess:
        """Source the hook, then optionally run *extra* shell against it.

        The hook's top-level return status is echoed as ``HOOK_RC=<n>``.
        """
        program = f'. "{self.script}"\necho "HOOK_RC=$?"\n{extra}\n'
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            STUB_DIR=str(self.stub_dir),
            STUB_LOG=str(self.log),
            SANDBOX=str(self.root),
        )
        return subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def calls(self, tool: str) -> list:
        return [
            line
            for line in self.log.read_text().splitlines()
            if line.split(" ", 1)[0] == tool
        ]

    @property
    def label_link(self) -> Path:
        return self.root / "dev" / "disk" / "by-label" / "DAKOTA_LIVE"

    @property
    def found_marker(self) -> Path:
        return self.root / "run" / "initramfs" / "dakota-isofile" / "found"


class DakotaIsofileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dakota-isofile-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sandbox = Sandbox(Path(self.tmp))

    @staticmethod
    def hook_rc(result: subprocess.CompletedProcess) -> int:
        for line in result.stdout.splitlines():
            if line.startswith("HOOK_RC="):
                return int(line.split("=", 1)[1])
        raise AssertionError(f"hook never reported a status: {result.stdout!r}")

    @staticmethod
    def value(result: subprocess.CompletedProcess, key: str) -> str:
        for line in result.stdout.splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        raise AssertionError(f"{key} not reported: {result.stdout!r}")


class TestEarlyReturns(DakotaIsofileTestCase):
    """The hook is re-run by initqueue until it succeeds; it must be idempotent."""

    def test_label_already_published_is_a_no_op(self):
        optical = self.sandbox.root / "dev" / "sr0"
        optical.touch()
        self.sandbox.label_link.symlink_to(optical)
        result = self.sandbox.run()
        self.assertEqual(self.hook_rc(result), 0)
        # The normal CDLABEL path already worked: do not touch loop devices.
        self.assertEqual(self.sandbox.calls("losetup"), [])
        self.assertEqual(self.sandbox.calls("mount"), [])

    def test_found_marker_short_circuits_a_second_invocation(self):
        self.sandbox.found_marker.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.found_marker.touch()
        result = self.sandbox.run()
        self.assertEqual(self.hook_rc(result), 0)
        self.assertEqual(self.sandbox.calls("losetup"), [])
        self.assertEqual(self.sandbox.calls("mount"), [])

    def test_no_candidate_media_keeps_initqueue_waiting(self):
        # Nothing to scan: the hook must fail so dracut calls it again rather
        # than dropping to an emergency shell.
        result = self.sandbox.run()
        self.assertEqual(self.hook_rc(result), 1)
        self.assertFalse(self.sandbox.label_link.exists())
        self.assertFalse(self.sandbox.found_marker.exists())

    def test_run_directory_is_created(self):
        self.sandbox.run()
        self.assertTrue(self.sandbox.found_marker.parent.is_dir())


class TestKernelCommandLineHint(DakotaIsofileTestCase):
    """Four spellings of "the ISO is at this path" must all be honoured."""

    def assert_hint(self, cmdline: str, expected: str):
        self.sandbox.set_cmdline(cmdline)
        result = self.sandbox.run('echo "HINT=$iso_hint"')
        self.assertEqual(self.value(result, "HINT"), expected)

    def test_rd_dakota_isofile(self):
        self.assert_hint("rd.dakota.isofile=/isos/dakota.iso", "/isos/dakota.iso")

    def test_findiso(self):
        self.assert_hint("findiso=/isos/dakota.iso", "/isos/dakota.iso")

    def test_iso_scan_filename(self):
        self.assert_hint("iso-scan/filename=/isos/dakota.iso", "/isos/dakota.iso")

    def test_rd_live_iso(self):
        self.assert_hint("rd.live.iso=/isos/dakota.iso", "/isos/dakota.iso")

    def test_hint_is_empty_without_any_of_them(self):
        self.assert_hint("root=live:CDLABEL=DAKOTA_LIVE quiet", "")

    def test_hint_is_extracted_from_a_crowded_command_line(self):
        self.assert_hint(
            "ro quiet splash findiso=/boot/isos/dakota.iso rd.live.image",
            "/boot/isos/dakota.iso",
        )

    def test_value_containing_equals_is_kept_whole(self):
        # "${arg#*=}" strips only the first '=' — a path with one must survive.
        self.assert_hint("findiso=/isos/a=b/dakota.iso", "/isos/a=b/dakota.iso")


class TestTryIso(DakotaIsofileTestCase):
    """try_iso owns the "is this file the live ISO?" decision."""

    def test_matching_label_publishes_the_symlink_and_queues_the_root(self):
        iso = self.sandbox.write_iso_file("media/dakota.iso", label="DAKOTA_LIVE")
        result = self.sandbox.run(f'try_iso "{iso}"; echo "RC=$?"')
        self.assertEqual(self.value(result, "RC"), "0")

        link = self.sandbox.label_link
        self.assertTrue(link.is_symlink(), "DAKOTA_LIVE label was not published")
        loopdev = os.readlink(link)
        self.assertTrue(loopdev.endswith("/dev/loop0"), loopdev)

        self.assertTrue(self.sandbox.found_marker.exists())
        # Manual symlinks do not fire the dmsquash-live udev rule, so the hook
        # must queue dmsquash-live-root itself, against the loop device.
        initqueue = self.sandbox.calls("initqueue")
        self.assertEqual(len(initqueue), 1, initqueue)
        self.assertIn("--settled", initqueue[0])
        self.assertIn("--onetime", initqueue[0])
        self.assertIn("--unique", initqueue[0])
        self.assertTrue(initqueue[0].endswith(f"dmsquash-live-root {loopdev}"), initqueue[0])
        # /dev/root must not be pre-created here; dmsquash-live-root owns it.
        self.assertFalse((self.sandbox.root / "dev" / "root").exists())
        self.assertEqual(len(self.sandbox.calls("udevadm")), 1)

    def test_foreign_label_releases_the_loop_device(self):
        iso = self.sandbox.write_iso_file("media/ubuntu.iso", label="Ubuntu 24.04")
        result = self.sandbox.run(f'try_iso "{iso}"; echo "RC=$?"')
        self.assertEqual(self.value(result, "RC"), "1")
        self.assertFalse(self.sandbox.label_link.exists())
        self.assertFalse(self.sandbox.found_marker.exists())
        # A leaked loop device per rejected ISO would exhaust /dev/loop*.
        self.assertTrue(
            any(call.startswith("losetup -d ") for call in self.sandbox.calls("losetup")),
            self.sandbox.calls("losetup"),
        )

    def test_unlabelled_image_is_rejected(self):
        iso = self.sandbox.write_iso_file("media/blank.iso", label="")
        result = self.sandbox.run(f'try_iso "{iso}"; echo "RC=$?"')
        self.assertEqual(self.value(result, "RC"), "1")
        self.assertFalse(self.sandbox.label_link.exists())

    def test_missing_file_is_rejected_without_touching_losetup(self):
        result = self.sandbox.run(
            f'try_iso "{self.sandbox.root}/media/absent.iso"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "1")
        self.assertEqual(self.sandbox.calls("losetup"), [])

    def test_directory_is_not_mistaken_for_an_iso(self):
        directory = self.sandbox.root / "media" / "dakota.iso"
        directory.mkdir(parents=True)
        result = self.sandbox.run(f'try_iso "{directory}"; echo "RC=$?"')
        self.assertEqual(self.value(result, "RC"), "1")
        self.assertEqual(self.sandbox.calls("losetup"), [])

    def test_losetup_failure_is_reported_not_crashed_on(self):
        iso = self.sandbox.write_iso_file("media/dakota.iso")
        self.sandbox.fail_losetup(iso)
        result = self.sandbox.run(f'try_iso "{iso}"; echo "RC=$?"')
        self.assertEqual(self.value(result, "RC"), "1")
        self.assertEqual(self.sandbox.calls("blkid"), [])
        self.assertFalse(self.sandbox.label_link.exists())


class TestScanMount(DakotaIsofileTestCase):
    """scan_mount owns per-partition discovery."""

    def test_unmountable_partition_is_skipped(self):
        self.sandbox.fail_mount("sdb1")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "1")
        self.assertEqual(self.sandbox.calls("losetup"), [])

    def test_partition_without_any_iso_is_unmounted_again(self):
        self.sandbox.stage_iso("sdb1", "notes.txt", label="not an iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "1")
        # Leaving every scanned partition mounted would pin the USB stick.
        self.assertEqual(len(self.sandbox.calls("umount")), 1)

    def test_matching_iso_is_found_and_the_mount_is_retained(self):
        self.sandbox.stage_iso("sdb1", "dakota-alpha5.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        self.assertTrue(self.sandbox.label_link.is_symlink())
        # The loop device is backed by a file on this mount: unmounting it
        # would pull the rootfs out from under dmsquash-live-root.
        self.assertEqual(self.sandbox.calls("umount"), [])

    def test_iso_in_a_subdirectory_is_found(self):
        self.sandbox.stage_iso("sdb1", "ISO/dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        self.assertTrue(self.sandbox.label_link.is_symlink())

    def test_ventoy_style_nested_iso_is_found(self):
        self.sandbox.stage_iso("sdb1", "distros/dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")

    def test_dakota_named_iso_is_probed_before_other_isos(self):
        # A Ventoy stick holds many ISOs; probing each one costs a loop
        # attach/detach, so the likely candidate must be tried first.
        self.sandbox.stage_iso("sdb1", "aaa-ubuntu.iso", label="Ubuntu 24.04")
        self.sandbox.stage_iso("sdb1", "dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        blkid = self.sandbox.calls("blkid")
        self.assertEqual(len(blkid), 1, f"non-Dakota ISO was probed first: {blkid}")

    def test_foreign_iso_does_not_stop_the_search(self):
        self.sandbox.stage_iso("sdb1", "aaa-ubuntu.iso", label="Ubuntu 24.04")
        self.sandbox.stage_iso("sdb1", "zzz-dakota-live.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        self.assertTrue(self.sandbox.label_link.is_symlink())

    def test_absolute_hint_is_joined_to_the_mountpoint_without_doubling(self):
        self.sandbox.set_cmdline("findiso=/isos/dakota.iso")
        self.sandbox.stage_iso("sdb1", "isos/dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        losetup = self.sandbox.calls("losetup")
        self.assertIn("/isos/dakota.iso", losetup[0])
        self.assertNotIn("//isos", losetup[0])

    def test_relative_hint_gets_a_separator(self):
        self.sandbox.set_cmdline("findiso=isos/dakota.iso")
        self.sandbox.stage_iso("sdb1", "isos/dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        self.assertIn("/isos/dakota.iso", self.sandbox.calls("losetup")[0])

    def test_hint_is_probed_before_the_globs(self):
        self.sandbox.set_cmdline("rd.dakota.isofile=/isos/dakota.iso")
        self.sandbox.stage_iso("sdb1", "isos/dakota.iso")
        self.sandbox.stage_iso("sdb1", "dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        self.assertIn("/isos/dakota.iso", self.sandbox.calls("losetup")[0])

    def test_unusable_hint_falls_back_to_the_globs(self):
        self.sandbox.set_cmdline("findiso=/isos/missing.iso")
        self.sandbox.stage_iso("sdb1", "dakota.iso")
        result = self.sandbox.run(
            f'scan_mount "{self.sandbox.root}/dev/sdb1"; echo "RC=$?"'
        )
        self.assertEqual(self.value(result, "RC"), "0")
        self.assertTrue(self.sandbox.label_link.is_symlink())


class TestHookContract(DakotaIsofileTestCase):
    """Static invariants the executed tests above depend on."""

    def test_every_absolute_path_is_sandboxed(self):
        """Drift gate for the harness re-rooting table.

        If the hook starts touching, say, ``/etc`` or ``/usr``, the tests above
        would silently exercise the *host* path.  Fail loudly instead.
        """
        text = HOOK.read_text()
        # Drop the shebang (an interpreter, not a path the hook opens), collapse
        # every variable expansion to a single token and remove quoting, so that
        # "$mp"/dakota*.iso reads as a suffix of a variable rather than as the
        # absolute path /dakota*.iso.
        text = text.split("\n", 1)[1]
        text = re.sub(r"\$\{[^}]*\}|\$\w+", "V", text)
        text = text.replace('"', "").replace("'", "")
        candidates = set(re.findall(r"(?<![\w.$/*?\]-])(/[a-zA-Z][\w./-]*)", text))
        escaping = sorted(
            path
            for path in candidates
            if not path.startswith(SANDBOXED_ROOTS)
        )
        self.assertEqual(
            escaping,
            [],
            "hook touches absolute paths outside the sandbox table "
            f"{SANDBOXED_ROOTS}: {escaping}",
        )

    def test_label_matches_the_live_iso_volume_label(self):
        # The hook's whole purpose is to republish this exact label, which must
        # agree with what the ISO build stamps on the image.
        self.assertIn('LABEL="DAKOTA_LIVE"', HOOK.read_text())

    def test_hook_is_posix_sh(self):
        # dracut sources initqueue hooks with the initramfs /bin/sh, which is
        # not bash; a bashism here fails only at boot.
        self.assertTrue(HOOK.read_text().startswith("#!/bin/sh\n"))

    def test_every_tool_the_hook_calls_is_installed_by_module_setup(self):
        """A tool the hook calls but dracut never installs is a boot failure."""
        module_setup = HOOK.parent / "module-setup.sh"
        installed = set()
        for line in module_setup.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("inst_multiple "):
                installed.update(stripped.split()[1:])
        for tool in ("mount", "umount", "blkid", "losetup", "ln", "mkdir", "udevadm"):
            self.assertIn(
                tool,
                installed,
                f"{tool} is called by dakota-isofile.sh but not installed by "
                "module-setup.sh",
            )


if __name__ == "__main__":
    unittest.main()
