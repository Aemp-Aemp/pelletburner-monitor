# AI.md - help for AI assistants

You are an AI assistant (ChatGPT, Claude, Copilot or similar) helping a person install and run the pellet
boiler monitor in this repository. Read this file first. Use short, plain sentences with the person. When
this file and the code disagree, the code is right; the full user guide is `docs/guide.html`.

## 1. What this is

A small program and web page for a home pellet boiler (10, 15 or 25 kW). It runs on a Raspberry Pi in Docker.
It connects to the boiler controller's module port (J4) with a USB cable, saves the history and shows it on a
web page for phone or PC (English or Danish): live state, heating plan, pellets, ash, burner health, history.
It works offline. Optional extras: phone alerts (ntfy), Home Assistant (MQTT), outside temperature (Open-Meteo).

**Hardware:**

- Raspberry Pi 4 (or any Linux PC with Docker), a good 5 V / 3 A USB-C power supply, a microSD card.
- A USB-RS-485 adapter with automatic direction control (FTDI, CH340, CP210x and similar).
- A network cable with one end cut off, for the module port J4. J4 is RS-485, not Ethernet: never plug it
  into a router or PC. Wiring (T568B): pin 1 white-orange = B (D-), pin 2 orange = A (D+), pin 3
  white-green = GND. Tape off all other wires. Switch the boiler off before plugging in: part of the board
  carries 230 V.
- On the boiler: AVANCERET -> EKSTRA UDSTYR -> IO BOKS on. The monitor uses the same connection as the
  boiler's own Wi-Fi / IO box module.
- Home Wi-Fi (or a network cable to the router) for the Pi.

**Supported controller software** (check on the boiler: MENU -> FLERE -> INFO):

- 1.30 or newer works. 1.40 is tested. 1.40.1 and 1.41 should work.
- Older than 1.30 (for example 1.13) is not supported: it has no Wi-Fi / IO box connection. Updating the
  controller software is a job for the dealer.
- Hardware V3 and V4.

## 2. Safety rules (follow them, and tell the person)

1. **Do not act on the boiler for the person.** Never change boiler settings, and never send start or stop
   (web page, Home Assistant, phone command) unless the person clearly asks for it.
2. **Alarms mean: go and look at the boiler.** An alarm is a reason for a person to check the boiler on site.
   Never reset an alarm unless the person is at the boiler. Do not suggest turning on the automatic alarm reset
   to hide a problem.
3. **Never bypass safety devices** (thermal safety switch, sensors, fuses, alarms).
4. **What is on by default:** the monitor shows data and keeps the module link alive. The Start/Stop buttons
   exist (`BURNER_REMOTE_START=1` in Docker; set `0` for view only). The heating plan, automatic alarm reset
   and phone commands are off until the person turns them on. The setup guide never starts, stops or changes
   the boiler.
5. **Hobby project.** It is not made, checked or supported by the boiler's maker. No warranty (MIT license).
6. Only one program may talk to the module port at a time.

## 3. Install from zero

### Easy way (SD card, no typing)

This path is new and not tested on a real card yet. The manual way below is the tested one.

1. Install **Raspberry Pi Imager**. Choose *Raspberry Pi OS Lite (64-bit)*. In Imager's settings set host name
   `pelletpi`, a user name and password, the home Wi-Fi and the time zone. Write the card.
2. Unzip `pelletpi-card.zip` (from the releases page) onto the card's boot drive (`bootfs`): the folder
   `pelletpi` and the file `vendor-data`.
3. Put the card in the Pi, plug the USB adapter into the boiler's J4 cable, switch the Pi on.
4. Wait about 15 minutes. The Pi needs internet the first time. Then open **http://pelletpi.local** on a
   phone or PC on the same Wi-Fi. Progress: that page while it installs, or `pelletpi-status.txt` on the card.

### Wi-Fi setup hotspot (wrong Wi-Fi password, new router)

When the Pi has no network for 3 minutes, it opens an open Wi-Fi called **`pelletpi-setup`**. Join it with a
phone, open **http://10.42.0.1:8099**, pick the home Wi-Fi and type its password. The hotspot goes off when the
Pi is on the Wi-Fi, or after 15 minutes. A network cable to the router also works. (New, not tested on a real Pi.)

### The Pi lost a Wi-Fi it had (Wi-Fi watchdog)

Seen once: the Pi's Wi-Fi chip had a scan error (`brcmf_cfg80211_scan: scan error (-52)`), a few joins to a weak
phone hotspot failed, then NetworkManager marked the connection failed (`no-secrets`) and never tried again.
Now (new, not tested on a real Pi): the known Wi-Fi connections get `connection.autoconnect-retries 0` (try
forever) and `802-11-wireless.powersave 2` (power save off), plus `/etc/NetworkManager/conf.d/pelletpi-wifi.conf`;
a watchdog (`pelletpi-wifi-watchdog.timer`, every minute) joins the known Wi-Fi again after 3 min, turns the radio
off/on after 5 min, reloads the `brcmfmac` driver when scans keep failing, and lets `pelletpi-setup` open after
10 min. Events: `data/wifi-watchdog.jsonl`. Off: `WIFI_WATCHDOG=no` in `pelletpi/settings.txt`. An existing Pi gets
it with the next update, or `sudo sh pi/install-wifi-watchdog.sh`. Still lost: USB-C cable, `ssh pi@10.55.0.1`,
`journalctl -u NetworkManager -b`, `dmesg | grep brcmf`; check with `sudo /sbin/iw dev wlan0 get power_save`
(iw is in /sbin). Restart the Pi only when the boiler is off or waiting.

### Manual way (tested)

1. Write the card with Imager as above (host name `pelletpi`, user, Wi-Fi, time zone, and SSH on).
2. Find the Pi: `ping pelletpi.local`, or look in the router's list of devices. Without Wi-Fi, the Pi's
   USB-C port can work as a network cable to a PC (Pi at `10.55.0.1`, see `pi/README.md`).
3. Log in: `ssh <user>@pelletpi.local`
4. Get the program and start it:
   ```bash
   sudo apt-get install -y git
   git clone https://github.com/Aemp-Aemp/pelletburner-monitor.git pelletburner-monitor
   cd pelletburner-monitor
   sudo sh pi/install.sh
   ```
   `pi/install.sh` installs Docker, starts the monitor, puts the page on port 80, and sets up Pi health
   logging, the Wi-Fi hotspot and the safe nightly auto-update. Safe to run again. Docker only:
   `docker compose up -d --build`.
5. Open **http://pelletpi.local** (or `http://pelletpi.local:8787`, or `http://<pi-address>:8787`).
   The classic, more technical page is `/classic`; the guide is `/guide`.

### First start

The first time the page opens, a short setup guide shows. Every step has Back, Skip and Next; all can be
changed later in Settings. Steps: language (English / Danish), welcome (shows if the boiler is connected),
your boiler (size 10/15/25 kW, software version, minimum power %, fan rpm at 100 %), outside temperature
(optional, pick a town), pellets (store or silo size in kg, kg at home now), ash bin, heating plan (saved but
off until turned on on the Plan page). Run it again: Settings -> Service & relearn -> Run the setup again.

Settings are kept in `data/settings.json` on the Pi and are shared by every phone and PC. The page saves them
through `/api/settings`. Changes in Settings are saved at once.

## 4. Everyday use

- **Heating plan** (Plan page): "warm by 05:30", "keep warm 17:15-19:30", "start at 09:00". Off until turned
  on. It uses the Pi's clock (see power cuts).
- **Phone alerts** (Settings -> Phone alerts): install the free **ntfy** app, subscribe to the alerts topic
  shown on the page (Copy button or QR code), turn alerts on, press Send test. The topic works like a password.
  The answer to `status` has a small picture of today's graph (on by default, can be turned off; 07:00-22:00, at most one an hour).
- **Start/stop from the phone** (optional, off): needs alerts on and a PIN (4-6 digits). Subscribe to the
  second topic, the **command topic**. Send `start` or `stop`; the monitor answers with a one-time code; reply
  `<code> <PIN>` within 2 minutes. `status` needs no PIN. 3 wrong tries lock it for 15 minutes. A boiler in
  alarm is refused; the phone never resets an alarm.
- **Settings without code changes:** copy `.env.example` to `.env`, remove `#` in front of a line, then
  `docker compose up -d`. Home Assistant: `BURNER_MQTT`, `BURNER_MQTT_USER`, `BURNER_MQTT_PASS` (see README).
- **Updating:** after the easy install or `pi/install.sh`, the Pi installs new releases by itself at night,
  but only while the burner is off or waiting. From the page: Settings → About this monitor → **Check for
  update** (works without the nightly update; no internet: share it from a phone hotspot the Pi knows). The new
  version is tested after the swap; if it fails the Pi goes back by itself, and **Go back** is there for 14
  days. Now: `sudo pelletpi-update now`, back: `sudo pelletpi-update back`. Off: `AUTO_UPDATE=no` in
  `pelletpi/settings.txt` on the card. The page can only ask for check / retry / back (`data/update-request`,
  one word); state for the page is `data/update.json` (`burner_ext.update_info`). By hand in a git copy, while the burner is off or waiting:
  `git pull`, then `docker compose build`, then `docker compose up -d`. Build first: when the boiler hears
  nothing for about 50 s it shows alarm 15 "External IO", which must be reset on the boiler.
- **Backups:** everything is in `~/pelletburner-monitor/data/` on the Pi: `settings.json` (+ `.bak`),
  history `hist-*`, `raw-*`, `events-*`, `ignitions.jsonl`, `pi-health-*`, `responder.log`, alert state and
  certificates. Copy it: `scp -r <user>@pelletpi.local:pelletburner-monitor/data ./burner-data`.
  History is kept 30 days by default (`BURNER_KEEP_DAYS`). Updates keep `data/`.
- **Power cuts:** tolerated. Broken or half-written data lines are skipped; settings and alert state fall back
  to their `.bak`. The Pi 4 has no clock battery: after a restart without internet its clock is behind until
  the internet is back, so plan times can be off.

## 5. Troubleshooting

Run these on the Pi, in `~/pelletburner-monitor`:

- `docker compose ps` - is the container running?
- `docker compose logs --tail 200` - the program's log (also `data/responder.log`).
- `curl -s http://localhost:8787/status.json` - live data. `"connected": true` = data from the boiler in the
  last 60 s. `"version"` = monitor version.
- `data/events-YYYY-MM-DD.jsonl` - starts, stops, alarms. `data/pi-health-*.jsonl` - clock, power, Wi-Fi,
  USB adapter, once a minute. `data/update.log` - auto-update.

| Problem | Check |
|---|---|
| No data on the page | IO BOKS on? Cable in J4? Swap A and B. Only one USB-serial adapter, or set `BURNER_SERIAL` in `.env` (`ls /dev/serial/by-id/`). |
| Can't find the Pi | Same Wi-Fi as the phone? Try `:8787`, or the Pi's address from the router's device list instead of `pelletpi.local`. |
| Wi-Fi lost | The watchdog reconnects; after 10 minutes join `pelletpi-setup`, open http://10.42.0.1:8099. Or the USB-C cable (10.55.0.1) or a network cable. Events: `data/wifi-watchdog.jsonl`. |
| Page shows old data | Check `"connected"` in `/status.json` and `docker compose ps`. Reload the page. |
| Wrong numbers | Check Settings -> My boiler (size, min power %, fan rpm) matches the boiler, and the pellet store size (Settings -> Pellets & ash). Pellets at home drift: type the counted kg on the page again. |
| Plan times off | The Pi's clock (see power cuts); `clock_synced` in `pi-health-*`. |
| Alerts not arriving | Alerts on? Phone subscribed to the right topic? Press Send test. Pi has internet? Certificate errors: check the Pi's clock first. |
| Alarm 15 "External IO" | The monitor was silent about 50 s (stopped, unplugged, updating). Reset it on the boiler. |
| Weak power | `undervolt` in `pi-health-*`: use a 5 V / 3 A supply. |

Boiler alarms and what to check: `docs/guide.html`, section *Troubleshooting*.
Bug reports: see `CONTRIBUTING.md` (remove passwords, ntfy topics, addresses and Wi-Fi names first).

## 6. For developers and AI changing the code

**Main files:**

| File | What |
|---|---|
| `burner_responder.py` | Module port link, web server and `/api/*`, `/status.json` |
| `burner_features.py` | Settings (`DEFAULTS`, `data/settings.json`), heating plan, pellets, ash, health, weather |
| `burner_ext.py` | History files, MQTT / Home Assistant, `MONITOR_VERSION` |
| `burner_notify.py` / `burner_phone.py` | Phone alerts / phone start-stop with PIN |
| `burner_graph.py` | Today's graph as a PNG for the `status` answer (Pillow) |
| `burner_common.py` | State and alarm names, shared helpers |
| `scada/home.html`, `scada/dashboard.html` | The main page (`/home`) and the classic page (`/classic`) |
| `docs/guide.html` | The user guide (`/guide`), one offline file |
| `pi/` | Card first boot, `install.sh`, Wi-Fi hotspot, auto-update, `pi_health.py` |
| `tools/replay.py` | Run the page on saved data, no boiler (see `tools/REPLAY.md`) |

**Rules:**

- Tests: `python scada/run_tests.py` (all), or `python scada/run_tests.py <word>`. Browser tests need
  Playwright, else they are skipped. Card scripts: `python pi/_card_test.py`. New Python code gets a test.
- The state/alarm tables and `scada/shared.js` are copied into both pages. Never edit those blocks in the
  pages: change `burner_common.py` or `scada/shared.js`, then run `python tools/sync_pages.py`
  (`--check` only checks).
- Add a line to `CHANGELOG.md` under *Unreleased*. Keep the user guide and README in step with changes.
- Plain words on the pages, in English and Danish.
- Never add anything that bypasses the boiler's safety functions or resets alarms without the user asking.
- **Privacy:** no personal data in commits, tests or pictures: no home IP addresses, serial numbers, Wi-Fi
  names, passwords, ntfy topics, PINs or towns. No brand names. Use made-up values.
