"""Enlace de archivos de instrucciones (issue #33): catálogo, plan, aplicación y deshacer."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import history, instructions, linker, symlinks
from backend.catalog import Harness, load_catalog
from backend.server import TOKEN_HEADER, ApiError, create_server, validate_instructions_plan
from backend.symlinks import LinkError, is_link
from tests.test_undo import snapshot


def harness(hid, project=None, global_=None) -> Harness:
    return Harness(id=hid, name=hid, global_template="", project_template="", binaries=(),
                   verified=True, instructions_global=global_, instructions_project=project,
                   instructions_source="https://example.test")


class CatalogInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.by_id = {h.id: h for h in load_catalog()}

    def test_project_files(self):
        expected = {"claude-code": "CLAUDE.md", "codex": "AGENTS.md", "cursor": "AGENTS.md",
                    "opencode": "AGENTS.md", "gemini-cli": "GEMINI.md", "antigravity": "AGENTS.md"}
        for hid, name in expected.items():
            self.assertEqual(self.by_id[hid].instructions_project_path("/w"), Path("/w") / name, hid)

    def test_global_files(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ.pop("CODEX_HOME", None)
            home = Path.home()
            self.assertEqual(self.by_id["claude-code"].instructions_global_path(), home / ".claude" / "CLAUDE.md")
            self.assertEqual(self.by_id["codex"].instructions_global_path(), home / ".codex" / "AGENTS.md")
            self.assertEqual(self.by_id["gemini-cli"].instructions_global_path(), home / ".gemini" / "GEMINI.md")
            self.assertEqual(self.by_id["opencode"].instructions_global_path(),
                             home / ".config" / "opencode" / "AGENTS.md")
            self.assertEqual(self.by_id["antigravity"].instructions_global_path(),
                             home / ".gemini" / "config" / "AGENTS.md")

    def test_env_var_overrides_global_file(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-home"}):
            self.assertEqual(self.by_id["codex"].instructions_global_path(),
                             Path("/tmp/codex-home") / "AGENTS.md")

    def test_cursor_has_no_global_file(self):
        self.assertIsNone(self.by_id["cursor"].instructions_global_path())

    def test_every_instruction_entry_has_an_official_source(self):
        with_files = [h for h in self.by_id.values() if h.instructions_global or h.instructions_project]
        self.assertTrue(with_files)
        for h in with_files:
            self.assertTrue((h.instructions_source or "").startswith("https://"), h.id)

    def test_agents_folder_has_no_instruction_file(self):
        self.assertIsNone(self.by_id["agents"].instructions_project_path("/w"))


class InstructionsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {history.HOME_ENV: str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.harnesses = [harness("claude", "CLAUDE.md"), harness("codex", "AGENTS.md"),
                          harness("gemini", "GEMINI.md"), harness("cursor", "AGENTS.md"),
                          harness("nofile")]

    def plan(self, ids=("claude", "codex"), **kw):
        kw.setdefault("scope", "project")
        return instructions.build_plan(list(ids), project_dir=str(self.proj), harnesses=self.harnesses, **kw)

    def apply(self, ids=("claude", "codex"), **kw):
        kw.setdefault("scope", "project")
        return instructions.apply(list(ids), project_dir=str(self.proj), harnesses=self.harnesses, **kw)

    def write(self, name, text="x\n"):
        (self.proj / name).write_text(text, encoding="utf-8")
        return self.proj / name

    def by_name(self, plan):
        return {a.skill: a for a in plan.actions}

    def undo(self):
        return linker.undo().to_dict()


class PlanTest(InstructionsBase):
    def test_creates_link_to_missing_file(self):
        self.write("AGENTS.md")
        plan = self.plan(ids=("claude", "codex", "gemini"), canonical="codex")
        acts = self.by_name(plan)
        self.assertEqual(acts["AGENTS.md"].action, "already_linked")
        self.assertEqual(acts["CLAUDE.md"].action, "create_link")
        self.assertEqual(acts["CLAUDE.md"].source, str(self.proj / "AGENTS.md"))
        self.assertEqual(acts["GEMINI.md"].action, "create_link")
        self.assertEqual(plan.canonical, str(self.proj / "AGENTS.md"))

    def test_dry_run_does_not_touch_disk(self):
        self.write("AGENTS.md")
        before = snapshot(self.tmp)
        self.plan(canonical="codex")
        self.assertEqual(snapshot(self.tmp), before)

    def test_harnesses_sharing_one_file_make_one_row(self):
        self.write("AGENTS.md")
        plan = self.plan(ids=("codex", "cursor", "claude"), canonical="codex")
        self.assertEqual(sorted(a.skill for a in plan.actions), ["AGENTS.md", "CLAUDE.md"])
        shared = self.by_name(plan)["AGENTS.md"]
        self.assertEqual(shared.harness_id, "codex")
        self.assertIn("cursor", shared.reason)

    def test_harness_without_file_is_reported_and_left_out(self):
        self.write("AGENTS.md")
        plan = self.plan(ids=("codex", "nofile"))
        self.assertEqual([a.skill for a in plan.actions], ["AGENTS.md"])
        self.assertTrue(any("nofile" in w for w in plan.warnings))

    def test_global_scope_without_file_warns_and_unknown_harness_raises(self):
        plan = instructions.build_plan(["nofile"], harnesses=self.harnesses)
        self.assertEqual(plan.actions, [])
        with self.assertRaises(ValueError):
            self.plan(ids=("nope",))

    def test_canonical_by_absolute_path(self):
        other = self.tmp / "shared" / "AGENTS.md"
        other.parent.mkdir()
        other.write_text("canon\n", encoding="utf-8")
        plan = self.plan(ids=("claude",), canonical=str(other))
        act = self.by_name(plan)["CLAUDE.md"]
        self.assertEqual((act.action, act.source), ("create_link", str(other)))

    def test_relative_canonical_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(canonical="AGENTS.md")

    def test_real_files_that_differ_are_a_conflict(self):
        self.write("AGENTS.md", "a\n")
        self.write("CLAUDE.md", "b\n")
        plan = self.plan()
        self.assertEqual({a.action for a in plan.actions}, {"conflict"})
        self.assertEqual(plan.summary()["conflict"], 2)

    def test_real_file_differing_from_canonical_is_a_conflict_without_rule(self):
        self.write("AGENTS.md", "a\n")
        self.write("CLAUDE.md", "b\n")
        act = self.by_name(self.plan(canonical="codex"))["CLAUDE.md"]
        self.assertEqual(act.action, "conflict")

    def test_directory_in_place_of_file_is_skipped(self):
        self.write("AGENTS.md")
        (self.proj / "CLAUDE.md").mkdir()
        act = self.by_name(self.plan(canonical="codex"))["CLAUDE.md"]
        self.assertEqual(act.action, "skipped")
        self.assertIn("no es un archivo", act.reason)

    def test_move_rule_without_canonical_warns(self):
        plan = self.plan(conflict_rule="move_to_canonical")
        self.assertTrue(any("necesita un archivo canónico" in w for w in plan.warnings))

    def test_to_dict_contract(self):
        self.write("AGENTS.md")
        d = self.plan(canonical="codex").to_dict()
        self.assertEqual(set(d), {"actions", "summary", "canonical", "warnings"})
        self.assertEqual(set(d["actions"][0]),
                         {"skill", "harness_id", "target", "source", "action", "reason", "resolution"})

    def test_windows_without_privileges_warns_about_hard_links(self):
        with mock.patch.object(instructions, "_IS_WINDOWS", True), \
                mock.patch.object(instructions, "can_create_symlinks", return_value=False):
            plan = self.plan()
        self.assertTrue(any("enlaces duros" in w for w in plan.warnings))


class ApplyUndoTest(InstructionsBase):
    def test_link_is_created_and_undone(self):
        self.write("AGENTS.md", "reglas\n")
        before = snapshot(self.tmp / "proj")
        result = self.apply(canonical="codex")
        self.assertEqual(result.to_dict()["summary"]["ok"], 1)
        link = self.proj / "CLAUDE.md"
        self.assertEqual(link.read_text(encoding="utf-8"), "reglas\n")
        self.assertTrue(os.path.samefile(link, self.proj / "AGENTS.md"))
        self.assertIsNotNone(result.application_id)

        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual(snapshot(self.tmp / "proj"), before)

    def test_second_apply_changes_nothing(self):
        self.write("AGENTS.md")
        self.apply(canonical="codex")
        again = self.apply(canonical="codex")
        self.assertIsNone(again.application_id)
        self.assertEqual(again.to_dict()["summary"]["ok"], 0)

    def test_editing_the_canonical_is_visible_from_the_link(self):
        canon = self.write("AGENTS.md", "uno\n")
        self.apply(canonical="codex")
        canon.write_text("dos\n", encoding="utf-8")
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"), "dos\n")

    def test_conflict_is_never_applied(self):
        self.write("AGENTS.md", "a\n")
        self.write("CLAUDE.md", "b\n")
        before = snapshot(self.tmp)
        result = self.apply(canonical="codex")
        self.assertIsNone(result.application_id)
        self.assertEqual(snapshot(self.tmp), before)

    def test_move_to_canonical_backs_up_and_undo_restores(self):
        self.write("CLAUDE.md", "mío\n")
        self.write("GEMINI.md", "otro\n")
        before = snapshot(self.tmp / "proj")
        result = self.apply(ids=("claude", "codex", "gemini"), canonical="codex",
                            conflict_rule="move_to_canonical")
        self.assertEqual(result.to_dict()["summary"]["error"], 0)
        self.assertEqual((self.proj / "AGENTS.md").read_text(encoding="utf-8"), "mío\n")
        self.assertTrue(os.path.samefile(self.proj / "AGENTS.md", self.proj / "CLAUDE.md"))
        self.assertTrue(os.path.samefile(self.proj / "AGENTS.md", self.proj / "GEMINI.md"))
        backups = list((self.proj / linker.BACKUP_DIR).rglob("*.md"))
        self.assertEqual(sorted(p.name for p in backups), ["CLAUDE.md", "GEMINI.md"])
        self.assertEqual(sorted(p.read_text(encoding="utf-8") for p in backups), ["mío\n", "otro\n"])

        res = self.undo()
        self.assertEqual(res["summary"]["error"], 0)
        self.assertEqual(res["summary"]["conflict"], 0)
        self.assertEqual(snapshot(self.tmp / "proj"), before)

    def test_move_all_moves_a_single_file_to_the_canonical(self):
        self.write("CLAUDE.md", "solo\n")
        before = snapshot(self.tmp / "proj")
        self.apply(canonical="codex", conflict_rule="move_all_to_canonical")
        self.assertEqual((self.proj / "AGENTS.md").read_text(encoding="utf-8"), "solo\n")
        self.assertTrue(os.path.samefile(self.proj / "AGENTS.md", self.proj / "CLAUDE.md"))
        self.undo()
        self.assertEqual(snapshot(self.tmp / "proj"), before)

    def test_prefer_harness_wins_and_others_keep_their_real_file(self):
        self.write("AGENTS.md", "a\n")
        self.write("CLAUDE.md", "b\n")
        plan = self.plan(ids=("claude", "codex", "gemini"), conflict_rule="prefer_harness:claude")
        acts = self.by_name(plan)
        self.assertEqual(acts["GEMINI.md"].source, str(self.proj / "CLAUDE.md"))
        self.assertEqual(acts["GEMINI.md"].resolution, "prefer_harness:claude")
        self.assertEqual(acts["AGENTS.md"].action, "skipped")

    def test_backup_rule_replaces_duplicate_real_file(self):
        self.write("AGENTS.md", "canon\n")
        self.write("CLAUDE.md", "dup\n")
        before = snapshot(self.tmp / "proj")
        self.apply(canonical="codex", conflict_rule="move_to_canonical")
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"), "canon\n")
        self.assertEqual(self.proj.joinpath(linker.BACKUP_DIR).exists(), True)
        self.undo()
        self.assertEqual(snapshot(self.tmp / "proj"), before)

    def test_undo_leaves_a_file_the_user_edited_in_place_of_the_link(self):
        self.write("AGENTS.md", "canon\n")
        self.apply(canonical="codex")
        (self.proj / "CLAUDE.md").unlink()
        self.write("CLAUDE.md", "nuevo\n")
        res = self.undo()
        self.assertEqual(res["summary"]["skipped"], 1)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"), "nuevo\n")

    def test_undo_removes_only_the_link_of_a_hard_link(self):
        self.write("AGENTS.md", "canon\n")
        with mock.patch.object(symlinks.os, "symlink", side_effect=OSError("sin permiso")), \
                mock.patch.object(symlinks, "_IS_WINDOWS", True):
            self.apply(canonical="codex")
        self.assertTrue(os.path.samefile(self.proj / "AGENTS.md", self.proj / "CLAUDE.md"))
        self.assertFalse(is_link(self.proj / "CLAUDE.md"))
        again = self.plan(canonical="codex")
        self.assertEqual(self.by_name(again)["CLAUDE.md"].action, "already_linked")
        self.undo()
        self.assertFalse((self.proj / "CLAUDE.md").exists())
        self.assertEqual((self.proj / "AGENTS.md").read_text(encoding="utf-8"), "canon\n")

    def test_missing_parent_folders_are_created_and_removed_on_undo(self):
        self.harnesses.append(harness("deep", "sub/dir/DEEP.md"))
        self.write("AGENTS.md")
        before = snapshot(self.tmp / "proj")
        self.apply(ids=("codex", "deep"), canonical="codex")
        self.assertTrue((self.proj / "sub" / "dir" / "DEEP.md").exists())
        self.undo()
        self.assertEqual(snapshot(self.tmp / "proj"), before)

    @unittest.skipUnless(symlinks.can_create_symlinks(), "necesita permiso para crear symlinks")
    def test_symlink_is_used_when_allowed_and_broken_link_is_replaced(self):
        self.write("AGENTS.md", "canon\n")
        os.symlink(self.tmp / "no-existe.md", self.proj / "CLAUDE.md")
        before = snapshot(self.tmp / "proj")
        self.assertEqual(self.by_name(self.plan(canonical="codex"))["CLAUDE.md"].action, "create_link")
        self.apply(canonical="codex")
        self.assertTrue(is_link(self.proj / "CLAUDE.md"))
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"), "canon\n")
        self.undo()
        self.assertEqual(snapshot(self.tmp / "proj"), before)


class FileLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "AGENTS.md"
        self.src.write_text("x", encoding="utf-8")

    def test_non_windows_failure_has_no_fallback(self):
        with mock.patch.object(symlinks.os, "symlink", side_effect=OSError("no")), \
                mock.patch.object(symlinks, "_IS_WINDOWS", False):
            with self.assertRaises(LinkError):
                symlinks.make_file_link(self.tmp / "l.md", self.src)
        self.assertFalse((self.tmp / "l.md").exists())

    def test_windows_falls_back_to_hard_link(self):
        with mock.patch.object(symlinks.os, "symlink", side_effect=OSError("no")), \
                mock.patch.object(symlinks, "_IS_WINDOWS", True):
            kind = symlinks.make_file_link(self.tmp / "l.md", self.src)
        self.assertEqual(kind, "hardlink")
        self.assertTrue(os.path.samefile(self.tmp / "l.md", self.src))

    def test_windows_never_copies_when_hard_link_fails(self):
        with mock.patch.object(symlinks.os, "symlink", side_effect=OSError("no")), \
                mock.patch.object(symlinks.os, "link", side_effect=OSError("otro volumen")), \
                mock.patch.object(symlinks, "_IS_WINDOWS", True):
            with self.assertRaises(LinkError) as ctx:
                symlinks.make_file_link(self.tmp / "l.md", self.src)
        self.assertIn("mismo volumen", str(ctx.exception))
        self.assertFalse((self.tmp / "l.md").exists())


class ScanAndValidationTest(InstructionsBase):
    def test_scan_reports_state_per_harness(self):
        self.write("AGENTS.md")
        by_id = {i["id"]: i for i in instructions.scan("project", str(self.proj), self.harnesses)}
        self.assertEqual(by_id["codex"]["kind"], "real")
        self.assertEqual(by_id["claude"]["kind"], "missing")
        self.assertEqual(by_id["nofile"]["kind"], "none")
        self.assertIsNone(by_id["nofile"]["path"])
        self.assertTrue(by_id["codex"]["detected"])
        self.assertFalse(by_id["claude"]["detected"])

    def test_plan_body_rejects_custom_folders(self):
        body = {"harness_ids": ["codex"], "scope": "global", "conflict_rule": "none", "custom": []}
        with self.assertRaises(ApiError):
            validate_instructions_plan(body)

    def test_plan_body_accepts_the_skill_contract_without_custom(self):
        body = {"harness_ids": ["codex"], "scope": "global", "conflict_rule": "none", "canonical": None}
        self.assertNotIn("custom", validate_instructions_plan(body))


class ApiTest(InstructionsBase):
    def setUp(self):
        super().setUp()
        self.server = create_server(0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            conn.request("POST", path, body=json.dumps(body),
                         headers={"Content-Type": "application/json", TOKEN_HEADER: self.server.token})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_scan_plan_apply_and_undo_over_http(self):
        self.write("AGENTS.md", "reglas\n")
        base = {"scope": "project", "project_dir": str(self.proj)}
        status, scan = self.post("/api/instructions/scan", base)
        self.assertEqual(status, 200)
        by_id = {h["id"]: h for h in scan["harnesses"]}
        self.assertEqual(by_id["codex"]["kind"], "real")
        self.assertEqual(by_id["claude-code"]["kind"], "missing")
        self.assertEqual(by_id["cursor"]["path"], str(self.proj / "AGENTS.md"))

        body = dict(base, harness_ids=["codex", "claude-code"], canonical="codex", conflict_rule="none")
        status, plan = self.post("/api/instructions/plan", body)
        self.assertEqual(status, 200)
        self.assertEqual(plan["summary"]["create_link"], 1)
        self.assertFalse((self.proj / "CLAUDE.md").exists())

        status, applied = self.post("/api/instructions/apply", body)
        self.assertEqual(status, 200)
        self.assertEqual(applied["summary"]["ok"], 1)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"), "reglas\n")

        status, undone = self.post("/api/undo", {"id": applied["application_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(undone["summary"]["ok"], 1)
        self.assertFalse((self.proj / "CLAUDE.md").exists())

    def test_bad_requests_are_400(self):
        status, _ = self.post("/api/instructions/plan", {"harness_ids": ["nope"], "scope": "global", "conflict_rule": "none"})
        self.assertEqual(status, 400)
        status, _ = self.post("/api/instructions/plan", {"harness_ids": [], "scope": "global",
                                                         "conflict_rule": "none", "custom": []})
        self.assertEqual(status, 400)
        status, _ = self.post("/api/instructions/scan", {"scope": "project"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
