"""Deshacer una aplicación (issue #32): registro, plan de reversión y ida y vuelta."""

import http.client
import json
import os
import shutil
import threading
import unittest
from pathlib import Path

from backend import history, linker
from backend.server import TOKEN_HEADER, create_server
from backend.symlinks import is_link
from tests.test_linker import LinkerBase
from tests.test_scanner import make_dir_link, make_skill


def snapshot(root: Path) -> dict:
    """Foto del disco bajo `root`: carpetas, ficheros con contenido y enlaces con destino."""
    out: dict[str, str] = {}

    def walk(folder: Path) -> None:
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            p = Path(entry.path)
            rel = str(p.relative_to(root))
            if is_link(p):
                out[rel] = "link:" + os.path.normcase(os.path.realpath(p))
            elif p.is_dir():
                out[rel] = "dir"
                walk(p)
            else:
                out[rel] = "file:" + p.read_text(encoding="utf-8")

    walk(root)
    return out


class UndoBase(LinkerBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: history.history_path().unlink(missing_ok=True))
        history.history_path().unlink(missing_ok=True)

    def undo(self):
        return linker.undo().to_dict()

    def by_harness(self, result):
        return {r["harness_id"]: r for r in result["results"]}


class RoundTripTest(UndoBase):
    def test_created_link_is_removed(self):
        make_skill(self.dirs["a"], "alpha")
        before = snapshot(self.tmp)
        self.apply()
        self.assertTrue(is_link(self.dirs["b"] / "alpha"))
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual(snapshot(self.tmp), before)

    def test_folders_created_by_apply_are_removed(self):
        make_skill(self.dirs["a"], "alpha")
        shutil.rmtree(self.dirs["b"])
        before = snapshot(self.tmp)
        self.apply()
        self.assertTrue((self.dirs["b"] / "alpha").exists())
        self.undo()
        self.assertEqual(snapshot(self.tmp), before)

    def test_replaced_link_points_to_old_destination_again(self):
        real = make_skill(self.dirs["a"], "alpha")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        make_dir_link(self.dirs["b"] / "alpha", elsewhere)
        before = snapshot(self.tmp)
        self.apply(canonical="a")
        self.assertEqual(self.real_of(self.dirs["b"] / "alpha"), self.real_of(real))
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual(snapshot(self.tmp), before)

    def test_duplicate_real_folder_comes_back_from_backup(self):
        a = make_skill(self.dirs["a"], "alpha")
        (a / "v.txt").write_text("a", encoding="utf-8")
        b = make_skill(self.dirs["b"], "alpha")
        (b / "v.txt").write_text("b", encoding="utf-8")
        before = snapshot(self.tmp)
        self.apply(canonical="a", conflict_rule="move_to_canonical")
        self.assertTrue(is_link(self.dirs["b"] / "alpha"))
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertFalse((self.dirs["b"] / linker.BACKUP_DIR).exists())

    def test_move_to_canonical_returns_folders_to_their_harness(self):
        a = make_skill(self.dirs["a"], "alpha")
        (a / "v.txt").write_text("a", encoding="utf-8")
        b = make_skill(self.dirs["b"], "alpha")
        (b / "v.txt").write_text("b", encoding="utf-8")
        canon = self.tmp / "canon"
        before = snapshot(self.tmp)
        kw = dict(ids=("a", "b", "c"), canonical=str(canon), conflict_rule="move_to_canonical")
        self.apply(**kw)
        self.assertTrue((canon / "alpha" / "v.txt").is_file())
        res = self.undo()
        self.assertEqual(res["summary"]["error"], 0)
        self.assertEqual(res["summary"]["conflict"], 0)
        self.assertEqual(snapshot(self.tmp), before)

    def test_move_all_to_canonical_round_trip(self):
        solo = self.tmp / "solo"
        solo.mkdir()
        make_skill(solo, "beta")
        self.custom.append({"id": "s", "name": "s", "path": str(solo)})
        canon = self.tmp / "canon"
        before = snapshot(self.tmp)
        self.apply(ids=("s", "b"), canonical=str(canon), conflict_rule="move_all_to_canonical")
        self.assertTrue((canon / "beta" / "SKILL.md").is_file())
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 2)
        self.assertEqual(snapshot(self.tmp), before)

    def test_edits_made_after_apply_travel_back_with_the_folder(self):
        a = make_skill(self.dirs["a"], "alpha")
        canon = self.tmp / "canon"
        self.apply(ids=("a", "b"), canonical=str(canon), conflict_rule="move_all_to_canonical")
        (canon / "alpha" / "nuevo.txt").write_text("mío", encoding="utf-8")
        self.undo()
        self.assertEqual((a / "nuevo.txt").read_text(encoding="utf-8"), "mío")


class UndoRulesTest(UndoBase):
    def setUp(self):
        super().setUp()
        self.a = make_skill(self.dirs["a"], "alpha")
        make_skill(self.dirs["a"], "beta")

    def test_missing_link_is_skipped_and_the_rest_continues(self):
        self.apply()
        link = self.dirs["b"] / "alpha"
        linker.remove_link(link)
        res = self.undo()
        self.assertEqual(res["summary"]["skipped"], 1)
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertFalse(os.path.lexists(self.dirs["b"] / "beta"))
        skipped = [r for r in res["results"] if r["status"] == "skipped"]
        self.assertEqual(skipped[0]["skill"], "alpha")
        self.assertIn("ya no existe", skipped[0]["message"])

    def test_link_pointing_elsewhere_is_not_touched(self):
        self.apply()
        link = self.dirs["b"] / "alpha"
        other = self.tmp / "other"
        other.mkdir()
        linker.remove_link(link)
        make_dir_link(link, other)
        res = self.undo()
        skipped = [r for r in res["results"] if r["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["skill"], "alpha")
        self.assertTrue(is_link(link))
        self.assertEqual(self.real_of(link), self.real_of(other))
        self.assertTrue(other.is_dir())

    def test_real_folder_in_place_of_link_is_never_deleted(self):
        self.apply()
        link = self.dirs["b"] / "alpha"
        linker.remove_link(link)
        link.mkdir()
        (link / "mio.txt").write_text("importante", encoding="utf-8")
        res = self.undo()
        self.assertEqual((link / "mio.txt").read_text(encoding="utf-8"), "importante")
        self.assertEqual([r["skill"] for r in res["results"] if r["status"] == "skipped"], ["alpha"])


class BackupRulesTest(UndoBase):
    def setUp(self):
        super().setUp()
        a = make_skill(self.dirs["a"], "alpha")
        (a / "v.txt").write_text("a", encoding="utf-8")
        b = make_skill(self.dirs["b"], "alpha")
        (b / "v.txt").write_text("b", encoding="utf-8")
        self.apply(canonical="a", conflict_rule="move_to_canonical")
        self.link = self.dirs["b"] / "alpha"
        self.assertTrue(is_link(self.link))

    def test_restore_destination_with_content_is_a_conflict(self):
        linker.remove_link(self.link)
        self.link.mkdir()
        (self.link / "otro.txt").write_text("nuevo", encoding="utf-8")
        plan = linker.undo_plan().to_dict()
        self.assertEqual(plan["summary"]["conflict"], 1)
        res = self.undo()
        self.assertEqual(res["summary"]["conflict"], 1)
        self.assertEqual((self.link / "otro.txt").read_text(encoding="utf-8"), "nuevo")
        self.assertEqual(len(list((self.dirs["b"] / linker.BACKUP_DIR).rglob("v.txt"))), 1)

    def test_conflict_stays_registered_and_can_be_retried(self):
        linker.remove_link(self.link)
        self.link.mkdir()
        (self.link / "otro.txt").write_text("nuevo", encoding="utf-8")
        self.undo()
        self.assertIsNotNone(history.last())
        shutil.rmtree(self.link)
        make_dir_link(self.link, self.dirs["a"] / "alpha")
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual((self.link / "v.txt").read_text(encoding="utf-8"), "b")
        self.assertIsNone(history.last())

    def test_empty_folder_at_destination_is_replaced_by_the_backup(self):
        linker.remove_link(self.link)
        self.link.mkdir()
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 1)
        self.assertEqual((self.link / "v.txt").read_text(encoding="utf-8"), "b")

    def test_backup_deleted_by_hand_warns_and_keeps_the_link(self):
        shutil.rmtree(self.dirs["b"] / linker.BACKUP_DIR)
        plan = linker.undo_plan().to_dict()
        self.assertEqual(plan["summary"]["skipped"], 1)
        res = self.undo()
        self.assertEqual(res["summary"]["skipped"], 1)
        self.assertIn("backup", res["results"][0]["message"])
        self.assertTrue(is_link(self.link))


class MoveRulesTest(UndoBase):
    def setUp(self):
        super().setUp()
        self.a = make_skill(self.dirs["a"], "alpha")
        self.canon = self.tmp / "canon"
        self.apply(ids=("a", "b"), canonical=str(self.canon), conflict_rule="move_all_to_canonical")

    def test_moved_folder_missing_is_skipped(self):
        shutil.rmtree(self.canon / "alpha")
        res = self.undo()
        self.assertEqual(res["summary"]["skipped"], 1)
        self.assertEqual(res["summary"]["ok"], 1)  # el enlace de b sí se quita
        self.assertTrue(os.path.lexists(self.dirs["a"] / "alpha"))

    def test_backup_copy_deleted_by_hand_does_not_stop_the_move_back(self):
        shutil.rmtree(self.dirs["a"] / linker.BACKUP_DIR)
        res = self.undo()
        self.assertEqual(res["summary"]["ok"], 2)
        self.assertTrue((self.dirs["a"] / "alpha" / "SKILL.md").is_file())
        self.assertFalse(is_link(self.dirs["a"] / "alpha"))


class HistoryTest(UndoBase):
    def test_no_application_when_nothing_changes(self):
        make_skill(self.dirs["a"], "alpha")
        self.apply()
        first = history.last()["id"]
        res = self.apply().to_dict()
        self.assertIsNone(res["application_id"])
        self.assertEqual(history.last()["id"], first)

    def test_apply_result_carries_the_application_id(self):
        make_skill(self.dirs["a"], "alpha")
        res = self.apply().to_dict()
        self.assertEqual(res["application_id"], history.last()["id"])

    def test_undo_plan_does_not_touch_the_disk(self):
        make_skill(self.dirs["a"], "alpha")
        self.apply()
        before = snapshot(self.tmp)
        plan = linker.undo_plan().to_dict()
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(plan["summary"], {"remove_link": 1, "restore": 0, "conflict": 0, "skipped": 0})
        self.assertEqual(set(plan["actions"][0]), {"skill", "harness_id", "target", "action", "reason"})
        self.assertEqual(plan["application"]["entries"], 1)

    def test_only_the_last_application_can_be_undone(self):
        make_skill(self.dirs["a"], "alpha")
        self.apply()
        first = history.last()["id"]
        make_skill(self.dirs["a"], "beta")
        self.apply()
        with self.assertRaises(linker.UndoError) as ctx:
            linker.undo(first)
        self.assertEqual(ctx.exception.code, "not_last")
        self.undo()
        self.assertEqual(history.last()["id"], first)
        self.undo()
        with self.assertRaises(linker.UndoError) as ctx:
            linker.undo()
        self.assertEqual(ctx.exception.code, "none")

    def test_only_the_latest_applications_are_kept(self):
        for i in range(history.MAX_APPLICATIONS + 3):
            history.record({"id": f"app-{i}", "timestamp": "t", "entries": []})
        apps = history._read()
        self.assertEqual(len(apps), history.MAX_APPLICATIONS)
        self.assertEqual(apps[-1]["id"], f"app-{history.MAX_APPLICATIONS + 2}")

    def test_unreadable_history_is_set_aside_not_overwritten(self):
        path = history.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{no es json", encoding="utf-8")
        self.addCleanup(lambda: [p.unlink() for p in path.parent.glob("history.corrupt-*.json")])
        self.assertIsNone(history.last())
        self.assertEqual(len(list(path.parent.glob("history.corrupt-*.json"))), 1)

    def test_failed_link_after_a_move_is_still_recorded(self):
        make_skill(self.dirs["a"], "alpha")
        canon = self.tmp / "canon"
        original = linker.make_dir_link
        linker.make_dir_link = lambda *a, **k: (_ for _ in ()).throw(linker.LinkError("sin permiso"))
        self.addCleanup(setattr, linker, "make_dir_link", original)
        before = snapshot(self.tmp)
        res = self.apply(ids=("a", "b"), canonical=str(canon), conflict_rule="move_all_to_canonical").to_dict()
        self.assertGreaterEqual(res["summary"]["error"], 1)
        self.assertIsNotNone(res["application_id"])
        linker.make_dir_link = original
        self.undo()
        self.assertEqual(snapshot(self.tmp), before)


class UndoApiTest(UndoBase):
    def setUp(self):
        super().setUp()
        self.server = create_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            headers = {TOKEN_HEADER: self.server.token}
            data = None
            if method == "POST":
                headers["Content-Type"] = "application/json"
                data = json.dumps(body if body is not None else {})
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def apply_body(self):
        return {"harness_ids": ["a", "b"], "scope": "global", "project_dir": None,
                "canonical": "a", "conflict_rule": "none", "custom": self.custom}

    def test_history_is_empty_at_first(self):
        status, data = self.call("GET", "/api/history")
        self.assertEqual((status, data), (200, {"last": None}))

    def test_nothing_to_undo_is_404(self):
        self.assertEqual(self.call("POST", "/api/undo-plan")[0], 404)
        self.assertEqual(self.call("POST", "/api/undo")[0], 404)

    def test_full_cycle(self):
        make_skill(self.dirs["a"], "alpha")
        before = snapshot(self.tmp)
        status, applied = self.call("POST", "/api/apply", self.apply_body())
        self.assertEqual(status, 200, applied)
        app_id = applied["application_id"]

        status, hist = self.call("GET", "/api/history")
        self.assertEqual(hist["last"]["id"], app_id)
        self.assertEqual(hist["last"]["entries"], 1)

        status, plan = self.call("POST", "/api/undo-plan", {"id": app_id})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["summary"]["remove_link"], 1)
        self.assertTrue(is_link(self.dirs["b"] / "alpha"))

        status, done = self.call("POST", "/api/undo", {"id": app_id})
        self.assertEqual(status, 200, done)
        self.assertEqual(done["summary"]["ok"], 1)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.call("GET", "/api/history")[1], {"last": None})

    def test_stale_id_is_409_and_changes_nothing(self):
        make_skill(self.dirs["a"], "alpha")
        self.call("POST", "/api/apply", self.apply_body())
        status, data = self.call("POST", "/api/undo", {"id": "otra"})
        self.assertEqual(status, 409)
        self.assertIn("última", data["error"])
        self.assertTrue(is_link(self.dirs["b"] / "alpha"))

    def test_rejects_unknown_keys_and_bad_id(self):
        self.assertEqual(self.call("POST", "/api/undo", {"x": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/undo", {"id": 3})[0], 400)


if __name__ == "__main__":
    unittest.main()
