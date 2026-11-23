import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.catalog import Harness
from backend.scanner import scan_all, scan_harness, scan_skill_dir


def make_dir_link(link: Path, target: Path) -> None:
    """Symlink de directorio; en Windows sin privilegio, junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )


def make_skill(parent: Path, name: str, with_md: bool = True) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    if with_md:
        (d / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    return d


def fake_harness(hid="fake", global_template="", binaries=()) -> Harness:
    return Harness(
        id=hid,
        name=hid,
        global_template=global_template,
        project_template=".fake/skills",
        binaries=tuple(binaries),
        verified=True,
    )


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.skills = self.tmp / "skills"
        self.skills.mkdir()

    def by_name(self, skills):
        return {s.name: s for s in skills}

    def test_lists_real_skills(self):
        make_skill(self.skills, "alpha")
        make_skill(self.skills, "beta", with_md=False)
        got = self.by_name(scan_skill_dir(self.skills))
        self.assertEqual(set(got), {"alpha", "beta"})
        self.assertEqual(got["alpha"].kind, "real")
        self.assertTrue(got["alpha"].has_skill_md)
        self.assertFalse(got["beta"].has_skill_md)
        self.assertEqual(got["alpha"].real_path, str(self.skills / "alpha"))

    def test_ignores_files_and_hidden(self):
        (self.skills / "README.md").write_text("x", encoding="utf-8")
        make_skill(self.skills, ".system")
        make_skill(self.skills, "ok")
        self.assertEqual([s.name for s in scan_skill_dir(self.skills)], ["ok"])

    def test_symlink_resolves_to_real_path(self):
        canonical = self.tmp / "canonical"
        real = make_skill(canonical, "alpha")
        make_dir_link(self.skills / "alpha", real)
        skill = scan_skill_dir(self.skills)[0]
        self.assertEqual(skill.kind, "symlink")
        self.assertEqual(Path(skill.real_path), real)
        self.assertTrue(skill.has_skill_md)
        self.assertIsNotNone(skill.link_target)

    def test_broken_link_marked(self):
        gone = make_skill(self.tmp / "canonical", "gone")
        make_dir_link(self.skills / "gone", gone)
        shutil.rmtree(gone)
        skill = scan_skill_dir(self.skills)[0]
        self.assertEqual(skill.kind, "broken")
        self.assertIsNone(skill.real_path)
        self.assertFalse(skill.has_skill_md)

    def test_missing_directory(self):
        self.assertEqual(scan_skill_dir(self.tmp / "nope"), [])

    def test_harness_detected_by_directory(self):
        make_skill(self.skills, "alpha")
        scan = scan_harness(fake_harness(global_template=str(self.skills)))
        self.assertTrue(scan.dir_exists)
        self.assertTrue(scan.detected)
        self.assertEqual(len(scan.skills), 1)

    def test_harness_not_detected(self):
        scan = scan_harness(fake_harness(global_template=str(self.tmp / "nope")))
        self.assertFalse(scan.detected)
        self.assertEqual(scan.skills, [])

    def test_harness_detected_by_binary_without_dir(self):
        scan = scan_harness(
            fake_harness(global_template=str(self.tmp / "nope"), binaries=["python"])
        )
        self.assertFalse(scan.dir_exists)
        self.assertIsNotNone(scan.binary_found)
        self.assertTrue(scan.detected)

    def test_project_scope(self):
        make_skill(self.tmp / ".fake" / "skills", "proj")
        scan = scan_harness(fake_harness(), "project", self.tmp)
        self.assertEqual([s.name for s in scan.skills], ["proj"])

    def test_project_scope_requires_dir(self):
        with self.assertRaises(ValueError):
            scan_harness(fake_harness(), "project")

    def test_scan_all_real_catalog_is_serializable(self):
        results = scan_all()
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertIn("detected", r.to_dict())


if __name__ == "__main__":
    unittest.main()
