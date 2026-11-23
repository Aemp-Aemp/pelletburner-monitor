# Raspberry Pi card setup

Tested on a Raspberry Pi 4 with Raspberry Pi OS Lite 64-bit (Trixie).

## Easy install (ready SD card)

> **Not tested on a real card yet.** Everything in this section passed only static checks
> (`python pi/_card_test.py`). Try it on a spare card before giving it to anyone.

For people: see "Easy install" at the top of the main README. What happens behind it:

1. Imager writes the card and its own `user-data` / `network-config` (user, password, Wi-Fi, host name).
   We never touch those files.
2. `vendor-data` (our file, next to them) is read by cloud-init on the first start. It runs
   `pelletpi/install.sh`, which installs and starts `pelletpi-firstboot.service`.
3. `pelletpi/firstboot.sh` runs as that service, after every start, until it is done. Steps, each with a
   marker in `/var/lib/pelletpi/done-<step>` so it goes on where it stopped:
   USB-C network (`pelletpi-setup.sh`, plus `dtoverlay=dwc2` in `config.txt`) - Wi-Fi setup hotspot - wait for internet and the
   clock - `docker.io docker-compose git` from Debian - the program (`app.tar.gz` from the card folder, or
   `git clone REPO_URL`) into `~/pelletburner-monitor` - `docker compose up -d --build` - Pi health
   logging - auto-update - port 80. Then it writes `/var/lib/pelletpi/installed` and restarts once.
   Any failure: systemd starts it again after 1 minute.
4. Progress: `pelletpi-status.txt` and `pelletpi-install.log` on the boot drive, and a small "Setting up…"
   page on port 80 (python3 `http.server`) until the real page takes over.

**Fallback** if cloud-init does not run `vendor-data` (no `pelletpi-status.txt` after 20 minutes): add to the end
of the single line in `cmdline.txt`:
` systemd.run=/boot/firmware/pelletpi/install.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target`

### Building the card zip (maintainers)

```bash
python scripts/make_card.py --repo-url https://github.com/Aemp-Aemp/pelletburner-monitor.git --ref v1.0.0
```

Writes `dist/pelletpi-card.zip` (attach it to the GitHub release). It holds the program as `app.tar.gz`, so
the Pi does not need GitHub for the install, and `REPO_URL` in `settings.txt` for the auto-update.
No Wi-Fi data, passwords or keys go into it.

### Building a ready card image (maintainers, Linux or a Pi)

```bash
sudo scripts/make-image.sh 2025-xx-xx-raspios-trixie-arm64-lite.img.xz pelletpi.img https://github.com/Aemp-Aemp/pelletburner-monitor.git
```

Mounts the official image through a loop device, puts the card folder on the boot partition, adds the USB-C
overlay and enables `pelletpi-firstboot.service` directly (no cloud-init hook needed), then writes
`pelletpi.img.xz`. People flash it with Imager → *Use custom* and still set host name, user and Wi-Fi in
Imager's settings. Untested; whether Imager offers its settings for a custom image depends on the Imager version.

### Safe auto-update

`install-auto-update.sh` (run by the easy install; by hand: `sudo REPO_URL=... sh pi/install-auto-update.sh`)
installs `/usr/local/bin/pelletpi-update` with a timer (hourly 01:00-05:59) and a path unit for the
page's **Check for update** link. Each run:

- looks for the newest release tag `vX.Y.Z` of `REPO_URL` (never a branch);
- does nothing unless the burner is off (state 0) or waiting (state 1) in `/status.json`. Never while lighting
  or burning: a gap in the module traffic then can make the boiler show alarm 15. No boiler link = no update;
- builds the new version while the old one runs, swaps (a few seconds) only after a new state check, and goes
  back to the old files and image if the page does not answer within 2 minutes (that version is then skipped);
- keeps `data/`, logs to `data/update.log`, and writes `data/update.json` for the version line on the page.

Off: `AUTO_UPDATE=no` in `pelletpi/settings.txt` on the boot drive (or `/etc/default/pelletpi-update`).
Install by hand even when off: `sudo pelletpi-update now`. Untested on a Pi; releases are not signed yet
(next step: check signed tags).

### Wi-Fi setup hotspot

> **New, not tested on a real Pi.** Only the logic and the page are tested, with a fake `nmcli`
> (`python pi/_wifi_setup_test.py`).

`install-wifi-setup.sh` (run by the easy install and `pi/install.sh`; by hand: `sudo sh pi/install-wifi-setup.sh`)
installs `pelletpi-wifi-setup.service` (`pelletpi_wifi_setup.py`, python3 + NetworkManager's `nmcli`, nothing new
installed). When the Pi has no cable and no Wi-Fi for 3 minutes, it scans once and opens the open Wi-Fi
`pelletpi-setup`. Join it with a phone and open `http://10.42.0.1:8099` (English/Danish): pick your Wi-Fi,
type the password, Save. The hotspot goes off, the Pi joins (connection `pelletpi-home-wifi`); if that fails,
the hotspot comes back with "Wrong password?". With no visitor for 15 minutes it goes off and the Pi tries its
known networks; it comes back every ~13 minutes while still offline. Never while a network works. The boiler
link is USB, so the monitor keeps logging. Off: `sudo systemctl disable --now pelletpi-wifi-setup`.

### What is untested

- cloud-init running our `vendor-data` script on Raspberry Pi OS (main trigger); the `cmdline.txt` fallback
  is the same method as the tested `pelletpi-setup.sh`.
- The whole unattended run (Docker install, build, port 80, restart), the setup page and the status file.
- `make-image.sh` and Imager settings on a custom image.
- The auto-update, rollback and the page's update line on a real Pi.
- The Wi-Fi setup hotspot (scan, hotspot, joining, fallback) on a real Pi 4.

## 1. Write the card

Use Raspberry Pi Imager. In the settings, choose a host name (e.g. `pelletpi`), a user, your SSH public key,
your Wi-Fi and your time zone. Your Wi-Fi password and keys stay on your card only. Never commit them.

## 2. Add the USB-C network setup

After writing, open the card's boot partition (`bootfs`) on your PC:

1. Copy `pelletpi-setup.sh` to the root of `bootfs`.
2. In `config.txt`, add at the end, under `[all]`:
   ```
   dtoverlay=dwc2,dr_mode=peripheral
   ```
3. In `cmdline.txt` (one single line, do not add line breaks), add at the end:
   ```
    systemd.run=/boot/firmware/pelletpi-setup.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
   ```

On the first start the Pi runs the script once, removes it from `cmdline.txt` and reboots. The script:

- makes the USB-C port a network adapter that Windows, macOS and Linux recognise by themselves
  (RNDIS with Microsoft OS descriptors), with the Pi at `10.55.0.1` handing the PC an address;
- writes `pi-setup.log` and, after every start, `pi-diag.txt` to `bootfs`, so you can read the Pi's
  status by putting the card in any PC.

## 3. Connect

Plug the Pi's USB-C port into the PC (this also powers the Pi; use a strong USB-C port), wait about 2 minutes,
then:

```bash
ssh pi@10.55.0.1
```

## 4. Docker

```bash
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker $USER
```

Then follow the main README. Or, after cloning, do steps 4-6 in one go: `sudo sh pi/install.sh`.

## 5. Page without a port number (optional)

```bash
sudo sh pi/enable-port-80.sh
```

Then the page opens at `http://pelletpi.local` (no `:8787`).

## 6. Logging for long tests (optional, recommended)

```bash
sudo sh pi/install-pi-logging.sh
```

Writes one line of Pi health per minute to `data/pi-health-*.jsonl` and keeps the system journal across
restarts. Works without internet.
