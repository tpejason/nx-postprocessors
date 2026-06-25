# systemd services — dashboard web apps auto-start on boot

The dashboards that run a **separate web-app process** (not embedded in the
postprocessor) are not reboot-persistent on their own — they were started with
`nohup` and die on reboot. These units make them auto-start on boot and
auto-restart on crash (`Restart=always`).

> The `dice` dashboard (8081) needs **no** service: its web server is embedded in
> the postprocessor, which the Nx mediaserver launches automatically.
>
> The `stress` dashboard (8120) is intentionally **not** a service — start it
> manually when needed.

## Units

| Unit | Port | User | ExecStart |
|------|------|------|-----------|
| `nx-dash-web-advance.service` | 8112 | networkoptix | `…/postprocessors/web-dashboard-advance-app.py --port 8112` |
| `nx-dash-gauge.service` | 8113 | nx | `/home/nx/gauge_web_app.py --port 8113` |
| `nx-dash-parking.service` | 8114 | nx | `/home/nx/parking_web_app.py --port 8114` |

Paths/User above match the DELL Nx Witness box (`/opt/networkoptix`, web apps for
gauge/parking deployed under `/home/nx`). **Adjust `ExecStart`, `WorkingDirectory`
and `User` per host** before installing (e.g. Nx Meta uses
`/opt/networkoptix-metavms`).

## Install

```bash
sudo cp nx-dash-*.service /etc/systemd/system/
sudo systemctl daemon-reload

# stop any manual (nohup) copy first so the service can bind the port
sudo pkill -f web-dashboard-advance-app.py

sudo systemctl enable --now nx-dash-web-advance nx-dash-gauge nx-dash-parking
```

## Verify / manage

```bash
systemctl status  nx-dash-web-advance        # state, recent logs
systemctl restart nx-dash-gauge              # restart one
journalctl -u nx-dash-parking -n 50          # logs
```

`Restart=always` is validated by killing the process — systemd respawns it
within `RestartSec` (5s).
