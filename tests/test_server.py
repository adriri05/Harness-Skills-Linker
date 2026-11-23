import http.client
import json
import threading
import unittest

from backend.server import TOKEN_HEADER, create_server


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.server = create_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_free_port_assigned(self):
        self.assertGreater(self.server.port, 0)

    def test_index_requires_token(self):
        status, _ = self.request("GET", "/")
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/?t=wrong")
        self.assertEqual(status, 403)

    def test_index_injects_token(self):
        status, body = self.request("GET", f"/?t={self.server.token}")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertNotIn(b"__SESSION_TOKEN__", body)

    def test_static_asset_needs_no_token(self):
        status, _ = self.request("GET", "/styles.css")
        self.assertEqual(status, 200)

    def test_path_traversal_blocked(self):
        status, _ = self.request("GET", "/../backend/server.py")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/%2e%2e/backend/server.py")
        self.assertEqual(status, 404)

    def test_api_requires_token(self):
        status, _ = self.request("GET", "/api/harnesses")
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/api/harnesses", {TOKEN_HEADER: "wrong"})
        self.assertEqual(status, 401)

    def test_api_with_token(self):
        status, body = self.request("GET", "/api/harnesses", {TOKEN_HEADER: self.server.token})
        self.assertEqual(status, 200)
        ids = {h["id"] for h in json.loads(body)["harnesses"]}
        self.assertIn("claude-code", ids)

    def test_rebinding_host_rejected(self):
        status, _ = self.request(
            "GET", "/api/harnesses", {"Host": "evil.example", TOKEN_HEADER: self.server.token}
        )
        self.assertEqual(status, 403)

    def test_foreign_origin_rejected(self):
        status, _ = self.request(
            "GET",
            "/api/harnesses",
            {"Origin": "http://evil.example", TOKEN_HEADER: self.server.token},
        )
        self.assertEqual(status, 403)

    def test_same_origin_accepted(self):
        origin = f"http://127.0.0.1:{self.server.port}"
        status, _ = self.request(
            "GET", "/api/harnesses", {"Origin": origin, TOKEN_HEADER: self.server.token}
        )
        self.assertEqual(status, 200)

    def test_post_to_static_rejected(self):
        status, _ = self.request("POST", "/")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
