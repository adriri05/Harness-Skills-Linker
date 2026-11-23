import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import linker
from backend.server import TOKEN_HEADER, create_server


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.server = create_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def _stop(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, path, body=None, headers=None, raw=None, ctype="application/json", token=True):
        data = raw if raw is not None else json.dumps(body).encode()
        h = {}
        if ctype:
            h["Content-Type"] = ctype
        if token:
            h[TOKEN_HEADER] = self.server.token
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request("POST", path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None
        finally:
            conn.close()

    # --- scan ---

    def test_scan_global(self):
        status, body = self.post("/api/scan", {"scope": "global"})
        self.assertEqual(status, 200)
        self.assertIn("claude-code", {h["id"] for h in body["harnesses"]})

    def test_scan_custom_folder(self):
        skill = self.tmp / "mis-skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x", encoding="utf-8")
        custom = [{"id": "mio", "name": "Mío", "path": str(self.tmp / "mis-skills")}]
        status, body = self.post("/api/scan", {"scope": "global", "custom": custom})
        self.assertEqual(status, 200)
        mine = next(h for h in body["harnesses"] if h["id"] == "mio")
        self.assertTrue(mine["dir_exists"])
        self.assertEqual([s["name"] for s in mine["skills"]], ["demo"])

    def test_scan_project_scope(self):
        status, body = self.post("/api/scan", {"scope": "project", "project_dir": str(self.tmp)})
        self.assertEqual(status, 200)
        self.assertTrue(body["harnesses"])

    def test_scan_validation(self):
        cases = [
            {"scope": "nope"},
            {},
            {"scope": "project"},
            {"scope": "project", "project_dir": "relativa"},
            {"scope": "project", "project_dir": str(self.tmp / "no-existe")},
            {"scope": "global", "extra": 1},
            {"scope": "global", "custom": "x"},
            {"scope": "global", "custom": [{"id": "a", "name": "A", "path": "rel/ruta"}]},
            {"scope": "global", "custom": [{"id": "a", "name": "A"}]},
            {"scope": "global", "custom": [{"id": "a", "name": "A", "path": str(self.tmp), "x": 1}]},
        ]
        for case in cases:
            with self.subTest(case=case):
                status, body = self.post("/api/scan", case)
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    # --- cuerpo y cabeceras ---

    def test_requires_json_content_type(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", None):
            with self.subTest(ctype=ctype):
                status, body = self.post("/api/scan", {"scope": "global"}, ctype=ctype)
                self.assertEqual(status, 415)
                self.assertIn("error", body)

    def test_invalid_json_and_non_object(self):
        self.assertEqual(self.post("/api/scan", raw=b"{no json")[0], 400)
        self.assertEqual(self.post("/api/scan", raw=b"[1]")[0], 400)

    def test_body_too_large(self):
        raw = b'{"scope":"global","custom":"' + b"a" * (1024 * 1024) + b'"}'
        status, _ = self.post("/api/scan", raw=raw)
        self.assertEqual(status, 400)

    def test_unknown_route(self):
        self.assertEqual(self.post("/api/nada", {})[0], 404)

    def test_post_requires_token(self):
        for path in ("/api/scan", "/api/plan", "/api/apply", "/api/shutdown"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {}, token=False)[0], 401)
                self.assertEqual(self.post(path, {}, headers={TOKEN_HEADER: "mal"})[0], 401)
        self.assertTrue(self.thread.is_alive())

    def test_post_rejects_foreign_origin_and_host(self):
        for path in ("/api/scan", "/api/shutdown"):
            with self.subTest(path=path):
                body = {"scope": "global"}
                status, _ = self.post(path, body, headers={"Origin": "http://evil.example"})
                self.assertEqual(status, 403)
                status, _ = self.post(path, body, headers={"Host": "evil.example"})
                self.assertEqual(status, 403)
        self.assertTrue(self.thread.is_alive())

    # --- plan / apply ---

    def valid_plan_body(self, **over):
        body = {
            "harness_ids": ["claude-code"],
            "scope": "global",
            "project_dir": None,
            "canonical": None,
            "conflict_rule": "skip",
            "custom": None,
        }
        body.update(over)
        return body

    def test_plan_and_apply_call_linker(self):
        for name, path in (("build_plan", "/api/plan"), ("apply", "/api/apply")):
            with self.subTest(path=path):
                result = mock.Mock()
                result.to_dict.return_value = {"ok": name}
                with mock.patch.object(linker, name, create=True, return_value=result) as fn:
                    status, body = self.post(path, self.valid_plan_body())
                self.assertEqual((status, body), (200, {"ok": name}))
                fn.assert_called_once_with(**self.valid_plan_body())

    def test_plan_without_linker_implementation_is_501(self):
        saved = {n: getattr(linker, n) for n in ("build_plan", "apply") if hasattr(linker, n)}
        for n in saved:
            delattr(linker, n)
        try:
            status, body = self.post("/api/plan", self.valid_plan_body())
        finally:
            for n, f in saved.items():
                setattr(linker, n, f)
        self.assertEqual(status, 501)
        self.assertIn("error", body)

    def test_plan_validation(self):
        cases = [
            self.valid_plan_body(harness_ids="claude-code"),
            self.valid_plan_body(harness_ids=[1]),
            self.valid_plan_body(scope="otro"),
            self.valid_plan_body(scope="project", project_dir="rel"),
            self.valid_plan_body(conflict_rule=""),
            self.valid_plan_body(canonical=3),
            self.valid_plan_body(custom=[{"id": "a", "name": "A", "path": "rel"}]),
            {**self.valid_plan_body(), "desconocida": 1},
            {"scope": "global"},
        ]
        with mock.patch.object(linker, "build_plan", create=True) as fn:
            for case in cases:
                with self.subTest(case=case):
                    status, body = self.post("/api/plan", case)
                    self.assertEqual(status, 400)
                    self.assertIn("error", body)
            fn.assert_not_called()

    def test_linker_value_error_is_400_and_crash_is_500(self):
        err = ValueError("harness desconocido")
        with mock.patch.object(linker, "build_plan", create=True, side_effect=err):
            status, body = self.post("/api/plan", self.valid_plan_body())
        self.assertEqual(status, 400)
        self.assertIn("harness desconocido", body["error"])
        with mock.patch.object(linker, "build_plan", create=True, side_effect=RuntimeError("boom")):
            status, body = self.post("/api/plan", self.valid_plan_body())
        self.assertEqual(status, 500)
        self.assertNotIn("boom", body["error"])

    # --- shutdown ---

    def test_shutdown_stops_server_thread(self):
        status, body = self.post("/api/shutdown", {})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        for t in threading.enumerate():
            if t.name == "shutdown":
                t.join(timeout=5)
                self.assertFalse(t.is_alive(), "queda un hilo de cierre colgado")
        self.assertEqual(self.server.socket.fileno(), -1)

    def test_shutdown_without_body_still_stops(self):
        status, _ = self.post("/api/shutdown", raw=b"", ctype=None)
        self.assertEqual(status, 200)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
