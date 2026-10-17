# Tapo Dashboard (local)

Controls your Tapo devices over the LAN with [python-kasa](https://github.com/python-kasa/python-kasa). Nothing goes through the Tapo cloud after login.

## Run

```bash
cd ~/dev/tapo-dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in TAPO_USER / TAPO_PASS
python discover.py          # sanity check: lists devices, H100 children, features
python app.py               # open http://<this-machine-ip>:8000
```

## Run with Docker (home server)

Runs on port **8811** with host networking (Linux only), which LAN discovery of Tapo and Tuya devices needs. Config and state live in `./data`:

```bash
# from this Mac: copy the code, then the config + state into data/
rsync -av --exclude .venv --exclude .git --exclude .idea --exclude __pycache__ \
  --exclude '*.json' --exclude .env ./ user@server:~/tapo-dashboard/
ssh user@server mkdir -p tapo-dashboard/data
scp .env devices.json tinytuya.json .smartthings_tokens.json .tuya_energy.json .tuya_energy_hourly.json \
  user@server:~/tapo-dashboard/data/        # plus actions.json if you have one

# on the server
cd ~/tapo-dashboard
docker compose up -d --build   # open http://<server-ip>:8811
docker compose logs -f
```

- `PORT` in `data/.env` is ignored; set `DASHBOARD_PORT=...` in the shell (or a `.env` next to `docker-compose.yml`) to change it.
- The container runs as UID/GID 1000. If `data/` belongs to another user, set `DASHBOARD_UID`/`DASHBOARD_GID` (`id -u`, `id -g`).
- Run only one dashboard at a time: two instances polling the same plug log each other out (403 errors) and the Tuya switch only accepts one local connection.
- Update: re-run the `rsync` line, then `docker compose up -d --build` on the server.

## Features

- Bulbs, plugs and other switchable devices: on/off toggle, plus a brightness slider when the bulb is dimmable.
- H100 hub: paired devices are grouped under it. Sensor values (battery, temperature, humidity…) show as chips.
- S200B buttons: the hub is polled every `HUB_POLL_SECONDS`. New trigger-log entries show up in the "Button presses" feed and the card flashes.
- Automations: copy `actions.example.json` to `actions.json` and map button alias → event → action. The supported actions are `toggle`, `on`, `off` (one alias or a list) and `"all_off"`. The file is reloaded on each press, so you don't need to restart. Event names come from the hub (e.g. `singleClick`, `doubleClick`, `rotation`); check the feed to see the exact names.
- Cameras (yi-hack): set `CAMERAS=Name=IP` in `.env`. The backend fetches the camera's `/cgi-bin/snapshot.sh` and the page refreshes it every `SNAPSHOT_SECONDS`. Click a camera to open **live video** in a modal (HD/SD toggle). The backend starts `ffmpeg` (`brew install ffmpeg`) on demand, turns the camera's RTSP stream into MJPEG (`LIVE_FPS`, `LIVE_MAX_WIDTH`), shares it between viewers and stops it 10 s after the last one closes. Without ffmpeg, the modal falls back to HD snapshots. The RTSP URL is shown on the card for VLC. Use `Name=http://user:pass@IP` if you set a web UI password; the credentials stay on the server.
- Samsung washer/dryer (SmartThings cloud, they have no local API): set `SMARTTHINGS_TOKEN` in `.env`. Status is polled every 30 s and shows state, phase, minutes left and a progress bar. `started` and `finished` events go to the Activity feed and work in `actions.json`, e.g. `"Washer": {"finished": {"toggle": "Living room bulb"}}`. New personal access tokens expire after 24 h. On a 401 the backend re-reads `.env`, so pasting a fresh token there is enough.
- **All off** scene and **Rediscover** button. Devices listed in `CONFIRM_OFF` (e.g. a fridge plug) ask for confirmation before switching off and are skipped by All off.
- Each device is polled on its own task with backoff, so one offline device doesn't slow down the others.

## API

| Method | Path | Body |
|---|---|---|
| GET | `/api/devices` | – |
| GET | `/api/events` | – |
| POST | `/api/devices/{id}/toggle` | – |
| POST | `/api/devices/{id}/state` | `{"on": true}` |
| POST | `/api/devices/{id}/brightness` | `{"value": 60}` |
| POST | `/api/scenes/all-off` | – |
| POST | `/api/discover` | – |
| GET | `/api/appliances` | – |
| GET | `/api/cameras` | – |
| GET | `/api/cameras/{id}/live?q=hd\|sd` | – (MJPEG stream) |
| GET | `/api/cameras/{id}/snapshot?res=low\|high` | – (JPEG) |
| WS | `/ws` | pushes `devices`, `events`, `event` messages |

## Troubleshooting

- **No devices**: broadcast discovery doesn't cross VLANs or Docker bridge networks. Set `TAPO_HOSTS=192.168.1.20,...` (include the H100) and give the devices DHCP reservations.
- **Auth errors**: use your Tapo app email and password. Quote any special characters in `.env`.
- **Empty S200B cards / no presses**: run `python discover.py` and check whether the button lists `trigger logs`. If it doesn't, your python-kasa version may name the module differently.
- **Pin versions**: firmware or library updates can break the local protocol. Test before you upgrade.

## SmartThings auto-renewing login (OAuth)

Personal access tokens die after 24 h. With OAuth, the backend refreshes the access token before it expires and stores the rotated refresh token in `.smartthings_tokens.json`. You log in once.

1. Install the SmartThings CLI: `npm install -g @smartthings/cli`
2. `smartthings apps:create` → choose **OAuth-In App**. Give it any name, pick the scope `r:devices:*`, and add the redirect URI `https://httpbin.org/get`.
3. Copy the **OAuth Client Id** and **Client Secret** it prints (the secret is shown only once) into `.env` as `SMARTTHINGS_CLIENT_ID` / `SMARTTHINGS_CLIENT_SECRET`, then restart.
4. On the dashboard, under Appliances, click **Open SmartThings login** and allow access. You'll land on httpbin.org with `code=…` in the address. Paste that whole address into the box and press **Connect**.

If the dashboard is off for more than ~29 days, the refresh token expires and the Connect box comes back.

## Immersion heater (Tuya / Smart Life, local)

1. Run `python -m tinytuya wizard` in this folder. It writes `devices.json` (git-ignored, holds the local keys).
2. Restart. Every device in `devices.json` gets a card under Appliances.

- **Boost 30/60/90**: turns the heater on and sets the plug's *own* countdown, so it switches off on time even if the dashboard is down. **Cancel timer** clears it.
- **Status**: *Heating* (drawing > 300 W), *Hot water ready* (the switch is on but the tank thermostat has cut the element), *On*, *Off*.
- **Energy**: worked out from live power, stored per day in `.tuya_energy.json`. Set `ELECTRICITY_PRICE` to show cost.
- **Safety cap**: `HEATER_MAX_ON_MINUTES` (default 180) switches it off if it stays on without a timer.
- **Events / actions**: `heating`, `hot_water_ready`, `safety_off` (in Activity) plus `on`/`off`. In `actions.json`, `{"boost": {"Agua caliente": 30}}` works from any trigger, e.g. an S200B double press.
- If the plug moves IP, it's found again by broadcast after a few failed polls. Re-run the wizard if you re-pair it in Smart Life, since that changes its key.

## Camera recordings (FTP)

The film icon on a camera opens its recordings, read from the camera's FTP server (yi-hack's `record/` folder: hour folders such as `2026Y10M09D14H/` holding clips such as `05M00S60.mp4`).

- Pick a day and hour on the left, then click a clip. **Prev/Next** (or ← →) step through clips, crossing into the next hour. **Play continuously** goes on to the next clip by itself.
- The backend streams clips from FTP with HTTP range support, so seeking works. At most 3 FTP connections are open at once.
- Settings (all optional): `CAMERA_FTP_USER` (default `root`), `CAMERA_FTP_PASS` (default empty), `CAMERA_RECORD_DIR` (detected automatically).
- Troubleshooting: `GET /api/debug/camera-ftp/<camera-id>` shows what the FTP server exposes.
