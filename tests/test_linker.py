import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import linker, symlinks
from backend.symlinks import LinkError, is_link
from tests.test_scanner import make_dir_link, make_skill


def can_real_symlink() -> bool:
    return symlinks.can_create_symlinks()


class LinkerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dirs = {}
        for hid in ("a", "b", "c"):
            self.dirs[hid] = self.tmp / hid
            self.dirs[hid].mkdir()
        self.custom = [{"id": h, "name": h, "path": str(d)} for h, d in self.dirs.items()]

    def plan(self, ids=("a", "b"), **kw):
        return linker.build_plan(list(ids), custom=self.custom, **kw)

    def apply(self, ids=("a", "b"), **kw):
        return linker.apply(list(ids), custom=self.custom, **kw)

    def actions(self, plan):
        return {(a.skill, a.harness_id): a for a in plan.actions}

    def real_of(self, path: Path):
        return Path(os.path.realpath(path))


class PlannerTest(LinkerBase):
    def test_creates_link_to_missing_harness(self):
        make_skill(self.dirs["a"], "alpha")
        plan = self.plan()
        acts = self.actions(plan)
        self.assertEqual(acts[("alpha", "a")].action, "already_linked")
        link = acts[("alpha", "b")]
        self.assertEqual(link.action, "create_link")
        self.assertEqual(link.target, str(self.dirs["b"] / "alpha"))
        self.assertEqual(link.source, str(self.dirs["a"] / "alpha"))
        self.assertEqual(plan.summary()["create_link"], 1)

    def test_dry_run_does_not_touch_disk(self):
        make_skill(self.dirs["a"], "alpha")
        self.plan()
        self.assertEqual(list(self.dirs["b"].iterdir()), [])

    def test_two_harnesses_sharing_one_folder_never_link_to_themselves(self):
        # agents y codex apuntan a la misma carpeta en el catálogo.
        make_skill(self.dirs["a"], "alpha")
        self.custom.append({"id": "a2", "name": "a2", "path": str(self.dirs["a"])})
        acts = self.actions(self.plan(ids=("a", "a2", "b")))
        self.assertEqual(acts[("alpha", "a")].action, "already_linked")
        self.assertEqual(acts[("alpha", "a2")].action, "already_linked")
        self.assertEqual(acts[("alpha", "b")].action, "create_link")
        result = self.apply(ids=("a", "a2", "b"))
        self.assertEqual(result.to_dict()["summary"]["error"], 0)
        self.assertTrue((self.dirs["a"] / "alpha" / "SKILL.md").is_file())

    def test_to_dict_contract(self):
        make_skill(self.dirs["a"], "alpha")
        d = self.plan().to_dict()
        self.assertEqual(set(d), {"actions", "summary", "canonical", "warnings"})
        self.assertEqual(
            set(d["actions"][0]),
            {"skill", "harness_id", "target", "source", "action", "reason", "resolution"},
        )
        self.assertEqual(set(d["summary"]), {"create_link", "already_linked", "conflict", "skipped"})

    def test_already_linked(self):
        real = make_skill(self.dirs["a"], "alpha")
        make_dir_link(self.dirs["b"] / "alpha", real)
        acts = self.actions(self.plan())
        self.assertEqual(acts[("alpha", "b")].action, "already_linked")

    def test_no_chains_source_is_realpath(self):
        real = make_skill(self.dirs["a"], "alpha")
        make_dir_link(self.dirs["b"] / "alpha", real)
        plan = self.plan(ids=("a", "b", "c"))
        link = self.actions(plan)[("alpha", "c")]
        self.assertEqual(Path(link.source), self.real_of(real))

    def test_broken_link_replaced_when_source_exists(self):
        real = make_skill(self.dirs["a"], "alpha")
        gone = make_skill(self.tmp / "gone", "alpha")
        make_dir_link(self.dirs["b"] / "alpha", gone)
        shutil.rmtree(gone)
        act = self.actions(self.plan())[("alpha", "b")]
        self.assertEqual(act.action, "create_link")
        self.assertTrue(act.replace_link)
        self.assertIn("roto", act.reason)
        self.assertEqual(act.source, str(real))

    def test_broken_link_without_source_is_skipped(self):
        gone = make_skill(self.tmp / "gone", "alpha")
        make_dir_link(self.dirs["a"] / "alpha", gone)
        shutil.rmtree(gone)
        acts = self.actions(self.plan())
        self.assertEqual(acts[("alpha", "a")].action, "skipped")
        self.assertEqual(acts[("alpha", "b")].action, "skipped")

    def test_loop_detected(self):
        # Dos enlaces que se apuntan entre sí. Con junctions no se puede, así que se
        # simula con la función de clasificación y un readlink falso.
        a, b = str(self.dirs["a"] / "x"), str(self.dirs["b"] / "x")
        links = {a: b, b: a}
        with mock.patch.object(linker, "is_link", side_effect=lambda p: p in links), \
                mock.patch.object(linker.os, "readlink", side_effect=lambda p: links[p]):
            self.assertEqual(linker._broken_reason(a), "bucle de enlaces")

    @unittest.skipUnless(can_real_symlink(), "requiere symlinks reales")
    def test_real_symlink_loop_in_plan(self):
        make_skill(self.dirs["c"], "alpha")
        os.symlink(self.dirs["b"] / "alpha", self.dirs["a"] / "alpha", target_is_directory=True)
        os.symlink(self.dirs["a"] / "alpha", self.dirs["b"] / "alpha", target_is_directory=True)
        acts = self.actions(self.plan(ids=("a", "b", "c")))
        self.assertEqual(acts[("alpha", "a")].action, "create_link")
        self.assertIn("bucle", acts[("alpha", "a")].reason)

    def test_duplicate_real_is_conflict(self):
        make_skill(self.dirs["a"], "alpha")
        make_skill(self.dirs["b"], "alpha")
        plan = self.plan()
        acts = self.actions(plan)
        self.assertEqual(acts[("alpha", "a")].action, "conflict")
        self.assertEqual(acts[("alpha", "b")].action, "conflict")
        self.assertEqual(plan.summary()["conflict"], 2)

    def test_unresolved_conflict_skips_missing_harness(self):
        make_skill(self.dirs["a"], "alpha")
        make_skill(self.dirs["b"], "alpha")
        acts = self.actions(self.plan(ids=("a", "b", "c")))
        self.assertEqual(acts[("alpha", "c")].action, "skipped")

    def test_canonical_by_id_is_source(self):
        real = make_skill(self.dirs["a"], "alpha")
        plan = self.plan(ids=("a", "b", "c"), canonical="a")
        self.assertEqual(plan.canonical, str(self.dirs["a"]))
        acts = self.actions(plan)
        self.assertEqual(acts[("alpha", "b")].source, str(real))
        self.assertEqual(acts[("alpha", "c")].action, "create_link")

    def test_canonical_by_absolute_path_not_a_target(self):
        canon = self.tmp / "canon"
        real = make_skill(canon, "alpha")
        plan = self.plan(canonical=str(canon))
        acts = self.actions(plan)
        self.assertEqual({h for _, h in acts}, {"a", "b"})
        self.assertEqual(acts[("alpha", "a")].source, str(real))

    def test_canonical_with_real_duplicate_is_conflict(self):
        make_skill(self.dirs["a"], "alpha")
        make_skill(self.dirs["b"], "alpha")
        acts = self.actions(self.plan(canonical="a"))
        self.assertEqual(acts[("alpha", "a")].action, "already_linked")
        self.assertEqual(acts[("alpha", "b")].action, "conflict")

    def test_relative_canonical_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(canonical="relativa/ruta")

    def test_unknown_harness_and_rule(self):
        with self.assertRaises(ValueError):
            linker.build_plan(["no-existe"], custom=self.custom)
        with self.assertRaises(ValueError):
            self.plan(conflict_rule="inventada")

    def test_project_scope_requires_dir(self):
        with self.assertRaises(ValueError):
            self.plan(scope="project")

    def test_project_scope_custom_uses_its_path(self):
        make_skill(self.dirs["a"], "alpha")
        plan = self.plan(scope="project", project_dir=str(self.tmp))
        self.assertEqual(self.actions(plan)[("alpha", "b")].action, "create_link")

    def test_shared_directory_is_already_linked(self):
        # b es un enlace a la carpeta de a: la skill ya existe en ambos.
        shutil.rmtree(self.dirs["b"])
        make_dir_link(self.dirs["b"], self.dirs["a"])
        make_skill(self.dirs["a"], "alpha")
        acts = self.actions(self.plan())
        self.assertEqual(acts[("alpha", "b")].action, "already_linked")


class ApplyTest(LinkerBase):
    def test_apply_creates_link_and_is_idempotent(self):
        real = make_skill(self.dirs["a"], "alpha")
        first = self.apply().to_dict()
        self.assertEqual(first["summary"], {"ok": 1, "error": 0, "skipped": 1})
        link = self.dirs["b"] / "alpha"
        self.assertTrue(is_link(link))
        self.assertEqual(self.real_of(link), self.real_of(real))
        self.assertTrue((link / "SKILL.md").is_file())

        second = self.apply().to_dict()
        self.assertEqual(second["summary"], {"ok": 0, "error": 0, "skipped": 2})
        self.assertTrue(is_link(link))

    def test_apply_creates_missing_harness_directory(self):
        make_skill(self.dirs["a"], "alpha")
        shutil.rmtree(self.dirs["b"])
        self.assertEqual(self.apply().to_dict()["summary"]["ok"], 1)
        self.assertTrue(is_link(self.dirs["b"] / "alpha"))

    def test_apply_never_overwrites_real_folder(self):
        make_skill(self.dirs["a"], "alpha")
        other = make_skill(self.dirs["b"], "alpha")
        (other / "mio.txt").write_text("importante", encoding="utf-8")
        res = self.apply().to_dict()
        self.assertEqual(res["summary"]["ok"], 0)
        self.assertEqual((other / "mio.txt").read_text(encoding="utf-8"), "importante")
        self.assertFalse(is_link(other))

    def test_apply_replaces_broken_link(self):
        real = make_skill(self.dirs["a"], "alpha")
        gone = make_skill(self.tmp / "gone", "alpha")
        make_dir_link(self.dirs["b"] / "alpha", gone)
        shutil.rmtree(gone)
        res = self.apply().to_dict()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual(self.real_of(self.dirs["b"] / "alpha"), self.real_of(real))

    def test_apply_replaces_link_to_other_place_without_touching_it(self):
        real = make_skill(self.dirs["a"], "alpha")
        elsewhere = make_skill(self.tmp / "else", "alpha")
        make_dir_link(self.dirs["b"] / "alpha", elsewhere)
        plan = self.plan(canonical="a")
        self.assertTrue(self.actions(plan)[("alpha", "b")].replace_link)
        self.apply(canonical="a")
        self.assertEqual(self.real_of(self.dirs["b"] / "alpha"), self.real_of(real))
        self.assertTrue((elsewhere / "SKILL.md").is_file())

    def test_apply_error_when_source_vanishes(self):
        real = make_skill(self.dirs["a"], "alpha")
        plan = self.plan()
        shutil.rmtree(real)
        status, msg = linker._execute(self.actions(plan)[("alpha", "b")])
        self.assertEqual(status, "error")
        self.assertIn("no existe", msg)

    def test_apply_result_contract(self):
        make_skill(self.dirs["a"], "alpha")
        r = self.apply().to_dict()["results"][1]
        self.assertEqual(r["status"], "ok")
        self.assertIn("message", r)
        self.assertIn("harness_id", r)


class ConflictRulesTest(LinkerBase):
    def setUp(self):
        super().setUp()
        self.a = make_skill(self.dirs["a"], "alpha")
        (self.a / "v.txt").write_text("a", encoding="utf-8")
        self.b = make_skill(self.dirs["b"], "alpha")
        (self.b / "v.txt").write_text("b", encoding="utf-8")

    def test_rule_none_changes_nothing(self):
        res = self.apply(ids=("a", "b", "c")).to_dict()
        self.assertEqual(res["summary"]["ok"], 0)
        self.assertFalse((self.dirs["c"] / "alpha").exists())

    def test_prefer_canonical(self):
        plan = self.plan(ids=("a", "b", "c"), canonical="a", conflict_rule="prefer_canonical")
        acts = self.actions(plan)
        self.assertEqual(acts[("alpha", "b")].action, "skipped")
        self.assertEqual(acts[("alpha", "b")].resolution, "prefer_canonical")
        self.assertEqual(acts[("alpha", "c")].source, str(self.a))
        self.apply(ids=("a", "b", "c"), canonical="a", conflict_rule="prefer_canonical")
        self.assertTrue(is_link(self.dirs["c"] / "alpha"))
        self.assertEqual((self.b / "v.txt").read_text(encoding="utf-8"), "b")
        self.assertFalse(is_link(self.b))

    def test_prefer_canonical_without_canonical_warns(self):
        plan = self.plan(conflict_rule="prefer_canonical")
        self.assertTrue(plan.warnings)
        self.assertEqual(plan.summary()["conflict"], 2)

    def test_prefer_harness(self):
        acts = self.actions(
            self.plan(ids=("a", "b", "c"), conflict_rule="prefer_harness:b")
        )
        self.assertEqual(acts[("alpha", "c")].source, str(self.b))
        self.assertEqual(acts[("alpha", "a")].action, "skipped")
        self.assertEqual(acts[("alpha", "c")].resolution, "prefer_harness:b")
        self.apply(ids=("a", "b", "c"), conflict_rule="prefer_harness:b")
        self.assertEqual(self.real_of(self.dirs["c"] / "alpha"), self.real_of(self.b))
        self.assertEqual((self.a / "v.txt").read_text(encoding="utf-8"), "a")

    def test_move_to_canonical_with_canonical_copy_backs_up_duplicate(self):
        self.apply(canonical="a", conflict_rule="move_to_canonical")
        link = self.dirs["b"] / "alpha"
        self.assertTrue(is_link(link))
        self.assertEqual((link / "v.txt").read_text(encoding="utf-8"), "a")
        backups = list((self.dirs["b"] / linker.BACKUP_DIR).rglob("v.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "b")

    def test_move_to_canonical_moves_first_real_into_external_canonical(self):
        canon = self.tmp / "canon"
        plan = self.plan(ids=("a", "b", "c"), canonical=str(canon), conflict_rule="move_to_canonical")
        acts = self.actions(plan)
        self.assertEqual(acts[("alpha", "a")].move_to, str(canon / "alpha"))
        self.assertEqual(plan.actions[0].harness_id, "a")  # el movimiento va primero
        res = self.apply(ids=("a", "b", "c"), canonical=str(canon), conflict_rule="move_to_canonical")
        self.assertEqual(res.to_dict()["summary"]["error"], 0)
        self.assertEqual((canon / "alpha" / "v.txt").read_text(encoding="utf-8"), "a")
        for hid in "abc":
            link = self.dirs[hid] / "alpha"
            self.assertTrue(is_link(link), hid)
            self.assertEqual((link / "v.txt").read_text(encoding="utf-8"), "a")
        for hid, content in (("a", "a"), ("b", "b")):
            found = list((self.dirs[hid] / linker.BACKUP_DIR).rglob("v.txt"))
            self.assertEqual([p.read_text(encoding="utf-8") for p in found], [content])

    def test_move_to_canonical_is_idempotent(self):
        canon = self.tmp / "canon"
        kw = dict(ids=("a", "b"), canonical=str(canon), conflict_rule="move_to_canonical")
        self.apply(**kw)
        again = self.apply(**kw).to_dict()
        self.assertEqual(again["summary"]["ok"], 0)
        self.assertEqual(again["summary"]["error"], 0)

    def test_move_all_moves_skill_that_exists_in_one_harness(self):
        # Sin conflicto: "alpha" solo existe en "a". move_to_canonical la deja donde está.
        solo = self.tmp / "solo"
        solo.mkdir()
        canon = self.tmp / "canon"
        make_skill(solo, "beta")
        self.custom.append({"id": "s", "name": "s", "path": str(solo)})
        kw = dict(ids=("s", "b"), canonical=str(canon))
        self.apply(conflict_rule="move_to_canonical", **kw)
        self.assertFalse((canon / "beta").exists())
        self.assertFalse(is_link(solo / "beta"))

        res = self.apply(conflict_rule="move_all_to_canonical", **kw)
        self.assertEqual(res.to_dict()["summary"]["error"], 0)
        self.assertTrue((canon / "beta" / "SKILL.md").is_file())
        for base in (solo, self.dirs["b"]):
            self.assertTrue(is_link(base / "beta"), base)
        self.assertEqual(len(list((solo / linker.BACKUP_DIR).rglob("SKILL.md"))), 1)

        again = self.apply(conflict_rule="move_all_to_canonical", **kw).to_dict()
        self.assertEqual(again["summary"]["ok"], 0)
        self.assertEqual(again["summary"]["error"], 0)

    def test_move_never_happens_without_explicit_rule(self):
        canon = self.tmp / "canon"
        for rule in ("none", "prefer_canonical"):
            self.apply(canonical=str(canon), conflict_rule=rule)
        self.assertFalse((canon / "alpha").exists())
        self.assertFalse(is_link(self.a))
        self.assertFalse((self.dirs["a"] / linker.BACKUP_DIR).exists())

    def test_preview_reflects_rule(self):
        plan = self.plan(canonical="a", conflict_rule="move_to_canonical")
        b = self.actions(plan)[("alpha", "b")]
        self.assertEqual(b.resolution, "move_to_canonical")
        self.assertTrue(b.backup)
        self.assertEqual(plan.to_dict()["actions"][1]["resolution"], "move_to_canonical")


class SymlinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "t"
        self.target.mkdir()

    def test_make_dir_link_works_with_symlink_or_junction(self):
        kind = symlinks.make_dir_link(self.tmp / "l", self.target)
        self.assertIn(kind, ("symlink", "junction"))
        self.assertTrue(is_link(self.tmp / "l"))
        self.assertTrue((self.tmp / "l").is_dir())

    def test_remove_link_keeps_target(self):
        (self.target / "f.txt").write_text("x", encoding="utf-8")
        symlinks.make_dir_link(self.tmp / "l", self.target)
        symlinks.remove_link(self.tmp / "l")
        self.assertFalse(os.path.lexists(self.tmp / "l"))
        self.assertTrue((self.target / "f.txt").is_file())

    def test_remove_link_rejects_real_dir(self):
        with self.assertRaises(LinkError):
            symlinks.remove_link(self.target)
        self.assertTrue(self.target.is_dir())

    def test_windows_fallback_to_junction_when_symlink_denied(self):
        denied = OSError(1314, "privilegio requerido")
        denied.winerror = 1314
        with mock.patch.object(symlinks, "_IS_WINDOWS", True), \
                mock.patch.object(symlinks.os, "symlink", side_effect=denied), \
                mock.patch.object(symlinks, "_create_junction") as junction:
            self.assertEqual(symlinks.make_dir_link(self.tmp / "l", self.target), "junction")
        junction.assert_called_once()

    def test_windows_clear_error_when_nothing_works(self):
        denied = OSError(1314, "privilegio requerido")
        denied.winerror = 1314
        with mock.patch.object(symlinks, "_IS_WINDOWS", True), \
                mock.patch.object(symlinks.os, "symlink", side_effect=denied), \
                mock.patch.object(symlinks, "_create_junction", side_effect=OSError("nope")):
            with self.assertRaises(LinkError) as ctx:
                symlinks.make_dir_link(self.tmp / "l", self.target)
        self.assertIn("Modo Desarrollador", str(ctx.exception))

    def test_non_windows_error_is_link_error(self):
        with mock.patch.object(symlinks, "_IS_WINDOWS", False), \
                mock.patch.object(symlinks.os, "symlink", side_effect=PermissionError("no")):
            with self.assertRaises(LinkError):
                symlinks.make_dir_link(self.tmp / "l", self.target)

    def test_link_mode_is_known(self):
        self.assertIn(symlinks.link_mode(), ("symlink", "junction", "none"))

    @unittest.skipUnless(can_real_symlink(), "requiere symlinks reales")
    def test_real_symlink_used_when_allowed(self):
        self.assertEqual(symlinks.make_dir_link(self.tmp / "l", self.target), "symlink")


if __name__ == "__main__":
    unittest.main()
