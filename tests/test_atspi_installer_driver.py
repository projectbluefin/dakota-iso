"""Unit tests for the guest-resident bootc-installer AT-SPI driver."""

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO = Path(__file__).parent.parent
DRIVER = REPO / "scripts" / "atspi-installer-driver.py"


def load_driver():
    spec = importlib.util.spec_from_file_location("atspi_installer_driver", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestInstallerDriverSelectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = load_driver()

    def test_disk_match_requires_the_whole_device_name(self):
        self.assertTrue(self.driver.disk_mentions_target("QEMU disk · /dev/vda · 64 GiB", "/dev/vda"))
        self.assertFalse(self.driver.disk_mentions_target("QEMU disk · /dev/vda1 · 64 GiB", "/dev/vda"))

    def test_disk_toggle_uses_checkbox_descendant_of_matching_row(self):
        nodes = [
            self.driver.NodeSnapshot((0,), "QEMU HARDDISK /dev/vda · 64 GiB", "list item"),
            self.driver.NodeSnapshot((0, 0), "", "check box"),
            self.driver.NodeSnapshot((1,), "scratch disk /dev/vdb · 16 GiB", "list item"),
            self.driver.NodeSnapshot((1, 0), "", "check box"),
        ]

        self.assertEqual(
            self.driver.find_disk_toggle(nodes, "/dev/vda"),
            self.driver.NodeSnapshot((0, 0), "", "check box"),
        )

    def test_disk_toggle_never_selects_a_sibling_control(self):
        nodes = [
            self.driver.NodeSnapshot((0,), "target disk /dev/vda · 64 GiB", "list item"),
            self.driver.NodeSnapshot((0, 0), "disk details", "label"),
            self.driver.NodeSnapshot((1,), "", "check box"),
        ]

        self.assertIsNone(self.driver.find_disk_toggle(nodes, "/dev/vda"))

    def test_page_classification_prioritizes_failure_over_completion(self):
        failed = [
            self.driver.NodeSnapshot((0,), "Installation failed", "label"),
            self.driver.NodeSnapshot((1,), "Finished!", "label"),
        ]
        complete = [self.driver.NodeSnapshot((0,), "Dakota is installed", "label")]
        restart_complete = [
            self.driver.NodeSnapshot(
                (0,), "Restart now to complete the installation.", "label"
            )
        ]

        self.assertEqual(self.driver.classify_page(failed), self.driver.Page.FAILURE)
        self.assertEqual(self.driver.classify_page(complete), self.driver.Page.COMPLETE)
        self.assertEqual(
            self.driver.classify_page(restart_complete), self.driver.Page.COMPLETE
        )

    def test_page_classification_recognizes_all_destructive_confirmations(self):
        disk_confirm = [self.driver.NodeSnapshot((0,), "Confirm Changes", "dialog")]
        install_confirm = [
            self.driver.NodeSnapshot((0,), "Confirm Installation", "label"),
            self.driver.NodeSnapshot((1,), "Become Legend", "push button"),
        ]

        self.assertEqual(
            self.driver.classify_page(disk_confirm),
            self.driver.Page.DISK_CONFIRMATION,
        )
        self.assertEqual(
            self.driver.classify_page(install_confirm),
            self.driver.Page.INSTALL_CONFIRMATION,
        )

    def test_tail_text_keeps_the_latest_lines(self):
        content = "\n".join(f"line {index}" for index in range(5))

        self.assertEqual(self.driver.tail_text(content, 2), "line 3\nline 4")


class TestInstallerDriverFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = load_driver()

    def test_waits_for_the_existing_auto_launched_application(self):
        installer = object()
        session = mock.Mock()
        session.find_installer.side_effect = [None, installer]
        driver = self.driver.InstallerDriver(
            session, "/dev/vda", (), poll_interval=0
        )

        output = io.StringIO()
        with (
            mock.patch.object(self.driver.time, "monotonic", side_effect=[0, 0]),
            mock.patch.object(self.driver.time, "sleep"),
            redirect_stdout(output),
        ):
            self.assertIs(driver.wait_for_auto_launched_installer(1), installer)

        self.assertEqual(session.find_installer.call_count, 2)
        self.assertIn("detected auto-launched bootc-installer", output.getvalue())

    def test_session_recognizes_the_installed_flatpak_application_id(self):
        class Accessible:
            def __init__(self, name, children=()):
                self.name = name
                self.description = ""
                self.children = children

            def __iter__(self):
                return iter(self.children)

        app = Accessible("org.bootcinstaller.Installer")
        atspi = mock.Mock()
        atspi.init.return_value = 0
        atspi.get_desktop.return_value = Accessible("desktop", (app,))

        self.assertIs(self.driver.AtspiSession(atspi).find_installer(), app)

    def test_disk_selection_clicks_only_the_target_row_control(self):
        toggle = self.driver.NodeSnapshot((0, 0), "", "check box")
        nodes = [
            self.driver.NodeSnapshot((0,), "target disk /dev/vda · 64 GiB", "list item"),
            toggle,
            self.driver.NodeSnapshot((1,), "scratch disk /dev/vdb · 16 GiB", "list item"),
            self.driver.NodeSnapshot((1, 0), "", "check box"),
        ]
        target_control = object()
        session = mock.Mock()
        session.is_checked.return_value = False
        driver = self.driver.InstallerDriver(session, "/dev/vda", (), poll_interval=1)

        driver._select_disk(nodes, {toggle.path: target_control})

        session.click.assert_called_once_with(target_control, "select /dev/vda")

    def test_click_uses_the_gobject_introspection_action_interface(self):
        """The available live-image binding exposes get_action(), not queryAction()."""
        atspi = mock.Mock()
        atspi.init.return_value = 0
        atspi.get_desktop.return_value = ()
        session = self.driver.AtspiSession(atspi)
        action = mock.Mock()
        action.get_n_actions.return_value = 1
        action.get_action_name.return_value = "click"
        action.do_action.return_value = True
        node = mock.Mock()
        node.get_action.return_value = action

        session.click(node, "accept test action")

        node.get_action.assert_called_once_with()
        action.get_n_actions.assert_called_once_with()
        action.get_action_name.assert_called_once_with(0)
        action.do_action.assert_called_once_with(0)

    def test_install_confirmation_rejects_a_page_without_the_target_disk(self):
        nodes = [
            self.driver.NodeSnapshot((0,), "Confirm Installation", "label"),
            self.driver.NodeSnapshot((1,), "scratch disk /dev/vdb", "label"),
            self.driver.NodeSnapshot((2,), "Become Legend", "push button"),
        ]
        driver = self.driver.InstallerDriver(mock.Mock(), "/dev/vda", (), poll_interval=1)

        with self.assertRaisesRegex(
            self.driver.DriverError, "does not identify selected target /dev/vda"
        ):
            driver._confirm_install(nodes, {})

    def test_failure_diagnostics_include_accessibility_tree_and_log_tail(self):
        installer = object()
        session = mock.Mock()
        session.find_installer.return_value = installer
        session.snapshots.return_value = (
            [self.driver.NodeSnapshot((0,), "Installation failed", "label")],
            {},
        )
        log = Path("/var/log/bootc-installer.log")
        driver = self.driver.InstallerDriver(
            session, "/dev/vda", (log,), poll_interval=1
        )
        output = io.StringIO()

        with (
            mock.patch.object(
                Path,
                "read_text",
                return_value="\n".join(f"line {index}" for index in range(205)),
            ),
            redirect_stderr(output),
        ):
            driver.diagnose()

        diagnostics = output.getvalue()
        self.assertIn("accessibility tree", diagnostics)
        self.assertIn("Installation failed", diagnostics)
        self.assertIn(f"source: {log}", diagnostics)
        self.assertNotIn("line 0", diagnostics)
        self.assertIn("line 204", diagnostics)


class TestDriverDependencies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = load_driver()

    def test_dependency_check_imports_atspi_without_driving_the_installer(self):
        """gui-e2e uses this against the booted debug ISO before UI actions."""
        output = io.StringIO()
        atspi = mock.Mock(__name__="Atspi")

        with (
            mock.patch.object(
                self.driver.sys, "argv", ["atspi-installer-driver.py", "--check-dependencies"]
            ),
            mock.patch.object(self.driver, "configure_session_environment"),
            mock.patch.object(self.driver, "load_atspi", return_value=atspi) as load,
            redirect_stdout(output),
        ):
            self.assertEqual(self.driver.main(), 0)

        load.assert_called_once_with()
        self.assertIn("AT-SPI driver binding check passed", output.getvalue())

    def test_missing_atspi_typelib_exits_cleanly_in_all_startup_modes(self):
        """PyGObject raises ValueError before import when the typelib is absent."""
        for argv in (
            ["atspi-installer-driver.py", "--check-dependencies"],
            ["atspi-installer-driver.py", "--disk", "/dev/vda"],
        ):
            output = io.StringIO()

            with (
                mock.patch.object(self.driver.sys, "argv", argv),
                mock.patch.object(self.driver, "configure_session_environment"),
                mock.patch.object(
                    self.driver,
                    "load_atspi",
                    side_effect=ValueError("Namespace Atspi not available"),
                ),
                redirect_stderr(output),
            ):
                self.assertEqual(self.driver.main(), 2)

            self.assertIn("requires the GNOME OS PyGObject Atspi binding", output.getvalue())
            self.assertIn("Namespace Atspi not available", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_atspi_initialization_failure_exits_cleanly(self):
        """A missing guest accessibility bus must not cause an unbound-local traceback."""
        atspi = mock.Mock()
        atspi.init.return_value = -1
        output = io.StringIO()

        with (
            mock.patch.object(
                self.driver.sys,
                "argv",
                ["atspi-installer-driver.py", "--disk", "/dev/vda"],
            ),
            mock.patch.object(self.driver, "configure_session_environment"),
            mock.patch.object(self.driver, "load_atspi", return_value=atspi),
            redirect_stderr(output),
        ):
            self.assertEqual(self.driver.main(), 1)

        self.assertIn("could not initialize the AT-SPI accessibility bus", output.getvalue())


if __name__ == "__main__":
    unittest.main()
