#!/usr/bin/env python3
"""One line of Pi health per run, for later analysis (runs every minute from pi-health.timer).

Writes <data dir>/pi-health-YYYY-MM-DD.jsonl next to the boiler history. Works without internet.
Fields: time, boot id, uptime, clock synced, CPU temperature, power warnings (under-voltage),
load, free memory and disk, Wi-Fi / cable / USB-C link, internet reachable (ping) and name lookup (DNS), USB-serial adapters,
container state and restarts. No names or passwords of networks are written.
"""
import datetime, glob, json, os, subprocess, sys, time

DATA = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~pi/pelletburner-monitor/data")
KEEP_DAYS = 30
DNS_NAME = "pool.ntp.org"          # a well-known name for the DNS check (the clock needs it anyway)
HOTSPOT = "pelletpi-setup"         # the Pi's own setup hotspot connection (HOTSPOT in pelletpi_wifi_setup.py)


def run(*cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def throttled():
    # bit 0 under-voltage now, 1 freq capped, 2 throttled, 3 soft temp limit; bits 16-19 = has happened since boot
    v = run("vcgencmd", "get_throttled").partition("=")[2]
    try:
        n = int(v, 16)
    except ValueError:
        return None
    return {"hex": v, "undervolt_now": bool(n & 1), "throttled_now": bool(n & 4),
            "undervolt_since_boot": bool(n & 0x10000), "throttled_since_boot": bool(n & 0x40000)}


def links():
    out = {}
    for line in run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev").splitlines():
        dev, typ, state, con = (line.split(":", 3) + ["", "", "", ""])[:4]
        if typ in ("wifi", "ethernet"):
            # on its own setup hotspot the Pi is not on the home Wi-Fi; the home network's name is never written
            out[dev] = "hotspot" if state == "connected" and con == HOTSPOT else state
    sig = run("nmcli", "-t", "-f", "IN-USE,SIGNAL", "dev", "wifi")
    for line in sig.splitlines():
        if line.startswith("*:"):
            out["wifi_signal"] = int(line[2:] or 0)
    addrs = {}
    for line in run("ip", "-4", "-br", "addr").splitlines():
        p = line.split()
        if len(p) >= 3 and p[0] != "lo" and not p[0].startswith(("docker", "br-", "veth")):
            addrs[p[0]] = p[2].split("/")[0]
    out["ip"] = addrs
    return out


def internet():
    """ok = ping by number; dns_ok = a name lookup works (a router with broken DNS still answers ping)."""
    t = time.time()
    ok = subprocess.run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], capture_output=True).returncode == 0
    rec = {"ok": ok, "ms": round((time.time() - t) * 1000) if ok else None}
    t = time.time()
    dns = bool(run("getent", "hosts", DNS_NAME, timeout=4))
    rec.update(dns_ok=dns, dns_ms=round((time.time() - t) * 1000) if dns else None)
    return rec


def container():
    s = run("docker", "inspect", "-f", "{{.State.Status}} {{.RestartCount}} {{.State.StartedAt}}", "pelletburner")
    if not s:
        return None
    st, rc, started = (s.split() + ["", "", ""])[:3]
    return {"state": st, "restarts": int(rc or 0), "started": started}


def main():
    now = time.time()
    try:
        cpu_c = round(int(read("/sys/class/thermal/thermal_zone0/temp")) / 1000, 1)
    except ValueError:
        cpu_c = None
    mem = {l.split(":")[0]: l.split()[1] for l in read("/proc/meminfo").splitlines() if ":" in l}
    st = os.statvfs("/")
    rec = {
        "epoch": round(now, 1),
        "t": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "boot_id": read("/proc/sys/kernel/random/boot_id")[:8],
        "uptime_s": int(float(read("/proc/uptime").split()[0] or 0)),
        "clock_synced": run("timedatectl", "show", "-p", "NTPSynchronized", "--value") == "yes",
        "cpu_c": cpu_c,
        "power": throttled(),
        "load1": float(read("/proc/loadavg").split()[0] or 0),
        "mem_free_mb": int(mem.get("MemAvailable", 0)) // 1024,
        "disk_free_mb": st.f_bavail * st.f_frsize // (1024 * 1024),
        "net": links(),
        "internet": internet(),
        "usb_serial": sorted(os.path.basename(p) for p in glob.glob("/dev/serial/by-id/*"))
                      or sorted(os.path.basename(p) for p in glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")),
        "container": container(),
    }
    os.makedirs(DATA, exist_ok=True)
    day = datetime.date.fromtimestamp(now).isoformat()
    path = os.path.join(DATA, f"pi-health-{day}.jsonl")
    try:                                   # a power cut can leave a half line: start the next record on a new line
        with open(path, "rb") as f:
            f.seek(0, 2)
            cut = f.tell() > 0 and (f.seek(-1, 2), f.read(1))[1] != b"\n"
    except OSError:
        cut = False
    with open(path, "a") as f:
        f.write(("\n" if cut else "") + json.dumps(rec, separators=(",", ":")) + "\n")
    cutoff = now - KEEP_DAYS * 86400
    for p in glob.glob(os.path.join(DATA, "pi-health-*.jsonl")):
        if os.path.getmtime(p) < cutoff:
            os.remove(p)


if __name__ == "__main__":
    main()
