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
import json
import io
import tarfile
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from tests.oci_fixture import make_oci_archive

REPO = Path(__file__).parent.parent
HELPER = REPO / "scripts" / "payload-oci.sh"

ROOT_MOUNT_DEST = "/usr/lib/bootc/install/00-defaults.toml"
ROOT_MOUNT_TOML = '[install]\nroot-mount-spec = "LABEL=root"\n'
DIFFID = "sha256:" + "ab" * 32


class TestEmbeddedMedia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        config = self.root / "etc/bootc-installer"
        config.mkdir(parents=True)
        (config / "recipe.json").write_text(json.dumps({
            "imgref": "ghcr.io/projectbluefin/dakota:stable",
            "local_imgref": "ghcr.io/projectbluefin/dakota-nvidia:stable",
        }))
        self.archive = Path(self.tmp.name) / "payload.tar"
        self.files, self.manifest, self.config = make_oci_archive(self.archive)

    def embed(self):
        return subprocess.run(["bash", "-c", 'set -euo pipefail; source "$1"; payload_embed_oci_archive "$2" "$3"',
                               "bash", str(HELPER), str(self.archive), str(self.root)],
                              capture_output=True, text=True, timeout=30)

    def test_receipt_binds_actual_platform_manifest_and_config(self):
        result = self.embed()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.root / "etc/bootc-installer/media-input.json").read_text())
        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertEqual(receipt["platformManifestDigest"], self.manifest["digest"])
        self.assertEqual(receipt["configDigest"], self.config["digest"])
        self.assertEqual(receipt["platform"], "linux/amd64")
        self.assertEqual(receipt["image"], "ghcr.io/projectbluefin/dakota-nvidia:stable")
        self.assertEqual(receipt["targetImgref"], "ghcr.io/projectbluefin/dakota:stable")
        self.assertEqual(receipt["ociLayoutPath"], "/run/bootcmedia/usr/share/bootc-installer/oci")
        layout = self.root / "usr/share/bootc-installer/oci"
        for name, data in self.files.items():
            self.assertEqual((layout / name).read_bytes(), data)
        self.assertFalse((self.root / "var/lib/containers/storage").exists())

    def test_corrupt_layer_is_refused_before_receipt_is_written(self):
        layer = json.loads(self.files["blobs/sha256/" + self.manifest["digest"].split(":")[1]])["layers"][0]
        name = "blobs/sha256/" + layer["digest"].split(":")[1]
        self.files[name] = b"X" + self.files[name][1:]
        with tarfile.open(self.archive, "w") as tar:
            for name, data in self.files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        result = self.embed()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("size/hash mismatch", result.stderr)
        self.assertFalse((self.root / "etc/bootc-installer/media-input.json").exists())

    def test_archive_symlinks_are_refused(self):
        with tarfile.open(self.archive, "a") as tar:
            entry = tarfile.TarInfo("alias")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc"
            tar.addfile(entry)
        result = self.embed()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe OCI archive member", result.stderr)

    def test_stale_layout_is_not_merged(self):
        self.assertEqual(self.embed().returncode, 0)
        result = self.embed()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale embedded OCI layout", result.stderr)

# podman stub: logs every call as one shell-quoted line, snapshots each build
# context (the last argument) into $STUB_CAPTURE/build-<n>, prints
# $STUB_DIFFID for `image inspect` (or fails when it is unset), fails builds
# whose -t tag matches $STUB_FAIL_TAG_GLOB with $STUB_FAIL_RC, and fails every
# `rmi` so the helper's `|| true` cleanup is exercised.
PODMAN_STUB = r"""
printf '%s\n' "$(printf '%q ' "$@")" >>"$STUB_CALLS"
# Remember the custom graphroot for branches that fabricate store content.
_graphroot=""
_prev=""
for _a in "$@"; do
    if [[ "$_prev" == "--root" ]]; then _graphroot="$_a"; fi
    _prev="$_a"
done
# Strip podman global flags (--root/--runroot/--storage-driver) so $1 is the
# verb whether or not the caller used a custom store via PODMAN=(...).
stripped=()
skip_next=0
for _a in "$@"; do
    if (( skip_next )); then skip_next=0; continue; fi
    case "$_a" in
    --root|--runroot) skip_next=1 ;;
    --root=*|--runroot=*|--storage-driver=*) ;;
    --storage-driver) skip_next=1 ;;
    *) stripped+=("$_a") ;;
    esac
done
set -- "${stripped[@]}"
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
    if [[ "$2" == "inspect" && "$3" == "--format" ]]; then
        case "$4" in
        "{{index .RootFS.Layers 0}}")
            [[ -n "${STUB_DIFFID:-}" ]] || exit 125
            echo "$STUB_DIFFID"
            ;;
        "{{.Digest}}")
            printf '%s\n' "${STUB_DIGEST:?missing digest}"
            ;;
        "{{.Os}}/{{.Architecture}}")
            printf 'linux/amd64\n'
            ;;
        *)
            echo "unexpected inspect format: $4" >&2
            exit 9
            ;;
        esac
        exit 0
    fi
    [[ -n "${STUB_DIFFID:-}" ]] || exit 125
    echo "$STUB_DIFFID"
    ;;
save)
    if [[ -n "${STUB_SAVE_RC:-}" ]]; then exit "${STUB_SAVE_RC}"; fi
    cat "${STUB_SAVE_TAR:?missing save fixture}"
    ;;
load)
    cat >/dev/null
    if [[ -n "$_graphroot" ]]; then
        echo "lock" > "$_graphroot/storage.lock"
    fi
    ;;
tag)
    :
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


class TestPodmanRecording(PayloadOciHarness):
    def test_overlapping_commands_keep_complete_records(self):
        barrier = self.sandbox / "barrier"
        barrier.mkdir()
        commands = [
            ["tag", "source one", "destination'one", ""],
            ["tag", "source two", 'destination"two', ""],
        ]
        # Pause both processes after formatting the arguments. With the old
        # split logger, both argument lists reach the log before either newline.
        # With a single append, formatting happens before either record is written.
        script = r"""
set -euo pipefail
printf() {
    builtin printf "$@"
    if [[ "$1" == '%q ' ]]; then
        touch "$STUB_BARRIER/$BASHPID"
        for ((attempt = 0; attempt < 500; attempt++)); do
            markers=("$STUB_BARRIER"/*)
            if (( ${#markers[@]} == 2 )); then return 0; fi
            sleep 0.01
        done
        echo 'timed out waiting for both log writers' >&2
        return 1
    fi
}
export -f printf
podman "$1" "$2" "$3" "$4" &
first=$!
podman "$5" "$6" "$7" "$8" &
second=$!
status=0
wait "$first" || status=$?
wait "$second" || status=$?
exit "$status"
"""
        result = subprocess.run(
            ["bash", "-c", script, "bash", *commands[0], *commands[1]],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{self.bindir}:{os.environ['PATH']}",
                "STUB_CALLS": str(self.calls),
                "STUB_BARRIER": str(barrier),
            },
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(barrier.iterdir())), 2)
        self.assertCountEqual(self.podman_calls(), commands)


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


DIGEST = "sha256:" + "ab" * 32
CONFIG_DIGEST = "sha256:" + "cd" * 32


class TestPayloadExportStore(PayloadOciHarness):
    def export(self, config_hex=CONFIG_DIGEST.removeprefix("sha256:"), **stub_env):
        from tests.oci_fixture import make_save_tar
        staging = self.sandbox / "store-staging"
        save_tar = self.sandbox / "save.tar"
        make_save_tar(save_tar, config_hex)
        env = {"STUB_DIGEST": DIGEST, "STUB_SAVE_TAR": str(save_tar)}
        env.update(stub_env)
        result = self.run_helper(
            "payload_export_store", "localhost/iso-payload-work:final-1",
            str(staging), "ghcr.io/projectbluefin/dakota-nvidia:stable",
            **env,
        )
        return result, staging

    @staticmethod
    def verbs(calls):
        """Strip podman global flags so the verb is first."""
        out = []
        for c in calls:
            c = list(c)
            while c[:1] == ["--root"] or c[:1] == ["--runroot"]:
                c = c[2:]
            if c[:1] == ["--storage-driver"]:
                c = c[2:]
            out.append(c)
        return out

    def test_export_transfers_tags_and_prints_receipt_values(self):
        result, staging = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{DIGEST} {CONFIG_DIGEST} linux/amd64")
        self.assertTrue(staging.is_dir())
        full = self.podman_calls()
        calls = self.verbs(full)
        identity = "ghcr.io/projectbluefin/dakota-nvidia:stable"
        # The tag lives in the default store; the export streams it across
        # with save (default store) piped into load (custom store).
        default_saves = [c for c in full
                         if c[:2] == ["save", "localhost/iso-payload-work:final-1"]]
        self.assertEqual(len(default_saves), 1, full)
        [logged_load] = [c for c in full if "load" in c]
        self.assertIn(str(staging), logged_load)
        self.assertIn("vfs", logged_load)
        # Podman caps runroot at 50 chars; the export must use a short
        # scratch runroot even when staging lives under a long checkout path.
        runroot = logged_load[logged_load.index("--runroot") + 1]
        self.assertLessEqual(len(runroot), 50, logged_load)
        # The scratch runroot is removed before returning.
        self.assertFalse(Path(runroot).exists(), "runroot not cleaned up")
        tags = [c for c in calls if c[0] == "tag"]
        self.assertEqual(len(tags), 1, full)
        self.assertEqual(tags[0][-2:], ["localhost/iso-payload-work:final-1", identity])
        self.assertIn(
            ["image", "inspect", "--format", "{{.Digest}}", identity],
            calls,
        )
        self.assertIn(["save", identity], [c[:2] for c in calls])

    def test_store_lockfiles_redirect_to_run(self):
        # Containers/storage locks the graphroot even for reads, so the live
        # session's read-only bind needs the locks redirected to /run.
        result, staging = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        link = staging / "storage.lock"
        self.assertTrue(link.is_symlink(), "export did not redirect store locks")
        self.assertTrue(str(os.readlink(link)).startswith("/run/"), os.readlink(link))
        self.assertEqual(result.stdout.strip(), f"{DIGEST} {CONFIG_DIGEST} linux/amd64")

    def test_failed_transfer_stops_before_receipt_values(self):
        result, _ = self.export(STUB_SAVE_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("transfer failed", result.stderr)

    def test_empty_config_digest_is_refused(self):
        result, _ = self.export(config_hex="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("empty receipt values", result.stderr)


class TestPayloadBindStore(PayloadOciHarness):
    def make_root(self):
        root = Path(tempfile.mkdtemp(prefix="payload-bind-test-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        config = root / "etc/bootc-installer"
        config.mkdir(parents=True)
        (config / "recipe.json").write_text(json.dumps({
            "imgref": "ghcr.io/projectbluefin/dakota:stable",
            "local_imgref": "ghcr.io/projectbluefin/dakota-nvidia:stable",
        }))
        return root

    def bind(self, root, staging, identity, target="ghcr.io/projectbluefin/dakota:stable"):
        return self.run_helper(
            "payload_bind_store", str(staging), str(root), identity, target,
            DIGEST, CONFIG_DIGEST, "linux/amd64",
        )

    def test_bind_copies_store_and_writes_store_receipt(self):
        root = self.make_root()
        staging = self.sandbox / "store-staging"
        staging.mkdir()
        (staging / "vfs-images").mkdir()
        (staging / "vfs-images" / "image-id").write_text("x")
        result = self.bind(root, staging, "ghcr.io/projectbluefin/dakota-nvidia:stable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "var/lib/containers/storage/vfs-images/image-id").exists())
        receipt = json.loads((root / "etc/bootc-installer/media-input.json").read_text())
        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertNotIn("ociLayoutPath", receipt)
        self.assertEqual(receipt["containersStoragePath"], "/var/lib/containers/storage")
        self.assertEqual(receipt["image"], "ghcr.io/projectbluefin/dakota-nvidia:stable")
        self.assertEqual(receipt["targetImgref"], "ghcr.io/projectbluefin/dakota:stable")
        self.assertEqual(receipt["platformManifestDigest"], DIGEST)
        self.assertEqual(receipt["configDigest"], CONFIG_DIGEST)
        self.assertEqual(receipt["platform"], "linux/amd64")

    def test_empty_placeholder_dir_is_taken_over(self):
        # The live root ships an empty /var/lib/containers/storage placeholder
        # (configure-live.sh mkdirs it alongside storage.conf); binding takes
        # it over instead of refusing.
        root = self.make_root()
        store = root / "var/lib/containers/storage"
        store.mkdir(parents=True)
        staging = self.sandbox / "store-staging"
        staging.mkdir()
        (staging / "vfs-images").mkdir()
        result = self.bind(root, staging, "ghcr.io/projectbluefin/dakota-nvidia:stable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "var/lib/containers/storage/vfs-images").is_dir())
        receipt = json.loads((root / "etc/bootc-installer/media-input.json").read_text())
        self.assertEqual(receipt["containersStoragePath"], "/var/lib/containers/storage")

    def test_populated_store_is_refused(self):
        root = self.make_root()
        store = root / "var/lib/containers/storage"
        store.mkdir(parents=True)
        (store / "vfs").mkdir()
        (store / "vfs" / "leftover").write_text("stale")
        staging = self.sandbox / "store-staging"
        staging.mkdir()
        result = self.bind(root, staging, "ghcr.io/projectbluefin/dakota-nvidia:stable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr)
        self.assertFalse((root / "etc/bootc-installer/media-input.json").exists())

    def test_empty_receipt_values_are_refused(self):
        root = self.make_root()
        staging = self.sandbox / "store-staging"
        staging.mkdir()
        result = self.run_helper(
            "payload_bind_store", str(staging), str(root),
            "ghcr.io/projectbluefin/dakota-nvidia:stable",
            "ghcr.io/projectbluefin/dakota:stable", "", CONFIG_DIGEST, "linux/amd64",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty receipt values", result.stderr)
        self.assertFalse((root / "etc/bootc-installer/media-input.json").exists())

    def test_identity_mismatch_is_refused(self):
        root = self.make_root()
        staging = self.sandbox / "store-staging"
        staging.mkdir()
        result = self.bind(root, staging, "ghcr.io/projectbluefin/other:stable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
