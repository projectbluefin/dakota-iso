"""Guard the offline-install contract: the image a live ISO embeds must be the
image the installer asks for.

`.github/workflows/build-iso-utah.yml` pulls `matrix.payload_image` and
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
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
BUILD_ISO_UTAH_WORKFLOW = REPO / ".github" / "workflows" / "build-iso-utah.yml"
BUILD_ISO_DAKOTA_WORKFLOW = REPO / ".github" / "workflows" / "build-iso.yml"
CONFIGURE_LIVE = REPO / "live" / "src" / "configure-live.sh"
SHARED_IMAGES_JSON = REPO / "live" / "src" / "etc" / "bootc-installer" / "images.json"


def _matrix_entries():
    """Return the build-iso-utah.yml matrix includes keyed by variant."""
    with BUILD_ISO_UTAH_WORKFLOW.open() as handle:
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
            f"{BUILD_ISO_UTAH_WORKFLOW.name} — the consistency checks below "
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
                        f"{BUILD_ISO_UTAH_WORKFLOW.name} uses "
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


def _dakota_workflow_runs():
    """Return the concatenated `run:` scripts of build-iso.yml."""
    with BUILD_ISO_DAKOTA_WORKFLOW.open() as handle:
        workflow = yaml.safe_load(handle)

    runs = []
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            if "run" in step:
                runs.append(step["run"])
    return "\n".join(runs)


def _configure_live_default(key: str) -> str:
    """Return the literal default configure-live.sh uses for read_variant_config <key>."""
    match = re.search(
        rf'read_variant_config {re.escape(key)} "([^"]+)"', CONFIGURE_LIVE.read_text()
    )
    if match is None:
        raise AssertionError(
            f"configure-live.sh no longer reads {key!r} via read_variant_config "
            f"with a literal default; update this guard"
        )
    return match.group(1)


class TestDakotaPayloadConsistency(unittest.TestCase):
    """The published Dakota ISO must embed the image its installer requests.

    Dakota has no matrix and no live/src/dakota/ dir, so none of the checks
    above reach it. Its refs live in four places wired only by convention:

    - `.github/workflows/build-iso.yml` hardcodes the payload it pulls and
      embeds, and the live container TARGET, for the *published* ISO;
    - `dakota/{payload_ref,live_target}` drive `just iso-sd-boot dakota`,
      which is what the LUKS / plain-install E2E gates build and test;
    - `live/src/configure-live.sh` falls back to literal defaults for
      `base_imgref` / `nvidia_imgref`, which become the installer's
      `imgref` / `local_imgref` because live/src/dakota/ does not exist;
    - the shared `live/src/etc/bootc-installer/images.json` catalogue.

    `dakota/payload_ref` is treated as the source of truth.
    """

    @classmethod
    def setUpClass(cls):
        cls.payload_ref = _read_scalar(REPO / "dakota" / "payload_ref")
        cls.live_target = _read_scalar(REPO / "dakota" / "live_target")
        cls.runs = _dakota_workflow_runs()

    def test_dakota_has_no_variant_src_dir(self):
        """The default-based checks below only hold while live/src/dakota/ is absent."""
        self.assertFalse(
            (REPO / "live" / "src" / "dakota").exists(),
            "live/src/dakota/ now exists, so configure-live.sh reads it instead of "
            "its literal defaults; extend TestVariantPayloadConsistency to dakota",
        )

    def test_published_iso_embeds_the_tested_payload(self):
        """Every image build-iso.yml pulls or embeds must be dakota/payload_ref."""
        refs = re.findall(r"(?:--oci-image|\bIMAGE=)\s*(\S+)", self.runs)
        self.assertTrue(
            refs,
            f"no `--oci-image` / `IMAGE=` found in {BUILD_ISO_DAKOTA_WORKFLOW.name}; "
            f"this guard would silently pass",
        )
        for ref in refs:
            with self.subTest(ref=ref):
                self.assertEqual(
                    ref,
                    self.payload_ref,
                    f"{BUILD_ISO_DAKOTA_WORKFLOW.name} embeds {ref!r} but "
                    f"dakota/payload_ref (what the E2E gates test) is "
                    f"{self.payload_ref!r}",
                )

    def test_published_iso_boots_the_tested_live_target(self):
        """build-iso.yml's live container TARGET must be dakota/live_target."""
        targets = re.findall(r"--build-arg TARGET=(\S+)", self.runs)
        self.assertTrue(
            targets,
            f"no `--build-arg TARGET=` found in {BUILD_ISO_DAKOTA_WORKFLOW.name}",
        )
        for target in targets:
            with self.subTest(target=target):
                self.assertEqual(
                    target,
                    self.live_target,
                    f"{BUILD_ISO_DAKOTA_WORKFLOW.name} builds TARGET={target!r} but "
                    f"dakota/live_target is {self.live_target!r}",
                )

    def test_installer_requests_the_embedded_payload(self):
        """configure-live.sh's nvidia_imgref default becomes local_imgref for dakota."""
        nvidia_default = _configure_live_default("nvidia_imgref")
        self.assertEqual(
            nvidia_default,
            self.payload_ref,
            f"configure-live.sh defaults nvidia_imgref to {nvidia_default!r} but the "
            f"Dakota ISO embeds {self.payload_ref!r}; offline install will fail with "
            f"'pull failed attempt podman pull containers-storage {nvidia_default}'",
        )

    def test_shared_images_json_matches_configure_live_defaults(self):
        """The shared catalogue must offer the same refs the recipe is built from."""
        base_default = _configure_live_default("base_imgref")
        nvidia_default = _configure_live_default("nvidia_imgref")

        with SHARED_IMAGES_JSON.open() as handle:
            catalogue = json.load(handle)

        self.assertEqual(
            catalogue["default_image"],
            base_default,
            "live/src/etc/bootc-installer/images.json default_image does not match "
            "configure-live.sh's base_imgref default",
        )
        images = catalogue.get("images") or []
        self.assertTrue(images, "shared images.json lists no images")
        for image in images:
            with self.subTest(image=image.get("name")):
                self.assertEqual(image["imgref"], base_default)
                self.assertEqual(
                    image["nvidia_imgref"],
                    nvidia_default,
                    f"shared images.json entry {image['name']!r} offers "
                    f"{image['nvidia_imgref']!r}, which the Dakota ISO does not carry",
                )


if __name__ == "__main__":
    unittest.main()
