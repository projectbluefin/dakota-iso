"""Keep the live/Containerfile stage table in docs/architecture.md honest.

docs/architecture.md publishes the stage graph of live/Containerfile as a
table. Nothing tied the two together, so the table drifted: it named a
`dakota-ref` stage that never existed and omitted `initramfs-native`. This gate
parses the real stage graph and the published table and requires them to agree
on the named stages, their order, the stage count in the heading, and a single
unnamed final stage.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
CONTAINERFILE = REPO / "live" / "Containerfile"
ARCHITECTURE = REPO / "docs" / "architecture.md"

FROM_RE = re.compile(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^### Container: .*`live/Containerfile` — (\d+) stages\)$")
ROW_RE = re.compile(r"^\|\s*(`[^`]+`|final)\s*\|")


def containerfile_stages(text):
    """Return one entry per FROM line: the stage name, or None if unnamed."""
    stages = []
    for line in text.splitlines():
        match = FROM_RE.match(line.strip())
        if match:
            stages.append(match.group(1))
    return stages


def documented_stages(text):
    """Return (stage count from heading, stage names from the table)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = HEADING_RE.match(line)
        if heading:
            break
    else:
        raise AssertionError("live/Containerfile stage heading not found")
    rows = []
    for line in lines[index + 1:]:
        if line.startswith("|---") or line.startswith("| Stage "):
            continue
        if not line.startswith("|"):
            if rows:
                break
            continue
        row = ROW_RE.match(line)
        if row:
            rows.append(row.group(1).strip("`"))
    return int(heading.group(1)), rows


class ContainerfileStageInventoryTest(unittest.TestCase):
    def setUp(self):
        self.stages = containerfile_stages(CONTAINERFILE.read_text())
        self.count, self.rows = documented_stages(ARCHITECTURE.read_text())

    def test_only_the_last_stage_is_unnamed(self):
        self.assertTrue(self.stages, "no FROM lines found in live/Containerfile")
        self.assertIsNone(self.stages[-1], "final stage should be unnamed")
        self.assertNotIn(None, self.stages[:-1], "only the final stage may be unnamed")

    def test_table_matches_stage_graph(self):
        expected = self.stages[:-1] + ["final"]
        self.assertEqual(self.rows, expected)

    def test_heading_counts_every_stage(self):
        self.assertEqual(self.count, len(self.stages))


class ParserTest(unittest.TestCase):
    def test_parses_named_and_unnamed_stages(self):
        text = "ARG X=1\nFROM a:b AS ref\nRUN true\nFROM ref AS two\nFROM c:${X}\n"
        self.assertEqual(containerfile_stages(text), ["ref", "two", None])

    def test_parses_documented_table(self):
        text = (
            "### Container: `x` (`live/Containerfile` — 2 stages)\n\n"
            "| Stage | Base | Purpose |\n|---|---|---|\n"
            "| `ref` | img | a |\n| final | img | b |\n\nprose\n| `other` | x | y |\n"
        )
        self.assertEqual(documented_stages(text), (2, ["ref", "final"]))


if __name__ == "__main__":
    unittest.main()
