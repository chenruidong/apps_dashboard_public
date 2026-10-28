"""Command line: ``python3 -m apps_dashboard <command>`` (or ``./apps-dashboard <command>``).

    serve                          run the dashboard on 127.0.0.1:8790 (what the LaunchAgent runs)
    scan [--json]                  print what the dashboard shows
    check [ID ...] [--port N]      report on some apps; exit 1 if any is not up (or idle, if on demand)
    register ID [--port N ...]     add or update a registry entry (fills repo/LaunchAgent/https port
                                   from what is running unless --no-detect)
    unregister ID                  drop a registry entry
    list                           print the registry
    ignore PORT / unignore PORT    hide / show an unregistered local listener
    launchagent install|restart|status|uninstall [-- serve args]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, discover, launchagent, registry as reg
from .server import DEFAULT_PORT, serve

MARK = {"up": "●", "degraded": "◐", "down": "○", "idle": "◌"}


def _human_age(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def describe(app):
    """Text lines for one app (the same facts as a dashboard card)."""
    lines = [f"{MARK.get(app['status'], '?')} {app['status']:<9}{app['name']}"
             + (f"  [{app['id']}]" if app["name"] != app["id"] else "")
             + ("" if app["registered"] else "  (not registered)")]
    pad = " " * 11
    if app.get("description"):
        lines.append(pad + app["description"])
    for route in app["routes"]:
        probe = route.get("probe") or {}
        answer = probe.get("code") or probe.get("error") or ""
        target = route["target"] if route["kind"] in ("proxy", "tcp") else route["kind"]
        if route.get("stale"):
            answer = "stale name"
        lines.append(f"{pad}tailnet {route['url'] or route.get('hostport')}  -> {target}  [{answer}]"
                     + ("  FUNNEL" if route.get("funnel") else ""))
    if app.get("local"):
        probe = app["local"].get("probe") or {}
        lines.append(f"{pad}local   {app['local']['url']}  [{probe.get('code') or probe.get('error') or ''}]")
    proc = app.get("process")
    if proc:
        extra = f", started from {proc['started_from']}" if proc.get("started_from") else ""
        lines.append(f"{pad}process pid {proc['pid']}, up {_human_age(proc.get('uptime_s'))}{extra}: {proc['command']}")
    ld = app.get("launchd")
    if ld:
        bits = [ld.get("state") or ("loaded" if ld["loaded"] else "not loaded")]
        if ld.get("keep_alive"):
            bits.append("KeepAlive")
        if ld.get("run_at_load"):
            bits.append("RunAtLoad")
        if ld.get("last_exit") not in (None, "0"):
            bits.append(f"last exit {ld['last_exit']}")
        if ld.get("runs"):
            bits.append(f"runs {ld['runs']}")
        lines.append(f"{pad}launchd {ld['label']} ({', '.join(bits)})" + (f"  {ld['plist']}" if ld.get("plist") else ""))
        if ld.get("logs"):
            lines.append(f"{pad}logs    {'  '.join(ld['logs'])}")
    code = app.get("codebase")
    if code:
        if code.get("git"):
            state = "clean" if not code.get("dirty") else f"{code['dirty']} uncommitted"
            sync = []
            if code.get("ahead"):
                sync.append(f"{code['ahead']} ahead")
            if code.get("behind"):
                sync.append(f"{code['behind']} behind")
            if code.get("upstream") and not sync:
                sync.append(f"= {code['upstream']}")
            head = f"{code.get('branch')}@{code['head']}" if code.get("head") else f"{code.get('branch')} (no commits)"
            lines.append(f"{pad}code    {code['display']}  {head}  {state}"
                         + (f"  {' '.join(sync)}" if sync else "") + (f"  {code['remote']}" if code.get("remote") else ""))
        else:
            lines.append(f"{pad}code    {code['display']} "
                         + ("(macOS does not let this process read it)" if code.get("blocked") else "(not git)"))
        if code.get("runs_from"):
            lines.append(f"{pad}runs    a copy in {code['runs_from']}")
    for problem in app["problems"]:
        lines.append(f"{pad}✗ {problem}")
    for warning in app["warnings"]:
        lines.append(f"{pad}! {warning}")
    return lines


def _scan(args):
    return discover.scan(registry=reg.load(args.registry), self_port=DEFAULT_PORT)


def cmd_scan(args):
    result = _scan(args)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    host = result["host"]
    print(f"{host['dns'] or host['name']} · {len(result['apps'])} apps · "
          f"scanned in {result['took_ms'] / 1000:.1f} s")
    for error in result["errors"]:
        print(f"  error: {error}")
    for app in result["apps"]:
        print()
        print("\n".join(describe(app)))
    return 0


def _select(result, ids, ports):
    if not ids and not ports:
        return result["apps"], []
    chosen, missing = [], []
    for app_id in ids:
        found = [a for a in result["apps"] if a["id"] == app_id]
        chosen += found
        if not found:
            missing.append(app_id)
    for port in ports:
        found = [a for a in result["apps"] if a.get("local") and a["local"]["port"] == port
                 or any(r["https_port"] == port for r in a["routes"])]
        chosen += [a for a in found if a not in chosen]
        if not found:
            missing.append(f"port {port}")
    return chosen, missing


def _healthy(app, strict):
    ok = app["status"] in ("up", "idle") and not app["problems"]
    return ok and not (strict and app["warnings"])


def cmd_check(args):
    deadline = time.monotonic() + args.wait
    while True:
        result = _scan(args)
        chosen, missing = _select(result, args.ids, args.port)
        healthy = not missing and chosen and all(_healthy(a, args.strict) for a in chosen)
        if healthy or time.monotonic() >= deadline:
            break
        time.sleep(2)
    for error in result["errors"]:
        print(f"error: {error}")
    for i, app in enumerate(chosen):
        print(("\n" if i else "") + "\n".join(describe(app)))
    for name in missing:
        print(f"✗ {name}: no such app on this Mac (scan found: {', '.join(a['id'] for a in result['apps']) or 'none'})")
    if not chosen and not missing:
        print("no apps found")
    return 0 if healthy else 1


def cmd_register(args):
    data = reg.load(args.registry)
    current = data["apps"].get(args.id, {})
    fields = {"name": args.name, "description": args.description, "port": args.port, "https_port": args.https_port,
              "path": args.path, "repo": args.repo, "launchd": args.launchd, "on_demand": args.on_demand}
    if fields["repo"]:
        fields["repo"] = str(Path(fields["repo"]).expanduser().resolve())
    port = fields["port"] or current.get("port")
    if not port:
        print("register: give --port (the loopback port the app listens on)", file=sys.stderr)
        return 2
    clash = [k for k, v in data["apps"].items() if k != args.id and v.get("port") == port]
    if clash:
        print(f"register: port {port} already belongs to {', '.join(clash)}; unregister it first", file=sys.stderr)
        return 2
    if not args.no_detect:
        found = [a for a in discover.scan(registry={"apps": {}, "ignore_ports": []})["apps"]
                 if a.get("local") and a["local"]["port"] == port]
        if found:
            app = found[0]
            detected = {}
            if app.get("codebase") and app["codebase"].get("path"):
                detected["repo"] = app["codebase"]["path"]
            if app.get("launchd") and app["launchd"].get("plist"):
                detected["launchd"] = app["launchd"]["label"]
            https = sorted({r["https_port"] for r in app["routes"] if r["kind"] == "proxy" and not r.get("stale")})
            if len(https) == 1:
                detected["https_port"] = https[0]
            for key, value in detected.items():
                if fields.get(key) is None and key not in current:
                    fields[key] = value
                    print(f"  detected {key}: {value}")
        else:
            print(f"  nothing answers on port {port} now; registered without detection")
    entry = reg.upsert(data, args.id, fields)
    path = reg.save(data, args.registry)
    print(f"{args.id}: {json.dumps(entry, ensure_ascii=False)}\n  saved {path}")
    return 0


def cmd_unregister(args):
    data = reg.load(args.registry)
    if data["apps"].pop(args.id, None) is None:
        print(f"{args.id} is not registered", file=sys.stderr)
        return 1
    print(f"removed {args.id}; saved {reg.save(data, args.registry)}")
    return 0


def cmd_list(args):
    data = reg.load(args.registry)
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_ignore(args, add):
    data = reg.load(args.registry)
    ports = set(data["ignore_ports"])
    (ports.add if add else ports.discard)(args.port_number)
    data["ignore_ports"] = sorted(ports)
    print(f"ignore_ports = {data['ignore_ports']}; saved {reg.save(data, args.registry)}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="apps-dashboard", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--registry", metavar="PATH", default=None,
                        help=f"registry file (default $APPS_DASHBOARD_REGISTRY or {reg.DEFAULT})")
    # Also accepted after the command (the LaunchAgent runs `serve --registry PATH`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser
    sub.add_parser = lambda *a, **k: add(*a, parents=[common], **k)

    p = sub.add_parser("serve", help="run the dashboard server")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--owner", metavar="LOGIN", help="tailnet login allowed through tailscale serve (default: this node's owner)")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="extra Host name to accept besides this node's MagicDNS name")
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    p.add_argument("--no-previews", action="store_true", help="no screenshots for the thumbnail view")

    p = sub.add_parser("scan", help="print every app")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="report on some apps; exit 1 unless all are healthy")
    p.add_argument("ids", nargs="*", metavar="ID")
    p.add_argument("--port", type=int, action="append", default=[], help="select by loopback or tailnet https port")
    p.add_argument("--strict", action="store_true", help="warnings fail too")
    p.add_argument("--wait", type=float, default=0, metavar="SECONDS", help="retry until healthy for this long")

    p = sub.add_parser("register", help="add or update a registry entry")
    p.add_argument("id")
    p.add_argument("--port", type=int, help="loopback port the app listens on")
    p.add_argument("--https-port", type=int, help="tailnet https port it should be published on")
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--path", help="path to open (default /)")
    p.add_argument("--repo", help="codebase checkout")
    p.add_argument("--launchd", metavar="LABEL", help="LaunchAgent label")
    p.add_argument("--on-demand", dest="on_demand", action="store_const", const=True,
                   help="not running is normal (shown as idle)")
    p.add_argument("--always-on", dest="on_demand", action="store_const", const=False)
    p.add_argument("--no-detect", action="store_true", help="do not fill fields from the running app")

    p = sub.add_parser("unregister", help="drop a registry entry")
    p.add_argument("id")
    sub.add_parser("list", help="print the registry")
    for name in ("ignore", "unignore"):
        p = sub.add_parser(name, help=f"{name} an unregistered local listener")
        p.add_argument("port_number", type=int, metavar="PORT")

    p = sub.add_parser("launchagent", help="manage the LaunchAgent that runs serve")
    p.add_argument("action", choices=("install", "restart", "status", "uninstall"))
    p.add_argument("serve_args", nargs=argparse.REMAINDER, help="extra serve options after --")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve(args.port, args.registry, args.owner, args.allow_host, args.open, not args.no_previews)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "register":
        return cmd_register(args)
    if args.command == "unregister":
        return cmd_unregister(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command in ("ignore", "unignore"):
        return cmd_ignore(args, args.command == "ignore")
    if args.command == "launchagent":
        extra = [a for a in args.serve_args if a != "--"]
        if args.registry and args.action == "install":
            extra += ["--registry", args.registry]
        return launchagent.run(args.action, extra)
    build_parser().print_help()
    return 0
