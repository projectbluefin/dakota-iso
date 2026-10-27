"""Behavioural unit tests for scripts/bump-installer-bundle.sh.

``scripts/bump-installer-bundle.sh`` is the only supported way to move the
bootc-installer pin in ``live/src/installer-bundle.lock``. ``install-flatpaks.sh``
trusts exactly the tag and SHA-256 values that lock holds, so a regression
here either pins a moving tag (the trust gap the lock exists to close) or
writes a lock the ISO build cannot read. Nothing in CI or the E2E lanes runs
the script; these tests cover:

1. **Tag resolution.** With no argument the tag is taken from the
   ``/releases/latest`` redirect; an explicit tag skips the lookup.
2. **Tag refusal.** Rolling names, an empty tag, a tag containing ``/`` and a
   redirect that never reached a ``/tag/`` URL are refused before anything is
   downloaded, and the existing lock is left alone.
3. **Lock content.** Both bundles are downloaded from the resolved tag and
   their real SHA-256 digests are written in the format install-flatpaks.sh
   parses.
4. **Atomicity.** A failed download leaves the existing lock untouched.

``curl`` is replaced by a recording stub; ``INSTALLER_REPO`` and
``INSTALLER_LOCK`` (the script's documented test overrides) keep every write
inside a per-test sandbox.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "bump-installer-bundle.sh"
INSTALL_FLATPAKS = REPO / "live" / "src" / "install-flatpaks.sh"

FAKE_REPO = "example-org/fake-installer"
STABLE = "org.bootcinstaller.Installer.flatpak"
DEVEL = "org.bootcinstaller.Installer.Devel.flatpak"
ORIGINAL_LOCK = "tag=v-original\nkeep=me\n"


class BumpInstallerBundleTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="bump-installer-bundle-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.calls = self.sandbox / "calls.log"
        self.calls.touch()
        self.lock = self.sandbox / "installer-bundle.lock"
        self.lock.write_text(ORIGINAL_LOCK)

        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        # The download branch writes "<bundle file name> @ <tag>" so each
        # bundle (and each tag) has a distinct, predictable digest.
        stub = self.bindir / "curl"
        stub.write_text(f"#!{shutil.which('bash')}\n" + textwrap.dedent("""\
            echo "curl $*" >> "$STUB_CALLS"
            out="" url="" head=0 prev=""
            for arg in "$@"; do
                [[ "$prev" == "-o" ]] && out="$arg"
                [[ "$arg" == "--head" ]] && head=1
                [[ "$arg" == https://* ]] && url="$arg"
                prev="$arg"
            done
            if (( head )); then
                printf '%s' "$STUB_EFFECTIVE_URL"
                exit 0
            fi
            file="${url##*/}"
            if [[ -n "${STUB_FAIL_FILE:-}" && "$file" == "$STUB_FAIL_FILE" ]]; then
                echo "curl: (22) The requested URL returned error: 404" >&2
                exit 22
            fi
            tag="${url%/*}"; tag="${tag##*/}"
            printf '%s @ %s\\n' "$file" "$tag" > "$out"
        """))
        stub.chmod(0o755)

    def run_bump(self, *args, effective_url=None, fail_file=None):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bindir}:{env.get('PATH', '/usr/bin:/bin')}",
            INSTALLER_REPO=FAKE_REPO,
            INSTALLER_LOCK=str(self.lock),
            STUB_CALLS=str(self.calls),
            STUB_EFFECTIVE_URL=effective_url
            or f"https://github.com/{FAKE_REPO}/releases/tag/v2026.11.01-abcdef12",
            STUB_FAIL_FILE=fail_file or "",
        )
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
        )

    def curl_calls(self):
        return self.calls.read_text().splitlines()

    def download_urls(self):
        return [c.split()[-3] for c in self.curl_calls() if "--head" not in c]

    @staticmethod
    def digest(bundle, tag):
        return hashlib.sha256(f"{bundle} @ {tag}\n".encode()).hexdigest()

    def lock_entries(self):
        return dict(
            line.split("=", 1)
            for line in self.lock.read_text().splitlines()
            if line and not line.startswith("#")
        )

    def assert_lock_untouched(self):
        self.assertEqual(self.lock.read_text(), ORIGINAL_LOCK)

    # --- tag resolution ---------------------------------------------------

    def test_no_argument_resolves_the_tag_from_the_latest_redirect(self):
        result = self.run_bump()
        self.assertEqual(result.returncode, 0, result.stderr)
        head = [c for c in self.curl_calls() if "--head" in c]
        self.assertEqual(len(head), 1)
        self.assertIn(f"https://github.com/{FAKE_REPO}/releases/latest", head[0])
        self.assertEqual(self.lock_entries()["tag"], "v2026.11.01-abcdef12")

    def test_explicit_tag_skips_the_redirect_lookup(self):
        result = self.run_bump("v2026.12.24-deadbeef")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([c for c in self.curl_calls() if "--head" in c])
        self.assertEqual(self.lock_entries()["tag"], "v2026.12.24-deadbeef")

    # --- tag refusal ------------------------------------------------------

    def assert_refused(self, result, tag):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"refusing release tag '{tag}'", result.stderr)
        self.assertEqual(self.download_urls(), [], "nothing may be downloaded for a refused tag")
        self.assert_lock_untouched()

    def test_rolling_tags_are_refused(self):
        for tag in ("latest", "latest-dev", "latest-stable", "dev-rolling"):
            with self.subTest(tag=tag):
                self.calls.write_text("")
                self.assert_refused(self.run_bump(tag), tag)

    def test_tag_containing_a_slash_is_refused(self):
        self.assert_refused(self.run_bump("v1/../../evil"), "v1/../../evil")

    def test_redirect_without_a_tag_url_is_refused(self):
        # A repository with no releases redirects /releases/latest to the
        # releases index; the whole URL then becomes the "tag".
        url = f"https://github.com/{FAKE_REPO}/releases"
        self.assert_refused(self.run_bump(effective_url=url), url)

    def test_redirect_to_a_rolling_tag_is_refused(self):
        url = f"https://github.com/{FAKE_REPO}/releases/tag/latest-dev"
        self.assert_refused(self.run_bump(effective_url=url), "latest-dev")

    # --- lock content -----------------------------------------------------

    def test_both_bundles_are_downloaded_from_the_resolved_tag(self):
        tag = "v2026.12.24-deadbeef"
        result = self.run_bump(tag)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.download_urls(),
            [
                f"https://github.com/{FAKE_REPO}/releases/download/{tag}/{STABLE}",
                f"https://github.com/{FAKE_REPO}/releases/download/{tag}/{DEVEL}",
            ],
        )

    def test_lock_records_the_sha256_of_each_downloaded_bundle(self):
        tag = "v2026.12.24-deadbeef"
        result = self.run_bump(tag)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.lock_entries(),
            {
                "tag": tag,
                STABLE: self.digest(STABLE, tag),
                DEVEL: self.digest(DEVEL, tag),
            },
        )
        self.assertIn(f"# bootc-installer release baked into every live ISO ({FAKE_REPO}).",
                      self.lock.read_text())
        self.assertIn(f"tag={tag}", result.stdout)

    def test_written_lock_is_accepted_by_install_flatpaks_parser(self):
        tag = "v2026.12.24-deadbeef"
        self.assertEqual(self.run_bump(tag).returncode, 0)

        # Run the exact lock-parsing lines from install-flatpaks.sh against
        # the freshly written lock, for both installer channels.
        text = INSTALL_FLATPAKS.read_text(encoding="utf-8")
        parse = [
            ln for ln in text.splitlines()
            if re.match(r"\s*INSTALLER_(TAG|SHA256)=\$\(sed ", ln)
        ]
        self.assertEqual(len(parse), 2, "install-flatpaks.sh lock parser changed; update this test")
        for bundle in (STABLE, DEVEL):
            with self.subTest(bundle=bundle):
                snippet = "\n".join([
                    f"INSTALLER_LOCK={self.lock}",
                    f"FLATPAK_FILENAME={bundle}",
                    *parse,
                    'echo "${INSTALLER_TAG} ${INSTALLER_SHA256}"',
                ])
                out = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True)
                self.assertEqual(out.stdout.split(), [tag, self.digest(bundle, tag)])

    def test_checked_in_lock_has_the_shape_the_script_writes(self):
        entries = dict(
            line.split("=", 1)
            for line in (REPO / "live" / "src" / "installer-bundle.lock").read_text().splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(set(entries), {"tag", STABLE, DEVEL})
        self.assertNotIn("/", entries["tag"])
        self.assertNotIn(entries["tag"], ("latest", "latest-dev", "latest-stable", "dev-rolling"))
        for bundle in (STABLE, DEVEL):
            self.assertRegex(entries[bundle], r"^[0-9a-f]{64}$")

    # --- atomicity --------------------------------------------------------

    def test_failed_download_leaves_the_existing_lock_untouched(self):
        for bundle in (STABLE, DEVEL):
            with self.subTest(failing=bundle):
                result = self.run_bump("v2026.12.24-deadbeef", fail_file=bundle)
                self.assertNotEqual(result.returncode, 0)
                self.assert_lock_untouched()


if __name__ == "__main__":
    unittest.main()
