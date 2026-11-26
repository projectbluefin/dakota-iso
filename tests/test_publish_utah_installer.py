"""publish-utah-installer.yml: the Utah download is the gnome-setup medium."""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


class PublishUtahInstallerTests(unittest.TestCase):
    workflow = load("publish-utah-installer.yml")

    def test_it_publishes_only_what_passed_the_e2e(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(jobs["e2e"]["uses"], "./.github/workflows/utah-image-installer.yml")
        self.assertEqual(jobs["e2e"]["with"]["image_ref"], "${{ needs.resolve.outputs.ref }}")
        self.assertEqual(set(jobs["publish"]["needs"]), {"resolve", "e2e"})
        self.assertEqual(jobs["publish"]["if"], "github.ref == 'refs/heads/main'")

    def test_the_source_is_the_signature_verified_stable_digest(self):
        run = "\n".join(s.get("run", "") for s in self.workflow["jobs"]["resolve"]["steps"])
        self.assertIn("scripts/verify-image-signature.sh ghcr.io/projectbluefin/utah:stable", run)

    def test_the_published_medium_has_no_test_credentials(self):
        run = "\n".join(s.get("run", "") for s in self.workflow["jobs"]["publish"]["steps"])
        for debug in ("UTAH_RAW_LIVE_KARGS", "UTAH_RAW_SSH_PUBKEY", "UTAH_RAW_DEBUG_SSHD", "utah.live.debug"):
            self.assertNotIn(debug, run)
        self.assertIn("cosign sign-blob", run)

    def test_permissions_are_empty_but_signing(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["jobs"]["publish"]["permissions"],
                         {"contents": "read", "id-token": "write"})

    def test_the_fisherman_utah_iso_is_off_the_schedule(self):
        self.assertNotIn("schedule", load("build-iso-utah.yml")[True])
        self.assertIn("schedule", self.workflow[True])

    def test_the_e2e_workflow_can_be_called(self):
        on = load("utah-image-installer.yml")[True]
        self.assertIn("image_ref", on["workflow_call"]["inputs"])
