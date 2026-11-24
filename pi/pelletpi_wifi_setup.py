#!/usr/bin/env python3
"""Wi-Fi setup hotspot for the Pi (new, not tested on a real Pi yet).

When the Pi has had no network (no cable, no Wi-Fi) for 3 minutes, it opens an open Wi-Fi "pelletpi-setup".
After a network was lost (the Pi was online before), it waits 10 minutes instead, so the Wi-Fi watchdog
(pelletpi_wifi_watchdog.py) can reconnect first, and then uses shorter rounds (5 minutes on without a visitor,
5 minutes off to try the known networks).
Join it with a phone and open http://10.42.0.1:8099: pick your home Wi-Fi, type its password, Save.
The hotspot turns off after a successful join, or after 15 minutes without a visitor (then the Pi tries its
known networks and comes back every 10 minutes while still offline). Never while a network works.

Only NetworkManager (nmcli) and python3 from Raspberry Pi OS; nothing to install. The boiler link is USB, so the
monitor keeps logging meanwhile. The password is only given to NetworkManager; it is never logged or saved here.
Installed as a service by pi/install-wifi-setup.sh.
"""
import html, signal, subprocess, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOTSPOT = "pelletpi-setup"      # name of the hotspot and of its NetworkManager connection
HOME_CON = "pelletpi-home-wifi"  # the NetworkManager connection made from the page
ADDR = "10.42.0.1"
PORT = 8099
OFFLINE_FOR = 180   # s without network before the hotspot opens
IDLE_MAX = 900      # s without a visitor before the hotspot turns off
RETRY = 600         # s before the hotspot opens again while still offline
LOST_FOR = 600      # after a lost network: s without network before the hotspot opens (watchdog goes first)
LOST_IDLE = 300     # after a lost network: s without a visitor before the hotspot turns off
LOST_RETRY = 300    # after a lost network: s off (the Pi tries its known networks) before it opens again
JOIN_WAIT = 60      # s to join the chosen Wi-Fi


def log(msg):
    print(msg, flush=True)


def run(args, timeout=90):
    """Run nmcli (or another command); returns (exit code, stdout). Tests replace this."""
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
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def devices():
    code, out = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    return [fields(ln) for ln in out.splitlines() if ln.strip()] if code == 0 else []


def online(devs):
    """A home network: a connected cable (not the USB-C link to a PC) or Wi-Fi that is not our hotspot."""
    for d in devs:
        if len(d) < 4 or not d[2].startswith("connected"):
            continue
        if d[1] == "ethernet" and not d[0].startswith("usb"):
            return True
        if d[1] == "wifi" and d[3] != HOTSPOT:
            return True
    return False


def wifi_device(devs):
    for d in devs:
        if len(d) >= 2 and d[1] == "wifi":
            return d[0]
    return None


def scan(dev):
    """Networks nearby, strongest first: [(name, signal, secured)]. Done before the hotspot: the Pi 4 Wi-Fi chip
    cannot scan well while it is a hotspot."""
    run(["nmcli", "radio", "wifi", "on"])
    code, out = run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes",
                     "ifname", dev])
    best = {}
    for ln in out.splitlines() if code == 0 else []:
        f = fields(ln)
        if len(f) < 3 or not f[0] or f[0] == HOTSPOT:
            continue
        sig = int(f[1]) if f[1].isdigit() else 0
        if f[0] not in best or sig > best[f[0]][1]:
            best[f[0]] = (f[0], sig, f[2] not in ("", "--"))
    return sorted(best.values(), key=lambda n: -n[1])


def hotspot_up(dev):
    run(["nmcli", "connection", "delete", HOTSPOT])
    code, _ = run(["nmcli", "connection", "add", "type", "wifi", "ifname", dev, "con-name", HOTSPOT,
                   "autoconnect", "no", "ssid", HOTSPOT, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                   "ipv4.method", "shared", "ipv4.addresses", ADDR + "/24", "ipv6.method", "ignore"])
    if code == 0:
        code, _ = run(["nmcli", "-w", "30", "connection", "up", HOTSPOT])
    return code == 0


def hotspot_down():
    run(["nmcli", "connection", "down", HOTSPOT])
    run(["nmcli", "connection", "delete", HOTSPOT])


def harden(con):
    """NetworkManager tries this connection forever (it can give up for good after a few failed joins) and Wi-Fi
    power save is off (a known cause of drops on the Pi's Wi-Fi chip). From the next connect; nothing drops now."""
    run(["nmcli", "connection", "modify", con, "connection.autoconnect-retries", "0", "802-11-wireless.powersave", "2"])


def join(dev, name, pw, hidden):
    """Join the chosen Wi-Fi as connection HOME_CON (replaces the one made here before). True when it works."""
    run(["nmcli", "connection", "delete", HOME_CON])
    run(["nmcli", "device", "wifi", "rescan", "ifname", dev])
    time.sleep(5)
    args = ["nmcli", "-w", str(JOIN_WAIT), "device", "wifi", "connect", name]
    if pw:
        args += ["password", pw]
    args += ["ifname", dev, "name", HOME_CON]
    if hidden:
        args += ["hidden", "yes"]
    code, _ = run(args, timeout=JOIN_WAIT + 30)
    if code == 0 and online(devices()):
        harden(HOME_CON)
        return True
    run(["nmcli", "connection", "delete", HOME_CON])
    return False


# ---------------------------------------------------------------- the page (English or Danish)
TEXT = {
    "en": dict(title="Pellet burner monitor: Wi-Fi", h="Connect the Pi to your Wi-Fi", net="Your Wi-Fi",
               other="Other (hidden) network:", pw="Password", save="Save", none="No networks found.",
               wrong="Could not join {n}. Wrong password? Try again.",
               saved="Saved. The Pi now joins {n} and this hotspot turns off. Join your home Wi-Fi again and open "
                     "http://pelletpi.local in 2 minutes. If pelletpi-setup comes back, the password was wrong.",
               missing="Pick a network first.", lang="Dansk", other_lang="da"),
    "da": dict(title="Pillefyr-monitor: Wi-Fi", h="Forbind Pi'en med dit Wi-Fi", net="Dit Wi-Fi",
               other="Andet (skjult) netværk:", pw="Adgangskode", save="Gem", none="Ingen netværk fundet.",
               wrong="Kunne ikke forbinde til {n}. Forkert adgangskode? Prøv igen.",
               saved="Gemt. Pi'en forbinder nu til {n}, og dette hotspot slukker. Gå tilbage på dit eget Wi-Fi og "
                     "åbn http://pelletpi.local om 2 minutter. Kommer pelletpi-setup igen, var adgangskoden forkert.",
               missing="Vælg et netværk først.", lang="English", other_lang="en"),
}
STYLE = ("body{font:18px/1.5 system-ui,sans-serif;max-width:30em;margin:1.5em auto;padding:0 16px}"
         "select,input,button{font:inherit;width:100%;box-sizing:border-box;margin:.3em 0 .9em;padding:.4em}"
         "button{background:#b2440d;color:#fff;border:0;border-radius:6px}.msg{color:#b2440d;font-weight:bold}")


def page(lang, nets, msg="", form=True):
    t = TEXT[lang]
    if not form:
        return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{t["title"]}</title>'
                f'<style>{STYLE}</style></head><body><h1>{t["h"]}</h1><p class="msg">{msg}</p></body></html>')
    opts = "".join(f'<option value="{html.escape(n)}">{html.escape(n)} ({s} %{", 🔒" if sec else ""})</option>'
                   for n, s, sec in nets)
    pick = (f'<label>{t["net"]}<select name="net">{opts}</select></label>' if nets else f"<p>{t['none']}</p>")
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{t["title"]}</title>'
            f'<style>{STYLE}</style></head><body><p style="text-align:right"><a href="/?lang={t["other_lang"]}">'
            f'{t["lang"]}</a></p><h1>{t["h"]}</h1>'
            + (f'<p class="msg">{msg}</p>' if msg else "")
            + f'<form method="post" action="/save?lang={lang}">{pick}'
            f'<label>{t["other"]}<input name="other" autocomplete="off"></label>'
            f'<label>{t["pw"]}<input name="pw" type="password" autocomplete="off"></label>'
            f'<button type="submit">{t["save"]}</button></form></body></html>')


class State:
    def __init__(self, nets):
        self.nets = nets
        self.error = ""          # name of the network that failed last time
        self.last_visit = time.time()
        self.request = None      # (name, pw, hidden) once Save was pressed
        self.saved = threading.Event()


def choose_lang(query, accept):
    q = urllib.parse.parse_qs(query).get("lang", [""])[0]
    if q in TEXT:
        return q
    return "da" if (accept or "").strip().lower().startswith("da") else "en"


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # no access log (keeps the journal short)
            pass

        def send(self, body, code=200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def lang(self):
            return choose_lang(urllib.parse.urlsplit(self.path).query, self.headers.get("Accept-Language"))

        def do_GET(self):
            state.last_visit = time.time()
            lang = self.lang()
            msg = TEXT[lang]["wrong"].format(n=html.escape(state.error)) if state.error else ""
            self.send(page(lang, state.nets, msg))

        def do_POST(self):
            state.last_visit = time.time()
            lang = self.lang()
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            other = form.get("other", [""])[0].strip()
            name = other or form.get("net", [""])[0]
            pw = form.get("pw", [""])[0]
            if not name or len(name) > 32:
                self.send(page(lang, state.nets, TEXT[lang]["missing"]))
                return
            hidden = bool(other) and other not in [x[0] for x in state.nets]
            self.send(page(lang, [], TEXT[lang]["saved"].format(n=html.escape(name)), form=False))
            if not state.saved.is_set():
                state.request = (name, pw, hidden)
                state.saved.set()
    return Handler


def serve(state, port=None, host=None):
    """The page, only on the hotspot's own address (not on the home network or the USB-C link)."""
    srv = ThreadingHTTPServer((host or ADDR, PORT if port is None else port), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- main loop
def setup_round(dev, error="", idle_max=None):
    """One hotspot round. Returns 'joined', 'online' (cable), 'wrong:<name>' (join failed) or 'idle' (nobody came)."""
    idle_max = IDLE_MAX if idle_max is None else idle_max
    state = State(scan(dev))
    state.error = error
    if not hotspot_up(dev):
        log("could not start the hotspot")
        hotspot_down()
        return "idle"
    log(f"hotspot {HOTSPOT} is on: open http://{ADDR}:{PORT}")
    for _ in range(15):          # the hotspot address can take a moment
        try:
            srv = serve(state)
            break
        except OSError:
            time.sleep(1)
    else:
        log("could not start the page")
        hotspot_down()
        return "idle"
    try:
        ticks = 0
        while not state.saved.wait(2):
            ticks += 1
            if time.time() - state.last_visit > idle_max:
                log("nobody came: hotspot off")
                return "idle"
            if ticks % 15 == 0 and online(devices()):   # a cable was plugged in
                log("network found: hotspot off")
                return "online"
        time.sleep(3)   # let the "Saved" page reach the phone
    finally:
        srv.shutdown()
        srv.server_close()
        hotspot_down()
    name, pw, hidden = state.request
    log("joining the chosen Wi-Fi")
    if join(dev, name, pw, hidden):
        log("joined: hotspot stays off")
        return "joined"
    log("could not join: hotspot comes back")
    return "wrong:" + name


def main(rounds=None):
    """rounds: stop after this many hotspot rounds (tests); None = forever."""
    if run(["nmcli", "--version"])[0] != 0:
        log("no NetworkManager (nmcli): nothing to do")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # finally-blocks take the hotspot down
    offline_since = time.time()
    was_online = False      # online since this service started: a later outage is a lost network
    next_round = 0          # after a round nobody came to: not again before this time
    error = ""
    try:
        while rounds is None or rounds > 0:
            devs = devices()
            if online(devs):
                offline_since, error, next_round, was_online = None, "", 0, True
                time.sleep(30)
                continue
            now = time.time()
            offline_since = offline_since or now
            dev = wifi_device(devs)
            wait = LOST_FOR if was_online else OFFLINE_FOR
            if not dev or now - offline_since < wait or now < next_round:
                time.sleep(30)
                continue
            res = setup_round(dev, error, LOST_IDLE if was_online else IDLE_MAX)
            if rounds is not None:
                rounds -= 1
            if res.startswith("wrong:"):
                error = res[6:]
                continue
            error = ""
            if res == "idle":            # NetworkManager and the watchdog try the known networks meanwhile
                next_round = time.time() + (LOST_RETRY if was_online else RETRY)
            else:
                offline_since = None
    finally:
        hotspot_down()


if __name__ == "__main__":
    sys.exit(main())
