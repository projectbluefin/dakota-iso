#!/usr/bin/env python3
"""Drive the already-running bootc-installer through its AT-SPI interface."""

import argparse
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_INSTALLER_LOGS = (
    "/var/log/bootc-installer.log",
    "/var/home/liveuser/.cache/bootc-installer/fisherman-output.log",
    "/home/liveuser/.cache/bootc-installer/fisherman-output.log",
)
INSTALLER_NAMES = (
    "bootc installer",
    "bootc-installer",
    "dakota installer",
    "org.bootcinstaller.installer",
)
BUTTON_ROLES = ("button", "push button")
TOGGLE_ROLES = ("check", "radio", "switch")


class DriverError(RuntimeError):
    """The installer could not be driven safely through its visible UI."""


class Page(Enum):
    WAIT = "waiting"
    NEXT = "next"
    DISK = "disk"
    DISK_CONFIRMATION = "disk-confirmation"
    INSTALL_CONFIRMATION = "install-confirmation"
    PROGRESS = "progress"
    COMPLETE = "complete"
    FAILURE = "failure"


@dataclass(frozen=True)
class NodeSnapshot:
    path: tuple[int, ...]
    name: str
    role: str


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold().strip()


def disk_mentions_target(text: str, disk: str) -> bool:
    """Return whether text names the whole block device, not one of its partitions."""
    return bool(
        re.search(
            rf"(?<![\w.-]){re.escape(disk.casefold())}(?![\w-])",
            normalized(text),
        )
    )


def is_descendant(path: tuple[int, ...], ancestor: tuple[int, ...]) -> bool:
    return path[: len(ancestor)] == ancestor


def find_disk_toggle(
    nodes: Sequence[NodeSnapshot], disk: str
) -> NodeSnapshot | None:
    """Find the checkable control in the accessible row describing ``disk``."""
    matching_rows = [
        node for node in nodes if disk_mentions_target(node.name, disk)
    ]
    candidates: list[tuple[int, NodeSnapshot]] = []
    for row in matching_rows:
        for node in nodes:
            if is_descendant(node.path, row.path) and any(
                role in normalized(node.role) for role in TOGGLE_ROLES
            ):
                candidates.append((len(node.path) - len(row.path), node))
    return min(candidates, default=(0, None), key=lambda item: item[0])[1]


def classify_page(nodes: Sequence[NodeSnapshot]) -> Page:
    """Classify only the visible page from its user-facing accessibility text."""
    text = "\n".join(normalized(node.name) for node in nodes)
    if "installation failed" in text:
        return Page.FAILURE
    if (
        " is installed" in text
        or "restart now to complete the installation" in text
        or ("finished!" in text and "reboot now" in text)
    ):
        return Page.COMPLETE
    if "confirm installation" in text and "become legend" in text:
        return Page.INSTALL_CONFIRMATION
    if "confirm changes" in text:
        return Page.DISK_CONFIRMATION
    if "install location" in text:
        return Page.DISK
    if "installing" in text:
        return Page.PROGRESS
    if any(normalized(node.name) == "next" for node in nodes):
        return Page.NEXT
    return Page.WAIT


def tail_text(content: str, lines: int) -> str:
    return "\n".join(content.splitlines()[-lines:])


def configure_session_environment() -> None:
    """Make a non-graphical SSH login use the live user's existing session bus."""
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    if runtime_dir.is_dir():
        os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime_dir))
        bus = runtime_dir / "bus"
        if bus.exists():
            os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")


def load_atspi():
    """Load GNOME OS's PyGObject AT-SPI binding."""
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    return Atspi


class AtspiSession:
    def __init__(self, atspi):
        self.atspi = atspi
        if atspi.init() not in (0, 1):
            raise DriverError("could not initialize the AT-SPI accessibility bus")
        self.desktop = atspi.get_desktop(0)

    def find_installer(self):
        for application in self._children(self.desktop):
            if self._is_installer_application(application):
                return application
        return None

    def snapshots(
        self, root, visible_only: bool = True, max_depth: int = 12
    ) -> tuple[list[NodeSnapshot], dict[tuple[int, ...], object]]:
        snapshots: list[NodeSnapshot] = []
        objects: dict[tuple[int, ...], object] = {}

        def visit(node, path: tuple[int, ...], depth: int) -> None:
            if depth > max_depth:
                return
            if not visible_only or self._is_visible(node):
                snapshots.append(
                    NodeSnapshot(path, self._node_text(node), self._role(node))
                )
                objects[path] = node
            for index, child in enumerate(self._children(node)):
                visit(child, path + (index,), depth + 1)

        visit(root, (), 0)
        return snapshots, objects

    def is_checked(self, node) -> bool:
        try:
            return node.get_state_set().contains(self.atspi.StateType.CHECKED)
        except Exception:
            return False

    def is_sensitive(self, node) -> bool:
        try:
            return node.get_state_set().contains(self.atspi.StateType.SENSITIVE)
        except Exception:
            return True

    def click(self, node, purpose: str) -> None:
        try:
            action = node.get_action()
            available = [
                (index, normalized(action.get_action_name(index)))
                for index in range(action.get_n_actions())
            ]
        except Exception as error:
            raise DriverError(f"{purpose}: control has no AT-SPI action: {error}") from error

        for preferred in ("click", "activate", "press", "toggle"):
            for index, name in available:
                if name == preferred:
                    if action.do_action(index):
                        return
                    raise DriverError(f"{purpose}: AT-SPI action {name!r} was rejected")
        if len(available) == 1 and action.do_action(available[0][0]):
            return
        actions = ", ".join(name for _, name in available) or "none"
        raise DriverError(f"{purpose}: no usable AT-SPI action (available: {actions})")

    def _is_installer_application(self, application) -> bool:
        name = normalized(self._node_text(application))
        if any(candidate in name for candidate in INSTALLER_NAMES):
            return True
        for child in self._children(application):
            if any(candidate in normalized(self._node_text(child)) for candidate in INSTALLER_NAMES):
                return True
        return False

    def _is_visible(self, node) -> bool:
        try:
            state = node.get_state_set()
            return state.contains(self.atspi.StateType.VISIBLE) and state.contains(
                self.atspi.StateType.SHOWING
            )
        except Exception:
            return True

    @staticmethod
    def _children(node) -> list:
        try:
            return [
                node.get_child_at_index(index)
                for index in range(node.get_child_count())
            ]
        except Exception:
            try:
                return list(node)
            except Exception:
                return []

    @staticmethod
    def _role(node) -> str:
        try:
            return node.get_role_name() or ""
        except Exception:
            return ""

    @staticmethod
    def _node_text(node) -> str:
        values = []
        for accessor, attribute in (
            ("get_name", "name"),
            ("get_description", "description"),
        ):
            try:
                value = getattr(node, accessor)()
            except Exception:
                try:
                    value = getattr(node, attribute)
                except Exception:
                    value = ""
            if value:
                values.append(str(value))
        try:
            value = node.get_text().get_text(0, -1)
            if value:
                values.append(value)
        except Exception:
            pass
        return " · ".join(dict.fromkeys(values))


def find_button(
    nodes: Sequence[NodeSnapshot],
    objects: dict[tuple[int, ...], object],
    label: str,
) -> object | None:
    expected = normalized(label)
    for node in nodes:
        if (
            normalized(node.name) == expected
            and any(role in normalized(node.role) for role in BUTTON_ROLES)
        ):
            return objects[node.path]
    return None


class InstallerDriver:
    def __init__(
        self,
        session: AtspiSession,
        disk: str,
        installer_logs: Iterable[Path],
        poll_interval: float,
    ):
        self.session = session
        self.disk = disk
        self.installer_logs = tuple(installer_logs)
        self.poll_interval = poll_interval

    def wait_for_auto_launched_installer(self, deadline: float):
        while time.monotonic() < deadline:
            installer = self.session.find_installer()
            if installer is not None:
                print(
                    f"AT-SPI: detected auto-launched bootc-installer for {self.disk}",
                    flush=True,
                )
                return installer
            time.sleep(self.poll_interval)
        raise DriverError("timed out waiting for the auto-launched bootc-installer")

    def run(self, installer, deadline: float) -> None:
        while time.monotonic() < deadline:
            nodes, objects = self.session.snapshots(installer)
            page = classify_page(nodes)

            if page is Page.COMPLETE:
                print("AT-SPI: installer reported successful completion", flush=True)
                return
            if page is Page.FAILURE:
                raise DriverError("installer reported failure")
            if page is Page.DISK:
                self._select_disk(nodes, objects)
            elif page is Page.DISK_CONFIRMATION:
                self._click_button(nodes, objects, "Confirm Changes", "accept disk erase")
            elif page is Page.INSTALL_CONFIRMATION:
                self._confirm_install(nodes, objects)
            elif page is Page.NEXT:
                self._click_button(nodes, objects, "Next", "advance installer page")
            elif page is Page.PROGRESS:
                pass
            time.sleep(self.poll_interval)
        raise DriverError("timed out waiting for installer completion")

    def diagnose(self) -> None:
        installer = self.session.find_installer()
        print("=== AT-SPI INSTALLER DIAGNOSTICS ===", file=sys.stderr)
        if installer is None:
            print("Installer accessibility application was not found.", file=sys.stderr)
        else:
            nodes, _ = self.session.snapshots(installer, visible_only=False)
            print("--- accessibility tree ---", file=sys.stderr)
            for node in nodes:
                indent = "  " * len(node.path)
                name = node.name or "<unnamed>"
                print(f"{indent}{node.path}: {node.role}: {name}", file=sys.stderr)
        print("--- installer log ---", file=sys.stderr)
        for log_path in self.installer_logs:
            try:
                content = log_path.read_text(errors="replace")
            except OSError:
                continue
            print(f"source: {log_path}", file=sys.stderr)
            print(tail_text(content, 200) or "<empty>", file=sys.stderr)
            break
        else:
            paths = ", ".join(str(path) for path in self.installer_logs)
            print(f"No installer log found (looked in: {paths})", file=sys.stderr)

    def _select_disk(
        self,
        nodes: Sequence[NodeSnapshot],
        objects: dict[tuple[int, ...], object],
    ) -> None:
        toggle = find_disk_toggle(nodes, self.disk)
        if toggle is None:
            raise DriverError(f"disk selector did not expose requested target {self.disk}")
        disk_control = objects[toggle.path]
        if not self.session.is_checked(disk_control):
            self.session.click(disk_control, f"select {self.disk}")
            print(f"AT-SPI: selected target disk {self.disk}", flush=True)
            return
        self._click_button(nodes, objects, "Use Entire Disk", f"erase {self.disk}")

    def _confirm_install(
        self,
        nodes: Sequence[NodeSnapshot],
        objects: dict[tuple[int, ...], object],
    ) -> None:
        if not any(disk_mentions_target(node.name, self.disk) for node in nodes):
            raise DriverError(
                f"confirmation page does not identify selected target {self.disk}"
            )
        self._click_button(
            nodes,
            objects,
            "Become Legend",
            "confirm destructive installation",
        )

    def _click_button(
        self,
        nodes: Sequence[NodeSnapshot],
        objects: dict[tuple[int, ...], object],
        label: str,
        purpose: str,
    ) -> None:
        button = find_button(nodes, objects, label)
        if button is None:
            raise DriverError(f"{purpose}: visible {label!r} button was not found")
        if not self.session.is_sensitive(button):
            return
        self.session.click(button, purpose)
        print(f"AT-SPI: {purpose}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive the auto-launched bootc-installer via guest AT-SPI."
    )
    parser.add_argument("--disk", help="whole target device, e.g. /dev/vda")
    parser.add_argument(
        "--check-dependencies",
        action="store_true",
        help="verify the GNOME OS AT-SPI binding imports in the debug ISO, then exit",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600,
        help="maximum seconds for discovery and installation (default: 3600)",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=180,
        help="maximum seconds to wait for the auto-launched app (default: 180)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1,
        help="seconds between accessibility polls (default: 1)",
    )
    parser.add_argument(
        "--installer-log",
        action="append",
        default=[],
        help="additional installer log to include in failure diagnostics",
    )
    args = parser.parse_args()
    if not args.check_dependencies and not args.disk:
        parser.error("--disk is required unless --check-dependencies is used")
    if args.disk and not re.fullmatch(r"/dev/[A-Za-z0-9._-]+", args.disk):
        parser.error("--disk must name a whole /dev device")
    if args.timeout <= 0 or args.startup_timeout <= 0 or args.poll_interval <= 0:
        parser.error("timeout values must be positive")
    return args


def main() -> int:
    args = parse_args()
    configure_session_environment()
    try:
        atspi = load_atspi()
    except (ImportError, ValueError, DriverError) as error:
        print(
            f"AT-SPI driver requires the GNOME OS PyGObject Atspi binding: {error}",
            file=sys.stderr,
        )
        return 2
    if args.check_dependencies:
        print(f"AT-SPI driver binding check passed: {atspi.__name__}", flush=True)
        return 0

    deadline = time.monotonic() + args.timeout
    installer_logs = tuple(Path(path) for path in args.installer_log) + tuple(
        Path(path) for path in DEFAULT_INSTALLER_LOGS
    )
    driver = None
    try:
        driver = InstallerDriver(
            AtspiSession(atspi),
            args.disk,
            installer_logs,
            args.poll_interval,
        )
        startup_deadline = min(deadline, time.monotonic() + args.startup_timeout)
        installer = driver.wait_for_auto_launched_installer(startup_deadline)
        driver.run(installer, deadline)
    except (DriverError, KeyboardInterrupt) as error:
        print(f"AT-SPI driver failed: {error}", file=sys.stderr)
        if driver is not None:
            driver.diagnose()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
