"""Find the web apps this Mac serves and describe each one.

Every source is read-only:

* ``tailscale serve status --json``: what the tailnet sees (https port + path -> backend);
* ``lsof`` listeners: which process holds each port, and its working directory;
* ``ps``: command line, start time, parent chain;
* ``launchctl list`` / ``launchctl print`` and ``~/Library/LaunchAgents/*.plist``: the
  LaunchAgent behind a process;
* ``git`` with ``GIT_OPTIONAL_LOCKS=0`` (``status`` does not even refresh the index), never
  fetching: the codebase a process runs from;
* one HTTP GET on each local and tailnet URL: whether it answers.

An app is one backend: a loopback port that ``tailscale serve`` proxies to, a registry
entry, or a local web server found listening.  ``scan`` returns plain JSON-able dicts.
"""
from __future__ import annotations

import collections
import concurrent.futures
import datetime
import http.client
import json
import os
import plistlib
import re
import signal
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

HOME = str(Path.home())
LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
TAILSCALE_CANDIDATES = ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                        "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ANY = {"*", "0.0.0.0", "::"}
ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C",
           PATH=os.pathsep.join([os.environ.get("PATH", ""), "/usr/local/bin", "/opt/homebrew/bin",
                                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
# Interpreters and servers whose loopback listeners are worth a look when nothing registered them.
WEB_RUNTIMES = re.compile(r"^(python[0-9.]*|node|deno|bun|ruby|php|java|uvicorn|gunicorn|hypercorn|flask|"
                          r"jupyter[\w-]*|http-server|caddy|nginx|httpd|hugo|streamlit|voila)$")
SCRIPT_EXT = (".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".php")
PROBE_TIMEOUT = 3.0
LOG_TAIL_LINES = 15


# ------------------------------------------------------------------ running commands

def run(cmd, timeout=10, cwd=None, env=None):
    """``(returncode, stdout, stderr)``; returncode is None when the command could not run."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env or ENV,
                              stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def tailscale_exe():
    for candidate in TAILSCALE_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def unreadable_config(stderr):
    """git refused to start because macOS hides ``~/.gitconfig`` from this process: a LaunchAgent cannot
    read Dropbox or iCloud folders, and the file is often a link into one (Mackup)."""
    return "unable to access" in (stderr or "") and "Operation not permitted" in stderr


class System:
    """The commands discovery reads.  Tests replace it with canned outputs."""

    def __init__(self):
        self.git_env = ENV

    def tailscale(self, *args):
        exe = tailscale_exe()
        if not exe:
            return None, "", "no tailscale CLI found"
        return run([exe, *args], timeout=8)

    def listeners(self):
        return run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"], timeout=15)

    def cwds(self, pids):
        if not pids:
            return 0, "", ""
        return run(["lsof", "-nP", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p) for p in sorted(pids))],
                   timeout=15)

    def processes(self):
        return run(["ps", "-A", "-o", "pid=,ppid=,lstart=,command="], timeout=10)

    def launchctl_list(self):
        return run(["launchctl", "list"], timeout=10)

    def launchctl_print(self, label):
        return run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], timeout=10)

    def plists(self):
        out = {}
        for path in sorted(LAUNCH_AGENTS.glob("*.plist")):
            try:
                with path.open("rb") as handle:
                    data = plistlib.load(handle)
            except Exception:  # noqa: BLE001 - a broken plist is skipped, not fatal
                continue
            if isinstance(data, dict) and isinstance(data.get("Label"), str):
                out[data["Label"]] = dict(data, _path=str(path))
        return out

    def git(self, path, *args):
        env = self.git_env  # scans call git from several threads at once
        rc, out, err = run(["git", "-C", str(path), *args], timeout=10, env=env)
        if rc not in (None, 0) and env is ENV and unreadable_config(err):
            # Without the global config; remembered for every later call of this process.
            self.git_env = dict(ENV, GIT_CONFIG_GLOBAL=os.devnull)
            rc, out, err = run(["git", "-C", str(path), *args], timeout=10, env=self.git_env)
        return rc, out, err

    def probe(self, url):
        return probe(url)

    def log_tail(self, path, lines=LOG_TAIL_LINES):
        return log_tail(path, lines)

    def now(self):
        return time.time()


# ------------------------------------------------------------------ parsers (pure)

def parse_proxy_target(target):
    """``"http://127.0.0.1:8766"`` / ``"127.0.0.1:8766"`` / ``"8766"`` -> (scheme, host, port, path)."""
    target = (target or "").strip()
    if target.isdigit():
        return "http", "127.0.0.1", int(target), "/"
    if "://" not in target:
        target = "http://" + target
    parts = urlsplit(target)
    scheme = parts.scheme.split("+")[0] or "http"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, (parts.hostname or "127.0.0.1").lower(), port, parts.path or "/"


def public_url(hostport, path="/"):
    host, _, port = hostport.rpartition(":")
    if not host:
        host, port = hostport, "443"
    base = f"https://{host}" if port == "443" else f"https://{host}:{port}"
    return base + (path if path.startswith("/") else "/" + path)


def parse_serve(text):
    """Routes from ``tailscale serve status --json`` (an empty config prints ``{}`` or nothing)."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    funnel = data.get("AllowFunnel") or {}
    routes = []
    for hostport, web in sorted((data.get("Web") or {}).items()):
        port = hostport.rpartition(":")[2]
        for path, handler in sorted(((web or {}).get("Handlers") or {}).items()):
            handler = handler or {}
            route = {"hostport": hostport, "https_port": int(port) if port.isdigit() else 443, "path": path,
                     "url": public_url(hostport, path), "funnel": bool(funnel.get(hostport))}
            if handler.get("Proxy"):
                scheme, host, bport, bpath = parse_proxy_target(handler["Proxy"])
                route.update(kind="proxy", target=handler["Proxy"], backend_host=host, backend_port=bport,
                             backend_scheme=scheme, backend_path=bpath)
            elif handler.get("Path"):
                route.update(kind="files", target=handler["Path"])
            elif "Text" in handler:
                route.update(kind="text", target="(text)")
            else:
                route.update(kind="other", target=json.dumps(handler)[:120])
            routes.append(route)
    for port, tcp in sorted((data.get("TCP") or {}).items()):
        tcp = tcp or {}
        if tcp.get("TCPForward"):
            _, host, bport, _ = parse_proxy_target("tcp://" + tcp["TCPForward"])
            routes.append({"hostport": f"*:{port}", "https_port": int(port) if port.isdigit() else 0, "path": "",
                           "url": None, "funnel": False, "kind": "tcp", "target": tcp["TCPForward"],
                           "backend_host": host, "backend_port": bport, "backend_scheme": "tcp", "backend_path": ""})
    return routes


def parse_self(text):
    """``(dns_name, login)`` of this node from ``tailscale status --json``."""
    try:
        data = json.loads(text)
        me = data["Self"]
        dns = (me.get("DNSName") or "").rstrip(".").lower() or None
        login = data.get("User", {}).get(str(me.get("UserID")), {}).get("LoginName")
        return dns, login
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None


def split_addr(addr):
    """``"127.0.0.1:8766"`` / ``"[::1]:8766"`` / ``"*:8766"`` -> (host, port)."""
    host, _, port = addr.rpartition(":")
    host = host.strip("[]")
    return host, int(port) if port.isdigit() else None


def parse_lsof_listen(text):
    """``lsof -Fpcn`` listeners -> [{"pid", "lsof_command", "host", "port"}] (one per address)."""
    out, pid, command, seen = [], None, None, set()
    for line in (text or "").splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid, command = int(value), None
        elif tag == "c":
            command = value
        elif tag == "n" and pid is not None:
            host, port = split_addr(value.split("->")[0])
            if port is None or (pid, host, port) in seen:
                continue
            seen.add((pid, host, port))
            out.append({"pid": pid, "lsof_command": command, "host": host, "port": port})
    return out


def parse_lsof_cwd(text):
    out, pid = {}, None
    for line in (text or "").splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def parse_ps(text):
    """``ps -A -o pid=,ppid=,lstart=,command=`` -> {pid: {"ppid", "started", "command"}}."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].isdigit():
            continue
        try:
            started = time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            started = None
        out[int(parts[0])] = {"ppid": int(parts[1]) if parts[1].isdigit() else 0, "started": started,
                              "command": parts[7]}
    return out


def parse_launchctl_list(text):
    out = {}
    for line in (text or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, status, label = parts
        out[label] = {"pid": int(pid) if pid.strip().lstrip("-").isdigit() else None,
                      "status": int(status) if status.strip().lstrip("-").isdigit() else None}
    return out


_PRINT_LINE = re.compile(r"^\t([a-z][a-z ]*?) = (.*)$")


def parse_launchctl_print(text):
    """Top-level ``key = value`` lines of ``launchctl print`` (nested blocks are skipped)."""
    out = {}
    for line in (text or "").splitlines():
        match = _PRINT_LINE.match(line)
        if match and match.group(1) not in out:
            out[match.group(1)] = match.group(2).strip()
    return out


def describe_status(status):
    """The ``launchctl list`` status column: 0, an exit code, or minus a signal number."""
    if status is None:
        return None
    if status == 0:
        return "0"
    if status < 0:
        try:
            return f"{status} ({signal.Signals(-status).name})"
        except ValueError:
            return str(status)
    return f"exit {status}"


def web_url(remote):
    """A browsable URL for a git remote (ssh or https), without credentials; None if unknown."""
    remote = (remote or "").strip()
    match = re.match(r"^(?:ssh://)?[\w.-]+@([\w.-]+)[:/](.+?)(?:\.git)?/?$", remote)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    parts = urlsplit(remote)
    if parts.scheme in ("http", "https") and parts.hostname:
        path = re.sub(r"\.git/?$", "", parts.path).rstrip("/")
        port = f":{parts.port}" if parts.port else ""
        return f"https://{parts.hostname}{port}{path}"
    return None


# Secrets passed on a command line or in a URL (--token, ?token=...): shown as ••• on the page.
_SECRET_FLAG = re.compile(r"((?:^|\s)--?(?:[\w-]*(?:token|password|passwd|secret|api[-_]?key|auth)[\w-]*)"
                          r"(?<!file)(?<!path)(?<!dir)(?:=|\s+))((?!-)\S+)",
                          re.IGNORECASE)
_SECRET_QUERY = re.compile(r"([?&](?:[\w-]*(?:token|password|secret|key|auth|sig)[\w-]*)=)[^&\s#\"']+", re.IGNORECASE)


def redact(text):
    if not text:
        return text
    return _SECRET_QUERY.sub(r"\1•••", _SECRET_FLAG.sub(r"\1•••", text))


def tilde(path):
    if path and (path == HOME or path.startswith(HOME + "/")):
        return "~" + path[len(HOME):]
    return path


def slug(text):
    text = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-._")
    return text[:64] or "app"


def name_from_label(label):
    parts = [p for p in label.split(".") if p]
    if len(parts) > 2 and parts[0] in ("com", "org", "io", "net", "dev", "me", "edu", "local", "application"):
        parts = parts[2:]
    while len(parts) > 1 and parts[-1] in ("serve", "server", "web", "agent", "daemon", "app", "http"):
        parts = parts[:-1]
    return ".".join(parts) or label


def script_path(command, cwd=None):
    """The script a command line runs (paths may contain spaces), or None."""
    tokens = (command or "").split(" ")
    for i in range(1, len(tokens)):
        if tokens[i].startswith("-"):
            continue
        for j in range(i, len(tokens)):
            candidate = " ".join(tokens[i:j + 1])
            if candidate.endswith(SCRIPT_EXT):
                full = candidate if os.path.isabs(candidate) or not cwd else os.path.join(cwd, candidate)
                if os.path.isfile(full):
                    return full
    return None


def module_name(command):
    match = re.search(r"(?:^|\s)-m\s+([\w.]+)", command or "")
    return match.group(1) if match else None


def runtime_name(command):
    exe = (command or "").split(" ")[0]
    return os.path.basename(exe)


def is_app_bundle(command):
    exe = (command or "").split(" -")[0]
    return ".app/Contents/" in exe or exe.startswith(("/System/", "/usr/libexec/", "/usr/sbin/"))


# ------------------------------------------------------------------ probes and files

def probe(url, timeout=PROBE_TIMEOUT):
    """One GET; ``{"code", "ms", "error"}``.  Redirects are not followed."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    start = time.monotonic()
    conn = None
    try:
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
        conn.request("GET", path, headers={"User-Agent": "apps-dashboard", "Accept": "text/html,*/*"})
        resp = conn.getresponse()
        resp.read(2048)
        return {"code": resp.status, "ms": round((time.monotonic() - start) * 1000), "error": None}
    except ConnectionRefusedError:
        return {"code": None, "ms": None, "error": "connection refused"}
    except socket.timeout:
        return {"code": None, "ms": None, "error": f"no answer in {timeout:g} s"}
    except ssl.SSLError as exc:
        return {"code": None, "ms": None, "error": f"TLS: {exc.reason or exc}"}
    except (OSError, http.client.HTTPException) as exc:
        return {"code": None, "ms": None, "error": type(exc).__name__ + (f": {exc}" if str(exc) else "")}
    finally:
        if conn is not None:
            conn.close()


def answered(result):
    return bool(result) and result.get("code") is not None and result["code"] < 500


def log_tail(path, lines=LOG_TAIL_LINES):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 16384))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    return [line[:400] for line in data.splitlines()[-lines:]]


def iso(epoch):
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).replace(microsecond=0) \
        .isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ scan

def git_info(system, path):
    """Branch, HEAD, cleanliness and upstream of the repository containing ``path``."""
    rc, top, err = system.git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not top.strip():
        info = {"path": path, "display": tilde(path), "git": False}
        if "Operation not permitted" in (err or "") and not unreadable_config(err):
            info["blocked"] = True  # macOS privacy: ~/Documents, iCloud and Dropbox are closed to LaunchAgents
        return info
    top = top.strip()
    info = {"path": top, "display": tilde(top), "git": True}
    rc, out, _ = system.git(top, "log", "-1", "--format=%h%x1f%s%x1f%ct")
    if rc == 0 and out.count("\x1f") == 2:
        head, subject, ct = out.strip().split("\x1f")
        info.update(head=head, subject=subject, committed=iso(int(ct)) if ct.isdigit() else None,
                    committed_epoch=int(ct) if ct.isdigit() else None)
    rc, out, _ = system.git(top, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:  # no commits yet: rev-parse fails, the branch name is still in HEAD
        rc, out, _ = system.git(top, "symbolic-ref", "--short", "-q", "HEAD")
    info["branch"] = out.strip() if rc == 0 else None
    rc, out, _ = system.git(top, "status", "--porcelain=v1")
    info["dirty"] = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else None
    rc, out, _ = system.git(top, "rev-parse", "--abbrev-ref", "@{upstream}")
    upstream = out.strip() if rc == 0 else None
    info["upstream"] = upstream
    remote_name = upstream.split("/")[0] if upstream else "origin"
    if upstream:
        rc, out, _ = system.git(top, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
        nums = out.split()
        if rc == 0 and len(nums) == 2:
            info["behind"], info["ahead"] = int(nums[0]), int(nums[1])
    rc, out, _ = system.git(top, "remote", "get-url", remote_name)
    info["remote"] = web_url(out) if rc == 0 else None
    return info


def _codebase_path(entry, plist, cwd, command):
    if entry.get("repo"):
        return os.path.expanduser(entry["repo"]), "registry"
    workdir = (plist or {}).get("WorkingDirectory")
    if workdir and workdir != "/":
        return workdir, "LaunchAgent WorkingDirectory"
    if cwd and cwd != "/":
        return cwd, "process working directory"
    args = (plist or {}).get("ProgramArguments")
    if isinstance(args, list):
        for arg in args[1:]:
            if isinstance(arg, str) and arg.endswith(SCRIPT_EXT) and os.path.isfile(arg):
                return os.path.dirname(arg), "LaunchAgent script"
    script = script_path(command, cwd)
    if script:
        return os.path.dirname(script), "process script"
    return None, None


def _launchd_label(pid, procs, by_pid):
    """The launchd job running ``pid`` or its nearest ancestor that is one."""
    seen = 0
    while pid and pid > 1 and seen < 12:
        if pid in by_pid:
            return by_pid[pid]
        pid = procs.get(pid, {}).get("ppid")
        seen += 1
    return None


def app_from_label(label):
    """``application.com.anthropic.claudefordesktop.372578781.372578787`` -> ``com.anthropic.claudefordesktop``:
    launchd's job for an app opened from the Dock or Finder, whose children (terminals) it parents."""
    parts = label.split(".")[1:] if label.startswith("application.") else label.split(".")
    while parts and parts[-1].isdigit():
        parts.pop()
    return ".".join(parts) or label


def _label_info(system, label, plists, listed):
    plist = plists.get(label)
    row = listed.get(label)
    info = {"label": label, "plist": tilde(plist["_path"]) if plist else None, "loaded": row is not None,
            "pid": row["pid"] if row else None,
            "last_exit": describe_status(row["status"]) if row else None,
            "keep_alive": bool(plist.get("KeepAlive")) if plist else None,
            "run_at_load": bool(plist.get("RunAtLoad")) if plist else None,
            "disabled": bool(plist.get("Disabled")) if plist else None}
    if plist:
        args = plist.get("ProgramArguments") or ([plist["Program"]] if plist.get("Program") else [])
        info["program"] = redact(" ".join(str(a) for a in args))
        logs = []
        for key in ("StandardErrorPath", "StandardOutPath"):
            if plist.get(key) and plist[key] not in logs:
                logs.append(plist[key])
        info["logs"] = [tilde(p) for p in logs]
        info["_logs"] = logs
    if row is not None:
        rc, out, _ = system.launchctl_print(label)
        if rc == 0:
            fields = parse_launchctl_print(out)
            info["state"] = fields.get("state")
            info["runs"] = int(fields["runs"]) if fields.get("runs", "").isdigit() else None
            info["last_exit_code"] = fields.get("last exit code")
            info["last_signal"] = fields.get("last terminating signal")
    return info


def scan(system=None, registry=None, self_port=None):
    """Everything the dashboard shows, as a dict."""
    system = system or System()
    registry = registry or {"apps": {}, "ignore_ports": []}
    started = time.monotonic()
    errors = []

    rc, out, err = system.tailscale("serve", "status", "--json")
    routes = parse_serve(out) if rc == 0 else []
    if rc != 0:
        errors.append(f"tailscale serve status: {err.strip() or out.strip() or rc}")
    rc, out, err = system.tailscale("status", "--json")
    dns, _login = parse_self(out) if rc == 0 else (None, None)

    rc, out, err = system.listeners()
    listeners = parse_lsof_listen(out)
    if rc is None:
        errors.append(f"lsof: {err.strip()}")
    rc, out, _ = system.processes()
    procs = parse_ps(out)
    rc, out, _ = system.launchctl_list()
    listed = parse_launchctl_list(out)
    plists = system.plists()
    # Only the user's own LaunchAgents own an app; any other launchd job above a process (the
    # terminal app it was started from) is reported as "started from".
    by_pid = {row["pid"]: label for label, row in listed.items() if row["pid"] and label in plists}
    by_pid_any = {row["pid"]: label for label, row in listed.items() if row["pid"]}

    by_port = {}
    for lst in listeners:
        if lst["host"] in LOOPBACK or lst["host"] in ANY:
            by_port.setdefault(lst["port"], []).append(lst)

    # Group into apps, keyed by backend.
    apps = {}
    reg_by_port = {e["port"]: app_id for app_id, e in sorted(registry["apps"].items()) if e.get("port")}

    def group(key, port=None):
        if key not in apps:
            apps[key] = {"key": key, "port": port, "routes": [], "registry_id": None}
        return apps[key]

    for route in routes:
        if route["kind"] in ("proxy", "tcp") and route["backend_host"] in LOOPBACK:
            group(("port", route["backend_port"]), route["backend_port"])["routes"].append(route)
        elif route["kind"] in ("proxy", "tcp"):
            group(("remote", route["target"]))["routes"].append(route)
        else:
            group(("static", route["hostport"] + route["path"]))["routes"].append(route)
    for app_id, entry in sorted(registry["apps"].items()):
        key = ("port", entry["port"]) if entry.get("port") else ("registry", app_id)
        app = group(key, entry.get("port"))
        app["registry_id"] = app_id
    ignored = set(registry.get("ignore_ports", []))
    candidates = []
    for port, rows in sorted(by_port.items()):
        if ("port", port) in apps or port in ignored:
            continue
        command = procs.get(rows[0]["pid"], {}).get("command", "")
        if WEB_RUNTIMES.match(runtime_name(command)) and not is_app_bundle(command):
            candidates.append(port)

    pids = {r["pid"] for port in list(reg_by_port) + [a["port"] for a in apps.values() if a["port"]] + candidates
            for r in by_port.get(port, [])}
    rc, out, _ = system.cwds(pids)
    cwds = parse_lsof_cwd(out)

    # Probes run in their own pool: a describe task waits on probes, so sharing one pool could deadlock.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    outer = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    try:
        candidate_probes = {p: pool.submit(system.probe, f"http://127.0.0.1:{p}/") for p in candidates}
        for port, future in candidate_probes.items():
            result = future.result()
            if result["code"] is not None:  # it speaks HTTP: a local web app nobody registered
                group(("port", port), port)["discovered_probe"] = result

        now = system.now()
        git_cache = {}
        futures = [outer.submit(_describe, system, app, registry, by_port, procs, cwds, listed, plists,
                                by_pid, by_pid_any, git_cache, now, self_port, pool, dns)
                   for app in apps.values()]
        described = [f.result() for f in futures]
    finally:
        outer.shutdown(wait=True)
        pool.shutdown(wait=True)

    # One unregistered process on several ports makes several cards with the id of its LaunchAgent: keep them
    # apart by port, so the layout, hiding and `check` tell them apart.  (An unregistered card that shares a
    # registered app's id, a dev instance, keeps it.)
    seen = collections.Counter(a["id"] for a in described if not a["registered"])
    for a in described:
        if seen[a["id"]] > 1 and not a["registered"] and a.get("local") and a["local"].get("port"):
            new = f"{a['id']}-{a['local']['port']}"
            a["name"] = new if a["name"] == a["id"] else a["name"]
            a["id"] = new

    order = {"down": 0, "degraded": 1, "up": 2, "idle": 3}
    described.sort(key=lambda a: (not a["on_tailnet"], order.get(a["status"], 9), a["name"].lower()))
    return {"host": {"name": socket.gethostname().split(".")[0], "dns": dns},
            "scanned": iso(system.now()), "took_ms": round((time.monotonic() - started) * 1000),
            "apps": described, "errors": errors}


def _describe(system, app, registry, by_port, procs, cwds, listed, plists, by_pid, by_pid_any, git_cache, now,
              self_port, pool, dns=None):
    entry = registry["apps"].get(app["registry_id"], {}) if app["registry_id"] else {}
    port = app["port"]
    rows = by_port.get(port, []) if port else []
    pid = rows[0]["pid"] if rows else None
    proc = procs.get(pid, {}) if pid else {}
    command = proc.get("command")
    cwd = cwds.get(pid)
    problems, warnings = [], []

    # LaunchAgent: the one running this process (or an ancestor), else the registry's.
    label = _launchd_label(pid, procs, by_pid) if pid else None
    started_from = None
    if pid and not label:
        outer_job = _launchd_label(pid, procs, by_pid_any)
        started_from = app_from_label(outer_job) if outer_job else None
    if not label and entry.get("launchd"):
        label = entry["launchd"]
    if label and entry.get("launchd") and entry["launchd"] != label:
        warnings.append(f"registry says LaunchAgent {entry['launchd']}, but {label} runs the process")
    launchd = _label_info(system, label, plists, listed) if label else None
    plist = plists.get(label) if label else None

    path_hint = entry.get("path") or (app["routes"][0]["backend_path"] if app["routes"] and
                                      app["routes"][0].get("backend_path") else "/")
    local = None
    local_future = None
    if port:
        scheme = "http"
        if app["routes"] and app["routes"][0].get("backend_scheme") == "https":
            scheme = "https"
        if not app["routes"] or app["routes"][0]["kind"] != "tcp":
            url = f"{scheme}://127.0.0.1:{port}{path_hint}"
            local = {"url": url, "port": port}
            if "discovered_probe" in app:
                local["probe"] = app["discovered_probe"]
            else:
                local_future = pool.submit(system.probe, url)
    # tailscale serve keeps entries keyed by the node's old MagicDNS name after a rename; they no longer resolve.
    def stale(route):
        host = route["hostport"].rpartition(":")[0]
        return bool(dns and host and host != "*" and host.lower() != dns)

    route_futures = [(r, pool.submit(system.probe, r["url"])) if r.get("url") and not stale(r) else (r, None)
                     for r in app["routes"]]

    code_path, code_source = _codebase_path(entry, plist, cwd, command)
    codebase = None
    if code_path:
        if code_path not in git_cache:
            git_cache[code_path] = git_info(system, code_path)
        codebase = dict(git_cache[code_path], source=code_source)
        # A registry repo may be the source of a deployed copy that runs elsewhere.
        live_path = _codebase_path({}, plist, cwd, command)[0] if pid else None
        top = codebase["path"].rstrip("/") + "/"
        if live_path and not (live_path.rstrip("/") + "/").startswith(top):
            codebase["runs_from"] = tilde(live_path)

    if local_future is not None:
        local["probe"] = local_future.result()
    routes = []
    for route, future in route_futures:
        shown = {k: route[k] for k in ("url", "https_port", "path", "kind", "target", "funnel")}
        shown["stale"] = stale(route)
        shown["probe"] = future.result() if future else None
        routes.append(shown)

    # Identity.
    if app["registry_id"]:
        app_id = app["registry_id"]
    elif label:
        app_id = slug(name_from_label(label))
    elif codebase:
        app_id = slug(os.path.basename(codebase["path"]))
    elif command and module_name(command):
        app_id = slug(module_name(command))
    elif port:
        app_id = f"port-{port}"
    else:
        app_id = slug(app["key"][1])
    name = entry.get("name") or app_id

    # Status.
    running = bool(rows)
    local_ok = answered(local.get("probe")) if local else None
    routes_ok = all(answered(r["probe"]) for r in routes if r["probe"] is not None)
    if app["routes"] and app["routes"][0]["kind"] == "tcp":
        local_ok = running
    # An on-demand app may stay published between runs: nothing listens on its port, and tailscale serve
    # answers 502 for each of its tailnet URLs.  That is stopped as expected; any other answer is not.
    probed = [r["probe"] for r in routes if r["probe"] is not None]
    published_stopped = bool(probed) and all(p.get("code") == 502 for p in probed)
    if port is None and not app["routes"]:
        status = "idle"
        problems.append("registry entry has no port")
    elif port is None:
        status = "up" if routes_ok else "down"
    elif local_ok:
        status = "up" if routes_ok else "degraded"
    elif not running and entry.get("on_demand") and (not app["routes"] or published_stopped):
        status = "idle"
    else:
        status = "down"

    if local and local.get("probe") and not local_ok and status != "idle":
        p = local["probe"]
        problems.append(f"{local['url']} " + (f"answered {p['code']}" if p["code"] else p["error"]))
        if not running:
            problems.append(f"nothing listens on port {port}")
    for r in routes:
        p = r["probe"]
        if p is not None and not answered(p) and status != "idle":  # idle: each answered 502 (see above)
            problems.append(f"{r['url']} " + (f"answered {p['code']}" if p["code"] else p["error"]))
    https_port = entry.get("https_port")
    if https_port and not any(r["https_port"] == https_port for r in routes):
        problems.append(f"not published on tailnet :{https_port} "
                        f"(tailscale serve --bg --https={https_port} http://127.0.0.1:{port})")
        if status == "up":
            status = "degraded"
    if launchd:
        if not launchd["loaded"] and launchd["plist"]:
            problems.append(f"LaunchAgent {label} is not loaded (launchctl bootstrap gui/{os.getuid()} {launchd['plist']})")
        elif not launchd["plist"]:
            problems.append(f"LaunchAgent {label} has no plist in ~/Library/LaunchAgents")
        if launchd.get("last_exit") not in (None, "0") and not launchd.get("pid"):
            problems.append(f"LaunchAgent {label} last exited {launchd['last_exit']}")
        logs = launchd.pop("_logs", [])
        if status in ("down", "degraded") or problems:
            launchd["log_tail"] = {tilde(p): [redact(line) for line in system.log_tail(p) or []] for p in logs}
    elif routes and running:
        warnings.append("no LaunchAgent: it will not come back after a crash or reboot")
    if any(r["host"] in ANY for r in rows):
        warnings.append(f"listens on all interfaces (port {port}), not just loopback: reachable without the tailnet")
    # Entries under the node's old name are left out of the links (the page lists them under Details).
    if any(r["funnel"] and not r["stale"] for r in routes):
        warnings.append("Funnel is on: public on the internet")
    elif any(r["funnel"] for r in routes):
        warnings.append("Funnel is set, but only on a stale name, so it is probably not reachable")
    if codebase and codebase.get("git"):
        if codebase.get("dirty"):
            warnings.append(f"{codebase['dirty']} uncommitted change{'s' if codebase['dirty'] != 1 else ''} in {codebase['display']}")
        if codebase.get("ahead"):
            warnings.append(f"{codebase['ahead']} commit{'s' if codebase['ahead'] != 1 else ''} not pushed to {codebase['upstream']}")
        if codebase.get("behind"):
            warnings.append(f"{codebase['behind']} commit{'s' if codebase['behind'] != 1 else ''} behind {codebase['upstream']} (as of the last fetch)")
        if (proc.get("started") and codebase.get("committed_epoch") and not codebase.get("runs_from")
                and codebase["committed_epoch"] > proc["started"] + 5):
            warnings.append(f"running since before HEAD {codebase.get('head')} was committed: restart to pick it up")

    process = None
    if pid:
        process = {"pid": pid, "command": redact(command), "cwd": tilde(cwd) if cwd else None,
                   "started": iso(proc.get("started")),
                   "uptime_s": round(now - proc["started"]) if proc.get("started") else None,
                   "listen": sorted({f"{r['host']}:{r['port']}" for r in rows}), "started_from": started_from}
    if codebase:
        codebase.pop("committed_epoch", None)
    return {"id": app_id, "name": name, "description": entry.get("description"), "registered": bool(app["registry_id"]),
            "on_demand": bool(entry.get("on_demand")), "self": bool(self_port and port == self_port),
            "status": status, "problems": problems, "warnings": warnings, "routes": routes,
            "on_tailnet": any(not r["stale"] for r in routes),
            "local": local, "process": process, "launchd": launchd, "codebase": codebase}
