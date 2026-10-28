import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from apps_dashboard import previews, server
from apps_dashboard.registry import Registry
from tests.test_discover import FakeSystem
from tests.test_previews import FakeCapture


class ScannerCacheTests(unittest.TestCase):
    def test_stale_scan_returns_during_one_background_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = server.Scanner(Registry(Path(tmp) / "registry.json"), system=FakeSystem())
            first = scanner.get()
            scanner._at -= 20
            release, entered = threading.Event(), threading.Event()

            def scan(*args):
                entered.set()
                release.wait(5)
                return dict(first, scanned="updated")

            with patch.object(server.discover, "scan", side_effect=scan) as mock:
                try:
                    start = time.monotonic()
                    self.assertIs(scanner.get(background=True), first)
                    self.assertLess(time.monotonic() - start, 0.5)
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(scanner.pending)
                    self.assertIs(scanner.get(fresh=True, background=True), first)
                    self.assertEqual(mock.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(scanner.get()["scanned"], "updated")
                self.assertFalse(scanner.pending)

    def test_disk_copy_survives_restart_and_failed_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(Path(tmp) / "registry.json")
            path = Path(tmp) / "scan-cache.json"
            first = server.Scanner(registry, system=FakeSystem(), cache_path=path).get()
            restored = server.Scanner(registry, system=FakeSystem(), cache_path=path)
            self.assertEqual(restored._result, first)
            with patch.object(server.discover, "scan", side_effect=OSError("offline")):
                result = restored.get()
                self.assertEqual(result["apps"], first["apps"])
                self.assertIn("offline", result["errors"][-1])
            path.write_text("broken JSON")
            self.assertIsNone(server.Scanner(registry, cache_path=path)._result)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        registry = Registry(Path(cls.tmp.name) / "registry.json")
        scanner = server.Scanner(registry, self_port=8790, system=FakeSystem())
        cls.app = server.App(scanner, server.Tailnet("me@example.com", ["node.tail.ts.net"]))
        cls.app.tailnet._dns = "node.tail.ts.net"
        cls.srv = server.make_server(cls.app, port=0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_local_page_and_api(self):
        resp, body = self.request("/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"<title>Apps dashboard</title>", body)
        self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))
        resp, body = self.request("/api/apps")
        self.assertEqual(resp.status, 200)
        data = json.loads(body)
        self.assertEqual(data["viewer"], "local")
        self.assertIn("plot-viewer", {a["id"] for a in data["apps"]})

    def test_tailnet_owner_only(self):
        tail = {"Host": "node.tail.ts.net"}
        resp, _ = self.request("/api/apps", headers=dict(tail, **{"Tailscale-User-Login": "me@example.com"}))
        self.assertEqual(resp.status, 200)
        resp, _ = self.request("/", headers=dict(tail, **{"Tailscale-User-Login": "someone@else.com"}))
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("/", headers=tail)
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("/", headers={"Host": "evil.example", "Tailscale-User-Login": "me@example.com"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("/", headers={"Host": "127.0.0.1", "Tailscale-User-Login": "someone@else.com"})
        self.assertEqual(resp.status, 403)

    def test_read_only_and_static(self):
        resp, _ = self.request("/api/apps", method="POST")
        self.assertEqual(resp.status, 405)
        self.assertEqual(self.request("/static/../server.py")[0].status, 404)
        self.assertEqual(self.request("/static/app.js")[0].status, 200)
        self.assertEqual(self.request("/apple-touch-icon.png")[0].getheader("Content-Type"), "image/png")
        self.assertEqual(self.request("/nope")[0].status, 404)

    def test_scan_cache(self):
        scanner = self.app.scanner
        first = scanner.get()
        self.assertIs(scanner.get(), first)
        scanner._at -= 3
        self.assertIsNot(scanner.get(fresh=True), first)

    def test_previews(self):
        resp, body = self.request("/api/apps")
        data = json.loads(body)
        self.assertFalse(data["previews"])
        self.assertTrue(all(a["preview"] is None for a in data["apps"]))
        self.assertEqual(self.request("/preview/plot-viewer")[0].status, 404)

        capture = FakeCapture()
        app = server.App(self.app.scanner, self.app.tailnet, previews.Previews("chrome", capture=capture))
        srv = server.make_server(app, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            conn.request("GET", "/api/apps")
            data = json.loads(conn.getresponse().read())
            shown = {a["id"]: a["preview"] for a in data["apps"]}
            self.assertTrue(data["previews"])
            self.assertTrue(shown["plot-viewer"]["src"].startswith("/preview/plot-viewer"))
            self.assertIsNone(shown["apps-dashboard"])  # never this page
            conn.request("GET", "/preview/plot-viewer")
            resp = conn.getresponse()
            self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "image/jpeg"))
            self.assertTrue(resp.read().startswith(b"jpeg of https://node.tail.ts.net:8766/"))
            self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
            conn.request("GET", "/preview/plot-viewer?v=1")
            resp = conn.getresponse()
            resp.read()
            self.assertIn("immutable", resp.getheader("Cache-Control"))
            for missing in ("/preview/apps-dashboard", "/preview/nope", "/preview/..%2Fapi%2Fapps"):
                conn.request("GET", missing)
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 404, missing)
            conn.close()
            self.assertNotIn("http://127.0.0.1:8790/", [url for url, _ in capture.calls])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_rejects_non_loopback(self):
        with self.assertRaises(ValueError):
            server.make_server(self.app, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
