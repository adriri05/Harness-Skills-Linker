"""Flujo completo contra el servidor real: scan, plan, apply, shutdown.

Usa solo carpetas personalizadas en un directorio temporal, nunca el home real.
"""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from backend.server import TOKEN_HEADER, create_server


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        (self.src / "alpha").mkdir(parents=True)
        (self.src / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")
        self.dst.mkdir()
        self.custom = [
            {"id": "src", "name": "Origen", "path": str(self.src)},
            {"id": "dst", "name": "Destino", "path": str(self.dst)},
        ]
        self.server = create_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            conn.request(
                "POST",
                path,
                body=json.dumps(body),
                headers={"Content-Type": "application/json", TOKEN_HEADER: self.server.token},
            )
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def request_body(self, **extra):
        return {
            "harness_ids": ["src", "dst"],
            "scope": "global",
            "project_dir": None,
            "canonical": "src",
            "conflict_rule": "none",
            "custom": self.custom,
            **extra,
        }

    def test_scan_sees_custom_folders(self):
        status, data = self.post(
            "/api/scan", {"scope": "global", "project_dir": None, "custom": self.custom}
        )
        self.assertEqual(status, 200)
        by_id = {h["id"]: h for h in data["harnesses"]}
        self.assertEqual([s["name"] for s in by_id["src"]["skills"]], ["alpha"])
        self.assertTrue(by_id["dst"]["dir_exists"])

    def test_plan_does_not_touch_disk(self):
        status, plan = self.post("/api/plan", self.request_body())
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["summary"]["create_link"], 1)
        self.assertEqual(os.listdir(self.dst), [])

    def test_apply_creates_link_and_is_idempotent(self):
        status, result = self.post("/api/apply", self.request_body())
        self.assertEqual(status, 200, result)
        self.assertEqual(result["summary"]["ok"], 1)
        link = self.dst / "alpha"
        self.assertTrue((link / "SKILL.md").is_file())
        self.assertEqual(Path(os.path.realpath(link)), self.src / "alpha")

        status, plan = self.post("/api/plan", self.request_body())
        self.assertEqual(plan["summary"]["create_link"], 0)
        dst_actions = [a for a in plan["actions"] if a["harness_id"] == "dst"]
        self.assertEqual([a["action"] for a in dst_actions], ["already_linked"])

        status, again = self.post("/api/apply", self.request_body())
        self.assertEqual(again["summary"]["error"], 0)
        self.assertTrue((self.src / "alpha" / "SKILL.md").is_file())

    def test_real_folder_in_destination_is_never_overwritten(self):
        (self.dst / "alpha").mkdir()
        (self.dst / "alpha" / "SKILL.md").write_text("mio", encoding="utf-8")
        status, result = self.post("/api/apply", self.request_body())
        self.assertEqual(status, 200, result)
        self.assertEqual((self.dst / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "mio")
        self.assertFalse(os.path.islink(self.dst / "alpha"))

    def test_shutdown_stops_server(self):
        status, data = self.post("/api/shutdown", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
