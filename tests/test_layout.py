import http.client
import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from apps_dashboard import layout, previews, registry as reg, server
from apps_dashboard.registry import Registry
from tests.test_discover import FakeSystem


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "layout.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean(self):
        self.assertEqual(layout.clean({"order": ["a", "a", "b", 3], "hidden": "x", "updated": "nope", "by": 1}),
                         {"order": ["a", "b"], "hidden": [], "updated": None, "by": None})
        for bad in (None, [], {"order": "a"}, {"order": [""]}, {"hidden": ["x" * 300]}, {"order": [1]}):
            with self.assertRaises(ValueError):
                layout.clean(bad, strict=True)
        self.assertEqual(layout.clean({}, strict=True)["order"], [])

    def test_store_put_get(self):
        store = layout.Store(self.path)
        self.assertEqual(store.get(), layout.empty())
        saved = store.put({"order": ["electrogui", "notes"], "hidden": ["port-5173"], "updated": "x"}, by="node")
        self.assertEqual(saved["by"], "node")
        self.assertRegex(saved["updated"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(layout.Store(self.path).get(), saved)  # on disk
        self.path.write_text("{broken")
        self.assertEqual(store.get(), saved)  # the last good copy


class LayoutServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()

    def start(self, system, dns):
        folder = Path(self.tmp.name) / dns
        reg.save(reg.empty(), folder / "registry.json")
        tailnet = server.Tailnet("me@example.com", [dns])
        tailnet._dns = dns
        app_ = server.App(server.Scanner(Registry(folder / "registry.json"), self_port=8790, system=system), tailnet)
        srv = server.make_server(app_, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        return app_, srv.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        is_json = (resp.getheader("Content-Type") or "").startswith("application/json")
        return resp.status, json.loads(data) if data and is_json else data

    def put(self, port, obj, headers=None):
        return self.request(port, "PUT", "/api/layout", json.dumps(obj).encode(),
                            dict({"Content-Type": "application/json"}, **(headers or {})))

    def test_put_and_guards(self):
        app_, port = self.start(FakeSystem(), "node.tail.ts.net")
        status, saved = self.put(port, {"order": ["plot-viewer"], "hidden": ["electrogui"]},
                                 {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual((saved["hidden"], saved["by"]), (["electrogui"], "node"))
        self.assertTrue(app_.layout.path.is_file())
        self.assertEqual(app_.layout.path.parent, app_.scanner.registry.path.parent)
        _, data = self.request(port, "GET", "/api/apps")
        self.assertEqual(data["layout"]["hidden"], ["electrogui"])

        tail = {"Host": "node.tail.ts.net", "Tailscale-User-Login": "me@example.com"}
        self.assertEqual(self.put(port, {"order": []}, dict(tail, Origin="https://node.tail.ts.net"))[0], 200)
        self.assertEqual(self.put(port, {"order": []}, dict(tail, **{"Tailscale-User-Login": "x@y.z"}))[0], 403)
        self.assertEqual(self.put(port, {"order": []}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.put(port, {"order": []}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.put(port, {"order": "x"})[0], 400)
        self.assertEqual(self.request(port, "PUT", "/api/layout", b"{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(port, "PUT", "/api/layout", b"", {"Content-Type": "application/json",
                                      "Content-Length": str(layout.MAX_BYTES + 1)})[0], 413)
        self.assertEqual(self.request(port, "PUT", "/api/apps", b"{}", {"Content-Type": "application/json"})[0], 405)
        self.assertEqual(self.request(port, "POST", "/api/layout", b"{}", {"Content-Type": "application/json"})[0], 405)
        self.assertEqual(self.request(port, "GET", "/healthz")[0], 200)  # still serving

    def test_origin_must_be_this_page_exactly(self):
        _, port = self.start(FakeSystem(), "node.tail.ts.net")
        local = f"127.0.0.1:{port}"
        self.assertEqual(self.put(port, {"order": []}, {"Origin": f"http://{local}"})[0], 200)
        for origin in ("null", "", f"https://{local}", f"http://localhost:{port}", f"http://127.0.0.1:{port + 1}",
                       "http://127.0.0.1", f"http://{local}/", f"http://{local}?token=x", f"http://{local}#x",
                       f"http://u@{local}", "http://127.0.0.1:bad", "http://127.0.0.1:0"):
            self.assertEqual(self.put(port, {"order": []}, {"Origin": origin})[0], 403, repr(origin))

        tail = {"Host": "node.tail.ts.net:8797", "Tailscale-User-Login": "me@example.com"}
        for origin in ("https://node.tail.ts.net:8797", "https://NODE.tail.ts.net:8797"):
            self.assertEqual(self.put(port, {"order": []}, dict(tail, Origin=origin))[0], 200, origin)
        for origin in ("http://node.tail.ts.net:8797", "https://node.tail.ts.net:8798", "https://node.tail.ts.net",
                       "https://evil.example:8797", "https://node.tail.ts.net:8797/", "null"):
            self.assertEqual(self.put(port, {"order": []}, dict(tail, Origin=origin))[0], 403, origin)
        bare = dict(tail, Host="node.tail.ts.net")  # https on 443: the port may be named or left out
        self.assertEqual(self.put(port, {"order": []}, dict(bare, Origin="https://node.tail.ts.net:443"))[0], 200)
        self.assertEqual(self.put(port, {"order": []}, dict(bare, Origin="https://node.tail.ts.net:0"))[0], 403)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)  # two Origins: refused, even if one fits
        conn.putrequest("PUT", "/api/layout")
        for key, value in (("Origin", f"http://{local}"), ("Origin", "https://evil.example"),
                           ("Content-Type", "application/json"), ("Content-Length", "2")):
            conn.putheader(key, value)
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)

    def test_page_saves_with_the_default_fetch_mode(self):
        # mode "same-origin" (or "no-cors") with the page's no-referrer policy makes Safari send Origin: null
        source = (Path(server.STATIC) / "app.js").read_text()
        call = re.search(r'fetch\("/api/layout",\s*\{(.*?)\}\);', source, re.S)
        self.assertIsNotNone(call)
        self.assertRegex(call.group(1), r'method:\s*"PUT"')
        self.assertNotRegex(call.group(1), r"\bmode\s*:")
        self.assertNotRegex(call.group(1), r"\bcredentials\s*:\s*\"omit\"")


# The page's layout save, as app.js makes it, then with mode "same-origin" (what Safari turned into Origin: null).
MODES_JS = """(async () => {
  const results = [];
  for (const mode of [null, "same-origin"]) {
    const options = { method: "PUT", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ order: [], hidden: [] }) };
    if (mode) options.mode = mode;
    const resp = await fetch("/api/layout", options);
    await resp.text();
    results.push(resp.status);
  }
  document.getElementById("result").textContent = JSON.stringify(results);
})().catch(err => { document.getElementById("result").textContent = "failed: " + err; });
"""


@unittest.skipUnless(previews.find_browser(), "no headless-capable browser on this Mac")
class BrowserOriginTests(unittest.TestCase):
    """A real browser, its own empty profile, this server's headers (no-referrer): the save keeps the Origin."""

    def test_default_mode_sends_the_pages_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<!doctype html><meta charset="utf-8">'
                                             '<script src="/static/modes.js" defer></script><pre id="result">waiting</pre>')
            (root / "modes.js").write_text(MODES_JS)
            data = reg.empty()
            reg.save(data, root / "registry.json")
            tailnet = server.Tailnet("me@example.com", ["node.tail.ts.net"])
            tailnet._dns = "node.tail.ts.net"
            app_ = server.App(server.Scanner(Registry(root / "registry.json"), system=FakeSystem()), tailnet)
            seen = []

            class Handler(server.make_handler(app_)):
                def _put(self):
                    seen.append((self.headers.get("Origin"), self.headers.get("Referer")))
                    return super()._put()

            srv = server.Server(("127.0.0.1", 0), Handler)
            port = srv.server_address[1]
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            with mock.patch.object(server, "STATIC", root):
                thread.start()
                try:
                    out = subprocess.run([previews.find_browser(), "--headless", "--disable-gpu", "--no-first-run",
                                          "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                                          "--disable-background-networking", "--use-mock-keychain",
                                          "--password-store=basic", f"--user-data-dir={root / 'profile'}",
                                          "--virtual-time-budget=5000", "--dump-dom", f"http://127.0.0.1:{port}/"],
                                         capture_output=True, text=True, timeout=60)
                finally:
                    srv.shutdown()
                    srv.server_close()
                    thread.join(5)
            found = re.search(r'<pre id="result">(.*?)</pre>', out.stdout, re.S)
            self.assertIsNotNone(found, out.stderr[-2000:])
            statuses = json.loads(found.group(1))
            self.assertEqual(len(seen), 2)
            self.assertEqual((statuses[0], seen[0]), (200, (f"http://127.0.0.1:{port}", None)))
            # Chrome keeps the Origin in "same-origin" mode too; Safari sent null. Either way: null is refused.
            self.assertEqual(statuses[1], 403 if seen[1][0] == "null" else 200)
            self.assertIsNone(seen[1][1])  # no Referer in either mode
            self.assertTrue(app_.layout.path.is_file())  # the scratch layout, next to the scratch registry


if __name__ == "__main__":
    unittest.main()
