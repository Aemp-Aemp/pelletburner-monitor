#!/usr/bin/env python3
"""Tests for pi/pelletpi_wifi_setup.py with a fake nmcli (runs on Windows, no Pi or Wi-Fi needed).

    python pi/_wifi_setup_test.py

It does NOT test the real hotspot: that needs a Raspberry Pi with NetworkManager.
"""
import os, sys, threading, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pelletpi_wifi_setup as w  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------- nmcli -t parsing
check(w.fields(r"My\:Net:72:WPA2") == ["My:Net", "72", "WPA2"], "fields: escaped ':'")
check(w.fields(r"a\\b:1:") == ["a\\b", "1", ""], "fields: escaped backslash, empty last field")

# ---------------------------------------------------------------- online / device
check(not w.online([["usb0", "ethernet", "connected", "usb0"], ["wlan0", "wifi", "disconnected", "--"]]),
      "online: the USB-C link to a PC is not a home network")
check(w.online([["eth0", "ethernet", "connected", "Wired connection 1"]]), "online: cable")
check(w.online([["wlan0", "wifi", "connected", "home"]]), "online: Wi-Fi")
check(not w.online([["wlan0", "wifi", "connected", w.HOTSPOT]]), "online: our own hotspot does not count")
check(not w.online([["wlan0", "wifi", "connecting (getting IP configuration)", "home"]]), "online: connecting")
check(w.wifi_device([["eth0", "ethernet", "unavailable", "--"], ["wlan0", "wifi", "disconnected", "--"]]) == "wlan0",
      "wifi_device")

# ---------------------------------------------------------------- fake nmcli
calls = []
answers = {}


def fake_run(args, timeout=90):
    calls.append(args)
    for key, ans in answers.items():
        if key in " ".join(args):
            return ans
    return 0, ""


w.run = fake_run
w.time.sleep = lambda s: None

answers["wifi list"] = (0, "Net A:40:WPA2\nNet A:80:WPA2\n:90:WPA2\nOpen\\:Cafe:55:\n" + w.HOTSPOT + ":99:\n")
nets = w.scan("wlan0")
check(nets == [("Net A", 80, True), ("Open:Cafe", 55, False)], f"scan: dedup, no hidden/own, sorted: {nets}")

calls.clear()
check(w.hotspot_up("wlan0"), "hotspot_up")
add = [c for c in calls if "add" in c][0]
check("wifi-sec.key-mgmt" not in add and "wifi-sec.psk" not in add, "hotspot: open (owner decision, no password)")
check("ap" in add and "shared" in add and w.ADDR + "/24" in add, "hotspot: AP mode, shared, 10.42.0.1")

# join: works / fails; the password goes only to nmcli
answers["device status"] = (0, "wlan0:wifi:connected:" + w.HOME_CON + "\n")
calls.clear()
check(w.join("wlan0", "Net A", "secret-pw", False), "join: ok")
con = [c for c in calls if "connect" in c][0]
check(con[con.index("password") + 1] == "secret-pw" and "hidden" not in con, "join: password and no hidden")
check(calls[0][-1] == w.HOME_CON and "delete" in calls[0], "join: old page-made connection replaced first")
answers["device status"] = (0, "wlan0:wifi:disconnected:--\n")
answers["wifi connect"] = (4, "")
calls.clear()
check(not w.join("wlan0", "Hidden", "", True), "join: fails")
con = [c for c in calls if "connect" in c][0]
check("password" not in con and "hidden" in con, "join: open network without password, hidden yes")
check(calls[-1] == ["nmcli", "connection", "delete", w.HOME_CON], "join: failed connection removed")
del answers["wifi connect"]

# ---------------------------------------------------------------- the page: EN / DA, never mixed
en = w.page("en", nets)
da = w.page("da", nets, w.TEXT["da"]["wrong"].format(n="X"))
check("Password" in en and "Adgangskode" not in en, "page: English")
check("Adgangskode" in da and "Password" not in da and "Save<" not in da and "Prøv igen" in da, "page: Danish")
check('lang="da"' in da and "Open:Cafe" in en and "&lt;" in w.page("en", [("<b>", 1, False)]), "page: escaped")
check(w.choose_lang("", "da-DK,da;q=0.9") == "da" and w.choose_lang("lang=en", "da") == "en"
      and w.choose_lang("", "en-GB") == "en", "choose_lang")

# ---------------------------------------------------------------- the page server: GET, POST, Save once
state = w.State(nets)
srv = w.serve(state, port=0, host="127.0.0.1")
base = f"http://127.0.0.1:{srv.server_address[1]}"
try:
    body = urllib.request.urlopen(base + "/", timeout=5).read().decode()
    check("Net A" in body and "<form" in body, "server: GET shows the list")
    t0 = state.last_visit
    data = urllib.parse.urlencode({"net": "", "other": "", "pw": ""}).encode()
    body = urllib.request.urlopen(base + "/save?lang=da", data, timeout=5).read().decode()
    check(not state.saved.is_set() and "Vælg et netværk" in body, "server: empty choice asks again")
    data = urllib.parse.urlencode({"net": "Net A", "other": "", "pw": "pw 1"}).encode()
    body = urllib.request.urlopen(base + "/save", data, timeout=5).read().decode()
    check(state.saved.is_set() and state.request == ("Net A", "pw 1", False), f"server: Save: {state.request}")
    check("pw 1" not in body and "<form" not in body and "Net A" in body, "server: Saved page, no password echo")
    check(state.last_visit >= t0, "server: visits are counted")
    state.saved.clear()
    data = urllib.parse.urlencode({"net": "Net A", "other": "My Hidden", "pw": ""}).encode()
    urllib.request.urlopen(base + "/save", data, timeout=5).read()
    check(state.request == ("My Hidden", "", True), f"server: hidden network typed in: {state.request}")
finally:
    srv.shutdown()
    srv.server_close()

# ---------------------------------------------------------------- one round: nobody comes -> hotspot off
answers["device status"] = (0, "wlan0:wifi:disconnected:--\n")
w.IDLE_MAX = 0
w.PORT = 0
w.ADDR = "127.0.0.1"
calls.clear()
check(w.setup_round("wlan0") == "idle", "round: idle")
check(["nmcli", "connection", "down", w.HOTSPOT] in calls, "round: hotspot turned off after idle")

# one round: Save -> join ok
w.IDLE_MAX = 60
answers["device status"] = (0, "wlan0:wifi:disconnected:--\n")
orig_serve = w.serve


def serve_and_save(state, port=0, host=None):
    s = orig_serve(state, 0, "127.0.0.1")

    def press():
        threading.Event().wait(0.2)   # real time: time.sleep is replaced by a no-op above
        answers["device status"] = (0, "wlan0:wifi:connected:" + w.HOME_CON + "\n")
        state.request = ("Net A", "pw", False)
        state.saved.set()
    threading.Thread(target=press, daemon=True).start()
    return s


w.serve = serve_and_save
calls.clear()
res = w.setup_round("wlan0")
check(res == "joined", f"round: joined ({res})")
i_down = calls.index(["nmcli", "connection", "down", w.HOTSPOT])
i_join = [i for i, c in enumerate(calls) if "connect" in c][0]
check(i_down < i_join, "round: hotspot off before joining (one radio)")
w.serve = orig_serve

# ---------------------------------------------------------------- main loop timing (fake clock)
# at start: hotspot after 3 min offline; after a lost network: 10 min (the watchdog goes first), then 5 min rounds
clock = [1000.0]
w.time.time = lambda: clock[0]
w.time.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
w.signal.signal = lambda *a: None
rounds_at = []
orig_round = w.setup_round


def fake_round(dev, error="", idle_max=None):
    rounds_at.append((clock[0], idle_max))
    clock[0] += idle_max
    return "idle"


w.setup_round = fake_round
answers["device status"] = (0, "wlan0:wifi:disconnected:--\n")
w.main(rounds=2)
check(180 <= rounds_at[0][0] - 1000 <= 210 and rounds_at[0][1] == w.IDLE_MAX,
      f"main: never online -> hotspot after 3 min, 15 min rounds {rounds_at}")
gap = rounds_at[1][0] - rounds_at[0][0] - w.IDLE_MAX
check(w.RETRY <= gap <= w.RETRY + 30, f"main: 10 min off between rounds ({gap})")
rounds_at.clear()
seq = ["wlan0:wifi:connected:home\n"] * 3


def status_seq(args, timeout=90):
    if "device status" in " ".join(args):
        return 0, (seq.pop(0) if seq else "wlan0:wifi:disconnected:--\n")
    return 0, ""


w.run = status_seq
clock[0] = 1000.0
w.main(rounds=2)
lost_at = 1000 + 3 * 30
check(w.LOST_FOR <= rounds_at[0][0] - lost_at <= w.LOST_FOR + 30 and rounds_at[0][1] == w.LOST_IDLE,
      f"main: lost network -> hotspot after 10 min, 5 min rounds {rounds_at}")
gap = rounds_at[1][0] - rounds_at[0][0] - w.LOST_IDLE
check(w.LOST_RETRY <= gap <= w.LOST_RETRY + 30, f"main: 5 min off between rounds after a loss ({gap})")
w.setup_round, w.run = orig_round, fake_run

if FAILS:
    print("FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("OK: Wi-Fi setup hotspot logic and page checked (fake nmcli)")
