"""Every external base image in live/Containerfile must be pinned by digest.

The ISO is signed, so everything that ends up inside it has to come from a
reference that cannot move underneath us. The projectbluefin base is pinned at
build time through the BASE_DIGEST build-arg; this gate makes sure any other
registry image a stage starts FROM carries a literal @sha256 digest, so a tag
retagged upstream cannot silently change what the live initramfs is built with.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
CONTAINERFILE = REPO / "live" / "Containerfile"

FROM_RE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
BUILD_ARG_DIGEST = "${BASE_DIGEST}"


def external_bases(text):
    """Return the image references of FROM lines that are not stage references."""
    stages = set()
    bases = []
    for line in text.splitlines():
        match = FROM_RE.match(line.strip())
        if not match:
            continue
        image, stage = match.groups()
        if image not in stages:
            bases.append(image)
        if stage:
            stages.add(stage)
    return bases


def unpinned_bases(text):
    return [
        image
        for image in external_bases(text)
        if not (DIGEST_RE.search(image) or image.endswith(BUILD_ARG_DIGEST))
    ]


class ContainerfilePinnedBasesTest(unittest.TestCase):
    def test_every_external_base_is_pinned(self):
        text = CONTAINERFILE.read_text()
        self.assertTrue(external_bases(text), "no FROM lines found in live/Containerfile")
        self.assertEqual(unpinned_bases(text), [])


class ParserTest(unittest.TestCase):
    def test_stage_references_are_not_bases(self):
        text = "FROM a:1@sha256:" + "0" * 64 + " AS ref\nFROM ref AS two\nFROM two\n"
        self.assertEqual(external_bases(text), ["a:1@sha256:" + "0" * 64])
        self.assertEqual(unpinned_bases(text), [])

    def test_build_arg_digest_counts_as_pinned(self):
        text = "FROM ghcr.io/${REGISTRY}/${TARGET}:${TAG}${BASE_DIGEST} AS ref\n"
        self.assertEqual(unpinned_bases(text), [])

    def test_tag_only_base_is_reported(self):
        text = "FROM debian:bookworm AS builder\nFROM ghcr.io/x/y:z@sha256:" + "f" * 64 + "\n"
        self.assertEqual(unpinned_bases(text), ["debian:bookworm"])

    def test_short_or_uppercase_digest_is_reported(self):
        self.assertEqual(unpinned_bases("FROM debian:bookworm@sha256:abc\n"), ["debian:bookworm@sha256:abc"])
        self.assertEqual(unpinned_bases("FROM debian:bookworm@sha256:" + "A" * 64 + "\n"), ["debian:bookworm@sha256:" + "A" * 64])
