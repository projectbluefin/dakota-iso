"""Guard the offline-install contract: the image a live ISO embeds must be the
image the installer asks for.

`.github/workflows/build-iso-bluefin.yml` pulls `matrix.payload_image` and
embeds exactly that one image into the live squashfs containers-storage.
`live/src/configure-live.sh` independently reads `live/src/<variant>/nvidia_imgref`
and writes it into the generated recipe as `local_imgref`, which is what the
installer resolves at install time. The two are wired together only by
convention, so they can drift silently and the drift is invisible until an
offline install fails on a booted ISO.

That is not hypothetical: the Utah ISO embedded `utah:testing` while the
installer requested `utah-nvidia:testing`, and every offline install died with
`pull failed attempt podman pull containers-storage utah-nvidia:testing`.

The same refs are duplicated in the top-level `<variant>/` config dir and in
`live/src/<variant>/images.json`; each copy can drift on its own, so all of
them are checked here against the workflow matrix as the single source of truth.
"""

import json
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
BUILD_ISO_BLUEFIN_WORKFLOW = REPO / ".github" / "workflows" / "build-iso-bluefin.yml"


def _matrix_entries():
    """Return the build-iso-bluefin.yml matrix includes keyed by variant."""
    with BUILD_ISO_BLUEFIN_WORKFLOW.open() as handle:
        workflow = yaml.safe_load(handle)

    includes = []
    for job in workflow["jobs"].values():
        strategy = job.get("strategy") or {}
        matrix = strategy.get("matrix") or {}
        includes.extend(matrix.get("include") or [])

    return {entry["variant"]: entry for entry in includes if "variant" in entry}


def _read_scalar(path: Path) -> str:
    """Read a single-value variant config file the way the build scripts do."""
    return path.read_text().strip()


class TestVariantPayloadConsistency(unittest.TestCase):
    """Every copy of a variant's image refs must name the same images."""

    @classmethod
    def setUpClass(cls):
        cls.entries = _matrix_entries()

    def test_matrix_is_not_empty(self):
        """A parsing regression must fail loudly, not silently skip every check."""
        self.assertTrue(
            self.entries,
            f"no matrix includes with a 'variant' key found in "
            f"{BUILD_ISO_BLUEFIN_WORKFLOW.name} — the consistency checks below "
            f"would silently pass over every variant",
        )

    def test_embedded_payload_is_what_the_installer_requests(self):
        """live/src/<variant>/nvidia_imgref must equal the embedded payload_image.

        configure-live.sh writes `local_imgref = containers-storage:<nvidia_imgref>`
        for both the composefs and the bootcDirect path, so any other value names
        an image that is not in the ISO's offline store.
        """
        for variant, entry in self.entries.items():
            with self.subTest(variant=variant):
                nvidia_imgref = _read_scalar(
                    REPO / "live" / "src" / variant / "nvidia_imgref"
                )
                self.assertEqual(
                    nvidia_imgref,
                    entry["payload_image"],
                    f"live/src/{variant}/nvidia_imgref is {nvidia_imgref!r} but the "
                    f"ISO embeds {entry['payload_image']!r}; offline install will "
                    f"fail with 'pull failed attempt podman pull containers-storage "
                    f"{nvidia_imgref}'",
                )

    def test_top_level_variant_dir_matches_the_matrix(self):
        """<variant>/{payload_ref,live_target,registry,tag} must match the matrix."""
        field_to_matrix_key = {
            "payload_ref": "payload_image",
            "live_target": "live_target",
            "registry": "registry",
            "tag": "tag",
        }

        for variant, entry in self.entries.items():
            for field, matrix_key in field_to_matrix_key.items():
                with self.subTest(variant=variant, field=field):
                    value = _read_scalar(REPO / variant / field)
                    self.assertEqual(
                        value,
                        str(entry[matrix_key]),
                        f"{variant}/{field} is {value!r} but "
                        f"{BUILD_ISO_BLUEFIN_WORKFLOW.name} uses "
                        f"{str(entry[matrix_key])!r} for matrix key {matrix_key!r}",
                    )

    def test_images_json_names_the_same_refs_as_the_scalar_files(self):
        """images.json feeds the installer's catalogue and must not drift either."""
        for variant in self.entries:
            with self.subTest(variant=variant):
                variant_src = REPO / "live" / "src" / variant
                base_imgref = _read_scalar(variant_src / "base_imgref")
                nvidia_imgref = _read_scalar(variant_src / "nvidia_imgref")

                with (variant_src / "images.json").open() as handle:
                    catalogue = json.load(handle)

                self.assertEqual(
                    catalogue["default_image"],
                    base_imgref,
                    f"live/src/{variant}/images.json default_image does not match "
                    f"live/src/{variant}/base_imgref",
                )

                images = catalogue.get("images") or []
                self.assertTrue(
                    images,
                    f"live/src/{variant}/images.json lists no images",
                )

                for image in images:
                    self.assertEqual(
                        image["imgref"],
                        base_imgref,
                        f"live/src/{variant}/images.json entry {image['name']!r} "
                        f"imgref does not match live/src/{variant}/base_imgref",
                    )
                    self.assertEqual(
                        image["nvidia_imgref"],
                        nvidia_imgref,
                        f"live/src/{variant}/images.json entry {image['name']!r} "
                        f"nvidia_imgref does not match "
                        f"live/src/{variant}/nvidia_imgref, so the catalogue offers "
                        f"an image the ISO does not carry",
                    )


if __name__ == "__main__":
    unittest.main()
