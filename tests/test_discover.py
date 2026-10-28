import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apps_dashboard import cli, discover

# A real script file: codebase detection only trusts paths that exist.
_TMP = tempfile.TemporaryDirectory()
WV = os.path.join(_TMP.name, "Side Projects", "wv", "photo_wall.py")
os.makedirs(os.path.dirname(WV))
open(WV, "w").close()

SERVE = json.dumps({
    "TCP": {"443": {"HTTPS": True}, "8766": {"HTTPS": True}, "8778": {"HTTPS": True},
            "5432": {"TCPForward": "127.0.0.1:5432"}},
    "Web": {
        "node.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"}}},
        "node.tail.ts.net:8766": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"},
                                              "/docs": {"Path": "/srv/docs"}}},
        "node.tail.ts.net:9890": {"Handlers": {"/": {"Proxy": "127.0.0.1:8796"}}},
        "old-name.tail.ts.net:8766": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}},
        "old-name.tail.ts.net:10000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"}}},
        "node.tail.ts.net:8778": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8778"}}},
    },
    "AllowFunnel": {"node.tail.ts.net:9890": True, "old-name.tail.ts.net:10000": True},
})
STATUS = json.dumps({"Self": {"DNSName": "node.tail.ts.net.", "UserID": 7},
                     "User": {"7": {"LoginName": "me@example.com"}}})
LSOF = "\n".join([
    "p100", "cpython3.8", "f3", "n127.0.0.1:8766",
    "p200", "cpython3.8", "f3", "n127.0.0.1:8796",
    "p300", "cpython3.1", "f3", "n127.0.0.1:8778",
    "p400", "cnode", "f9", "n127.0.0.1:51290",
    "p500", "cpython3.8", "f4", "n*:8790", "f5", "n[::1]:8790",
    "p600", "cpython3.8", "f4", "n127.0.0.1:40000",
]) + "\n"
PS = "\n".join([
    "    1     0 Mon Oct  5 08:00:00 2026     /sbin/launchd",
    "  100     1 Fri Oct  9 09:03:23 2026     /usr/bin/python3 -m plot_viewer serve --no-browser",
    f"  200     1 Thu Oct  8 15:27:45 2026     /usr/bin/python3 {WV} serve --port 8796",
    "  290     1 Thu Oct  8 10:00:00 2026     /Applications/Claude.app/Contents/MacOS/Claude",
    "  295   290 Thu Oct  8 10:00:01 2026     /bin/zsh -il",
    "  300   295 Fri Oct  9 16:07:21 2026     /opt/env/bin/python -m electro_gui --no-browser --port 8778 --token t0k",
    "  400     1 Fri Oct  9 16:07:21 2026     /Applications/Fusion.app/Contents/Frameworks/node --serve",
    "  500     1 Mon Oct 12 09:00:00 2026     /usr/bin/python3 -m apps_dashboard serve",
    "  600     1 Mon Oct 12 09:00:00 2026     /usr/bin/python3 -m ipykernel_launcher",
]) + "\n"
LAUNCHCTL_LIST = "PID\tStatus\tLabel\n100\t-15\tcom.me.plot-viewer\n200\t0\tcom.me.photo-wall.serve\n" \
                 "290\t0\tapplication.com.anthropic.claudefordesktop.1.2\n500\t0\tcom.me.apps-dashboard\n" \
                 "-\t1\tcom.me.other\n"
PLISTS = {
    "com.me.plot-viewer": {"Label": "com.me.plot-viewer", "_path": "/Users/x/Library/LaunchAgents/rv.plist",
                             "ProgramArguments": ["/usr/bin/python3", "-m", "plot_viewer"],
                             "WorkingDirectory": "/code/plot-viewer", "KeepAlive": True, "RunAtLoad": True,
                             "StandardErrorPath": "/logs/rv.log", "StandardOutPath": "/logs/rv.log"},
    "com.me.photo-wall.serve": {"Label": "com.me.photo-wall.serve", "_path": "/la/wv.plist",
                                     "ProgramArguments": ["/usr/bin/python3", WV]},
    "com.me.apps-dashboard": {"Label": "com.me.apps-dashboard", "_path": "/la/ad.plist",
                              "WorkingDirectory": "/code/apps-dashboard", "KeepAlive": True},
    "com.me.other": {"Label": "com.me.other", "_path": "/la/other.plist"},
}
PRINT = "gui/501/x = {\n\tactive count = 1\n\tstate = running\n\tprogram = /usr/bin/python3\n" \
        "\targuments = {\n\t\tstate = nested\n\t}\n\truns = 7\n\tpid = 100\n\tlast exit code = 0\n}\n"


class FakeSystem:
    def __init__(self, probes=None, listeners=LSOF, serve=SERVE):
        self.probes = probes or {}
        self.listener_text = listeners
        self.serve = serve
        self.probed = []

    def tailscale(self, *args):
        if args[:2] == ("serve", "status"):
            return 0, self.serve, ""
        return 0, STATUS, ""

    def listeners(self):
        return 0, self.listener_text, ""

    def cwds(self, pids):
        cwd = {100: "/code/plot-viewer", 200: "/", 300: "/code/electrogui", 500: "/code/apps-dashboard", 600: "/"}
        return 0, "".join(f"p{p}\nfcwd\nn{cwd[p]}\n" for p in sorted(pids) if p in cwd), ""

    def processes(self):
        return 0, PS, ""

    def launchctl_list(self):
        return 0, LAUNCHCTL_LIST, ""

    def launchctl_print(self, label):
        return 0, PRINT, ""

    def plists(self):
        return PLISTS

    def git(self, path, *args):
        path = str(path)
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return (0, path + "\n", "") if path.startswith("/code/") else (128, "", "not a git repository")
        if args[0] == "log":
            return 0, "abc1234\x1fFix things\x1f1760000000\n", ""
        if args[:2] == ("rev-parse", "--abbrev-ref") and args[2] == "HEAD":
            return 0, "main\n", ""
        if args[0] == "status":
            return 0, (" M a.py\n?? b.py\n" if path.endswith("electrogui") else ""), ""
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return 0, "origin/main\n", ""
        if args[0] == "rev-list":
            return 0, "0\t2\n" if path.endswith("plot-viewer") else "0\t0\n", ""
        if args[:2] == ("remote", "get-url"):
            return 0, f"git@github.com:me/{path.rsplit('/', 1)[-1]}.git\n", ""
        return 1, "", ""

    def probe(self, url):
        self.probed.append(url)
        if url in self.probes:
            return self.probes[url]
        local = re.match(r"^http://127\.0\.0\.1:(\d+)/", url)
        if local and f":{local.group(1)}\n" not in self.listener_text:
            return {"code": None, "ms": None, "error": "connection refused"}
        if url == "http://127.0.0.1:40000/":  # a kernel's ZMQ port, not HTTP
            return {"code": None, "ms": None, "error": "RemoteDisconnected"}
        return {"code": 200, "ms": 3, "error": None}

    def log_tail(self, path, lines=15):
        return ["last line"]

    def now(self):
        return 1760300000.0


REGISTRY = {"apps": {"plot-viewer": {"name": "Plot viewer", "port": 8766, "https_port": 8766},
                     "electro_gui": {"port": 8765, "on_demand": True},
                     "electrogui": {"name": "ElectroGUI", "port": 8778, "https_port": 8778},
                     "notes": {"port": 8801, "https_port": 8801, "launchd": "com.me.other"}},
            "ignore_ports": []}


class ParserTests(unittest.TestCase):
    def test_parse_serve(self):
        routes = discover.parse_serve(SERVE)
        by_url = {r["url"]: r for r in routes if r["url"]}
        self.assertEqual(by_url["https://node.tail.ts.net/"]["backend_port"], 8790)
        self.assertEqual(by_url["https://node.tail.ts.net:8766/docs"]["kind"], "files")
        self.assertEqual(by_url["https://node.tail.ts.net:9890/"]["backend_port"], 8796)
        self.assertTrue(by_url["https://node.tail.ts.net:9890/"]["funnel"])
        tcp = [r for r in routes if r["kind"] == "tcp"]
        self.assertEqual((tcp[0]["backend_port"], tcp[0]["https_port"]), (5432, 5432))

    def test_parse_serve_empty(self):
        self.assertEqual(discover.parse_serve(""), [])
        self.assertEqual(discover.parse_serve("{}"), [])
        self.assertEqual(discover.parse_serve("not json"), [])

    def test_proxy_targets(self):
        self.assertEqual(discover.parse_proxy_target("http://127.0.0.1:8766"), ("http", "127.0.0.1", 8766, "/"))
        self.assertEqual(discover.parse_proxy_target("localhost:3000/app"), ("http", "localhost", 3000, "/app"))
        self.assertEqual(discover.parse_proxy_target("3000"), ("http", "127.0.0.1", 3000, "/"))
        self.assertEqual(discover.parse_proxy_target("https+insecure://127.0.0.1:8443")[:3], ("https", "127.0.0.1", 8443))

    def test_parse_self(self):
        self.assertEqual(discover.parse_self(STATUS), ("node.tail.ts.net", "me@example.com"))
        self.assertEqual(discover.parse_self("{}"), (None, None))

    def test_lsof(self):
        rows = discover.parse_lsof_listen(LSOF)
        self.assertIn({"pid": 500, "lsof_command": "python3.8", "host": "*", "port": 8790}, rows)
        self.assertIn({"pid": 500, "lsof_command": "python3.8", "host": "::1", "port": 8790}, rows)
        self.assertEqual(discover.parse_lsof_cwd("p1\nfcwd\nn/a b\np2\nfcwd\nn/\n"), {1: "/a b", 2: "/"})

    def test_ps(self):
        procs = discover.parse_ps(PS)
        self.assertEqual(procs[300]["ppid"], 295)
        self.assertTrue(procs[200]["command"].endswith("--port 8796"))
        self.assertIsNotNone(procs[100]["started"])

    def test_launchctl(self):
        listed = discover.parse_launchctl_list(LAUNCHCTL_LIST)
        self.assertEqual(listed["com.me.plot-viewer"], {"pid": 100, "status": -15})
        self.assertEqual(listed["com.me.other"], {"pid": None, "status": 1})
        fields = discover.parse_launchctl_print(PRINT)
        self.assertEqual((fields["state"], fields["runs"], fields["last exit code"]), ("running", "7", "0"))
        self.assertEqual(discover.describe_status(-15), "-15 (SIGTERM)")
        self.assertEqual(discover.describe_status(1), "exit 1")

    def test_web_url(self):
        self.assertEqual(discover.web_url("git@github.com:me/repo.git"), "https://github.com/me/repo")
        self.assertEqual(discover.web_url("ssh://git@git.example.org/me/repo.git"), "https://git.example.org/me/repo")
        self.assertEqual(discover.web_url("https://user:tok@github.com/me/repo.git"), "https://github.com/me/repo")
        self.assertIsNone(discover.web_url("/local/path"))

    def test_names(self):
        self.assertEqual(discover.name_from_label("com.example.photo-wall.serve"), "photo-wall")
        self.assertEqual(discover.name_from_label("com.example.plot-viewer"), "plot-viewer")
        self.assertEqual(discover.app_from_label("application.com.anthropic.claudefordesktop.1.2"),
                         "com.anthropic.claudefordesktop")
        self.assertEqual(discover.module_name("/usr/bin/python3 -m electro_gui --port 1"), "electro_gui")

    def test_redact(self):
        self.assertEqual(discover.redact("python -m electro_gui --port 8790 --token abc123 --no-browser"),
                         "python -m electro_gui --port 8790 --token ••• --no-browser")
        self.assertEqual(discover.redact("srv --api-key=XYZ --password hunter2"), "srv --api-key=••• --password •••")
        self.assertEqual(discover.redact("open https://h.ts.net:8777/?token=s3cr3t&x=1"),
                         "open https://h.ts.net:8777/?token=•••&x=1")
        self.assertEqual(discover.redact("srv --no-auth --port 1"), "srv --no-auth --port 1")
        self.assertEqual(discover.redact("gw.py --secret-file /a/share-key --port 1"), "gw.py --secret-file /a/share-key --port 1")
        self.assertEqual(discover.redact("python -m plot_viewer serve --no-browser --port 8766"),
                         "python -m plot_viewer serve --no-browser --port 8766")

    def test_public_url(self):
        self.assertEqual(discover.public_url("n.ts.net:443", "/"), "https://n.ts.net/")
        self.assertEqual(discover.public_url("n.ts.net:8766", "/x"), "https://n.ts.net:8766/x")


class ScanTests(unittest.TestCase):
    def scan(self, **kw):
        system = FakeSystem(**kw)
        result = discover.scan(system, REGISTRY, self_port=8790)
        return system, result, {a["id"]: a for a in result["apps"]}

    def test_apps_found(self):
        _, result, apps = self.scan()
        self.assertEqual(result["host"]["dns"], "node.tail.ts.net")
        self.assertEqual(set(apps), {"plot-viewer", "photo-wall", "apps-dashboard", "electro_gui", "electrogui",
                                     "notes", "port-5432", "node.tail.ts.net-8766-docs"})

    def test_registered_tailnet_app(self):
        _, _, apps = self.scan()
        rv = apps["plot-viewer"]
        self.assertEqual((rv["name"], rv["status"], rv["registered"]), ("Plot viewer", "up", True))
        self.assertEqual(rv["launchd"]["label"], "com.me.plot-viewer")
        self.assertEqual(rv["launchd"]["last_exit"], "-15 (SIGTERM)")
        self.assertEqual(rv["codebase"]["remote"], "https://github.com/me/plot-viewer")
        self.assertEqual(rv["codebase"]["source"], "LaunchAgent WorkingDirectory")
        self.assertIn("2 commits not pushed to origin/main", rv["warnings"])
        stale = [r for r in rv["routes"] if r["stale"]]
        self.assertEqual([(r["url"], r["probe"]) for r in stale], [("https://old-name.tail.ts.net:8766/", None)])
        self.assertFalse(any("old-name" in w for w in rv["warnings"]))  # listed under Details, not a warning

    def test_discovered_app_codebase_from_script(self):
        _, _, apps = self.scan()
        wv = apps["photo-wall"]
        self.assertFalse(wv["registered"])
        self.assertIn("Funnel is on: public on the internet", wv["warnings"])
        self.assertEqual(wv["codebase"]["source"], "LaunchAgent script")
        self.assertFalse(wv["codebase"]["git"])

    def test_terminal_started_app(self):
        _, _, apps = self.scan()
        eg = apps["electrogui"]
        self.assertIsNone(eg["launchd"])
        self.assertEqual(eg["process"]["started_from"], "com.anthropic.claudefordesktop")
        self.assertTrue(eg["process"]["command"].endswith("--token •••"))
        self.assertIn("2 uncommitted changes in /code/electrogui", eg["warnings"])

    def test_one_process_on_two_ports_makes_two_cards_with_their_own_ids(self):
        class TwoPorts(FakeSystem):  # pid 700, its own LaunchAgent, listens on 8800 and 8802
            def processes(self):
                return 0, PS + "  700     1 Mon Oct 12 09:00:00 2026     /usr/bin/python3 -u /srv/proxy.py\n", ""

            def launchctl_list(self):
                return 0, LAUNCHCTL_LIST + "700\t0\tcom.me.proxy\n", ""

            def plists(self):
                return dict(PLISTS, **{"com.me.proxy": {"Label": "com.me.proxy", "_path": "/la/proxy.plist"}})
        system = TwoPorts(listeners=LSOF + "p700\ncpython3.8\nf4\nn127.0.0.1:8800\nf5\nn127.0.0.1:8802\n")
        result = discover.scan(system, REGISTRY, self_port=8790)
        ids = [a["id"] for a in result["apps"]]
        self.assertEqual(len(ids), len(set(ids)))
        proxies = {a["id"]: a for a in result["apps"] if a["id"].startswith("proxy")}
        self.assertEqual(sorted(proxies), ["proxy-8800", "proxy-8802"])
        self.assertEqual(proxies["proxy-8800"]["name"], "proxy-8800")
        self.assertIn("electrogui", ids)  # registered ids are never changed

    def test_only_stale_routes_is_not_on_the_tailnet(self):
        _, _, apps = self.scan()
        self.assertTrue(apps["apps-dashboard"]["on_tailnet"])  # :443 under the current name
        serve = json.loads(SERVE)  # electrogui only under the node's old name
        serve["Web"]["old-name.tail.ts.net:8778"] = serve["Web"].pop("node.tail.ts.net:8778")
        registry = dict(REGISTRY, apps=dict(REGISTRY["apps"], electrogui={"port": 8778, "https_port": 9999}))
        result = discover.scan(FakeSystem(serve=json.dumps(serve)), registry, self_port=8790)
        eg = {a["id"]: a for a in result["apps"]}["electrogui"]
        self.assertFalse(eg["on_tailnet"])
        self.assertIn("not published on tailnet :9999", " ".join(eg["problems"]))

    def test_on_demand_not_running_is_idle(self):
        _, _, apps = self.scan()
        self.assertEqual(apps["electro_gui"]["status"], "idle")
        self.assertEqual(apps["electro_gui"]["problems"], [])

    def test_registered_but_missing(self):
        _, _, apps = self.scan()
        notes = apps["notes"]
        self.assertEqual(notes["status"], "down")
        joined = " | ".join(notes["problems"])
        self.assertIn("not published on tailnet :8801", joined)
        self.assertIn("nothing listens on port 8801", joined)
        self.assertIn("last exited exit 1", joined)
        self.assertEqual(notes["launchd"]["log_tail"], {})

    def test_self_and_all_interfaces(self):
        _, _, apps = self.scan()
        me = apps["apps-dashboard"]
        self.assertTrue(me["self"])
        self.assertTrue(any("all interfaces" in w for w in me["warnings"]))
        self.assertIn("Funnel is set, but only on a stale name, so it is probably not reachable", me["warnings"])
        self.assertEqual(me["status"], "up")

    def test_app_bundles_and_non_http_are_skipped(self):
        system, _, apps = self.scan(probes={"http://127.0.0.1:40000/": {"code": None, "ms": None, "error": "closed"}})
        self.assertNotIn("http://127.0.0.1:51290/", system.probed)  # node inside an .app bundle
        self.assertIn("http://127.0.0.1:40000/", system.probed)
        self.assertFalse(any(a["local"] and a["local"]["port"] == 40000 for a in apps.values()))

    def test_degraded_when_tailnet_fails(self):
        _, _, apps = self.scan(probes={"https://node.tail.ts.net:8766/": {"code": 502, "ms": 4, "error": None}})
        rv = apps["plot-viewer"]
        self.assertEqual(rv["status"], "degraded")
        self.assertIn("https://node.tail.ts.net:8766/ answered 502", rv["problems"])
        self.assertEqual(rv["launchd"]["log_tail"], {"/logs/rv.log": ["last line"]})

    def test_down_when_backend_gone(self):
        lsof = LSOF.replace("p100\ncpython3.8\nf3\nn127.0.0.1:8766\n", "")
        _, _, apps = self.scan(listeners=lsof, probes={
            "http://127.0.0.1:8766/": {"code": None, "ms": None, "error": "connection refused"},
            "https://node.tail.ts.net:8766/": {"code": 502, "ms": 4, "error": None}})
        rv = apps["plot-viewer"]
        self.assertEqual(rv["status"], "down")
        self.assertIsNone(rv["process"])

    def test_ignore_ports(self):
        apps = {k: v for k, v in REGISTRY["apps"].items() if k != "electrogui"}  # 8778 unregistered
        serve = json.loads(SERVE)  # and not published either
        del serve["Web"]["node.tail.ts.net:8778"]
        registry = dict(REGISTRY, apps=apps, ignore_ports=[8778])
        result = discover.scan(FakeSystem(serve=json.dumps(serve)), registry, self_port=8790)
        self.assertNotIn("electrogui", {a["id"] for a in result["apps"]})

    def test_tailnet_apps_sort_first(self):
        _, result, _ = self.scan()
        kinds = [a["on_tailnet"] for a in result["apps"]]
        self.assertEqual(kinds, sorted(kinds, reverse=True))


class OnDemandPublishedTests(unittest.TestCase):
    """An on-demand app whose tailnet route stays published while it is stopped (a slide deck on 8781)."""
    URL = "https://node.tail.ts.net:8781/"
    BAD_GATEWAY = {"code": 502, "ms": 4, "error": None}

    def scan(self, entry=None, target="http://127.0.0.1:8781", listeners=LSOF, probes=None):
        serve = json.loads(SERVE)
        serve["TCP"]["8781"] = {"HTTPS": True}
        serve["Web"]["node.tail.ts.net:8781"] = {"Handlers": {"/": {"Proxy": target}}}
        entry = entry if entry is not None else {"port": 8781, "https_port": 8781, "on_demand": True}
        registry = dict(REGISTRY, apps=dict(REGISTRY["apps"], slides=entry))
        all_probes = {self.URL: self.BAD_GATEWAY}
        all_probes.update(probes or {})
        system = FakeSystem(probes=all_probes, listeners=listeners, serve=json.dumps(serve))
        result = discover.scan(system, registry, self_port=8790)
        return {a["id"]: a for a in result["apps"]}

    def test_stopped_and_published_is_idle(self):
        apps = self.scan()
        ld = apps["slides"]
        self.assertEqual((ld["status"], ld["problems"], ld["on_tailnet"]), ("idle", [], True))
        self.assertEqual([(r["url"], r["probe"]["code"]) for r in ld["routes"]], [(self.URL, 502)])
        self.assertTrue(cli._healthy(ld, strict=False))
        self.assertEqual(apps["plot-viewer"]["status"], "up")  # the others are unchanged

    def test_other_tailnet_failures_are_still_down(self):
        for probe in ({"code": 504, "ms": 4, "error": None}, {"code": 500, "ms": 4, "error": None},
                      {"code": None, "ms": None, "error": "timeout"}):
            ld = self.scan(probes={self.URL: probe})["slides"]
            self.assertEqual(ld["status"], "down", probe)
            self.assertIn("nothing listens on port 8781", ld["problems"])
            self.assertFalse(cli._healthy(ld, strict=False))

    def test_502_while_something_listens_is_down(self):
        lsof = LSOF + "p800\ncpython3.8\nf3\nn127.0.0.1:8781\n"
        ld = self.scan(listeners=lsof, probes={"http://127.0.0.1:8781/": {"code": 500, "ms": 2, "error": None}})["slides"]
        self.assertEqual(ld["status"], "down")
        self.assertIn(f"{self.URL} answered 502", ld["problems"])
        self.assertIn("http://127.0.0.1:8781/ answered 500", ld["problems"])

    def test_not_on_demand_is_down_as_before(self):
        ld = self.scan(entry={"port": 8781, "https_port": 8781})["slides"]
        self.assertEqual(ld["status"], "down")
        self.assertIn(f"{self.URL} answered 502", ld["problems"])
        self.assertIn("nothing listens on port 8781", ld["problems"])

    def test_route_to_the_wrong_port_is_a_problem(self):
        apps = self.scan(target="http://127.0.0.1:8782")
        ld = apps["slides"]
        self.assertEqual(ld["routes"], [])
        self.assertIn("not published on tailnet :8781 (tailscale serve --bg --https=8781 http://127.0.0.1:8781)",
                      ld["problems"])
        self.assertFalse(cli._healthy(ld, strict=False))
        self.assertEqual(apps["port-8782"]["status"], "down")  # the route's own backend is not on demand


class GitTests(unittest.TestCase):
    def test_retries_without_unreadable_global_config(self):
        calls = []

        def fake_run(cmd, timeout=10, cwd=None, env=None):
            calls.append(env.get("GIT_CONFIG_GLOBAL"))
            if env.get("GIT_CONFIG_GLOBAL") is None:
                return 128, "", ("warning: unable to access '/Users/x/.gitconfig': Operation not permitted\n"
                                 "fatal: unknown error occurred while reading the configuration files\n")
            return 0, "/code/x\n", ""

        system, saved = discover.System(), discover.run
        discover.run = fake_run
        try:
            self.assertEqual(system.git("/code/x", "rev-parse", "--show-toplevel"), (0, "/code/x\n", ""))
            self.assertEqual(system.git("/code/x", "status"), (0, "/code/x\n", ""))
        finally:
            discover.run = saved
        self.assertEqual(calls, [None, os.devnull, os.devnull])  # the fallback is remembered

    def test_retry_when_another_thread_switched_first(self):
        system, saved = discover.System(), discover.run
        unreadable = "warning: unable to access '/Users/x/.gitconfig': Operation not permitted\n"

        def fake_run(cmd, timeout=10, cwd=None, env=None):
            if env.get("GIT_CONFIG_GLOBAL") is None:
                system.git_env = dict(discover.ENV, GIT_CONFIG_GLOBAL=os.devnull)  # a concurrent call got there first
                return 128, "", unreadable
            return 0, "ok\n", ""

        discover.run = fake_run
        try:
            self.assertEqual(system.git("/code/x", "status"), (0, "ok\n", ""))
        finally:
            discover.run = saved

    def test_unreadable_config_is_not_a_blocked_folder(self):
        class Config:
            def git(self, path, *args):
                return 128, "", "warning: unable to access '/Users/x/.gitconfig': Operation not permitted\n"
        self.assertNotIn("blocked", discover.git_info(Config(), "/code/x"))

    def test_blocked_folder(self):
        class Blocked:
            def git(self, path, *args):
                return 128, "", "fatal: cannot change to '/Users/x/Documents/a': Operation not permitted\n"
        info = discover.git_info(Blocked(), "/Users/x/Documents/a")
        self.assertEqual((info["git"], info.get("blocked")), (False, True))


if __name__ == "__main__":
    unittest.main()
