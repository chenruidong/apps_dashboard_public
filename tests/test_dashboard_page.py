"""Synthetic browser coverage for the dashboard's presentation and real layout save endpoint.

No real registry, discovery, app previews or Tailscale commands are used.
"""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from apps_dashboard import registry, server

ROOT = Path(__file__).resolve().parent.parent


class DashboardBrowserTests(unittest.TestCase):
    def test_launch_organization_and_persistence(self):
        node = shutil.which("node")
        chrome = next((p for p in [os.environ.get("DASHBOARD_TEST_CHROME"),
                      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      shutil.which("google-chrome"), shutil.which("chromium")]
                       if p and Path(p).is_file()), None)
        if not node or not chrome:
            self.skipTest("Node and a Chromium browser are needed for the synthetic UI test")
        with tempfile.TemporaryDirectory(prefix="dashboard-ui-fixture-") as tmp:
            path = Path(tmp)
            registry.save(registry.empty(), path / "registry.json")
            scanner = server.Scanner(registry.Registry(path / "registry.json"))
            app = server.App(scanner, server.Tailnet("sample@example.invalid", ["sample.invalid"]))
            app.tailnet._dns = "sample.invalid"  # never run a real Tailscale command
            payload = json.loads((ROOT / "tests/fixtures/dashboard_payload.json").read_text())
            app.layout.put(payload.pop("layout"), by="fixture")
            app.apps = lambda where, **kwargs: dict(copy.deepcopy(payload), viewer=where, layout=app.layout.get())
            srv = server.make_server(app, port=0)
            port = srv.server_address[1]
            # A native Open click reaches this scratch server's read-only health page, never a real app.
            opened = next(a for a in payload["apps"] if a["id"] == "electrogui")
            opened["routes"][0]["url"] = "http://127.0.0.1:{}/healthz".format(port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            output = os.environ.get("DASHBOARD_QA_OUTPUT", str(path / "screenshots"))
            try:
                result = subprocess.run([node, str(ROOT / "tests/js/dashboard_browser.cjs"),
                                         "http://127.0.0.1:{}/".format(port), chrome, output],
                                        text=True, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["failures"], report)
                self.assertGreaterEqual(len(report["checks"]), 25)
                self.assertFalse(report["runtimeErrors"], report)
                self.assertFalse(report["externalRequests"], report)
                persisted = app.layout.get()
                self.assertIn("retired-app", persisted["order"])
            finally:
                srv.shutdown()
                srv.server_close()
