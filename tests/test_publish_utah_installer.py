"""publish-utah-installer.yml: the Utah download is the gnome-setup medium."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
SUFFIXES = ("", "-CHECKSUM", "-CHECKSUM.sigstore.json")


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


class PublishUtahInstallerTests(unittest.TestCase):
    workflow = load("publish-utah-installer.yml")

    def steps(self):
        return {s.get("name"): s for s in self.workflow["jobs"]["publish"]["steps"]}

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

    def test_the_medium_is_published_as_xz_for_fedora_media_writer(self):
        publish = self.workflow["jobs"]["publish"]
        self.assertEqual(publish["env"]["LATEST_NAME"], "utah-installer-latest.raw.xz")
        steps = {name: s.get("run", "") for name, s in self.steps().items()}
        compress = steps["Compress, checksum and sign"]
        self.assertIn('xz -T0 -6 -c "$WORK/medium.raw" > "$WORK/$LATEST_NAME"', compress)
        self.assertNotIn("zstd", compress)
        self.assertNotIn("--long", compress)
        self.assertIn("xz-utils", steps["Prepare host (podman storage and work dir on /mnt)"])
        upload = steps["Upload to Cloudflare R2"]
        self.assertIn('"$BASE-backup-$n.$ext$suffix" "$BASE-backup-$((n + 1)).$ext$suffix"', upload)
        self.assertIn('move_if_exists "$latest$suffix" "$BASE-backup-1.$ext$suffix"', upload)
        self.assertIn('publish "$LATEST_NAME" raw.xz', upload)
        self.assertNotIn(".zst", (WORKFLOWS / "publish-utah-installer.yml").read_text())

    def test_the_e2e_workflow_can_be_called(self):
        on = load("utah-image-installer.yml")[True]
        self.assertIn("image_ref", on["workflow_call"]["inputs"])


class PublishHybridIsoTests(unittest.TestCase):
    """The hybrid ISO (projectbluefin/utah#1404) beside the raw.xz, behind one switch."""

    workflow = load("publish-utah-installer.yml")

    def steps(self):
        return self.workflow["jobs"]["publish"]["steps"]

    def step(self, name):
        return next(s for s in self.steps() if s.get("name") == name)

    def test_the_iso_switch_is_on_for_schedules_and_by_default(self):
        flag = self.workflow[True]["workflow_dispatch"]["inputs"]["iso"]
        self.assertEqual((flag["type"], flag["default"]), ("boolean", True))
        # inputs.iso is null on schedule, and GitHub treats null and false as
        # equal, so `inputs.iso != false` would turn the ISO off there. Every
        # run that is not a manual dispatch is on; a dispatch follows its input.
        self.assertEqual(
            self.workflow["jobs"]["resolve"]["outputs"]["iso"],
            "${{ github.event_name != 'workflow_dispatch' || inputs.iso }}",
        )
        self.assertEqual(self.workflow["jobs"]["publish"]["env"]["ISO"], "${{ needs.resolve.outputs.iso }}")

    def test_the_iso_is_published_only_after_its_e2e_legs(self):
        # The same switch adds the CD-ROM and USB legs to the E2E the publish needs.
        self.assertEqual(self.workflow["jobs"]["e2e"]["with"]["iso"], "${{ needs.resolve.outputs.iso == 'true' }}")
        self.assertEqual(set(self.workflow["jobs"]["publish"]["needs"]), {"resolve", "e2e"})
        # Pull requests and dispatches of the E2E itself run the raw leg alone.
        e2e = load("utah-image-installer.yml")
        for trigger in ("workflow_call", "workflow_dispatch"):
            flag = e2e[True][trigger]["inputs"]["iso"]
            self.assertEqual((flag["type"], flag["default"]), ("boolean", False), trigger)
        for name in ("Build the hybrid ISO from the medium", "Checksum and sign the ISO (uncompressed)"):
            self.assertEqual(self.step(name)["if"], "env.ISO == 'true'", name)

    def test_the_iso_is_built_from_the_baked_shrunk_medium_before_it_is_compressed(self):
        names = [s.get("name") for s in self.steps()]
        build = names.index("Build the hybrid ISO from the medium")
        self.assertLess(names.index("Bake the Flatpaks and shrink the medium"), build)
        self.assertLess(build, names.index("Compress, checksum and sign"))
        run = self.step("Build the hybrid ISO from the medium")["run"]
        self.assertIn('bash "$RAW/build-hybrid-iso.sh" "$WORK/medium.raw" "$WORK/$ISO_NAME"', run)
        self.assertIn('UTAH_RAW_EVIDENCE="$WORK/raw-evidence"', run)
        self.assertEqual(self.workflow["jobs"]["publish"]["env"]["ISO_NAME"], "utah-installer-latest.iso")
        # The script brings its own systemd-repart and systemd-boot from the
        # medium; the host needs no ISO tooling.
        prepare = self.step("Prepare host (podman storage and work dir on /mnt)")["run"]
        for tool in ("xorriso", "mtools"):
            self.assertNotIn(tool, prepare)

    def test_the_iso_is_signed_like_the_raw_and_not_compressed(self):
        run = self.step("Checksum and sign the ISO (uncompressed)")["run"]
        self.assertIn('sha256sum "$ISO_NAME" > "$ISO_NAME-CHECKSUM"', run)
        self.assertIn('cosign sign-blob --yes --bundle "$ISO_NAME-CHECKSUM.sigstore.json" "$ISO_NAME-CHECKSUM"', run)
        for compressor in ("xz ", "zstd", "gzip"):
            self.assertNotIn(compressor, run)
        self.assertNotIn("ISO_NAME", self.step("Compress, checksum and sign")["run"])

    def upload(self, iso, existing):
        """Run the upload step against a fake rclone; return its calls and the summary."""
        step = self.step("Upload to Cloudflare R2")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bin").mkdir()
            rclone = tmp / "bin/rclone"
            rclone.write_text('#!/bin/sh\necho "$*" >> "$RCLONE_LOG"\n'
                              '[ "$1" != lsf ] || cat "$R2_FILES"\n')
            rclone.chmod(0o755)
            (tmp / "files").write_text("".join(f"{name}\n" for name in existing))
            env = {**self.workflow["jobs"]["publish"]["env"], "WORK": str(tmp), "ISO": iso,
                   "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "RCLONE_LOG": str(tmp / "log"),
                   "R2_FILES": str(tmp / "files"), "GITHUB_STEP_SUMMARY": str(tmp / "summary")}
            subprocess.run(["bash", "-c", step["run"]], check=True, env=env, capture_output=True)
            calls = (tmp / "log").read_text().splitlines()
            return [c.replace("--log-level INFO --s3-no-check-bucket ", "") for c in calls[1:]], \
                (tmp / "summary").read_text()

    def test_with_iso_off_the_raw_xz_upload_is_unchanged(self):
        calls, summary = self.upload("false", ["utah-installer-latest.raw.xz", "utah-installer-backup-1.raw.xz"])
        self.assertEqual(calls, [
            "moveto R2:testing/utah-installer-backup-1.raw.xz R2:testing/utah-installer-backup-2.raw.xz",
            "moveto R2:testing/utah-installer-latest.raw.xz R2:testing/utah-installer-backup-1.raw.xz",
            *(f"copyto utah-installer-latest.raw.xz{s} R2:testing/utah-installer-latest.raw.xz{s}" for s in SUFFIXES),
        ])
        self.assertNotIn(".iso", summary)
        self.assertIn("- [utah-installer-latest.raw.xz](https://projectbluefin.dev/utah-installer-latest.raw.xz)", summary)

    def test_with_iso_on_both_are_rotated_uploaded_and_linked(self):
        existing = [f"utah-installer-{n}{s}" for n in ("latest.iso", "backup-1.iso", "backup-2.iso") for s in SUFFIXES]
        calls, summary = self.upload("true", existing)
        iso_calls = [c for c in calls if ".iso" in c]
        self.assertEqual(iso_calls, [
            *(f"moveto R2:testing/utah-installer-backup-2.iso{s} R2:testing/utah-installer-backup-3.iso{s}"
              for s in SUFFIXES),
            *(f"moveto R2:testing/utah-installer-backup-1.iso{s} R2:testing/utah-installer-backup-2.iso{s}"
              for s in SUFFIXES),
            *(f"moveto R2:testing/utah-installer-latest.iso{s} R2:testing/utah-installer-backup-1.iso{s}"
              for s in SUFFIXES),
            *(f"copyto utah-installer-latest.iso{s} R2:testing/utah-installer-latest.iso{s}" for s in SUFFIXES),
        ])
        self.assertTrue(all(".raw.xz" not in c for c in iso_calls))
        for name in ("utah-installer-latest.raw.xz", "utah-installer-latest.iso"):
            for s in SUFFIXES:
                self.assertIn(f"- [{name}{s}](https://projectbluefin.dev/{name}{s})", summary)


class HybridIsoE2ELegTests(unittest.TestCase):
    """utah-image-installer.yml: the ISO legs the publish requires when the switch is on."""

    workflow = load("utah-image-installer.yml")

    def e2e_steps(self):
        return {s.get("name"): s for s in self.workflow["jobs"]["e2e"]["steps"]}

    def test_iso_on_adds_a_cdrom_and_a_usb_leg_next_to_the_raw(self):
        matrix = next(s for s in self.workflow["jobs"]["resolve"]["steps"] if s.get("id") == "matrix")
        self.assertEqual(matrix["env"]["ISO"], "${{ inputs.iso == true }}")
        self.assertIn("boots=(raw)\n", matrix["run"])
        self.assertIn('[[ "$ISO" != true ]] || boots+=(cdrom usb)', matrix["run"])
        self.assertIn("boot:$boot", matrix["run"])
        self.assertEqual(self.workflow["jobs"]["e2e"]["env"]["BOOT"], "${{ matrix.boot }}")
        # The raw leg keeps its check and artifact names.
        self.assertEqual(self.workflow["jobs"]["e2e"]["name"],
                         "e2e (${{ matrix.image }}${{ matrix.boot != 'raw' && format(', iso {0}', matrix.boot) || '' }})")

    def test_the_iso_legs_boot_the_live_stage_from_the_iso(self):
        steps = self.e2e_steps()
        build = steps["Build the hybrid ISO from the medium"]
        self.assertEqual(build["if"], "matrix.boot != 'raw'")
        self.assertIn('build-hybrid-iso.sh "$WORK/medium.raw" "$WORK/medium.iso"', build["run"])
        names = list(steps)
        self.assertLess(names.index("Bake the Flatpaks into the medium and shrink it"),
                        names.index("Build the hybrid ISO from the medium"))
        e2e = steps["End-to-end with screenshots"]["run"]
        self.assertIn('[[ "$BOOT" == raw ]] || iso=(--iso "$WORK/medium.iso" --boot-as "$BOOT")', e2e)
        self.assertIn('"${iso[@]}"', e2e)

    def test_each_leg_uploads_its_own_evidence_and_never_the_iso(self):
        upload = next(s for s in self.workflow["jobs"]["e2e"]["steps"] if "upload-artifact" in s.get("uses", ""))
        self.assertEqual(upload["with"]["name"],
                         "utah-image-installer-${{ matrix.image }}"
                         "${{ matrix.boot != 'raw' && format('-iso-{0}', matrix.boot) || '' }}")
        self.assertNotRegex(upload["with"]["path"], r"\.iso\b")


if __name__ == "__main__":
    unittest.main()
