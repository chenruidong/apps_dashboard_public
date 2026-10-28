# Example: putting ElectroGUI on the dashboard

[ElectroGUI](https://github.com/chenruidong/electrogui) is a browser-based session explorer for
neurophysiology recordings (rasters, PSTHs, population views). It is a good first app to register: a Python
web server that listens on loopback, so it follows the pattern every app here should follow.

The steps are the same for any app: run it on `127.0.0.1`, keep it alive with a LaunchAgent, publish it with
`tailscale serve`, register it, check it.

## 1. Install and run it once

Clone it and install it as its own README says, then start it by hand to see that it works:

```bash
git clone https://github.com/chenruidong/electrogui ~/code/electrogui
cd ~/code/electrogui
python -m electro_gui --no-browser --port 8765
```

It listens on `127.0.0.1:8765` only. Stop it with Ctrl-C once it answers.

## 2. Keep it running with a LaunchAgent

[`local.electrogui.plist`](local.electrogui.plist) runs it with RunAtLoad and KeepAlive and logs to
`~/Library/Logs/electrogui.log`. From the dashboard checkout, fill in the Python that has ElectroGUI
installed and the checkout, then load it:

```bash
PY="$(cd ~/code/electrogui && python -c 'import sys; print(sys.executable)')"
sed -e "s#__PYTHON__#$PY#" -e "s#__CHECKOUT__#$HOME/code/electrogui#" -e "s#__HOME__#$HOME#g" \
  examples/electrogui/local.electrogui.plist > ~/Library/LaunchAgents/local.electrogui.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.electrogui.plist
```

## 3. Publish it on the tailnet

```bash
tailscale serve --bg --https=8765 http://127.0.0.1:8765
```

The convention here is https port = loopback port. Never use Funnel and never bind the app to `0.0.0.0`.

## 4. Register it and check it

From the dashboard checkout:

```bash
./apps-dashboard register electrogui --port 8765 --name "ElectroGUI" \
  --description "Session explorer: rasters, PSTHs, population views"
./apps-dashboard check electrogui --wait 20
```

With the app running, `register` finds its checkout, LaunchAgent and https port by itself and prints what it
found. `check` exits 0 once the app answers locally and over the tailnet. The card then shows up on the
dashboard within 30 seconds.

## A second, on-demand instance

The same checkout can also back a local instance that you start only when you need it. Register it with
`--on-demand` so the dashboard shows it as idle, not down, while it is stopped:

```bash
./apps-dashboard register electro_gui_local --port 8766 --repo ~/code/electrogui --on-demand
```

## Updating it

After pulling new code into `~/code/electrogui`, restart it and check it again:

```bash
launchctl kickstart -k gui/$(id -u)/local.electrogui
./apps-dashboard check electrogui --wait 20
```

Until it is restarted, its card warns "running since before HEAD … was committed: restart to pick it up".
