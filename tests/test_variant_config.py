"""Unit tests for scripts/variant-config.sh — the single host-side reader for
per-variant ISO configuration.

Two things are pinned here:

1. **Behaviour** — the resolution contract (namespace routing, the
   ``live_target`` → bootloader-variant derivation, and every default) is
   exercised by sourcing the real shell file in a temporary repo-shaped
   fixture, so the contract is checked rather than described.

2. **Single source of truth** — no file outside ``scripts/variant-config.sh``
   may re-implement the reads.  This is the invariant that the refactor
   exists to create; without it the five copies grow back.
"""

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VARIANT_CONFIG = REPO / "scripts" / "variant-config.sh"

# Every host-side consumer of the reader.
CONSUMERS = [
    REPO / "justfile",
    REPO / "scripts" / "iso-sd-boot.sh",
    REPO / "scripts" / "luks-install-qemu.sh",
    REPO / "scripts" / "plain-install-qemu.sh",
    REPO / "scripts" / "build-live-squashfs.sh",
]


def run_reader(tmpdir, snippet, pipefail=True):
    """Source variant-config.sh with tmpdir as cwd and run `snippet`."""
    prelude = "set -euo pipefail\n" if pipefail else ""
    script = f'{prelude}source "{VARIANT_CONFIG}"\n{snippet}\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(tmpdir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"reader failed ({proc.returncode}): {proc.stderr}")
    return proc.stdout


class VariantFixture:
    """Builds a minimal repo-shaped tree: <variant>/ and live/src/<name>/."""

    def __init__(self, root):
        self.root = Path(root)

    def variant(self, name, **files):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        for key, value in files.items():
            (d / key).write_text(value)
        return self

    def live_src(self, name, **files):
        d = self.root / "live" / "src" / name
        d.mkdir(parents=True, exist_ok=True)
        for key, value in files.items():
            (d / key).write_text(value)
        return self


class TestNamespaceAReads(unittest.TestCase):
    """Top-level <variant>/ keys and their defaults."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = VariantFixture(self._tmp.name)

    def test_live_target_is_read_and_trimmed(self):
        self.fx.variant("dakota", live_target="dakota-nvidia\n")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_live_target dakota'),
            "dakota-nvidia",
        )

    def test_live_target_defaults_to_variant_name(self):
        self.fx.variant("stable", tag="stable")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_live_target stable'), "stable"
        )

    def test_tag_and_registry_defaults(self):
        self.fx.variant("lts", live_target="bluefin-lts-hwe-nvidia")
        self.assertEqual(run_reader(self._tmp.name, 'variant_tag lts'), "stable")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_registry lts'), "projectbluefin"
        )

    def test_blank_file_falls_back_to_default(self):
        """A key present but empty must not resolve to the empty string.

        An empty config file is a mistake, not an instruction to use "" as a
        registry or tag.  Treating it as absent is what makes the default set
        meaningful.
        """
        self.fx.variant("lts", registry="   \n")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_registry lts'), "projectbluefin"
        )


class TestBootloaderVariantDerivation(unittest.TestCase):
    """live_target → live/src/<name> key derivation."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = VariantFixture(self._tmp.name)

    def test_strips_nvidia_suffix(self):
        self.fx.variant("bluefin", live_target="bluefin-nvidia")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_bootloader_variant bluefin'),
            "bluefin",
        )

    def test_strips_nvidia_open_suffix(self):
        self.fx.variant("bluefin", live_target="bluefin-nvidia-open")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_bootloader_variant bluefin'),
            "bluefin",
        )

    def test_leaves_unsuffixed_target_alone(self):
        self.fx.variant("lts", live_target="bluefin-lts-hwe")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_bootloader_variant lts'),
            "bluefin-lts-hwe",
        )

    def test_namespace_b_is_keyed_on_derived_name_not_variant_name(self):
        """The config lives under the *derived* name.

        `lts` reads live/src/bluefin-lts-hwe/, not live/src/lts/.  A reader
        keyed on the variant name would silently return defaults here, which
        is precisely the failure this derivation exists to prevent.
        """
        self.fx.variant("lts", live_target="bluefin-lts-hwe-nvidia")
        self.fx.live_src("bluefin-lts-hwe", composefs="false", bootloader="grub")
        self.fx.live_src("lts", composefs="true", bootloader="systemd")
        self.assertEqual(run_reader(self._tmp.name, 'variant_composefs lts'), "false")
        self.assertEqual(run_reader(self._tmp.name, 'variant_bootloader lts'), "grub")


class TestNamespaceBReads(unittest.TestCase):
    """live/src/<name>/ boot-critical keys, their defaults, and recipe forms."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = VariantFixture(self._tmp.name)

    def test_defaults_when_live_src_dir_is_absent(self):
        """Variants with no live/src dir get composefs=true, bootloader=systemd."""
        self.fx.variant("dakota", live_target="dakota-nvidia")
        self.assertEqual(run_reader(self._tmp.name, 'variant_composefs dakota'), "true")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_bootloader dakota'), "systemd"
        )

    def test_explicit_values_win(self):
        self.fx.variant("bluefin", live_target="bluefin-nvidia")
        self.fx.live_src("bluefin", composefs="false\n", bootloader="grub\n")
        self.assertEqual(run_reader(self._tmp.name, 'variant_composefs bluefin'), "false")
        self.assertEqual(run_reader(self._tmp.name, 'variant_bootloader bluefin'), "grub")

    def test_bootloader_recipe_normalises_grub_to_grub2(self):
        """fisherman's recipe validator spells grub as "grub2"."""
        self.fx.variant("bluefin", live_target="bluefin-nvidia")
        self.fx.live_src("bluefin", bootloader="grub")
        self.assertEqual(run_reader(self._tmp.name, 'variant_bootloader "bluefin"'), "grub")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_bootloader_recipe "bluefin"'), "grub2"
        )

    def test_bootloader_recipe_passes_systemd_through(self):
        self.fx.variant("dakota", live_target="dakota-nvidia")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_bootloader_recipe dakota'), "systemd"
        )

    def test_composefs_json_is_strictly_true_or_false(self):
        self.fx.variant("dakota", live_target="dakota-nvidia")
        self.fx.variant("bluefin", live_target="bluefin-nvidia")
        self.fx.live_src("bluefin", composefs="false")
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_composefs_json dakota'), "true"
        )
        self.assertEqual(
            run_reader(self._tmp.name, 'variant_composefs_json bluefin'), "false"
        )

    def test_defaults_do_not_depend_on_caller_pipefail(self):
        """The reader must not need `set -o pipefail` in the caller's scope.

        The idiom this file replaced —
        `$(cat f 2>/dev/null | tr -d '[:space:]' || echo default)` — reaches
        its default only under pipefail, because otherwise the pipeline exits
        0 via `tr` and the `||` never runs.  These are boot-critical values;
        their defaults must not be contingent on a `set` line in the caller.
        """
        self.fx.variant("dakota", live_target="dakota-nvidia")
        for pipefail in (True, False):
            with self.subTest(pipefail=pipefail):
                self.assertEqual(
                    run_reader(
                        self._tmp.name, 'variant_composefs dakota', pipefail=pipefail
                    ),
                    "true",
                )
                self.assertEqual(
                    run_reader(
                        self._tmp.name, 'variant_bootloader dakota', pipefail=pipefail
                    ),
                    "systemd",
                )


class TestSingleSourceOfTruth(unittest.TestCase):
    """No consumer may re-implement the reads that variant-config.sh owns."""

    def test_reader_is_syntactically_valid(self):
        proc = subprocess.run(
            ["bash", "-n", str(VARIANT_CONFIG)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_consumer_reads_namespace_b_directly(self):
        for path in CONSUMERS:
            text = path.read_text()
            for key in ("composefs", "bootloader"):
                needle = f"live/src/${{BOOTLOADER_VARIANT}}/{key}"
                self.assertNotIn(
                    needle,
                    text,
                    f"{path.relative_to(REPO)} reads live/src/<variant>/{key} "
                    f"directly. Use variant_{key}() from scripts/variant-config.sh "
                    f"so the default lives in exactly one place.",
                )

    def test_no_consumer_derives_the_bootloader_variant_itself(self):
        for path in CONSUMERS:
            self.assertNotIn(
                "s/-nvidia-open$//;s/-nvidia$//",
                path.read_text(),
                f"{path.relative_to(REPO)} re-derives the bootloader variant. "
                f"Use variant_bootloader_variant() from "
                f"scripts/variant-config.sh.",
            )

    def test_no_consumer_normalises_grub_itself(self):
        for path in CONSUMERS:
            self.assertNotIn(
                'BOOTLOADER="grub2"',
                path.read_text(),
                f"{path.relative_to(REPO)} normalises grub→grub2 inline. "
                f"Use variant_bootloader_recipe() from "
                f"scripts/variant-config.sh.",
            )

    def test_every_consumer_sources_the_reader(self):
        for path in CONSUMERS:
            self.assertIn(
                "variant-config.sh",
                path.read_text(),
                f"{path.relative_to(REPO)} uses variant config helpers but "
                f"never sources scripts/variant-config.sh.",
            )


if __name__ == "__main__":
    unittest.main()
