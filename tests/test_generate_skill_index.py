"""Unit tests for scripts/generate_skill_index.py.

The only prior coverage was TestSkillCatalogUpToDate in
test_live_build_invariants.py, which shells out to --check against the real
repo catalog and only asserts freshness. It exercises none of the failure
branches in find_skill_files / parse_front_matter / build_skill_entry /
build_catalog / validate_catalog / render_markdown / main.

These tests patch REPO_ROOT, SKILLS_DIR, SCHEMA_PATH, and INDEX_PATH to a
temporary directory tree so the real repo catalog is never read or written.
The real docs/skills/index.schema.json is copied into the temp tree so
validate_catalog exercises the actual schema.
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import generate_skill_index as gsi  # noqa: E402

REAL_SCHEMA = REPO / "docs" / "skills" / "index.schema.json"


def _write_skill(path: Path, front_matter: str, body: str = "Body text.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front_matter}\n---\n{body}")


VALID_FM = """\
id: sample-skill
name: sample-skill
one_line_purpose: Do the sample thing.
entry_point: docs/skills/sample-skill.md
category: ci-ops
status: active
tags: [ci]
description: A longer description of the sample skill.
version: "1.0"
last_updated: "2026-01-01\""""


@contextmanager
def _tmp_catalog():
    """Point gsi's module-level paths at a fresh temp tree; restore after."""
    with TemporaryDirectory() as td:
        root = Path(td)
        skills_dir = root / "docs" / "skills"
        skills_dir.mkdir(parents=True)
        schema_path = skills_dir / "index.schema.json"
        shutil.copy(REAL_SCHEMA, schema_path)
        index_path = skills_dir / "index.json"
        with patch.object(gsi, "REPO_ROOT", root), \
             patch.object(gsi, "SKILLS_DIR", skills_dir), \
             patch.object(gsi, "SCHEMA_PATH", schema_path), \
             patch.object(gsi, "INDEX_PATH", index_path):
            yield root, skills_dir


class TestFindSkillFiles(unittest.TestCase):

    def test_finds_flat_and_directory_skills(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            _write_skill(
                skills_dir / "other-skill" / "SKILL.md",
                VALID_FM.replace("sample-skill", "other-skill"),
            )
            files = gsi.find_skill_files()
            names = sorted(p.name for p in files)
            self.assertEqual(names, ["SKILL.md", "sample-skill.md"])

    def test_excludes_index_md_and_index_md_uppercase(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            (skills_dir / "index.md").write_text("generated\n")
            (skills_dir / "INDEX.md").write_text("generated\n")
            files = gsi.find_skill_files()
            names = [p.name for p in files]
            self.assertNotIn("index.md", names)
            self.assertNotIn("INDEX.md", names)

    def test_empty_dir_returns_empty_list(self):
        with _tmp_catalog() as (root, skills_dir):
            self.assertEqual(gsi.find_skill_files(), [])

    def test_results_sorted(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "zzz-skill.md", VALID_FM.replace("sample-skill", "zzz-skill"))
            _write_skill(skills_dir / "aaa-skill.md", VALID_FM.replace("sample-skill", "aaa-skill"))
            files = gsi.find_skill_files()
            self.assertEqual([p.name for p in files], ["aaa-skill.md", "zzz-skill.md"])


class TestParseFrontMatter(unittest.TestCase):

    def test_parses_valid_front_matter(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            _write_skill(p, VALID_FM)
            data = gsi.parse_front_matter(p)
            self.assertEqual(data["id"], "sample-skill")
            self.assertEqual(data["tags"], ["ci"])

    def test_missing_front_matter_raises(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "no-fm.md"
            p.write_text("Just a body, no front matter.\n")
            with self.assertRaisesRegex(ValueError, "no YAML front matter found"):
                gsi.parse_front_matter(p)

    def test_front_matter_not_a_mapping_raises(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "list-fm.md"
            p.write_text("---\n- one\n- two\n---\nBody\n")
            with self.assertRaisesRegex(ValueError, "did not parse to a mapping"):
                gsi.parse_front_matter(p)

    def test_scalar_front_matter_raises(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "scalar-fm.md"
            p.write_text("---\njust a string\n---\nBody\n")
            with self.assertRaisesRegex(ValueError, "did not parse to a mapping"):
                gsi.parse_front_matter(p)


class TestBuildSkillEntry(unittest.TestCase):

    def test_builds_entry_from_valid_front_matter(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            _write_skill(p, VALID_FM)
            entry = gsi.build_skill_entry(p)
            self.assertEqual(entry["id"], "sample-skill")
            self.assertEqual(entry["entry_point"], "docs/skills/sample-skill.md")
            self.assertEqual(entry["version"], "1.0")
            self.assertEqual(entry["last_updated"], "2026-01-01")
            self.assertNotIn("doc_type", entry)

    def test_description_whitespace_is_collapsed(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            fm = VALID_FM.replace(
                "description: A longer description of the sample skill.",
                'description: "A   longer\n  description   of the sample skill."',
            )
            _write_skill(p, fm)
            entry = gsi.build_skill_entry(p)
            self.assertEqual(entry["description"], "A longer description of the sample skill.")

    def test_doc_type_included_when_metadata_present(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            fm = VALID_FM + "\nmetadata:\n  type: reference"
            _write_skill(p, fm)
            entry = gsi.build_skill_entry(p)
            self.assertEqual(entry["doc_type"], "reference")

    def test_missing_required_keys_raises(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            _write_skill(p, "id: sample-skill\nname: sample-skill")
            with self.assertRaisesRegex(ValueError, "missing required front-matter key"):
                gsi.build_skill_entry(p)

    def test_entry_point_mismatch_raises(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            fm = VALID_FM.replace(
                "entry_point: docs/skills/sample-skill.md",
                "entry_point: docs/skills/wrong-path.md",
            )
            _write_skill(p, fm)
            with self.assertRaisesRegex(ValueError, "does not match actual path"):
                gsi.build_skill_entry(p)

    def test_version_and_last_updated_coerced_to_str(self):
        """YAML may parse unquoted version/date-like scalars as non-strings."""
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "sample-skill.md"
            fm = VALID_FM.replace('version: "1.0"', "version: 1.0").replace(
                'last_updated: "2026-01-01"', "last_updated: 2026-01-01"
            )
            _write_skill(p, fm)
            entry = gsi.build_skill_entry(p)
            self.assertIsInstance(entry["version"], str)
            self.assertIsInstance(entry["last_updated"], str)


class TestBuildCatalog(unittest.TestCase):

    def test_builds_sorted_catalog(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "zzz-skill.md", VALID_FM.replace("sample-skill", "zzz-skill"))
            _write_skill(skills_dir / "aaa-skill.md", VALID_FM.replace("sample-skill", "aaa-skill"))
            catalog = gsi.build_catalog()
            ids = [s["id"] for s in catalog["skills"]]
            self.assertEqual(ids, ["aaa-skill", "zzz-skill"])
            self.assertEqual(catalog["schema_version"], gsi.SCHEMA_VERSION)
            self.assertIn("generated_at", catalog)

    def test_empty_catalog_has_no_skills(self):
        with _tmp_catalog() as (root, skills_dir):
            catalog = gsi.build_catalog()
            self.assertEqual(catalog["skills"], [])

    def test_propagates_build_skill_entry_errors(self):
        with _tmp_catalog() as (root, skills_dir):
            p = skills_dir / "bad-skill.md"
            p.write_text("no front matter here\n")
            with self.assertRaises(ValueError):
                gsi.build_catalog()


class TestValidateCatalog(unittest.TestCase):

    def test_valid_catalog_passes(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            catalog = gsi.build_catalog()
            gsi.validate_catalog(catalog)  # should not raise

    def test_missing_required_top_level_key_raises_systemexit(self):
        with _tmp_catalog() as (root, skills_dir):
            catalog = {"schema_version": "1.0", "skills": []}
            with self.assertRaises(SystemExit) as ctx:
                gsi.validate_catalog(catalog)
            self.assertEqual(ctx.exception.code, 1)

    def test_invalid_category_enum_raises_systemexit(self):
        with _tmp_catalog() as (root, skills_dir):
            catalog = gsi.build_catalog()
            catalog["skills"] = [{
                "id": "bad-skill", "name": "bad-skill", "one_line_purpose": "x",
                "entry_point": "docs/skills/bad-skill.md", "category": "not-a-real-category",
                "status": "active", "tags": ["ci"], "description": "x",
                "version": "1.0", "last_updated": "2026-01-01",
            }]
            with self.assertRaises(SystemExit):
                gsi.validate_catalog(catalog)

    def test_additional_properties_rejected(self):
        with _tmp_catalog() as (root, skills_dir):
            catalog = gsi.build_catalog()
            catalog["unexpected_key"] = "nope"
            with self.assertRaises(SystemExit):
                gsi.validate_catalog(catalog)

    def test_errors_printed_to_stderr(self):
        with _tmp_catalog() as (root, skills_dir):
            catalog = {"schema_version": "1.0", "skills": []}
            with patch("sys.stderr") as mock_stderr:
                with self.assertRaises(SystemExit):
                    gsi.validate_catalog(catalog)
            self.assertTrue(mock_stderr.write.called)


class TestRenderMarkdown(unittest.TestCase):

    def test_renders_table_row_per_skill(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            catalog = gsi.build_catalog()
            md = gsi.render_markdown(catalog)
            self.assertIn("| [sample-skill](sample-skill.md) | ci-ops | active |", md)
            self.assertIn("Do the sample thing.", md)

    def test_renders_header_with_count_and_metadata(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            catalog = gsi.build_catalog()
            md = gsi.render_markdown(catalog)
            self.assertIn(f"Generated: {catalog['generated_at']}", md)
            self.assertIn("1 skills", md)

    def test_empty_catalog_renders_header_only(self):
        with _tmp_catalog() as (root, skills_dir):
            catalog = gsi.build_catalog()
            md = gsi.render_markdown(catalog)
            self.assertIn("0 skills", md)
            table_rows = [line for line in md.splitlines() if line.startswith("| [")]
            self.assertEqual(table_rows, [])


class TestMain(unittest.TestCase):

    def _run_main(self, args):
        with patch.object(sys, "argv", ["generate_skill_index.py", *args]):
            return gsi.main()

    def test_write_creates_index_json_and_md(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            rc = self._run_main(["--write"])
            self.assertEqual(rc, 0)
            self.assertTrue(gsi.INDEX_PATH.exists())
            self.assertTrue((skills_dir / "index.md").exists())
            data = json.loads(gsi.INDEX_PATH.read_text())
            self.assertEqual(data["skills"][0]["id"], "sample-skill")

    def test_check_passes_when_up_to_date(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            self._run_main(["--write"])
            rc = self._run_main(["--check"])
            self.assertEqual(rc, 0)

    def test_check_fails_when_index_json_missing(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            rc = self._run_main(["--check"])
            self.assertEqual(rc, 1)

    def test_check_distinguishes_stale_json_from_stale_md(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            self._run_main(["--write"])
            # Make index.json stale but leave index.md fresh.
            gsi.INDEX_PATH.write_text("{}")
            with patch("sys.stderr") as mock_stderr:
                rc = self._run_main(["--check"])
            self.assertEqual(rc, 1)
            printed = "".join(c.args[0] for c in mock_stderr.write.call_args_list if c.args)
            self.assertIn("index.json is stale", printed)
            self.assertNotIn("index.md is stale", printed)

    def test_check_reports_stale_md_only(self):
        with _tmp_catalog() as (root, skills_dir):
            _write_skill(skills_dir / "sample-skill.md", VALID_FM)
            self._run_main(["--write"])
            (skills_dir / "index.md").write_text("stale\n")
            with patch("sys.stderr") as mock_stderr:
                rc = self._run_main(["--check"])
            self.assertEqual(rc, 1)
            printed = "".join(c.args[0] for c in mock_stderr.write.call_args_list if c.args)
            self.assertIn("index.md is stale", printed)
            self.assertNotIn("index.json is stale", printed)

    def test_main_returns_1_on_value_error(self):
        with _tmp_catalog() as (root, skills_dir):
            (skills_dir / "bad-skill.md").write_text("no front matter\n")
            rc = self._run_main(["--write"])
            self.assertEqual(rc, 1)

    def test_main_returns_1_on_schema_violation(self):
        with _tmp_catalog() as (root, skills_dir):
            fm = VALID_FM.replace("category: ci-ops", "category: not-a-real-category")
            _write_skill(skills_dir / "sample-skill.md", fm)
            with self.assertRaises(SystemExit) as ctx:
                self._run_main(["--write"])
            self.assertEqual(ctx.exception.code, 1)

    def test_write_and_check_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._run_main(["--write", "--check"])

    def test_requires_one_of_write_or_check(self):
        with self.assertRaises(SystemExit):
            self._run_main([])


if __name__ == "__main__":
    unittest.main()
