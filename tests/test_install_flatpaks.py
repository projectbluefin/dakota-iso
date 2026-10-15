"""Behavioural unit tests for live/src/install-flatpaks.sh.

``live/src/install-flatpaks.sh`` runs inside the live-image Containerfile and
decides which Flatpaks every ISO ships. Until now the only test touching it
(``TestInstallerChannelURLs`` in ``test_live_build_invariants.py``) greps the
installer URL lines; none of its control flow ever ran outside a multi-hour
ISO build. A regression in any of these surfaces as an ISO that ships the
wrong apps, no terminal, or a shared machine id, not as a test failure:

1. **Installer channel.** ``INSTALLER_CHANNEL=dev`` must switch both the
   downloaded bundle and the installed app ID to the Devel build; anything
   else installs the stable one.
2. **List parsing.** Comments and blank lines are skipped; ``remote:app-id``
   entries take an app from a known non-Flathub remote (Utah's Ghostty), and
   an unknown remote fails the build instead of silently dropping the app.
3. **Variant override.** ``TARGET`` with its ``-nvidia``/``-nvidia-open``
   suffix stripped selects ``/src/<variant>/flatpaks`` over the shared list.
4. **Reconcile.** Installed apps no longer wanted are removed, but the
   installer (stable or Devel) is always kept.
5. **Container-build workarounds.** The missing ``active`` deploy symlink is
   recreated, and a machine id created for the build is blanked again so live
   boots do not share one.
6. **Build cache.** The warm repo seeds ``/var/lib/flatpak/repo``; without
   rsync the save is staged, so a failed copy keeps the previous warm cache.

The tests run the real script under ``bash`` with every absolute path it
touches rewritten under a per-test sandbox root, and ``flatpak``, ``curl``,
``ostree``, ``dbus-daemon`` and the machine-id tools replaced by recording
shims. ``PATH`` holds only those shims plus the coreutils the script needs,
so the no-rsync branches run on any host.
"""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "live" / "src" / "install-flatpaks.sh"

# Every absolute path the script reads or writes. Each is rewritten to live
# under ${SANDBOX_ROOT}; test_every_absolute_path_is_sandboxed fails if the
# script grows a path this list does not cover.
ROOTED_PATHS = (
    "/var/cache/flatpak-dl",
    "/var/lib/flatpak",
    "/var/lib/dbus/machine-id",
    "/etc/machine-id",
    "/run/dbus",
    "/tmp/flatpaks-list",
    "/tmp/tuna-installer.flatpak",
    "/tmp/installer-local-repo",
    "/src/${VARIANT}",
)
_ROOTED_RE = re.compile(
    r"(?<![\w$}/.:-])(" + "|".join(re.escape(p) for p in ROOTED_PATHS) + r")"
)

HOST_TOOLS = ("mkdir", "cp", "rm", "mv", "ln", "find", "head", "sed", "grep", "cat")

STABLE_ID = "org.bootcinstaller.Installer"
DEVEL_ID = "org.bootcinstaller.Installer.Devel"


def _sandboxed_script_text():
    text = SCRIPT.read_text(encoding="utf-8")
    for path in ROOTED_PATHS:
        if path not in text:
            raise AssertionError(f"{SCRIPT.name} no longer references {path}; update ROOTED_PATHS")
    return _ROOTED_RE.sub(lambda m: "${SANDBOX_ROOT}" + m.group(1), text)


class InstallFlatpaksHarness(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="install-flatpaks-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.root = self.sandbox / "root"
        for d in ("etc", "tmp", "src", "var/lib/dbus", "var/cache"):
            (self.root / d).mkdir(parents=True)
        self.calls = self.sandbox / "calls.log"
        self.calls.touch()
        self.installed = self.sandbox / "installed-apps"
        self.installed.write_text("")

        self.script = self.sandbox / "install-flatpaks.sh"
        self.script.write_text(_sandboxed_script_text())

        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        for tool in HOST_TOOLS:
            real = shutil.which(tool)
            self.assertIsNotNone(real, f"host has no {tool}")
            (self.bindir / tool).symlink_to(real)
        self._install_stubs()
        self.write_list("org.gnome.Calculator\n")

    def _stub(self, name, body):
        path = self.bindir / name
        path.write_text(f"#!{shutil.which('bash')}\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def _install_stubs(self):
        self._stub("sleep", ":\n")
        self._stub("ostree", 'echo "ostree $*" >>"$STUB_CALLS"\n')
        self._stub("dbus-daemon", """
            echo "dbus-daemon $*" >>"$STUB_CALLS"
            for a in "$@"; do [[ "$a" == --print-address ]] && echo "unix:path=/fake-session-bus"; done
            exit 0
        """)
        self._stub("systemd-machine-id-setup", """
            echo "systemd-machine-id-setup" >>"$STUB_CALLS"
            echo 0123456789abcdef0123456789abcdef >"$SANDBOX_ROOT/etc/machine-id"
        """)
        self._stub("dbus-uuidgen", "echo fallback-uuid\n")
        self._stub("curl", """
            out=
            url=
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -o) out="$2"; shift 2 ;;
                    --retry) shift 2 ;;
                    -*) shift ;;
                    *) url="$1"; shift ;;
                esac
            done
            echo "curl $url" >>"$STUB_CALLS"
            [[ -n "${STUB_CURL_FAIL:-}" ]] && exit 22
            echo bundle >"$out"
        """)
        # flatpak: records every call. Installing the installer from the
        # local repo lays out a deployment without the `active` symlink, the
        # way a daemon-less container build does. `list` prints
        # $STUB_INSTALLED. STUB_FAIL_MATCH makes any call whose argv contains
        # that string fail.
        self._stub("flatpak", """
            echo "flatpak $*" >>"$STUB_CALLS"
            if [[ -n "${STUB_FAIL_MATCH:-}" && " $* " == *"${STUB_FAIL_MATCH}"* ]]; then
                exit 1
            fi
            case "$1" in
                remote-add)
                    mkdir -p "$SANDBOX_ROOT/var/lib/flatpak/repo/refs"
                    echo live >"$SANDBOX_ROOT/var/lib/flatpak/repo/marker"
                    ;;
                install)
                    if [[ " $* " == *" installer-local "* ]]; then
                        app="${@: -1}"
                        mkdir -p "$SANDBOX_ROOT/var/lib/flatpak/app/$app/x86_64/master/abc123"
                    fi
                    ;;
                list) cat "$STUB_INSTALLED" ;;
                uninstall)
                    if [[ " $* " == *" --unused "* && -n "${STUB_DROP_REPO_ON_PRUNE:-}" ]]; then
                        rm -rf "$SANDBOX_ROOT/var/lib/flatpak/repo"
                    fi
                    ;;
            esac
            exit 0
        """)

    def write_list(self, body, path="tmp/flatpaks-list"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    def run_script(self, **env):
        full_env = {
            "PATH": str(self.bindir),
            "HOME": str(self.sandbox),
            "SANDBOX_ROOT": str(self.root),
            "STUB_CALLS": str(self.calls),
            "STUB_INSTALLED": str(self.installed),
        }
        full_env.update(env)
        bash = shutil.which("bash")
        return subprocess.run(
            [bash, str(self.script)],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def flatpak_calls(self):
        return [ln[len("flatpak "):] for ln in self.calls.read_text().splitlines() if ln.startswith("flatpak ")]

    def curl_urls(self):
        return [ln[len("curl "):] for ln in self.calls.read_text().splitlines() if ln.startswith("curl ")]

    def reconcile_installs(self):
        return [c for c in self.flatpak_calls() if c.startswith("install ") and "--or-update" in c]


class TestSandbox(unittest.TestCase):
    def test_every_absolute_path_is_sandboxed(self):
        """A new hardcoded path must be added to ROOTED_PATHS, not leak to the host."""
        rewritten = _sandboxed_script_text()
        for line_no, line in enumerate(rewritten.splitlines(), start=1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            for m in re.finditer(r"(?<![\w$}/.:-])/(?:var|etc|run|tmp|src)/", code):
                self.fail(f"line {line_no} still names a host path: {line.strip()!r}")


class TestInstallerChannel(InstallFlatpaksHarness):
    def test_stable_channel_installs_the_stable_bundle(self):
        self.assert_ok(self.run_script())
        self.assertEqual(
            self.curl_urls(),
            [f"https://github.com/tuna-os/bootc-installer/releases/latest/download/{STABLE_ID}.flatpak"],
        )
        calls = self.flatpak_calls()
        self.assertIn(f"install --system --noninteractive installer-local {STABLE_ID}", calls)
        self.assertIn(f"override --system --filesystem=/etc:ro {STABLE_ID}", calls)
        self.assertIn("remote-delete --system --force installer-local", calls)
        self.assertFalse((self.root / "tmp/installer-local-repo").exists(), "local import repo left behind")
        self.assertFalse((self.root / "tmp/tuna-installer.flatpak").exists(), "downloaded bundle left behind")

    def test_dev_channel_installs_the_devel_bundle_and_app_id(self):
        self.assert_ok(self.run_script(INSTALLER_CHANNEL="dev"))
        self.assertEqual(
            self.curl_urls(),
            [f"https://github.com/tuna-os/bootc-installer/releases/latest/download/{DEVEL_ID}.flatpak"],
        )
        calls = self.flatpak_calls()
        self.assertIn(f"install --system --noninteractive installer-local {DEVEL_ID}", calls)
        self.assertIn(f"override --system --filesystem=/etc:ro {DEVEL_ID}", calls)
        self.assertNotIn(f"override --system --filesystem=/etc:ro {STABLE_ID}", calls)

    def test_failed_download_fails_the_build(self):
        result = self.run_script(STUB_CURL_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("installer-local" in c for c in self.flatpak_calls()))

    def test_installer_falls_back_to_update_when_install_fails(self):
        self.assert_ok(self.run_script(STUB_FAIL_MATCH=f"install --system --noninteractive installer-local {STABLE_ID}"))
        self.assertIn(f"update --system --noninteractive {STABLE_ID}", self.flatpak_calls())

    def test_installer_install_and_update_both_failing_fails_the_build(self):
        result = self.run_script(STUB_FAIL_MATCH=f" {STABLE_ID} ")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.reconcile_installs(), [], "reconciled apps after the installer failed")

    def test_missing_active_symlink_is_created(self):
        self.assert_ok(self.run_script())
        active = self.root / f"var/lib/flatpak/app/{STABLE_ID}/x86_64/master/active"
        self.assertTrue(active.is_symlink(), "active symlink not created")
        self.assertEqual(os.readlink(active), "abc123")

    def test_existing_active_symlink_is_left_alone(self):
        branch = self.root / f"var/lib/flatpak/app/{STABLE_ID}/x86_64/master"
        (branch / "def456").mkdir(parents=True)
        (branch / "active").symlink_to("def456")
        self.assert_ok(self.run_script())
        self.assertEqual(os.readlink(branch / "active"), "def456")


class TestListParsing(InstallFlatpaksHarness):
    def test_comments_and_blank_lines_are_skipped_and_flathub_apps_batched(self):
        self.write_list(textwrap.dedent("""\
            # shared list
            org.gnome.Calculator

              # indented comment
            org.mozilla.firefox
               \t
            """))
        self.assert_ok(self.run_script())
        self.assertEqual(
            self.reconcile_installs(),
            ["install --system --noninteractive --no-related --or-update flathub org.gnome.Calculator org.mozilla.firefox"],
        )

    def test_remote_prefixed_entry_adds_that_remote_and_installs_from_it(self):
        self.write_list("org.gnome.Calculator\ntuna-os:com.mitchellh.ghostty\n")
        self.assert_ok(self.run_script())
        calls = self.flatpak_calls()
        self.assertIn(
            "remote-add --system --if-not-exists tuna-os https://tunaos.org/flatpak/tuna-os.flatpakrepo", calls
        )
        self.assertCountEqual(
            self.reconcile_installs(),
            [
                "install --system --noninteractive --no-related --or-update flathub org.gnome.Calculator",
                "install --system --noninteractive --no-related --or-update tuna-os com.mitchellh.ghostty",
            ],
        )
        flathub_adds = [c for c in calls if c.startswith("remote-add") and " flathub " in f"{c} "]
        self.assertEqual(len(flathub_adds), 1, f"flathub re-added during reconcile: {flathub_adds}")

    def test_unknown_remote_fails_the_build_before_reconciling(self):
        self.write_list("org.gnome.Calculator\nnosuch:org.example.App\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown flatpak remote 'nosuch'", result.stderr)
        self.assertEqual(self.reconcile_installs(), [])
        self.assertFalse(any(c.startswith("uninstall") for c in self.flatpak_calls()))

    def test_failed_app_install_fails_the_build(self):
        result = self.run_script(STUB_FAIL_MATCH="--or-update flathub")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "var/cache/flatpak-dl/repo").exists(), "cache saved after a failed install")


class TestVariantOverride(InstallFlatpaksHarness):
    def setUp(self):
        super().setUp()
        self.write_list("org.shared.App\n")
        self.write_list("tuna-os:com.mitchellh.ghostty\n", path="src/utah/flatpaks")

    def installed_apps(self):
        return " ".join(self.reconcile_installs())

    def test_default_target_uses_the_shared_list(self):
        self.assert_ok(self.run_script())
        self.assertIn("org.shared.App", self.installed_apps())
        self.assertNotIn("ghostty", self.installed_apps())

    def test_variant_list_overrides_the_shared_list(self):
        for target in ("utah", "utah-nvidia", "utah-nvidia-open"):
            with self.subTest(target=target):
                self.calls.write_text("")
                self.assert_ok(self.run_script(TARGET=target))
                self.assertIn("com.mitchellh.ghostty", self.installed_apps())
                self.assertNotIn("org.shared.App", self.installed_apps())

    def test_variant_without_its_own_list_uses_the_shared_list(self):
        self.assert_ok(self.run_script(TARGET="bluefin-nvidia"))
        self.assertIn("org.shared.App", self.installed_apps())


class TestReconcileRemovals(InstallFlatpaksHarness):
    def test_dropped_apps_are_removed_but_wanted_apps_and_installers_kept(self):
        self.write_list("org.gnome.Calculator\ntuna-os:com.mitchellh.ghostty\n")
        self.installed.write_text(
            "\n".join([STABLE_ID, DEVEL_ID, "org.gnome.Calculator", "com.mitchellh.ghostty", "org.old.Dropped"]) + "\n"
        )
        self.assert_ok(self.run_script())
        uninstalls = [c for c in self.flatpak_calls() if c.startswith("uninstall")]
        self.assertEqual(
            uninstalls,
            [
                "uninstall --system --noninteractive org.old.Dropped",
                "uninstall --system --noninteractive --unused",
            ],
        )

    def test_app_id_prefix_of_a_wanted_app_is_still_removed(self):
        self.write_list("org.gnome.Calculator.Extra\n")
        self.installed.write_text("org.gnome.Calculator\n")
        self.assert_ok(self.run_script())
        self.assertIn("uninstall --system --noninteractive org.gnome.Calculator", self.flatpak_calls())

    def test_failed_removal_does_not_fail_the_build(self):
        self.installed.write_text("org.old.Dropped\n")
        self.assert_ok(self.run_script(STUB_FAIL_MATCH="org.old.Dropped"))


class TestMachineId(InstallFlatpaksHarness):
    def test_build_time_machine_id_is_blanked_afterwards(self):
        (self.root / "var/lib/dbus/machine-id").write_text("stale\n")
        self.assert_ok(self.run_script())
        self.assertIn("systemd-machine-id-setup", self.calls.read_text())
        machine_id = self.root / "etc/machine-id"
        self.assertTrue(machine_id.exists(), "machine-id removed instead of blanked")
        self.assertEqual(machine_id.read_text(), "", "build-time machine id shipped in the image")
        self.assertFalse((self.root / "var/lib/dbus/machine-id").exists())

    def test_empty_machine_id_is_treated_as_missing(self):
        (self.root / "etc/machine-id").write_text("")
        self.assert_ok(self.run_script())
        self.assertIn("systemd-machine-id-setup", self.calls.read_text())
        self.assertEqual((self.root / "etc/machine-id").read_text(), "")

    def test_existing_machine_id_is_kept(self):
        (self.root / "etc/machine-id").write_text("preexisting\n")
        (self.root / "var/lib/dbus/machine-id").write_text("preexisting\n")
        self.assert_ok(self.run_script())
        self.assertNotIn("systemd-machine-id-setup", self.calls.read_text())
        self.assertEqual((self.root / "etc/machine-id").read_text(), "preexisting\n")
        self.assertTrue((self.root / "var/lib/dbus/machine-id").exists())

    def test_session_bus_address_is_kept_when_already_set(self):
        self.assert_ok(self.run_script(DBUS_SESSION_BUS_ADDRESS="unix:path=/existing"))
        self.assertNotIn("dbus-daemon --session", self.calls.read_text())


class TestBuildCache(InstallFlatpaksHarness):
    def setUp(self):
        super().setUp()
        self.cache_repo = self.root / "var/cache/flatpak-dl/repo"

    def seed_cache(self):
        (self.cache_repo / "refs").mkdir(parents=True)
        (self.cache_repo / "objects").mkdir()
        (self.cache_repo / "objects" / "warm").write_text("warm\n")

    def test_warm_cache_seeds_the_system_repo_without_rsync(self):
        self.seed_cache()
        result = self.run_script()
        self.assert_ok(result)
        self.assertIn("Seeding flatpak repo from build cache", result.stdout)
        self.assertEqual((self.root / "var/lib/flatpak/repo/objects/warm").read_text(), "warm\n")

    def test_cold_cache_skips_seeding(self):
        result = self.run_script()
        self.assert_ok(result)
        self.assertNotIn("Seeding flatpak repo from build cache", result.stdout)

    def test_save_replaces_the_cache_with_the_built_repo(self):
        self.seed_cache()
        self.assert_ok(self.run_script())
        self.assertEqual((self.cache_repo / "marker").read_text(), "live\n")
        self.assertFalse((self.root / "var/cache/flatpak-dl/repo.new").exists())
        self.assertFalse((self.root / "var/cache/flatpak-dl/repo.old").exists())

    def test_failed_save_keeps_the_previous_warm_cache(self):
        self.seed_cache()
        result = self.run_script(STUB_DROP_REPO_ON_PRUNE="1")
        self.assert_ok(result)
        self.assertIn("cache save failed; keeping previous warm cache", result.stderr)
        self.assertEqual((self.cache_repo / "objects" / "warm").read_text(), "warm\n")
        self.assertFalse((self.root / "var/cache/flatpak-dl/repo.new").exists())

    def test_rsync_is_preferred_when_present(self):
        self._stub("rsync", 'echo "rsync $*" >>"$STUB_CALLS"\n')
        self.seed_cache()
        self.assert_ok(self.run_script())
        rsyncs = [ln for ln in self.calls.read_text().splitlines() if ln.startswith("rsync ")]
        self.assertEqual(len(rsyncs), 2, rsyncs)
        self.assertIn("--ignore-existing", rsyncs[0])
        self.assertIn("--delete", rsyncs[1])


if __name__ == "__main__":
    unittest.main()
