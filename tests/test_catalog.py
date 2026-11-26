import os
import unittest
from pathlib import Path
from unittest import mock

from backend.catalog import Harness, load_catalog


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.by_id = {h.id: h for h in load_catalog()}

    def test_initial_harnesses_present(self):
        for hid in ("agents", "claude-code", "codex", "cursor", "opencode"):
            self.assertIn(hid, self.by_id)

    def test_global_path_expands_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            path = self.by_id["claude-code"].global_path()
        self.assertEqual(path, Path.home() / ".claude" / "skills")

    def test_env_var_overrides_global_path(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/tmp/claude-home"}):
            path = self.by_id["claude-code"].global_path()
        self.assertEqual(path, Path("/tmp/claude-home") / "skills")

    def test_codex_shares_agents_folder(self):
        self.assertEqual(self.by_id["codex"].global_path(), self.by_id["agents"].global_path())
        self.assertEqual(
            self.by_id["codex"].project_path("/w"), self.by_id["agents"].project_path("/w")
        )

    def test_antigravity_paths(self):
        h = self.by_id["antigravity"]
        self.assertEqual(h.global_path(), Path.home() / ".gemini" / "config" / "skills")
        self.assertEqual(h.project_path("/w"), self.by_id["agents"].project_path("/w"))
        self.assertEqual(h.binaries, ("agy",))

    def test_github_copilot_paths(self):
        h = self.by_id["github-copilot"]
        self.assertEqual(h.global_path(), Path.home() / ".copilot" / "skills")
        self.assertEqual(h.project_path("/w"), Path("/w") / ".github" / "skills")
        self.assertEqual(h.binaries, ("copilot",))

    def test_cline_paths(self):
        h = self.by_id["cline"]
        self.assertEqual(h.global_path(), Path.home() / ".cline" / "skills")
        self.assertEqual(h.project_path("/w"), Path("/w") / ".cline" / "skills")

    def test_kiro_paths(self):
        h = self.by_id["kiro"]
        self.assertEqual(h.global_path(), Path.home() / ".kiro" / "skills")
        self.assertEqual(h.project_path("/w"), Path("/w") / ".kiro" / "skills")

    def test_amp_paths(self):
        h = self.by_id["amp"]
        self.assertEqual(h.global_path(), Path.home() / ".config" / "agents" / "skills")
        self.assertEqual(h.project_path("/w"), self.by_id["agents"].project_path("/w"))
        self.assertEqual(h.binaries, ("amp",))

    def test_goose_shares_agents_folder(self):
        h = self.by_id["goose"]
        self.assertEqual(h.global_path(), self.by_id["agents"].global_path())
        self.assertEqual(h.project_path("/w"), self.by_id["agents"].project_path("/w"))
        self.assertEqual(h.binaries, ("goose",))

    def test_every_entry_cites_an_official_source(self):
        for h in self.by_id.values():
            with self.subTest(harness=h.id):
                self.assertTrue(h.source and h.source.startswith("https://"))
                self.assertTrue(h.note)

    def test_all_entries_verified(self):
        self.assertEqual([h.id for h in self.by_id.values() if not h.verified], [])

    def test_project_path(self):
        path = self.by_id["opencode"].project_path("/work/app")
        self.assertEqual(path, Path("/work/app") / ".opencode" / "skills")

    def test_custom_folder_has_the_same_path_in_any_scope(self):
        custom = Harness(id="mio", name="Mío", global_template="/mis/skills", project_template="",
                         binaries=(), verified=False)
        self.assertEqual(custom.project_path("/work/app"), Path("/mis/skills"))
        self.assertEqual(custom.project_path("/work/app"), custom.global_path())


if __name__ == "__main__":
    unittest.main()
