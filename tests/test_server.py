import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path

from ctxshare.capsule import Capsule, Message
from ctxshare.server import ShareServer
from ctxshare.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        capsule = Capsule(
            title="Server test",
            provider="Claude",
            model="claude-sonnet",
            source_url="https://claude.ai/share/abc",
            messages=[Message("user", "Halo"), Message("assistant", "Hai!")],
        )
        capsule_id = self.store.save_capsule(capsule)
        self.share = self.store.create_share(capsule_id, ttl_hours=1)
        self.server = ShareServer(self.store, host="127.0.0.1", port=0)
        self.server.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, headers, body

    def test_health(self):
        status, _headers, body = self.request("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app"], "ctxshare")

    def test_landing(self):
        status, _headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"ctxshare", body)

    def test_capsule_page(self):
        status, headers, body = self.request(f"/c/{self.share['token']}?target=claude")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Server test", text)
        self.assertIn("Kamu adalah Claude", text)
        self.assertIn("prompt-gemini", text)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("noindex", headers["x-robots-tag"])

    def test_json(self):
        status, headers, body = self.request(f"/c/{self.share['token']}.json")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"])
        payload = json.loads(body)
        self.assertEqual(payload["format"], "ctxshare.capsule/1")
        self.assertEqual(payload["title"], "Server test")

    def test_txt_target(self):
        status, _headers, body = self.request(f"/c/{self.share['token']}.txt?target=gemini")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Kamu adalah Gemini", text)
        self.assertIn("=== TRANSKRIP ===", text)

    def test_unknown_token(self):
        status, _headers, body = self.request("/c/" + "a" * 24)
        self.assertEqual(status, 404)
        self.assertIn(b"404", body)

    def test_head(self):
        status, headers, body = self.request(f"/c/{self.share['token']}", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("content-length", headers)

    def test_expired_share(self):
        with self.store._write() as conn:
            conn.execute(
                "UPDATE shares SET expires_at = ? WHERE token = ?",
                (time.time() - 1, self.share["token"]),
            )
        status, _headers, _body = self.request(f"/c/{self.share['token']}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
