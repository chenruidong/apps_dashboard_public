---
name: apps-dashboard
description: Keep this Mac's apps dashboard current. It is the owner-only page at the node's bare tailnet URL listing every web app the Mac serves, with status, links, codebase and LaunchAgent. Use whenever you create, publish, restart, update, move or remove a website or web app on this Mac that is served over the tailnet (`tailscale serve`) or runs as a local web server under a LaunchAgent. After the change, register the app and check it is up. Also use when the user asks what this machine serves, for the dashboard link, or whether a tailnet site is up.
---

# apps-dashboard: register, then check

The dashboard discovers apps live: each scan reads `tailscale serve`, listening ports, `ps`, LaunchAgents
and git, so status updates by itself. What needs you is the **registry**
(`~/.config/apps-dashboard/registry.json`, machine-local): names, descriptions, expected tailnet ports, the
checkout when it differs from where the process runs, and apps that are not running. Without it, a new app
appears as "not registered" with a guessed name, and a stopped app may not appear at all. Then **check**: the
same scan from the terminal tells you whether the change worked.

Run every command from the dashboard checkout (`~/code/apps-dashboard` unless the user keeps it elsewhere):

```bash
~/code/apps-dashboard/apps-dashboard check electrogui
```

## When you create or publish a site

1. Before choosing ports, run `apps-dashboard scan` to see the loopback and tailnet https ports apps already
   use, and `lsof -nP -iTCP:<port> -sTCP:LISTEN` to confirm a candidate is free. The bare URL (https port 443)
   belongs to the dashboard. Give each app its own https port; by convention https port = loopback port.
2. Serve on 127.0.0.1 only. Keep Host-header and same-origin checks: they stop DNS rebinding and cross-site
   writes. An app that needs to know the caller reads `Tailscale-User-Login`, which serve sets and strips from
   clients. Publish it with `tailscale serve --bg --https=<p> http://127.0.0.1:<port>`. A persistent site gets a
   LaunchAgent (RunAtLoad + KeepAlive, log in `~/Library/Logs/<name>.log`); without one the dashboard warns.
3. Register it:
   ```bash
   apps-dashboard register <id> --port <loopback port> --name "<Name>" --description "<one line: what it is for>"
   ```
   `<id>` is a lowercase slug, normally the repo name. When the app is running, `register` fills in `repo`,
   `launchd` and `https_port` and prints what it detected. Pass them explicitly (`--repo`, `--launchd`,
   `--https-port`) when it is not running yet, or when it runs a deployed copy outside its checkout. Use
   `--on-demand` for an app that is normally stopped; it then shows as idle rather than down.
4. `apps-dashboard check <id> --wait 20`, then give the user both the app's tailnet URL and the dashboard URL.

## When you restart or update one

- After `launchctl kickstart -k gui/$(id -u)/<label>`, a reinstall, or a restart of any kind:
  `apps-dashboard check <id> --wait 20`.
- After new code lands in the checkout an app runs from, check it too. The warning "running since before
  HEAD … was committed: restart to pick it up" means the process still runs the old code. Restart it, unless
  the user said not to; then tell them.
- If the port, path, LaunchAgent label or checkout changed, re-run `register <id>` with the new values
  (`--launchd ""` etc. removes a field) before `check`.
- If you changed apps-dashboard itself: run its tests, then `apps-dashboard launchagent restart`, then
  `apps-dashboard check apps-dashboard --wait 20`.

## When you remove one

Unpublish it (`tailscale serve --https=<p> off`) and stop or uninstall its LaunchAgent. Then
`apps-dashboard unregister <id>`, and run `apps-dashboard scan` to confirm it is gone.

## Reading `check`

Exit 0 means up (or idle, for an on-demand app). Exit 1 means at least one `✗` problem, for example:
the backend does not answer; the tailnet URL answered 502 (`tailscale serve` with nothing behind it); not
published on the registered https port (the fix command is printed); the LaunchAgent is not loaded, or its
last exit was non-zero (`check` prints the log paths; read the log). Fix the problem and check again.

`!` lines are warnings and do not fail the check: uncommitted or unpushed code, a stale process, no
LaunchAgent, a listener on all interfaces, Funnel on. Mention the ones that matter to the user. `--strict`
makes warnings fail too.

## Rules

- Never put a secret (a `?token=` URL, a password) in `--path`, `--description` or `--name`: the page displays
  them.
- The dashboard re-reads the registry on every scan. Do not restart it after `register`; the page shows the
  change within 30 s, or at once with Refresh.
- Do not hand-edit `registry.json` while scripting; use `register` / `unregister` / `ignore <port>`
  (`ignore` hides an unregistered local listener that is not an app).
- Never bind a site to 0.0.0.0 or a LAN address, and never turn on Funnel unless the user asks.
