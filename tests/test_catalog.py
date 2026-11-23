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
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-home"}):
            path = self.by_id["codex"].global_path()
        self.assertEqual(path, Path("/tmp/codex-home") / "skills")

    def test_project_path(self):
        path = self.by_id["opencode"].project_path("/work/app")
        self.assertEqual(path, Path("/work/app") / ".opencode" / "skills")


if __name__ == "__main__":
    unittest.main()
