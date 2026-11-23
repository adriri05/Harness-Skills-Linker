import os
import unittest
from pathlib import Path
from unittest import mock

from backend.catalog import load_catalog


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

    def test_all_entries_verified(self):
        self.assertEqual([h.id for h in self.by_id.values() if not h.verified], [])

    def test_project_path(self):
        path = self.by_id["opencode"].project_path("/work/app")
        self.assertEqual(path, Path("/work/app") / ".opencode" / "skills")


if __name__ == "__main__":
    unittest.main()
