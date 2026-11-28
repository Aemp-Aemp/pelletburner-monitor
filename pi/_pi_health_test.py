"""pi_health.links(): on the Pi's own setup hotspot wlan0 is "hotspot", not "connected"; the home Wi-Fi's name is never
written. Made-up nmcli output, nothing is run. Run: py pi\\_pi_health_test.py"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pi_health as H
import pelletpi_wifi_setup as S

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

DEV = {}
def fake_run(*cmd, timeout=5):
    a = " ".join(cmd)
    if a.startswith("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev"):
        return DEV["out"]
    if a.startswith("nmcli -t -f IN-USE,SIGNAL"):
        return "*:71\n:40"
    if a.startswith("ip -4 -br addr"):
        return "lo UNKNOWN 127.0.0.1/8\nwlan0 UP 10.42.0.1/24"
    return ""
H.run = fake_run

check("the hotspot name is the one the setup page uses", H.HOTSPOT == S.HOTSPOT)
DEV["out"] = f"wlan0:wifi:connected:{S.HOTSPOT}\neth0:ethernet:unavailable:\nlo:loopback:unmanaged:"
r = H.links()
check("on the setup hotspot: wlan0 is 'hotspot'", r.get("wlan0") == "hotspot" and r.get("eth0") == "unavailable")
DEV["out"] = "wlan0:wifi:connected:MyHome\\:Net 5G\neth0:ethernet:unavailable:"
r = H.links()
check("on the home Wi-Fi: 'connected'", r.get("wlan0") == "connected")
check("the home Wi-Fi's name is never in the record", "MyHome" not in json.dumps(r))
DEV["out"] = "wlan0:wifi:disconnected:"
check("not connected stays as it is", H.links().get("wlan0") == "disconnected")
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
