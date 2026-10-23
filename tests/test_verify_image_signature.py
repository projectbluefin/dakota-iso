"""Unit tests for scripts/verify-image-signature.sh.

The script is the only gate between a mutable registry tag (``utah:testing``,
``dakota-nvidia:stable``) and the ISO: every caller trusts the digest it
prints. These tests run the real script under ``bash`` with ``cosign`` and
``jq`` replaced by recording stubs and assert the verification contract:

1. The signing repository is pinned per image family with
   ``--certificate-github-workflow-repository`` -- Utah and Bluefin sign via
   the shared ``projectbluefin/actions`` reusable workflow, so the SAN alone
   cannot say which repository produced the image, but the certificate's
   repository extension can.
2. The identity regexp requires a GitHub Actions workflow identity inside the
   organisation and nothing looser.
3. Images with no known producer, and images outside
   ``ghcr.io/projectbluefin/``, are refused before cosign runs.
4. The digest printed is the one cosign returned, and a verify that returns
   no digest fails instead of printing an unpinned reference.
"""

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "verify-image-signature.sh"

HOST_TOOLS = ("mkdir", "rm", "cat", "head", "sed", "grep", "uname", "mktemp", "chmod", "sha256sum")

DIGEST = "sha256:" + "ab" * 32


class VerifyImageSignatureHarness(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="verify-image-signature-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.calls = self.sandbox / "calls.log"
        self.calls.touch()
        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        for tool in HOST_TOOLS:
            real = shutil.which(tool)
            self.assertIsNotNone(real, f"host has no {tool}")
            (self.bindir / tool).symlink_to(real)
        self._stub("cosign", """
            echo "cosign $*" >>"$STUB_CALLS"
            [[ -n "${STUB_COSIGN_FAIL:-}" ]] && { echo "Error: no matching signatures" >&2; exit 1; }
            printf '[{"critical":{"image":{"docker-manifest-digest":"%s"}}}]\\n' "${STUB_DIGEST}"
        """)
        self._stub("curl", 'echo "curl $*" >>"$STUB_CALLS"; exit 1\n')
        # Real jq if present, otherwise a stub that answers the one query the
        # script makes with the digest the cosign stub emitted.
        real_jq = shutil.which("jq")
        if real_jq:
            (self.bindir / "jq").symlink_to(real_jq)
        else:
            self._stub("jq", 'cat >/dev/null; echo "${STUB_DIGEST}"\n')

    def _stub(self, name, body):
        path = self.bindir / name
        path.write_text(f"#!{shutil.which('bash')}\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def run_script(self, ref, **env):
        full_env = {
            "PATH": str(self.bindir),
            "HOME": str(self.sandbox),
            "STUB_CALLS": str(self.calls),
            "STUB_DIGEST": DIGEST,
        }
        full_env.update(env)
        return subprocess.run(
            [shutil.which("bash"), str(SCRIPT), ref],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def cosign_calls(self):
        return [ln[len("cosign "):] for ln in self.calls.read_text().splitlines() if ln.startswith("cosign ")]

    def cosign_flag(self, call, name):
        m = re.search(rf"(?:^| ){re.escape(name)}=(\S+)", call)
        self.assertIsNotNone(m, f"{name} not passed in: {call}")
        return m.group(1)


class TestSigningRepositoryPin(VerifyImageSignatureHarness):
    CASES = {
        "ghcr.io/projectbluefin/utah:testing": "projectbluefin/utah",
        "ghcr.io/projectbluefin/utah-nvidia:testing": "projectbluefin/utah",
        "ghcr.io/projectbluefin/dakota:stable": "projectbluefin/dakota",
        "ghcr.io/projectbluefin/dakota-nvidia:stable": "projectbluefin/dakota",
        "ghcr.io/projectbluefin/bluefin:stable": "projectbluefin/bluefin",
        "ghcr.io/projectbluefin/bluefin-nvidia:stable": "projectbluefin/bluefin",
        "ghcr.io/projectbluefin/bluefin-lts-hwe:stable": "projectbluefin/bluefin-lts",
        "ghcr.io/projectbluefin/bluefin-lts-hwe-nvidia:stable": "projectbluefin/bluefin-lts",
        "ghcr.io/projectbluefin/utah@sha256:" + "cd" * 32: "projectbluefin/utah",
    }

    def test_each_image_family_is_pinned_to_its_producing_repository(self):
        for ref, expected_repo in self.CASES.items():
            with self.subTest(ref=ref):
                self.calls.write_text("")
                result = self.run_script(ref)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.cosign_calls()
                self.assertEqual(len(calls), 1, calls)
                self.assertEqual(
                    self.cosign_flag(calls[0], "--certificate-github-workflow-repository"), expected_repo
                )
                self.assertEqual(
                    self.cosign_flag(calls[0], "--certificate-oidc-issuer"),
                    "https://token.actions.githubusercontent.com",
                )
                self.assertTrue(calls[0].startswith(f"verify {ref} "), calls[0])

    def test_identity_regexp_requires_a_workflow_identity_in_the_organisation(self):
        self.assertEqual(self.run_script("ghcr.io/projectbluefin/utah:testing").returncode, 0)
        regexp = self.cosign_flag(self.cosign_calls()[0], "--certificate-identity-regexp")
        pattern = re.compile(regexp)
        for identity in (
            "https://github.com/projectbluefin/actions/.github/workflows/reusable-build.yml@refs/tags/v1",
            "https://github.com/projectbluefin/actions/.github/workflows/reusable-build.yml@" + "a" * 40,
            "https://github.com/projectbluefin/dakota/.github/workflows/publish.yml@refs/heads/testing",
        ):
            self.assertRegex(identity, pattern)
        for identity in (
            "https://github.com/projectbluefin/",
            "https://github.com/projectbluefin/utah",
            "https://github.com/projectbluefin/utah/not-a-workflow@refs/heads/main",
            "https://github.com/evil/projectbluefin/.github/workflows/x.yml@refs/heads/main",
            "https://github.com/projectbluefin-fork/utah/.github/workflows/build.yml@refs/heads/main",
            "http://github.com/projectbluefin/utah/.github/workflows/build.yml@refs/heads/main",
        ):
            self.assertNotRegex(identity, pattern)


class TestRefusals(VerifyImageSignatureHarness):
    def test_unknown_image_family_is_refused_before_cosign_runs(self):
        result = self.run_script("ghcr.io/projectbluefin/experimental-thing:latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no known signing repository for image 'experimental-thing'", result.stderr)
        self.assertEqual(self.cosign_calls(), [])
        self.assertEqual(result.stdout, "")

    def test_images_outside_the_project_registry_are_refused(self):
        for ref in (
            "quay.io/projectbluefin/utah:testing",
            "ghcr.io/someone-else/utah:testing",
            "localhost/utah:testing",
            "ghcr.io/projectbluefinx/utah:testing",
        ):
            with self.subTest(ref=ref):
                self.calls.write_text("")
                result = self.run_script(ref)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing to verify non-project image", result.stderr)
                self.assertEqual(self.cosign_calls(), [])

    def test_wrong_argument_count_is_a_usage_error(self):
        result = subprocess.run(
            [shutil.which("bash"), str(SCRIPT)],
            env={"PATH": str(self.bindir)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


class TestDigestContract(VerifyImageSignatureHarness):
    def test_prints_the_digest_pinned_reference_cosign_verified(self):
        result = self.run_script("ghcr.io/projectbluefin/dakota-nvidia:stable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"ghcr.io/projectbluefin/dakota-nvidia@{DIGEST}")
        self.assertIn("signed by projectbluefin/dakota CI", result.stderr)

    def test_failed_verification_prints_nothing(self):
        result = self.run_script("ghcr.io/projectbluefin/utah:testing", STUB_COSIGN_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_verify_without_a_digest_fails_instead_of_printing_an_unpinned_ref(self):
        result = self.run_script("ghcr.io/projectbluefin/utah:testing", STUB_DIGEST="null")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("returned no manifest digest", result.stderr)
        self.assertEqual(result.stdout, "")


class TestScriptInvariants(unittest.TestCase):
    def test_identity_regexp_is_not_the_bare_organisation_prefix(self):
        """Regression guard: '^https://github\\.com/projectbluefin/' alone matched every
        workflow in every repository of the organisation."""
        text = SCRIPT.read_text(encoding="utf-8")
        m = re.search(r"^IDENTITY_REGEXP='([^']+)'$", text, re.M)
        self.assertIsNotNone(m, "IDENTITY_REGEXP not found")
        self.assertIn(r"\.github/workflows/", m.group(1))
        self.assertIn("--certificate-github-workflow-repository=", text)


if __name__ == "__main__":
    unittest.main()
