"""Unit tests for scripts/generate_skill_index.py.

Covers the pure helpers (find_skill_files, parse_front_matter,
build_skill_entry, build_catalog, validate_catalog, render_markdown) and the
--write / --check argument paths of main(), all against a temporary skills
tree so the repo's real docs/skills/ is never read or written.
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
REAL_SCHEMA = REPO / "docs" / "skills" / "index.schema.json"

spec = importlib.util.spec_from_file_location(
    "generate_skill_index",
    os.path.join(REPO, "scripts", "generate_skill_index.py"),
)
gsi = importlib.util.module_from_spec(spec)
sys.modules["generate_skill_index"] = gsi
spec.loader.exec_module(gsi)


FRONT_MATTER_DEFAULTS = {
    "name": "Example Skill",
    "one_line_purpose": "Do the example thing.",
    "category": "meta",
    "status": "active",
    "tags": ["example"],
    "description": "A longer description of the example skill.",
    "version": "1.0",
    "last_updated": "2026-01-01",
}


def front_matter(entry_point, skill_id="example", **overrides):
    fields = dict(FRONT_MATTER_DEFAULTS)
    fields["id"] = skill_id
    fields["entry_point"] = entry_point
    fields.update(overrides)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            lines.extend(f"  {k}: {v}" for k, v in value.items())
        else:
            lines.append(f"{key}: {value}")
    lines += ["---", "", "# Body", ""]
    return "\n".join(lines)


class SkillsTreeTestCase(unittest.TestCase):
    """Base case that points the module at a throwaway skills tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.skills_dir = self.tmp / "docs" / "skills"
        self.skills_dir.mkdir(parents=True)
        shutil.copyfile(REAL_SCHEMA, self.skills_dir / "index.schema.json")

        for attr, value in (
            ("REPO_ROOT", self.tmp),
            ("SKILLS_DIR", self.skills_dir),
            ("SCHEMA_PATH", self.skills_dir / "index.schema.json"),
            ("INDEX_PATH", self.skills_dir / "index.json"),
        ):
            patcher = patch.object(gsi, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_skill(self, relpath, skill_id=None, **overrides):
        """Write a skill markdown file and return its Path."""
        path = self.skills_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        entry_point = f"docs/skills/{relpath}"
        if skill_id is None:
            skill_id = Path(relpath).stem.lower().replace("_", "-")
            if skill_id == "skill":
                skill_id = Path(relpath).parent.name
        path.write_text(front_matter(entry_point, skill_id, **overrides))
        return path


class TestFindSkillFiles(SkillsTreeTestCase):
    def test_returns_flat_markdown_sorted(self):
        self.write_skill("beta.md")
        self.write_skill("alpha.md")
        self.assertEqual(
            [p.name for p in gsi.find_skill_files()], ["alpha.md", "beta.md"]
        )

    def test_excludes_generated_index_files(self):
        self.write_skill("alpha.md")
        (self.skills_dir / "index.md").write_text("# generated\n")
        (self.skills_dir / "INDEX.md").write_text("# generated\n")
        self.assertEqual([p.name for p in gsi.find_skill_files()], ["alpha.md"])

    def test_includes_per_directory_skill_files_after_flat_files(self):
        self.write_skill("alpha.md")
        self.write_skill("nested/SKILL.md")
        found = [p.relative_to(self.skills_dir).as_posix() for p in gsi.find_skill_files()]
        self.assertEqual(found, ["alpha.md", "nested/SKILL.md"])

    def test_ignores_non_markdown_and_nested_non_skill_markdown(self):
        self.write_skill("alpha.md")
        (self.skills_dir / "notes.txt").write_text("ignored")
        (self.skills_dir / "nested").mkdir()
        (self.skills_dir / "nested" / "other.md").write_text("ignored")
        self.assertEqual([p.name for p in gsi.find_skill_files()], ["alpha.md"])


class TestParseFrontMatter(SkillsTreeTestCase):
    def test_parses_mapping(self):
        path = self.write_skill("alpha.md")
        data = gsi.parse_front_matter(path)
        self.assertEqual(data["id"], "alpha")
        self.assertEqual(data["entry_point"], "docs/skills/alpha.md")

    def test_missing_front_matter_raises(self):
        path = self.skills_dir / "bare.md"
        path.write_text("# no front matter\n")
        with self.assertRaises(ValueError) as ctx:
            gsi.parse_front_matter(path)
        self.assertIn("no YAML front matter found", str(ctx.exception))

    def test_front_matter_must_be_at_start_of_file(self):
        path = self.skills_dir / "late.md"
        path.write_text("intro\n---\nid: late\n---\n")
        with self.assertRaises(ValueError):
            gsi.parse_front_matter(path)

    def test_non_mapping_front_matter_raises(self):
        path = self.skills_dir / "list.md"
        path.write_text("---\n- one\n- two\n---\n\nbody\n")
        with self.assertRaises(ValueError) as ctx:
            gsi.parse_front_matter(path)
        self.assertIn("did not parse to a mapping", str(ctx.exception))


class TestBuildSkillEntry(SkillsTreeTestCase):
    def test_builds_expected_entry(self):
        path = self.write_skill("alpha.md")
        entry = gsi.build_skill_entry(path)
        self.assertEqual(
            entry,
            {
                "id": "alpha",
                "name": "Example Skill",
                "one_line_purpose": "Do the example thing.",
                "entry_point": "docs/skills/alpha.md",
                "category": "meta",
                "status": "active",
                "tags": ["example"],
                "description": "A longer description of the example skill.",
                "version": "1.0",
                "last_updated": "2026-01-01",
            },
        )

    def test_description_whitespace_is_collapsed(self):
        path = self.skills_dir / "alpha.md"
        path.write_text(
            "---\n"
            "id: alpha\n"
            "name: Example\n"
            "one_line_purpose: Purpose.\n"
            "entry_point: docs/skills/alpha.md\n"
            "category: meta\n"
            "status: active\n"
            "tags:\n  - example\n"
            "description: >\n  first line\n  second   line\n"
            "version: 1.0\n"
            "last_updated: 2026-01-01\n"
            "---\n\nbody\n"
        )
        self.assertEqual(gsi.build_skill_entry(path)["description"], "first line second line")

    def test_version_and_last_updated_are_stringified(self):
        # YAML parses unquoted 1.0 as float and 2026-01-01 as datetime.date.
        path = self.write_skill("alpha.md", version=1.0, last_updated="2026-01-01")
        entry = gsi.build_skill_entry(path)
        self.assertEqual(entry["version"], "1.0")
        self.assertEqual(entry["last_updated"], "2026-01-01")
        self.assertIsInstance(entry["version"], str)
        self.assertIsInstance(entry["last_updated"], str)

    def test_doc_type_included_when_metadata_type_present(self):
        path = self.write_skill("alpha.md", metadata={"type": "procedure"})
        self.assertEqual(gsi.build_skill_entry(path)["doc_type"], "procedure")

    def test_doc_type_omitted_when_metadata_absent_or_empty(self):
        path = self.write_skill("alpha.md")
        self.assertNotIn("doc_type", gsi.build_skill_entry(path))

    def test_missing_required_keys_listed_in_error(self):
        path = self.skills_dir / "alpha.md"
        path.write_text(
            "---\nid: alpha\nname: Example\nentry_point: docs/skills/alpha.md\n---\n\nbody\n"
        )
        with self.assertRaises(ValueError) as ctx:
            gsi.build_skill_entry(path)
        message = str(ctx.exception)
        self.assertIn("missing required front-matter key(s)", message)
        self.assertIn("one_line_purpose", message)
        self.assertIn("last_updated", message)

    def test_entry_point_mismatch_raises(self):
        path = self.skills_dir / "alpha.md"
        path.write_text(front_matter("docs/skills/wrong.md", "alpha"))
        with self.assertRaises(ValueError) as ctx:
            gsi.build_skill_entry(path)
        self.assertIn("does not match actual path", str(ctx.exception))

    def test_nested_skill_entry_point_uses_directory_path(self):
        path = self.write_skill("nested/SKILL.md")
        entry = gsi.build_skill_entry(path)
        self.assertEqual(entry["entry_point"], "docs/skills/nested/SKILL.md")
        self.assertEqual(entry["id"], "nested")


class TestBuildCatalog(SkillsTreeTestCase):
    def test_catalog_shape_and_id_sort(self):
        self.write_skill("zulu.md")
        self.write_skill("alpha.md")
        self.write_skill("nested/SKILL.md")
        catalog = gsi.build_catalog()
        self.assertEqual(set(catalog), {"generated_at", "schema_version", "skills"})
        self.assertEqual(catalog["schema_version"], gsi.SCHEMA_VERSION)
        self.assertEqual([s["id"] for s in catalog["skills"]], ["alpha", "nested", "zulu"])

    def test_generated_at_is_today(self):
        self.write_skill("alpha.md")
        self.assertEqual(
            gsi.build_catalog()["generated_at"], gsi.date.today().isoformat()
        )

    def test_empty_skills_dir_yields_empty_catalog(self):
        self.assertEqual(gsi.build_catalog()["skills"], [])


class TestValidateCatalog(SkillsTreeTestCase):
    def test_valid_catalog_passes(self):
        self.write_skill("alpha.md")
        gsi.validate_catalog(gsi.build_catalog())  # must not raise

    def test_schema_violation_exits_1_and_reports_location(self):
        self.write_skill("alpha.md")
        catalog = gsi.build_catalog()
        catalog["skills"][0]["category"] = "not-a-category"
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            gsi.validate_catalog(catalog)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("schema error at", stderr.getvalue())

    def test_root_level_error_is_reported(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            gsi.validate_catalog({"schema_version": "1.0", "skills": []})
        self.assertIn("<root>", stderr.getvalue())


class TestRenderMarkdown(SkillsTreeTestCase):
    def test_table_row_per_skill_with_relative_link(self):
        self.write_skill("alpha.md")
        self.write_skill("nested/SKILL.md")
        md = gsi.render_markdown(gsi.build_catalog())
        self.assertIn("| [alpha](alpha.md) | meta | active | Do the example thing. |", md)
        self.assertIn("| [nested](nested/SKILL.md) |", md)
        self.assertNotIn("docs/skills/", md.split("|---|---|---|---|", 1)[1])

    def test_header_reports_count_and_schema_version(self):
        self.write_skill("alpha.md")
        catalog = gsi.build_catalog()
        md = gsi.render_markdown(catalog)
        self.assertIn(
            f"Generated: {catalog['generated_at']} · schema 1.0 · 1 skills", md
        )
        self.assertTrue(md.startswith("# Skill catalog (generated)"))
        self.assertTrue(md.endswith("\n"))


class TestMain(SkillsTreeTestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["generate_skill_index.py", *argv]):
            with redirect_stdout(out), redirect_stderr(err):
                rc = gsi.main()
        return rc, out.getvalue(), err.getvalue()

    def test_write_creates_index_json_and_index_md(self):
        self.write_skill("alpha.md")
        rc, out, _ = self.run_main("--write")
        self.assertEqual(rc, 0)
        index = json.loads((self.skills_dir / "index.json").read_text())
        self.assertEqual([s["id"] for s in index["skills"]], ["alpha"])
        self.assertTrue((self.skills_dir / "index.md").exists())
        self.assertIn("1 skills", out)

    def test_written_json_is_indented_and_newline_terminated(self):
        self.write_skill("alpha.md")
        self.run_main("--write")
        text = (self.skills_dir / "index.json").read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertIn('\n  "schema_version"', text)

    def test_check_passes_immediately_after_write(self):
        self.write_skill("alpha.md")
        self.run_main("--write")
        rc, out, _ = self.run_main("--check")
        self.assertEqual(rc, 0)
        self.assertIn("up to date", out)

    def test_check_fails_when_index_json_missing(self):
        self.write_skill("alpha.md")
        rc, _, err = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("index.json is stale", err)

    def test_check_fails_when_index_json_stale(self):
        self.write_skill("alpha.md")
        self.run_main("--write")
        self.write_skill("beta.md")
        rc, _, err = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("index.json is stale", err)

    def test_check_fails_when_only_index_md_stale(self):
        self.write_skill("alpha.md")
        self.run_main("--write")
        (self.skills_dir / "index.md").write_text("hand edited\n")
        rc, _, err = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("index.md is stale", err)
        self.assertNotIn("index.json is stale", err)

    def test_front_matter_error_returns_1_without_writing(self):
        (self.skills_dir / "broken.md").write_text("# no front matter\n")
        rc, _, err = self.run_main("--write")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertFalse((self.skills_dir / "index.json").exists())

    def test_write_and_check_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main("--write", "--check")
        self.assertEqual(ctx.exception.code, 2)

    def test_a_mode_flag_is_required(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
