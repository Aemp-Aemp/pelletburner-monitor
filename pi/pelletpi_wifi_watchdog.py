#!/usr/bin/env python3
"""Wi-Fi watchdog for the Pi (new, not tested on a real Pi yet). Runs every minute as root (pelletpi-wifi-watchdog.timer).

Why: the Pi 4 Wi-Fi chip (brcmfmac) can fail its scans ("scan error -52"); NetworkManager then marks the known
Wi-Fi as failed ('no-secrets') and never tries it again, so the Pi stays off the network until a power cycle.

What it does while the Pi has no network (no cable, Wi-Fi not connected or without an address):
  after 3 min    'connection up' for the known Wi-Fi (this also clears NetworkManager's "failed" mark)
  after 5 min    Wi-Fi radio off and on, then 'connection up' again
  scans failing  reload the Wi-Fi driver (brcmfmac), at most every 30 min and 4 times per start;
                 after 7 min it does this once anyway as the last resort
  after 10 min   makes sure the setup hotspot service runs; it opens "pelletpi-setup" (pelletpi_wifi_setup.py)
  meanwhile      'connection up' every 3 min and the radio every 20 min while the hotspot is off; nothing at all
                 while the hotspot is on (a phone may be using it)
Always: known Wi-Fi connections get autoconnect-retries 0 (try forever) and power save off.
Never touches the USB link to the boiler or the monitor's container. Never writes Wi-Fi names (connections are
picked by type and used by UUID). Events go to <data dir>/wifi-watchdog.jsonl ("lost", "action", "setup", "back").
Off: WIFI_WATCHDOG=no in pelletpi/settings.txt on the card's boot drive, or in /etc/default/pelletpi-wifi-watchdog.

    pelletpi_wifi_watchdog.py DATA_DIR             one run (the timer)
    pelletpi_wifi_watchdog.py --dry-run DATA_DIR   look and say what it would do; changes nothing, writes nothing
    pelletpi_wifi_watchdog.py --fix-only           only the two connection settings (used by the installer)
"""
import datetime, json, os, subprocess, sys, time

HOTSPOT = "pelletpi-setup"          # the setup hotspot's connection (pelletpi_wifi_setup.py)
SETUP_UNIT = "pelletpi-wifi-setup.service"
STATE_FILE = "/run/pelletpi/wifi-watchdog.json"     # cleared at every start (tmpfs)
CARD_SETTINGS = "/boot/firmware/pelletpi/settings.txt"
CONF = "/etc/default/pelletpi-wifi-watchdog"
LOG_NAME = "wifi-watchdog.jsonl"
LOG_MAX = 200_000                   # bytes; then only the newest half is kept

LOST_AFTER = 180        # s without network before anything is done
RADIO_AFTER = 300
DRIVER_AFTER = 420      # the last-resort driver reload (once per outage)
SETUP_AFTER = 600       # the setup hotspot service must run from here on
GAP = 110               # s between two actions (the timer runs every 60 s)
CONNECT_EVERY = 180     # later: 'connection up' this often while the hotspot is off
RADIO_EVERY = 1200
DRIVER_EVERY = 1800
DRIVER_MAX = 4          # per start of the Pi
SCAN_ERRORS = 2         # scan errors in the last 10 min that mean "the chip is stuck"
FIX_EVERY = 3600        # check the two connection settings this often while all is well
ECHO = True             # also print each event (to the journal)


def run(args, timeout=60):
    """Run a command; returns (exit code, stdout). Tests replace this."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def fields(line):
    """Split one `nmcli -t` line: ':' separates fields, '\\:' and '\\\\' are escaped."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur, esc = cur + ch, False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def setting(path, key):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            vals = [ln.strip().split("=", 1)[1].strip() for ln in f if ln.strip().startswith(key + "=")]
        return vals[-1] if vals else ""
    except OSError:
        return ""


def turned_off(card=None, conf=None):
    """WIFI_WATCHDOG=no on the card (wins) or in /etc/default/pelletpi-wifi-watchdog."""
    v = setting(card or CARD_SETTINGS, "WIFI_WATCHDOG") or setting(conf or CONF, "WIFI_WATCHDOG")
    return v.lower() in ("no", "off", "0", "false")


# ---------------------------------------------------------------- looking (only reads)
def known_wifi():
    """UUIDs of the Wi-Fi client connections NetworkManager knows, most recently used first (not our hotspot,
    not any access-point connection). Picked by type; names are never used, they can hold the network name."""
    code, out = run(["nmcli", "-t", "-f", "UUID,TYPE,TIMESTAMP,NAME", "connection", "show"])
    cons = []
    for ln in out.splitlines() if code == 0 else []:
        f = fields(ln)
        if len(f) >= 4 and f[1] == "802-11-wireless" and f[3] != HOTSPOT:
            cons.append((int(f[2]) if f[2].isdigit() else 0, f[0]))
    out_ = []
    for _, uuid in sorted(cons, reverse=True):
        if con_props(uuid).get("802-11-wireless.mode", "") != "ap":
            out_.append(uuid)
    return out_


def con_props(uuid):
    code, out = run(["nmcli", "-t", "-f", "connection.autoconnect-retries,802-11-wireless.powersave,"
                     "802-11-wireless.mode", "connection", "show", uuid])
    props = {}
    for ln in out.splitlines() if code == 0 else []:
        k, _, v = ln.partition(":")
        props[k.strip()] = v.strip()
    return props


def scan_errors():
    """Scan errors of the last 10 minutes: brcmfmac's in the kernel log, and wpa_supplicant's "Failed to initiate
    sched scan" (how a stuck chip shows on a real Pi 4, hundreds per outage) from wpa_supplicant / NetworkManager."""
    code, out = run(["journalctl", "-k", "-q", "--no-pager", "--since", "-10min"])
    n = sum(1 for ln in out.splitlines() if "scan error" in ln and "brcmf" in ln) if code == 0 else 0
    code, out = run(["journalctl", "-q", "--no-pager", "--since", "-10min", "-u", "wpa_supplicant.service",
                     "-u", "NetworkManager.service"])
    if code == 0:
        n += sum(1 for ln in out.splitlines() if "failed to initiate sched scan" in ln.lower())
    return n


def observe():
    """What the network looks like now. No names in it, only device names and yes/no."""
    code, out = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    devs = [fields(ln) for ln in out.splitlines() if ln.strip()] if code == 0 else []
    o = {"nm": code == 0, "cable": False, "dev": None, "state": "", "hotspot": False, "wifi_ok": False}
    for d in devs:
        if len(d) < 4:
            continue
        up = d[2].startswith("connected")
        if d[1] == "ethernet" and up and not d[0].startswith("usb"):
            o["cable"] = True                     # a cable to the router (usb0 is the USB-C link to a PC)
        if d[1] == "wifi" and o["dev"] is None:
            o["dev"], o["state"] = d[0], d[2]
            o["hotspot"] = up and d[3] == HOTSPOT
            o["wifi_ok"] = up and d[3] != HOTSPOT
    if o["wifi_ok"]:                              # connected without an address is not working
        code, out = run(["ip", "-4", "-o", "addr", "show", "dev", o["dev"]])
        o["wifi_ok"] = code == 0 and " inet " in " " + out
    o["driver"] = os.path.isdir("/sys/module/brcmfmac")
    o["online"] = o["cable"] or o["wifi_ok"]
    return o


# ---------------------------------------------------------------- deciding (no side effects, tested)
def decide(st, o, now, known, scan_err, setup_active):
    """-> list of actions for this run, in order: 'connect', 'radio', 'driver', 'start_setup'.
    Changes st (the watchdog's own memory). Only called while the Pi has no network."""
    acts = []
    since = st.setdefault("down_since", now)
    down = now - since
    if o["hotspot"] or not o["nm"]:
        return acts                                # a phone may be on the hotspot; never disturb it
    if down >= SETUP_AFTER and not setup_active and now - st.get("t_setup", 0) >= SETUP_AFTER:
        st["t_setup"] = now
        acts.append("start_setup")
    if not known or down < LOST_AFTER or now - st.get("t_act", 0) < GAP:
        return acts
    stuck = scan_err >= SCAN_ERRORS or o["dev"] is None or o["state"].startswith("unavailable")
    driver_ok = (o["driver"] and now - st.get("t_driver", -1e9) >= DRIVER_EVERY
                 and st.get("n_driver", 0) < DRIVER_MAX)
    n = st.get("n_act", 0)
    if driver_ok and (stuck or (down >= DRIVER_AFTER and not st.get("driver_this_outage"))):
        acts += ["driver", "connect"]
        st["t_driver"], st["n_driver"], st["driver_this_outage"] = now, st.get("n_driver", 0) + 1, True
    elif o["dev"] is None:
        return acts                                # no Wi-Fi device and no driver reload allowed: nothing to do
    elif n == 0:
        acts.append("connect")
    elif (down >= RADIO_AFTER and not st.get("t_radio")) or (st.get("t_radio") and now - st["t_radio"] >= RADIO_EVERY):
        acts += ["radio", "connect"]
        st["t_radio"] = now
    elif now - st.get("t_connect", 0) >= CONNECT_EVERY:
        acts.append("connect")
    else:
        return acts
    st["t_act"], st["n_act"] = now, n + 1
    if "connect" in acts:
        st["t_connect"] = now
    return acts


# ---------------------------------------------------------------- doing
def connect(dev, known):
    """'connection up' for each known Wi-Fi (most recent first) until one works; it also clears NetworkManager's
    'failed / no-secrets' mark. Without known ones: let NetworkManager pick ('device connect')."""
    for uuid in known[:3]:
        if run(["nmcli", "-w", "40", "connection", "up", "uuid", uuid] + (["ifname", dev] if dev else []),
               timeout=60)[0] == 0:
            return True
    if dev:
        return run(["nmcli", "-w", "40", "device", "connect", dev], timeout=60)[0] == 0
    return False


def radio():
    run(["nmcli", "radio", "wifi", "off"])
    time.sleep(3)
    return run(["nmcli", "radio", "wifi", "on"])[0] == 0


def driver():
    """Reload the built-in Wi-Fi driver (Pi 4: SDIO, not USB, so the boiler's USB adapter is not touched)."""
    mods = [m for m in ("brcmfmac_wcc", "brcmfmac") if os.path.isdir("/sys/module/" + m)]
    if "brcmfmac" not in mods:
        return False
    run(["modprobe", "-r"] + mods, timeout=30)
    time.sleep(2)
    ok = run(["modprobe", "brcmfmac"], timeout=30)[0] == 0
    time.sleep(8)                                  # the chip loads its firmware, wlan0 comes back
    return ok


def fix_connections(known=None, dry=False):
    """Known Wi-Fi: autoconnect-retries 0 (NetworkManager never gives up) and power save off (2 = disable).
    Returns how many connections were changed."""
    changed = 0
    for uuid in known if known is not None else known_wifi():
        p = con_props(uuid)
        mods = []
        if p.get("connection.autoconnect-retries", "0") != "0":
            mods += ["connection.autoconnect-retries", "0"]
        ps = p.get("802-11-wireless.powersave", "2")
        if not (ps.startswith("2") or "disable" in ps):
            mods += ["802-11-wireless.powersave", "2"]
        if mods and not dry:                       # applies from the next activation; the link stays as it is
            if run(["nmcli", "connection", "modify", "uuid", uuid] + mods)[0] == 0:
                changed += 1
        elif mods:
            changed += 1
    return changed


def hm(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def log_event(data, ev, now, dry=False, **kw):
    rec = {"epoch": round(now), "t": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds"), "ev": ev}
    rec.update(kw)
    line = json.dumps(rec, separators=(",", ":"))
    if ECHO or dry:
        print(line, flush=True)
    if dry or not data:
        return
    path = os.path.join(data, LOG_NAME)
    try:
        if os.path.getsize(path) > LOG_MAX:            # keep the newest half (whole lines)
            with open(path, "rb") as f:
                f.seek(-(LOG_MAX // 2), 2)
                tail = f.read()
            keep = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
            with open(path + ".new", "wb") as f:
                f.write(keep)
            os.replace(path + ".new", path)
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass                                       # a full card must not stop the watchdog


def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w") as f:
            json.dump(st, f)
        os.replace(path + ".new", path)
    except OSError:
        pass


def once(data, now=None, dry=False, state_file=None):
    """One run of the timer. Returns the actions taken (for tests and --dry-run)."""
    now = time.time() if now is None else now
    sf = state_file or STATE_FILE
    st = load_state(sf)
    o = observe()
    if not o["nm"]:
        return []
    if o["online"]:
        if st.get("lost_logged"):
            log_event(data, "back", now, dry, lost_at=hm(st["down_since"]), back_at=hm(now),
                      min=round((now - st["down_since"]) / 60), actions=st.get("n_act", 0),
                      via="cable" if o["cable"] and not o["wifi_ok"] else "wifi")
        keep = {k: st[k] for k in ("t_driver", "n_driver", "t_fix") if k in st}
        if now - keep.get("t_fix", 0) >= FIX_EVERY:
            fix_connections(dry=dry)
            keep["t_fix"] = now
        if not dry:
            save_state(sf, keep)
        return []
    known = known_wifi()
    first = "down_since" not in st
    st.setdefault("down_since", now)
    if not first and not st.get("lost_logged") and now - st["down_since"] >= LOST_AFTER:
        st["lost_logged"] = True
        log_event(data, "lost", now, dry, lost_at=hm(st["down_since"]), known=len(known), state=o["state"])
    if o["hotspot"] and not st.get("setup_logged"):
        st["setup_logged"] = True
        log_event(data, "setup", now, dry, note="setup hotspot open")
    elif not o["hotspot"]:
        st["setup_logged"] = False
    serr = scan_errors() if o["dev"] is None or now - st["down_since"] >= LOST_AFTER else 0
    # a setup service that was turned off on purpose (systemctl disable) is left off
    setup_active = (run(["systemctl", "is-active", "--quiet", SETUP_UNIT])[0] == 0
                    or run(["systemctl", "is-enabled", "--quiet", SETUP_UNIT])[0] != 0)
    acts = decide(st, o, now, known, serr, setup_active)
    for a in acts:
        ok = True
        if not dry:
            if a == "connect":
                fix_connections(known)
                ok = connect(observe()["dev"] or o["dev"], known)
            elif a == "radio":
                ok = radio()
            elif a == "driver":
                ok = driver()
            elif a == "start_setup":
                ok = run(["systemctl", "start", SETUP_UNIT])[0] == 0
        log_event(data, "action", now, dry, do=a, ok=ok, down_min=round((now - st["down_since"]) / 60),
                  scan_errors=serr)
    if not dry:
        save_state(sf, st)
    return acts


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if "--fix-only" in argv:
        print(f"Wi-Fi connections changed: {fix_connections()}")
        return 0
    if turned_off():
        return 0
    if run(["nmcli", "--version"])[0] != 0:
        return 0                                   # no NetworkManager: nothing to watch
    once(args[0] if args else None, dry="--dry-run" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
