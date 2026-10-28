"""A per-user LaunchAgent that keeps ``serve`` running (RunAtLoad + KeepAlive).

The plist runs this checkout with the Python that installed it, so reinstall
after moving the repository or changing Python.  Logs go to
``~/Library/Logs/apps-dashboard.log``.  The label is ``local.apps-dashboard``
unless ``$APPS_DASHBOARD_LABEL`` names another (for example ``com.<you>.apps-dashboard``).

``install`` over a loaded job: an unchanged plist is just restarted (``kickstart -k``).  A changed
one is unloaded first; ``bootout`` returns before the old server has exited, and ``bootstrap`` fails
("5: Input/output error") until it has, so install waits for the job to be gone and retries.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

LABEL = os.environ.get("APPS_DASHBOARD_LABEL") or "local.apps-dashboard"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
LOG = Path("~/Library/Logs/apps-dashboard.log").expanduser()
REPO = Path(__file__).resolve().parent.parent
GONE_TIMEOUT = 20.0  # seconds to wait for a booted-out job to go away
BOOTSTRAP_TRIES = 5


def _domain():
    return f"gui/{os.getuid()}"


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _loaded():
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def plist(serve_args):
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "apps_dashboard", "serve", *serve_args],
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


def install(serve_args, sleep=time.sleep, clock=time.monotonic):
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    content = plistlib.dumps(plist(serve_args))
    runs = f"  runs: {' '.join(plist(serve_args)['ProgramArguments'])}\n  log:  {LOG}"
    loaded = _loaded()
    try:
        unchanged = PLIST.read_bytes() == content
    except OSError:
        unchanged = False
    if loaded and unchanged:
        result = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
        if result.returncode == 0:
            print(f"{PLIST} unchanged; restarted {LABEL}\n{runs}")
            return 0
    PLIST.write_bytes(content)
    if loaded:
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        deadline = clock() + GONE_TIMEOUT
        while _loaded() and clock() < deadline:
            sleep(0.5)
    for attempt in range(BOOTSTRAP_TRIES):
        result = _launchctl("bootstrap", _domain(), str(PLIST))
        if result.returncode == 0:
            break
        if attempt + 1 < BOOTSTRAP_TRIES:
            sleep(2)
    else:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        print(f"load it by hand: launchctl bootstrap {_domain()} {PLIST}", file=sys.stderr)
        return result.returncode
    print(f"installed {PLIST}\n{runs}")
    return 0


def restart():
    result = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or f"{LABEL} is not loaded", file=sys.stderr)
        return result.returncode
    print(f"restarted {LABEL}")
    return 0


def uninstall():
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    if PLIST.exists():
        PLIST.unlink()
        print(f"removed {PLIST}")
    else:
        print(f"{PLIST} was not installed")
    return 0


def status():
    result = _launchctl("print", f"{_domain()}/{LABEL}")
    if result.returncode != 0:
        print(f"{LABEL} is not loaded")
        return 1
    keep = ("state =", "pid =", "last exit code", "program =", "runs =")
    for line in result.stdout.splitlines():
        if any(k in line for k in keep):
            print(line.strip())
    return 0


def run(action, serve_args):
    if action == "install":
        return install(serve_args)
    if action == "uninstall":
        return uninstall()
    if action == "restart":
        return restart()
    return status()
