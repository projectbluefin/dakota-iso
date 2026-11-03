"""Behavioural unit tests for scripts/payload-oci.sh.

``build-live-squashfs.sh`` and ``iso-sd-boot.sh`` both source this helper to
turn the payload image into the oci-archive that the live ISO embeds and the
installer copies, byte for byte, onto every installed system. The existing
invariant tests only read the helper's text; nothing ran it. The ISO build on
``main`` exercises one path (squash with a readable layer diff_id), so the
unsquashed path, the empty-diff_id fallback, the uninjected build and the
failure handling were never executed anywhere.

These tests source the real helper under ``set -euo pipefail`` (as both
callers do) with ``podman`` replaced by a recording stub, and cover:

1. **payload_build.** The generated Containerfile, the injected bootc install
   config, the ``podman build`` flags, the build-context cleanup, and that a
   failed build returns podman's exit status and still removes its context.
2. **payload_save.** An existing archive is replaced and the image is pushed
   to ``oci-archive:<archive>:<ref>``.
3. **payload_to_oci_archive.** The unsquashed path, the squashed path with and
   without a diff_id, intermediate-image cleanup, and that a failed build
   stops the run before anything is written to the archive.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
HELPER = REPO / "scripts" / "payload-oci.sh"

ROOT_MOUNT_DEST = "/usr/lib/bootc/install/00-defaults.toml"
ROOT_MOUNT_TOML = '[install]\nroot-mount-spec = "LABEL=root"\n'
DIFFID = "sha256:" + "ab" * 32

# podman stub: logs every call as one shell-quoted line, snapshots each build
# context (the last argument) into $STUB_CAPTURE/build-<n>, prints
# $STUB_DIFFID for `image inspect` (or fails when it is unset), fails builds
# whose -t tag matches $STUB_FAIL_TAG_GLOB with $STUB_FAIL_RC, and fails every
# `rmi` so the helper's `|| true` cleanup is exercised.
PODMAN_STUB = r"""
printf '%q ' "$@" >>"$STUB_CALLS"; echo >>"$STUB_CALLS"
case "$1" in
build)
    n=$(find "$STUB_CAPTURE" -mindepth 1 -maxdepth 1 -type d | wc -l)
    ctx="${@: -1}"
    cp -r "$ctx" "$STUB_CAPTURE/build-$n"
    echo "$ctx" >"$STUB_CAPTURE/build-$n.ctxpath"
    tag=""
    prev=""
    for a in "$@"; do
        [[ "$prev" == "-t" ]] && tag="$a"
        prev="$a"
    done
    # shellcheck disable=SC2053
    if [[ -n "${STUB_FAIL_TAG_GLOB:-}" && "$tag" == $STUB_FAIL_TAG_GLOB ]]; then
        exit "${STUB_FAIL_RC:-1}"
    fi
    ;;
image)
    [[ -n "${STUB_DIFFID:-}" ]] || exit 125
    echo "$STUB_DIFFID"
    ;;
push)
    archive="${3#oci-archive:}"
    archive="${archive%%:*}"
    echo "pushed $2" >"$archive"
    ;;
rmi)
    exit 1
    ;;
esac
"""


class PayloadOciHarness(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="payload-oci-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.calls = self.sandbox / "calls.log"
        self.calls.touch()
        self.capture = self.sandbox / "capture"
        self.capture.mkdir()
        self.tmpdir = self.sandbox / "tmp"
        self.tmpdir.mkdir()
        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        stub = self.bindir / "podman"
        stub.write_text(f"#!{shutil.which('bash')}\n" + textwrap.dedent(PODMAN_STUB))
        stub.chmod(0o755)
        self.archive = self.sandbox / "out" / "payload.oci.tar"
        self.archive.parent.mkdir()

    def run_helper(self, *argv, **stub_env):
        env = {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "TMPDIR": str(self.tmpdir),
            "STUB_CALLS": str(self.calls),
            "STUB_CAPTURE": str(self.capture),
        }
        for key in ("STUB_DIFFID", "STUB_FAIL_TAG_GLOB", "STUB_FAIL_RC"):
            env.pop(key, None)
        env.update(stub_env)
        script = 'set -euo pipefail\nsource "$0"\n"$@"\n'
        return subprocess.run(
            ["bash", "-c", script, str(HELPER), *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def podman_calls(self):
        return [shlex.split(line) for line in self.calls.read_text().splitlines()]

    def builds(self):
        """Captured build contexts in call order."""
        count = len([p for p in self.capture.iterdir() if p.is_dir()])
        return [self.capture / f"build-{n}" for n in range(count)]

    def build_ctx_path(self, n):
        return Path((self.capture / f"build-{n}.ctxpath").read_text().strip())

    def assertNoBuildContextLeft(self):
        self.assertEqual(list(self.tmpdir.iterdir()), [], "build context not removed")


class TestPayloadBuild(PayloadOciHarness):
    def test_injected_build_copies_only_the_root_mount_config(self):
        result = self.run_helper("payload_build", "localhost/t:1", "quay.io/x/y:z", "true")
        self.assertEqual(result.returncode, 0, result.stderr)

        [ctx] = self.builds()
        self.assertEqual(
            (ctx / "Containerfile").read_text(),
            f"FROM quay.io/x/y:z\nCOPY bootc-root-mount.toml {ROOT_MOUNT_DEST}\n",
        )
        self.assertEqual((ctx / "bootc-root-mount.toml").read_text(), ROOT_MOUNT_TOML)
        self.assertEqual(
            sorted(p.name for p in ctx.iterdir()),
            ["Containerfile", "bootc-root-mount.toml"],
        )
        self.assertNoBuildContextLeft()

    def test_uninjected_build_is_from_only(self):
        result = self.run_helper("payload_build", "localhost/t:1", "localhost/base", "false")
        self.assertEqual(result.returncode, 0, result.stderr)

        [ctx] = self.builds()
        self.assertEqual((ctx / "Containerfile").read_text(), "FROM localhost/base\n")
        self.assertEqual([p.name for p in ctx.iterdir()], ["Containerfile"])
        self.assertNoBuildContextLeft()

    def test_build_never_pulls_writes_oci_and_passes_extra_flags(self):
        result = self.run_helper(
            "payload_build", "localhost/t:1", "img", "false",
            "--squash-all", "--label", "a=b",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        [call] = self.podman_calls()
        ctx = str(self.build_ctx_path(0))
        self.assertEqual(
            call,
            [
                "build", "--pull=never", "--format", "oci",
                "--squash-all", "--label", "a=b",
                "-f", f"{ctx}/Containerfile", "-t", "localhost/t:1", ctx,
            ],
        )
        self.assertTrue(ctx.startswith(f"{self.tmpdir}/payload-build."), ctx)

    def test_failed_build_returns_podman_status_and_cleans_up(self):
        result = self.run_helper(
            "payload_build", "localhost/t:1", "img", "true",
            STUB_FAIL_TAG_GLOB="localhost/t:1", STUB_FAIL_RC="7",
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNoBuildContextLeft()


class TestPayloadSave(PayloadOciHarness):
    def test_replaces_existing_archive_and_pushes_under_ref(self):
        self.archive.write_text("stale payload from a previous build")
        result = self.run_helper(
            "payload_save", "localhost/t:1", str(self.archive), "ghcr.io/o/dakota:latest"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(
            self.podman_calls(),
            [["push", "localhost/t:1", f"oci-archive:{self.archive}:ghcr.io/o/dakota:latest"]],
        )
        self.assertEqual(self.archive.read_text(), "pushed localhost/t:1\n")

    def test_push_failure_leaves_no_stale_archive(self):
        self.archive.write_text("stale payload from a previous build")
        failing = self.bindir / "podman"
        failing.write_text(f"#!{shutil.which('bash')}\nexit 3\n")
        result = self.run_helper("payload_save", "localhost/t:1", str(self.archive), "r")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(self.archive.exists(), "stale archive survived a failed push")


class TestPayloadToOciArchive(PayloadOciHarness):
    REF = "ghcr.io/projectbluefin/dakota:latest"

    def to_archive(self, squash, **stub_env):
        return self.run_helper(
            "payload_to_oci_archive", "quay.io/src/img:tag", str(self.archive),
            self.REF, squash, **stub_env,
        )

    def tags(self, verb):
        return [c for c in self.podman_calls() if c[0] == verb]

    def test_unsquashed_keeps_layers_and_saves_injected_base(self):
        result = self.to_archive("false", STUB_DIFFID=DIFFID)
        self.assertEqual(result.returncode, 0, result.stderr)

        verbs = [c[0] for c in self.podman_calls()]
        self.assertEqual(verbs, ["build", "push", "rmi"])
        [build] = self.tags("build")
        self.assertNotIn("--squash-all", build)
        base_tag = build[build.index("-t") + 1]
        self.assertRegex(base_tag, r"^localhost/iso-payload-work:base-\d+$")
        [ctx] = self.builds()
        self.assertEqual(
            (ctx / "Containerfile").read_text(),
            f"FROM quay.io/src/img:tag\nCOPY bootc-root-mount.toml {ROOT_MOUNT_DEST}\n",
        )
        self.assertEqual(self.tags("push"), [["push", base_tag, f"oci-archive:{self.archive}:{self.REF}"]])
        self.assertEqual(self.tags("rmi"), [["rmi", base_tag]])
        self.assertNoBuildContextLeft()

    def test_squashed_records_final_diffid_as_label_and_annotation(self):
        result = self.to_archive("true", STUB_DIFFID=DIFFID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"updating ostree.final-diffid to {DIFFID}", result.stdout)

        base_build, final_build = self.tags("build")
        base_tag = base_build[base_build.index("-t") + 1]
        final_tag = final_build[final_build.index("-t") + 1]
        self.assertRegex(base_tag, r"^localhost/iso-payload-work:base-\d+$")
        self.assertRegex(final_tag, r"^localhost/iso-payload-work:final-\d+$")
        self.assertIn("--squash-all", base_build)
        self.assertIn("--squash-all", final_build)
        for flag in ("--label", "--annotation"):
            self.assertEqual(
                final_build[final_build.index(flag) + 1], f"ostree.final-diffid={DIFFID}"
            )
        self.assertNotIn("--label", base_build)

        [inspect] = self.tags("image")
        self.assertEqual(inspect[-1], base_tag)

        base_ctx, final_ctx = self.builds()
        self.assertIn(f"COPY bootc-root-mount.toml {ROOT_MOUNT_DEST}", (base_ctx / "Containerfile").read_text())
        # The second build starts from the injected base; it must not inject again.
        self.assertEqual((final_ctx / "Containerfile").read_text(), f"FROM {base_tag}\n")

        self.assertEqual(self.tags("push"), [["push", final_tag, f"oci-archive:{self.archive}:{self.REF}"]])
        self.assertEqual(self.tags("rmi"), [["rmi", final_tag], ["rmi", base_tag]])
        self.assertNoBuildContextLeft()

    def test_squashed_without_diffid_saves_base_without_label(self):
        result = self.to_archive("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("final-diffid", result.stdout)

        [build] = self.tags("build")
        base_tag = build[build.index("-t") + 1]
        self.assertIn("--squash-all", build)
        self.assertEqual(self.tags("push"), [["push", base_tag, f"oci-archive:{self.archive}:{self.REF}"]])
        self.assertEqual(self.tags("rmi"), [["rmi", base_tag]])

    def test_failed_base_build_stops_before_writing_archive(self):
        for squash in ("true", "false"):
            with self.subTest(squash=squash):
                self.calls.write_text("")
                self.archive.write_text("previous archive")
                result = self.to_archive(
                    squash, STUB_DIFFID=DIFFID,
                    STUB_FAIL_TAG_GLOB="localhost/iso-payload-work:base-*", STUB_FAIL_RC="9",
                )
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertEqual([c[0] for c in self.podman_calls()], ["build"])
                self.assertEqual(self.archive.read_text(), "previous archive")
                self.assertNoBuildContextLeft()

    def test_failed_final_build_stops_before_writing_archive(self):
        self.archive.write_text("previous archive")
        result = self.to_archive(
            "true", STUB_DIFFID=DIFFID,
            STUB_FAIL_TAG_GLOB="localhost/iso-payload-work:final-*", STUB_FAIL_RC="4",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual([c[0] for c in self.podman_calls()], ["build", "image", "build"])
        self.assertEqual(self.archive.read_text(), "previous archive")
        self.assertNoBuildContextLeft()


if __name__ == "__main__":
    unittest.main()
