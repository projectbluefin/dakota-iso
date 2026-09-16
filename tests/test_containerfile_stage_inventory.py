"""Drift gate: docs/architecture.md must describe every live/Containerfile stage.

`live/Containerfile` is a multi-stage build whose stage graph is the load-bearing
description of how the live environment image is produced. `docs/architecture.md`
publishes that graph as a table, and the table is the only place a reader is told
which stage does what.

The two drifted: the `initramfs-native` stage was added on 2026-06-15 (f85b620,
"native dracut for Fedora images") and the architecture table was never updated,
so it still advertised three stages and named a `dakota-ref` stage that has never
existed in the file.

This gate re-couples them. It fails when a stage is added, removed or renamed in
`live/Containerfile` without the architecture table being updated to match, and
when the stage count in the section heading disagrees with the table.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTAINERFILE = REPO / "live" / "Containerfile"
ARCHITECTURE_DOC = REPO / "docs" / "architecture.md"

# The last `FROM` with no `AS <name>` is the image the build actually produces.
# It has no name in the Containerfile, so the docs refer to it by this label.
FINAL_STAGE_LABEL = "final"

_FROM_RE = re.compile(r"^\s*FROM\s+(?P<rest>.+?)\s*$", re.IGNORECASE)
_AS_RE = re.compile(r"\sAS\s+(?P<name>[A-Za-z0-9._-]+)\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(
    r"^###\s+Container:.*`live/Containerfile`\s*—\s*(?P<count>\d+)\s+stages\s*\)",
)
_TABLE_ROW_RE = re.compile(r"^\|\s*(?P<cell>[^|]+?)\s*\|")


def _containerfile_stages():
    """Return the ordered stage names declared by live/Containerfile.

    Named stages keep their `AS` name. A trailing unnamed `FROM` — the image the
    build emits — is reported as FINAL_STAGE_LABEL.
    """
    stages = []
    for raw in CONTAINERFILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        from_match = _FROM_RE.match(line)
        if not from_match:
            continue
        as_match = _AS_RE.search(from_match.group("rest"))
        stages.append(as_match.group("name") if as_match else None)
    return stages


def _documented_stages():
    """Return the ordered stage names in the architecture doc's stage table."""
    lines = ARCHITECTURE_DOC.read_text().splitlines()
    for index, line in enumerate(lines):
        if _HEADING_RE.match(line):
            break
    else:
        raise AssertionError(
            "docs/architecture.md has no '### Container: ... (`live/Containerfile` "
            "— N stages)' heading; this gate cannot locate the stage table."
        )

    names = []
    seen_table = False
    for line in lines[index + 1 :]:
        if not line.startswith("|"):
            if seen_table:
                break
            continue
        seen_table = True
        cell = _TABLE_ROW_RE.match(line).group("cell")
        if cell.lower() == "stage" or set(cell) <= {"-", ":"}:
            continue
        names.append(cell.strip("`"))
    return names


def _documented_stage_count():
    for line in ARCHITECTURE_DOC.read_text().splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            return int(heading.group("count"))
    raise AssertionError("docs/architecture.md stage-count heading not found")


class TestContainerfileStageInventory(unittest.TestCase):
    """live/Containerfile stages and docs/architecture.md must not drift."""

    def test_exactly_one_unnamed_final_stage(self):
        """Only the last FROM may be unnamed — it is the emitted image."""
        stages = _containerfile_stages()
        self.assertTrue(stages, "live/Containerfile declares no FROM stages")
        unnamed = [i for i, name in enumerate(stages) if name is None]
        self.assertEqual(
            unnamed,
            [len(stages) - 1],
            "live/Containerfile must have exactly one unnamed FROM and it must be "
            f"the last one; unnamed stages found at indexes {unnamed} of "
            f"{len(stages)} stages. An unnamed intermediate stage cannot be "
            "referenced by COPY --from and cannot be documented by name.",
        )

    def test_documented_stages_match_containerfile(self):
        """Every Containerfile stage is in the table, in build order, and vice versa."""
        actual = [
            name if name is not None else FINAL_STAGE_LABEL
            for name in _containerfile_stages()
        ]
        documented = _documented_stages()

        missing = [name for name in actual if name not in documented]
        extra = [name for name in documented if name not in actual]
        self.assertFalse(
            missing or extra,
            "docs/architecture.md stage table has drifted from live/Containerfile.\n"
            f"  Stages in Containerfile but undocumented: {missing or 'none'}\n"
            f"  Stages documented but not in Containerfile: {extra or 'none'}\n"
            f"  Containerfile order: {actual}\n"
            f"  Documented order:    {documented}\n"
            "Update the table under '### Container:' in docs/architecture.md.",
        )
        self.assertEqual(
            documented,
            actual,
            "docs/architecture.md lists the right stages in the wrong order; the "
            "table must follow Containerfile build order.",
        )

    def test_heading_stage_count_matches_table(self):
        """The '— N stages)' heading must agree with the table it introduces."""
        documented = _documented_stages()
        self.assertEqual(
            _documented_stage_count(),
            len(documented),
            "docs/architecture.md '### Container: ... — N stages)' heading "
            f"claims {_documented_stage_count()} stages but the table lists "
            f"{len(documented)}.",
        )


if __name__ == "__main__":
    unittest.main()
