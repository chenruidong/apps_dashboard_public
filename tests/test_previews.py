import threading
import tempfile
import unittest
from pathlib import Path

from apps_dashboard import previews


def app(id_="web", status="up", routes=(), local=None, **extra):
    return dict({"id": id_, "status": status, "routes": list(routes), "local": local}, **extra)


OK = {"code": 200, "ms": 3, "error": None}


class FakeCapture:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail  # exception to raise, or a function (settle) -> exception or None
        self.release = threading.Event()
        self.release.set()

    def __call__(self, browser, url, settle=True):
        self.calls.append((url, settle))
        self.release.wait(5)
        fail = self.fail(settle) if callable(self.fail) else self.fail
        if fail is not None:
            raise fail
        return f"jpeg of {url} {len(self.calls)}".encode(), "image/jpeg"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def wait(p):
    with p._lock:
        running = list(p._running.values())
    for done in running:
        done.wait(5)


class PreviewUrlTests(unittest.TestCase):
    def test_first_good_tailnet_url_then_local(self):
        route = {"url": "https://node.ts.net:8766/", "probe": OK}
        self.assertEqual(previews.preview_url(app(routes=[route])), route["url"])
        stale = {"url": "https://old.ts.net:8766/", "probe": OK, "stale": True}
        missing = {"url": "https://node.ts.net:8767/", "probe": {"code": 404}}
        local = {"url": "http://127.0.0.1:8766/", "probe": OK}
        self.assertEqual(previews.preview_url(app(routes=[stale, missing], local=local)), local["url"])
        self.assertEqual(previews.preview_url(app(routes=[{"url": None, "https_port": 10000}])), None)

    def test_not_up_or_this_page(self):
        route = {"url": "https://node.ts.net:8766/", "probe": OK}
        self.assertIsNone(previews.preview_url(app(status="idle", routes=[route])))
        self.assertIsNone(previews.preview_url(app(status="down", routes=[route])))
        self.assertIsNone(previews.preview_url(app(routes=[route], self=True)))
        self.assertEqual(previews.preview_url(app(status="degraded", routes=[route])), route["url"])


class PreviewsTests(unittest.TestCase):
    route = {"url": "https://node.ts.net:8766/", "probe": OK}

    def make(self, **kwargs):
        clock = Clock()
        capture = FakeCapture(**kwargs)
        return previews.Previews("chrome", capture=capture, clock=clock), capture, clock

    def test_capture_on_demand_then_versioned(self):
        p, capture, clock = self.make()
        apps = [app(routes=[self.route]), app("idle", status="idle", routes=[self.route])]
        first = p.annotate(apps)
        self.assertEqual(first[0]["preview"], {"src": "/preview/web", "error": None})
        self.assertIsNone(first[1]["preview"])
        self.assertNotIn("preview", apps[0])  # copies, the scan result is untouched
        wait(p)
        self.assertEqual(capture.calls, [(self.route["url"], True)])
        again = p.annotate(apps)[0]["preview"]
        self.assertEqual(again["src"], "/preview/web?v=1000000")
        self.assertEqual(p.get("web", self.route["url"]), (b"jpeg of https://node.ts.net:8766/ 1", "image/jpeg"))
        self.assertEqual(len(capture.calls), 1)

    def test_get_waits_for_the_first_capture(self):
        p, capture, _ = self.make()
        data, ctype = p.get("web", self.route["url"])
        self.assertTrue(data.startswith(b"jpeg of"))
        self.assertEqual(ctype, "image/jpeg")

    def test_stale_preview_kept_while_recapturing(self):
        p, capture, clock = self.make()
        p.get("web", self.route["url"])
        clock.now += previews.TTL + 1
        capture.release.clear()
        self.assertEqual(p.annotate([app(routes=[self.route])])[0]["preview"]["src"], "/preview/web?v=1000000")
        self.assertTrue(p.get("web", self.route["url"])[0].endswith(b" 1"))  # the old one, without waiting
        capture.release.set()
        wait(p)
        self.assertEqual(p.annotate([app(routes=[self.route])])[0]["preview"]["src"], f"/preview/web?v={int(clock.now * 1000)}")

    def test_live_page_falls_back_to_load_event(self):
        p, capture, clock = self.make(fail=lambda settle: previews.PreviewTimeout("no screenshot in 12 s") if settle else None)
        p.get("web", self.route["url"])
        self.assertEqual(capture.calls, [(self.route["url"], True), (self.route["url"], False)])
        clock.now += previews.TTL + 1
        p.annotate([app(routes=[self.route])])
        wait(p)
        self.assertEqual(capture.calls[-1], (self.route["url"], False))  # straight to the load event from now on
        self.assertEqual(len(capture.calls), 3)

    def test_failure_reported_and_retried_later(self):
        p, capture, clock = self.make(fail=previews.PreviewError("no screenshot (exit 1)"))
        with self.assertRaises(previews.PreviewError):
            p.get("web", self.route["url"])
        self.assertEqual(p.annotate([app(routes=[self.route])])[0]["preview"],
                         {"src": "/preview/web", "error": "no screenshot (exit 1)"})
        wait(p)
        self.assertEqual(len(capture.calls), 1)  # not again before RETRY
        clock.now += previews.RETRY + 1
        capture.fail = None
        p.annotate([app(routes=[self.route])])
        wait(p)
        self.assertEqual(len(capture.calls), 2)
        self.assertIsNone(p.annotate([app(routes=[self.route])])[0]["preview"]["error"])

    def test_off_without_browser(self):
        p = previews.Previews(None, capture=FakeCapture())
        self.assertIsNone(p.annotate([app(routes=[self.route])])[0]["preview"])

    def test_cached_only_never_launches_a_browser(self):
        p, capture, clock = self.make()
        p.annotate([app(routes=[self.route])], refresh=False)
        with self.assertRaises(previews.PreviewError):
            p.get("web", self.route["url"], refresh=False)
        self.assertEqual(capture.calls, [])
        original = p.get("web", self.route["url"])
        clock.now += previews.TTL + 1
        p.annotate([app(routes=[self.route])], refresh=False)
        self.assertEqual(p.get("web", self.route["url"], refresh=False), original)
        self.assertEqual(len(capture.calls), 1)

    def test_fresh_request_waits_for_scan_validation(self):
        p, capture, clock = self.make()
        p.get("web", self.route["url"])
        clock.now += previews.FRESH + 1
        p.annotate([app(routes=[self.route])], fresh=True, refresh=False)
        self.assertEqual(len(capture.calls), 1)
        p.annotate([app(routes=[self.route])])
        wait(p)
        self.assertEqual(len(capture.calls), 2)

    def test_disk_preview_returns_during_background_refresh_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture, clock = FakeCapture(), Clock()
            p = previews.Previews("chrome", capture=capture, clock=clock, cache_dir=tmp)
            original = p.get("web", self.route["url"])
            clock.now += previews.TTL + 1
            restored = previews.Previews("chrome", capture=capture, clock=clock, cache_dir=tmp)
            capture.release.clear()
            try:
                self.assertEqual(restored.annotate([app(routes=[self.route])])[0]["preview"]["src"], "/preview/web?v=1000000")
                self.assertEqual(restored.get("web", self.route["url"]), original)
                self.assertTrue(restored.pending)
            finally:
                capture.release.set()
                wait(restored)
            self.assertFalse(restored.pending)
            self.assertNotEqual(restored.get("web", self.route["url"]), original)

    def test_disk_preview_is_bound_to_url_and_corruption_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = FakeCapture()
            p = previews.Previews("chrome", capture=capture, cache_dir=tmp)
            p.get("web", self.route["url"])
            other = previews.Previews("chrome", capture=capture, cache_dir=tmp)
            changed = "https://node.ts.net:9999/"
            self.assertIn(changed.encode(), other.get("web", changed)[0])
            for path in Path(tmp).glob("*.json"):
                path.write_text("corrupt")
            restored = previews.Previews("chrome", capture=capture, cache_dir=tmp)
            self.assertTrue(restored.get("web", changed)[0])
            self.assertEqual(len(capture.calls), 3)


if __name__ == "__main__":
    unittest.main()
