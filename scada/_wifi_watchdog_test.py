#!/usr/bin/env python3
"""Tests for pi/pelletpi_wifi_watchdog.py with a fake nmcli / ip / systemctl (no Pi, no Wi-Fi, no root needed).

    python scada/_wifi_watchdog_test.py

Covers the decisions minute by minute, the connection settings (retries 0, power save off; only client
connections), the event log and the phone message text. It does NOT test the real nmcli, radio or driver.
"""
import json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pi"))
sys.path.insert(0, os.path.join(HERE, ".."))
import pelletpi_wifi_watchdog as W  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------- the fake system
CLIENT = "11111111-aaaa-bbbb-cccc-000000000001"
CLIENT2 = "11111111-aaaa-bbbb-cccc-000000000002"
APCON = "22222222-aaaa-bbbb-cccc-000000000003"
SETUPCON = "33333333-aaaa-bbbb-cccc-000000000004"
fake = {}
calls = []


def reset(**kw):
    fake.clear()
    fake.update(devs="usb0:ethernet:connected:usb0\nwlan0:wifi:disconnected:--\n", ip="", setup_active=True,
                setup_enabled=True, kernel="", up_ok=False,
                props={CLIENT: {"connection.autoconnect-retries": "-1", "802-11-wireless.powersave": "0 (default)",
                                "802-11-wireless.mode": "infrastructure"},
                       CLIENT2: {"connection.autoconnect-retries": "0", "802-11-wireless.powersave": "2 (disable)",
                                 "802-11-wireless.mode": "infrastructure"},
                       APCON: {"connection.autoconnect-retries": "-1", "802-11-wireless.powersave": "0 (default)",
                               "802-11-wireless.mode": "ap"},
                       SETUPCON: {"connection.autoconnect-retries": "-1", "802-11-wireless.powersave": "0",
                                  "802-11-wireless.mode": "ap"}})
    # connection names are made up and hold no real network name; the code must not rely on them
    fake["cons"] = (f"{CLIENT}:802-11-wireless:1700000500:netplan-wlan0-x\\:y\n"
                    f"{CLIENT2}:802-11-wireless:1700000100:other\n"
                    f"{APCON}:802-11-wireless:1700000900:some-ap\n"
                    f"{SETUPCON}:802-11-wireless:0:{W.HOTSPOT}\n"
                    "44444444-aaaa-bbbb-cccc-000000000005:802-3-ethernet:1700000000:usb0\n")
    fake.update(kw)
    calls.clear()


def fake_run(args, timeout=60):
    calls.append(list(args))
    a = " ".join(args)
    if a.startswith("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status"):
        return 0, fake["devs"]
    if a.startswith("nmcli -t -f UUID,TYPE,TIMESTAMP,NAME connection show"):
        return 0, fake["cons"]
    if a.startswith("nmcli -t -f connection.autoconnect-retries"):
        p = fake["props"].get(args[-1], {})
        return 0, "".join(f"{k}:{v}\n" for k, v in p.items())
    if a.startswith("nmcli connection modify uuid"):
        uuid = args[4]
        rest = args[5:]
        for k, v in zip(rest[::2], rest[1::2]):
            fake["props"][uuid][k] = v
        return 0, ""
    if a.startswith("ip -4 -o addr show"):
        return 0, fake["ip"]
    if a.startswith("journalctl -k"):
        return 0, fake["kernel"]
    if a.startswith("systemctl is-active"):
        return (0 if fake["setup_active"] else 3), ""
    if a.startswith("systemctl is-enabled"):
        return (0 if fake["setup_enabled"] else 1), ""
    if "connection up" in a or "device connect" in a:
        return (0 if fake["up_ok"] else 4), ""
    return 0, ""


W.run = fake_run
W.ECHO = False
W.time.sleep = lambda s: None
W.os.path.isdir = (lambda real: (lambda p: True if p == "/sys/module/brcmfmac" else real(p)))(os.path.isdir)

# ---------------------------------------------------------------- the connection settings (idempotent)
reset()
check(W.known_wifi() == [CLIENT, CLIENT2], f"known: client connections by type, newest first, no AP: {W.known_wifi()}")
check(W.fix_connections() == 1, "fix: one client connection needed it")
mods = [c for c in calls if c[:3] == ["nmcli", "connection", "modify"]]
check(len(mods) == 1 and mods[0][4] == CLIENT
      and mods[0][5:] == ["connection.autoconnect-retries", "0", "802-11-wireless.powersave", "2"],
      f"fix: only the client changes, retries 0 and power save 2: {mods}")
check(fake["props"][APCON]["802-11-wireless.powersave"].startswith("0")
      and fake["props"][SETUPCON]["connection.autoconnect-retries"] == "-1", "fix: access points untouched")
calls.clear()
check(W.fix_connections() == 0 and not [c for c in calls if "modify" in c], "fix: second run changes nothing")
reset()
check(W.fix_connections(dry=True) == 1 and not [c for c in calls if "modify" in c], "fix: dry run changes nothing")
check(not any("netplan" in " ".join(c) or "other" in c for c in calls), "fix: connections used by UUID, never by name")

# the setup page's own connection gets the same two settings after a good join
import pelletpi_wifi_setup as S  # noqa: E402
scalls = []
S.run = lambda args, timeout=90: (scalls.append(args), (0, "wlan0:wifi:connected:" + S.HOME_CON + "\n"))[1]
S.time.sleep = lambda s: None
check(S.join("wlan0", "N", "pw", False), "setup join ok")
check(["nmcli", "connection", "modify", S.HOME_CON, "connection.autoconnect-retries", "0",
       "802-11-wireless.powersave", "2"] in scalls, "setup page: new connection tries forever, power save off")

# ---------------------------------------------------------------- off switch
tmp = tempfile.mkdtemp(prefix="pb_wd_")
card, conf = os.path.join(tmp, "settings.txt"), os.path.join(tmp, "conf")
with open(card, "w") as f:
    f.write("AUTO_UPDATE=yes\r\nWIFI_WATCHDOG=no\r\n")
check(W.turned_off(card, conf), "off: WIFI_WATCHDOG=no on the card (CRLF)")
with open(card, "w") as f:
    f.write("AUTO_UPDATE=no\n")
with open(conf, "w") as f:
    f.write("WIFI_WATCHDOG=no\n")
check(W.turned_off(card, conf), "off: in /etc/default")
with open(card, "w") as f:
    f.write("WIFI_WATCHDOG=yes\n")
check(not W.turned_off(card, conf), "off: the card setting wins")
check(not W.turned_off(os.path.join(tmp, "none"), os.path.join(tmp, "none2")), "off: on by default")

# ---------------------------------------------------------------- minute by minute
data = os.path.join(tmp, "data")
os.makedirs(data)
sf = os.path.join(tmp, "run", "wd.json")
T0 = 1_700_000_000


def minute(m, **kw):
    fake.update(kw)
    calls.clear()
    return W.once(data, now=T0 + m * 60, state_file=sf)


reset()
plan = {}
for m in range(0, 31):
    plan[m] = minute(m)
check(all(not plan[m] for m in range(0, 3)), f"0-2 min: nothing {[plan[m] for m in range(3)]}")
check(plan[3] == ["connect"], f"3 min: connection up {plan[3]}")
check(plan[4] == [], "4 min: wait (no two actions within 2 min)")
check(plan[5] == ["radio", "connect"], f"5 min: radio off/on {plan[5]}")
check(plan[7] == ["driver", "connect"], f"7 min: driver reload as last resort {plan[7]}")
check(plan[9] == [] and plan[8] == [], f"8-9 min: wait {plan[8]} {plan[9]}")
check(plan[10] == ["connect"], f"10 min: connection up again {plan[10]}")
check(plan[13] == ["connect"] and plan[16] == ["connect"], "later: connection up every 3 min")
check(not any("driver" in plan[m] for m in range(8, 31)), "driver once per outage (and 30 min apart)")
check(plan[25] == ["radio", "connect"], f"20 min after the first radio: radio again {plan[25]}")
check(not any("start_setup" in plan[m] for m in plan), "setup service already running: not started again")

# the connect really is 'connection up' by UUID for the client connections, not by name
reset(setup_active=True)
os.remove(sf)
for m in range(0, 4):
    minute(m)
ups = [c for c in calls if "up" in c]
check(ups and ups[0][-4:] == ["uuid", CLIENT, "ifname", "wlan0"], f"connect: connection up uuid, newest first: {ups}")
check(not [c for c in ups if APCON in c or SETUPCON in c], "connect: never an AP connection")

# hotspot open: nothing at all; then Wi-Fi back -> "back" event with both times
os.remove(sf)
os.remove(os.path.join(data, W.LOG_NAME))
reset()
for m in range(0, 12):
    minute(m)
acts = minute(12, devs=f"wlan0:wifi:connected:{W.HOTSPOT}\n")
acts += minute(15, devs=f"wlan0:wifi:connected:{W.HOTSPOT}\n")
check(acts == [] and not [c for c in calls if "up" in c or "radio" in c or "modprobe" in c],
      "hotspot open: the watchdog does nothing")
minute(20, devs="wlan0:wifi:connected:x\n", ip="3: wlan0    inet 10.42.0.1/24 brd x scope global wlan0\n")
with open(os.path.join(data, W.LOG_NAME)) as f:
    ev = [json.loads(x) for x in f]
kinds = [e["ev"] for e in ev]
check(kinds.count("lost") == 1 and kinds.count("setup") == 1 and kinds[-1] == "back", f"events: {kinds}")
back = ev[-1]
check(back["lost_at"] == W.hm(T0) and back["back_at"] == W.hm(T0 + 1200) and back["min"] == 20,
      f"back event: lost/back times and minutes: {back}")
lost = [e for e in ev if e["ev"] == "lost"][0]
check(lost["lost_at"] == W.hm(T0) and lost["epoch"] == T0 + 180, f"lost: logged at 3 min with the first bad minute: {lost}")
with open(os.path.join(data, W.LOG_NAME)) as f:
    txt = f.read()
check("netplan" not in txt and "other" not in txt and CLIENT not in txt, "log: no connection names or ids")

# connected without an address counts as lost
os.remove(sf)
reset(devs="wlan0:wifi:connected:x\n", ip="")
for m in range(0, 4):
    a = minute(m)
check(a == ["connect"], f"connected but no IPv4: treated as lost ({a})")

# a cable to the router: nothing to do; the USB-C link to a PC does not count
os.remove(sf)
reset(devs="eth0:ethernet:connected:wired\nwlan0:wifi:disconnected:--\n")
check(all(minute(m) == [] for m in range(0, 15)), "cable: nothing")

# scan errors from the chip: driver reload at 3 min already; at most every 30 min and 4 per start
os.remove(sf)
reset(kernel="brcmfmac: brcmf_cfg80211_scan: scan error (-52)\n" * 2)
got = {m: minute(m) for m in range(0, 200)}
drv = [m for m, a in got.items() if "driver" in a]
check(drv[:1] == [3], f"scan errors: driver at 3 min {drv[:3]}")
check(all(b - a >= 30 for a, b in zip(drv, drv[1:])) and len(drv) == W.DRIVER_MAX, f"driver: rate limit {drv}")

# Wi-Fi device gone (driver crashed): reload
os.remove(sf)
reset(devs="usb0:ethernet:connected:usb0\n")
got = [minute(m) for m in range(0, 5)]
check(got[3] == ["driver", "connect"], f"no wlan0: driver reload {got}")

# setup service stopped: started after 10 min (only when enabled), not every minute
os.remove(sf)
reset(setup_active=False)
got = {m: minute(m) for m in range(0, 25)}
st = [m for m, a in got.items() if "start_setup" in a]
check(st == [10, 20], f"setup service started at 10 min, again 10 min later: {st}")
os.remove(sf)
reset(setup_active=False, setup_enabled=False)
check(not any("start_setup" in minute(m) for m in range(0, 25)), "setup service turned off on purpose: left off")

# no known Wi-Fi at all: no reconnects (the setup hotspot is for that)
os.remove(sf)
reset(cons="")
check(all("connect" not in minute(m) for m in range(0, 15)), "no known Wi-Fi: no reconnects")

# dry run: no state, no log, no changes
os.remove(sf)
reset()
before = os.path.getsize(os.path.join(data, W.LOG_NAME))
for m in range(0, 5):
    W.once(data, now=T0 + m * 60, state_file=sf, dry=True)
check(not os.path.exists(sf) and os.path.getsize(os.path.join(data, W.LOG_NAME)) == before
      and not [c for c in calls if "modify" in c or "up" in c or "radio" in c], "dry run: changes nothing")

# the log stays small
big = os.path.join(data, W.LOG_NAME)
with open(big, "a") as f:
    for i in range(4000):
        f.write(json.dumps({"epoch": i, "ev": "action", "do": "connect"}) + "\n")
W.log_event(data, "action", T0, do="connect")
check(os.path.getsize(big) < W.LOG_MAX, "log: trimmed")

# ---------------------------------------------------------------- the server side: phone message text
import burner_ext as X  # noqa: E402
import burner_notify as N  # noqa: E402
with open(big, "a") as f:
    f.write(json.dumps({"epoch": T0 + 1200, "ev": "back", "lost_at": "14:34", "back_at": "14:52"}) + "\n")
check(X.wifi_outage(data, T0) == {"lost_at": "14:34", "back_at": "14:52"}, "server: newest back event")
check(X.wifi_outage(data, T0 + 5000) is None, "server: an older outage is not shown")
check(X.wifi_outage(os.path.join(tmp, "nope"), 0) is None, "server: no file")
body = N.message("net_down", "en", min=31, wifi=N.WIFI_TEXT["en"].format(lost_at="14:34", back_at="14:52"))[1]
check(body.endswith("It works again. Wi-Fi was lost at 14:34 and came back at 14:52."), f"message: {body}")
check(N.message("net_down", "en", min=31)[1].endswith("It works again."), "message: without a watchdog event")
da = N.message("net_down", "da", min=31, wifi=N.WIFI_TEXT["da"].format(lost_at="14:34", back_at="14:52"))[1]
check("kl. 14:34" in da and "{wifi}" not in da, f"message: Danish {da}")

shutil.rmtree(tmp, ignore_errors=True)
if FAILS:
    for f in FAILS:
        print("FAIL " + f)
    sys.exit(1)
print("OK: Wi-Fi watchdog decisions, connection settings, event log and message text checked (fake nmcli)")
