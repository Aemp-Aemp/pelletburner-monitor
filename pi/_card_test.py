#!/usr/bin/env python3
"""Static checks for the easy-install card files and the auto-update (runs on Windows, no Pi needed).

    python pi/_card_test.py

Checks: LF line ends and no BOM, a shebang and `set -e` in every script, firstboot steps with idempotency
markers, no prompts, no secrets / Wi-Fi data / brand names, the card folder build, the updater's safety rules,
and `bash -n` syntax when a bash is found (Git for Windows has one). It does NOT run anything on a Pi.
"""
import glob, os, re, shutil, subprocess, sys, tempfile, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import make_card  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def rd(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


SCRIPTS = sorted(set(
    [os.path.relpath(p, ROOT).replace(os.sep, "/") for p in glob.glob(os.path.join(ROOT, "pi", "**", "*.sh"), recursive=True)]
    + [os.path.relpath(p, ROOT).replace(os.sep, "/") for p in glob.glob(os.path.join(ROOT, "scripts", "*.sh"))]
    + ["pi/firstboot/vendor-data"]))
CARD_TEXT = ["pi/firstboot/settings.txt", "pi/firstboot/README.txt", "pi/firstboot/pelletpi-firstboot.service"]
SET_E_EXEMPT = {"pi/pelletpi-setup.sh": "uses set +e on purpose: it must always reach the cmdline.txt clean-up"}

# ---------------------------------------------------------------- line ends, shebang, set -e, no prompts
for rel in SCRIPTS + CARD_TEXT:
    b = rd(rel)
    check(b"\r" not in b, f"{rel}: has CR (must be LF only)")
    check(not b.startswith(b"\xef\xbb\xbf"), f"{rel}: has a UTF-8 BOM")
    check(b.endswith(b"\n"), f"{rel}: no newline at the end")
for rel in SCRIPTS:
    s = rd(rel).decode("utf-8")
    check(s.startswith("#!/bin/sh\n") or s.startswith("#!/bin/bash\n"), f"{rel}: needs #!/bin/sh or #!/bin/bash")
    if rel not in SET_E_EXEMPT:
        check(re.search(r"^set -[a-zA-Z]*e", s, re.M), f"{rel}: no 'set -e'")
    check(not re.search(r"^\s*read\s", s, re.M), f"{rel}: waits for keyboard input")
    for m in re.finditer(r"apt-get[^\n]*\binstall\b", s):
        check("-y" in s[max(0, m.start() - 200):m.end()] or "APT[@]" in m.group(0), f"{rel}: apt-get install without -y")
    if s.startswith("#!/bin/sh"):
        check(not re.search(r"\[\[|^\s*local\s|\$\{[A-Za-z_]+\[|pipefail", s), f"{rel}: bash-only syntax under #!/bin/sh")

# ---------------------------------------------------------------- firstboot: every step has a marker, runs once
fb = rd("pi/firstboot/firstboot.sh").decode()
steps = re.findall(r"^step (\w+)\s+(\d+)\s+\"[^\"]+\"\s+(\w+)\s*$", fb, re.M)
check(len(steps) >= 7, f"firstboot.sh: expected >= 7 steps, found {len(steps)}")
for name, _n, fn in steps:
    check(re.search(rf"^{fn}\(\) \{{", fb, re.M), f"firstboot.sh: step {name} calls missing function {fn}")
check('if [ -e "$STATE/done-$name" ]' in fb and 'touch "$STATE/done-$name"' in fb, "firstboot.sh: step markers missing")
check('touch "$STATE/installed"' in fb, "firstboot.sh: no final 'installed' marker")
for need in ("docker.io", "docker-compose", "up -d --build", "install-pi-logging.sh", "enable-port-80.sh",
             "install-auto-update.sh", "pelletpi-status.txt", "WAITING FOR INTERNET", "dtoverlay=dwc2"):
    check(need in fb, f"firstboot.sh: missing '{need}'")
check("grep -q '^dtoverlay=dwc2'" in fb, "firstboot.sh: config.txt edit is not guarded (not idempotent)")
svc = rd("pi/firstboot/pelletpi-firstboot.service").decode()
check("ConditionPathExists=!/var/lib/pelletpi/installed" in svc, "service: must not run again after install")
check("Restart=on-failure" in svc and "StartLimitIntervalSec=0" in svc, "service: must keep retrying")
inst = rd("pi/firstboot/install.sh").decode()
check("systemd.run=" in inst and "sed -i" in inst, "install.sh: must remove itself from cmdline.txt")
# ---------------------------------------------------------------- pi/install.sh: one command after cloning
one = rd("pi/install.sh").decode()
for need in ('id -u)" != 0', "docker.io", "up -d --build", "chown 1000:1000 data", "enable-port-80.sh",
             "install-pi-logging.sh", "install-auto-update.sh", "NO_PORT80", "NO_LOGGING", "NO_UPDATE"):
    check(need in one, f"pi/install.sh: missing '{need}'")
check("cmdline.txt" not in one and "config.txt" not in one, "pi/install.sh: must not change the boot files")
# ---------------------------------------------------------------- Wi-Fi setup hotspot (new, not tested on a Pi)
check("install-wifi-setup.sh" in one and "NO_WIFI_SETUP" in one, "pi/install.sh: Wi-Fi setup hotspot step missing")
check("wifi_setup" in fb and fb.find("step wifisetup") < fb.find("step docker"),
      "firstboot.sh: Wi-Fi setup must be installed before the steps that wait for internet")
iw = rd("pi/install-wifi-setup.sh").decode()
check("command -v nmcli" in iw and "apt-get" not in iw, "install-wifi-setup.sh: must use nmcli only, install nothing")
check("WantedBy=multi-user.target" in iw and "systemctl enable" in iw, "install-wifi-setup.sh: service not enabled")
ws = rd("pi/pelletpi_wifi_setup.py").decode()
for need in ('HOTSPOT = "pelletpi-setup"', "OFFLINE_FOR = 180", "IDLE_MAX = 900", '"ipv4.method", "shared"'):
    check(need in ws, f"pelletpi_wifi_setup.py: missing '{need}'")
check("wifi-sec" not in ws.split("def hotspot_up")[1].split("def hotspot_down")[0],
      "pelletpi_wifi_setup.py: the setup hotspot is open (owner decision 22 Nov)")
check("tty" not in ws and "serial" not in ws.lower(), "pelletpi_wifi_setup.py: must not touch the boiler's USB link")
# Wi-Fi watchdog (new, not tested on a Pi): installed by every path, never near the boiler link or the container
check("install-wifi-watchdog.sh" in one and "NO_WIFI_WATCHDOG" in one, "pi/install.sh: Wi-Fi watchdog step missing")
check("step wifiwd" in fb and "install-wifi-watchdog.sh" in fb, "firstboot.sh: Wi-Fi watchdog step missing")
check("install-wifi-watchdog.sh" in rd("pi/install-auto-update.sh").decode(),
      "install-auto-update.sh: an updated Pi must get the Wi-Fi watchdog")
wd = rd("pi/pelletpi_wifi_watchdog.py").decode()
check("tty" not in wd and "serial" not in wd.lower() and "docker" not in wd,
      "pelletpi_wifi_watchdog.py: must not touch the boiler's USB link or the monitor's container")
check("WIFI_WATCHDOG" in wd and "wifi.powersave = 2" in rd("pi/install-wifi-setup.sh").decode(),
      "Wi-Fi watchdog off switch or power save drop-in missing")
for doc in ("README.md", "docs/guide.html", "pi/README.md"):
    d = rd(doc).decode()
    check("pelletpi-setup" in d and "10.42.0.1:8099" in d and "not tested on a real Pi" in d,
          f"{doc}: Wi-Fi setup hotspot section (marked new, not tested on a real Pi) missing")
r = subprocess.run([sys.executable, os.path.join(ROOT, "pi", "_wifi_setup_test.py")], capture_output=True, text=True)
check(r.returncode == 0, "pi/_wifi_setup_test.py failed:\n" + r.stdout + r.stderr)
readme = rd("README.md").decode()
check("sudo sh pi/install.sh" in readme and "sudo sh pi/install.sh" in rd("docs/guide.html").decode(),
      "README.md and docs/guide.html must show 'sudo sh pi/install.sh'")

st = rd("pi/firstboot/settings.txt").decode()
# a card made from this repository must get updates: the public repository, https, no placeholder, no other account
check(re.search(r"^REPO_URL=https://github\.com/Aemp-Aemp/pelletburner-monitor\.git$", st, re.M),
      "settings.txt: REPO_URL must be the project's public repository")
check("CHANGE-ME" not in st, "settings.txt: placeholder left in")
check(re.search(r"^AUTO_UPDATE=yes$", st, re.M), "settings.txt: AUTO_UPDATE=yes missing")

# ---------------------------------------------------------------- updater: the safety rules
up = rd("pi/pelletpi-update.sh").decode()
check("0 | 1) return 0" in up, "updater: swap must be gated on burner state 0 or 1")
check(0 < up.find('"$BUILD_TIMEOUT" "${DC[@]}" build') < up.rfind("until safe_now; do") < up.rfind("dc up -d"),
      "updater: must build, then wait for the burner, then swap")
check("START_TRIES=24" in up and "STEP=5" in up and 'seq 1 "$START_TRIES"' in up, "updater: 2-minute health check missing")
check("LINK_TRIES=60" in up and "WHY=boiler" in up and "WHY=settings" in up, "updater: boiler link / settings test missing")
check('"" | *CHANGE-ME*) finish no_repo' in up and "https://*)" in up, "updater: REPO_URL check missing")
check("in-progress" in up and "pelletpi-previous" in up, "updater: journal or kept previous version missing")
check(not re.search(r"\beval\b", up) and '. "$REQ"' not in up and "source " not in up, "updater: the request file must never be run")
check("back_to_old" in up and 'docker tag "$OLD_IMAGE"' in up, "updater: rollback missing")
check("bad-$LATEST" in up, "updater: a failed version must not be retried")
check("AUTO_UPDATE" in up and "flock" in up, "updater: off switch or lock missing")
check(re.search(r"\^v\[0-9\]", up), "updater: only release tags")
check(all(l.strip() == 'rm -rf "$APP/.git"' for l in up.splitlines() if "rm -rf" in l and not l.strip().startswith("#"))
      and "clean -" not in up, "updater: must not delete files (the data folder stays; only a half-made .git may go)")
resp = rd("burner_responder.py").decode()
check('"/api/update"' in resp and '"/api/update/check"' in resp and '"/api/update/back"' in resp,
      "responder: /api/update routes missing")
for pg in ("scada/home.html", "scada/dashboard.html"):
    check(rd(pg).decode().count('btn_check:"') == 2, f"{pg}: the update texts must be in EN and DA")

# ---------------------------------------------------------------- no secrets, no personal data, no brand
SECRET = re.compile(r"(?i)(\bpsk\s*=|wpa_passphrase|passwd\s*=|password\s*[:=]\s*\S|BEGIN [A-Z ]*PRIVATE KEY|"
                    r"ssh-(rsa|ed25519) AAAA|ghp_[A-Za-z0-9]{20}|github_pat_|\bssid\s*[:=]\s*\S)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")
NEW = SCRIPTS + CARD_TEXT + ["scripts/make_card.py", "pi/_card_test.py", "pi/pelletpi_wifi_setup.py"]
for rel in NEW:
    s = rd(rel).decode("utf-8")
    if rel != "pi/_card_test.py":
        for m in SECRET.finditer(s):
            FAILS.append(f"{rel}: looks like a secret: {m.group(0)!r}")
        for m in EMAIL.finditer(s):
            FAILS.append(f"{rel}: e-mail address {m.group(0)!r}")
brand = "bio" + "max"
for base, dirs, names in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "data", "dist", "__pycache__")]
    for n in names:
        p = os.path.join(base, n)
        if n.endswith((".jpg", ".png", ".gz", ".zip", ".pyc")):
            continue
        with open(p, "rb") as f:
            if brand.encode() in f.read().lower():
                FAILS.append(f"{os.path.relpath(p, ROOT)}: contains the brand name")
gi = rd(".gitignore").decode()
check("dist/" in gi, ".gitignore: dist/ (built card folders) must be ignored")

# ---------------------------------------------------------------- build the card folder and zip
tmp = tempfile.mkdtemp()
try:
    card, z = make_card.build(tmp, "https://example.invalid/x/pelletburner-monitor.git", with_app=False)
    want = {"vendor-data", "pelletpi/install.sh", "pelletpi/firstboot.sh", "pelletpi/pelletpi-setup.sh",
            "pelletpi/pelletpi-firstboot.service", "pelletpi/settings.txt", "pelletpi/README.txt",
            "pelletpi/install-wifi-setup.sh", "pelletpi/pelletpi_wifi_setup.py"}
    names = set(zipfile.ZipFile(z).namelist())
    check(want <= names, f"card zip misses {sorted(want - names)}")
    check(not any(n in names for n in ("cmdline.txt", "config.txt", "user-data", "network-config")),
          "card zip must never contain Imager's own files")
    with open(os.path.join(card, "pelletpi", "settings.txt"), "rb") as f:
        s = f.read()
    check(b"REPO_URL=https://example.invalid/x/pelletburner-monitor.git\n" in s and b"\r" not in s,
          "make_card: REPO_URL not written or CRLF")
    if shutil.which("git") and os.path.exists(os.path.join(ROOT, ".git")):
        try:
            card, z = make_card.build(tmp, None, with_app=True)
            names = set(zipfile.ZipFile(z).namelist())
            check({"pelletpi/app.tar.gz", "pelletpi/app-version.txt"} <= names, "card zip: app.tar.gz missing")
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"  (app.tar.gz build skipped: {e})")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- bash -n (syntax only) when a bash is here
bash = None
for cand in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"), r"C:\Program Files\Git\bin\bash.exe",
             "/bin/bash", "/usr/bin/bash"):
    if os.path.isfile(cand):
        bash = cand
        break
if bash:
    for rel in SCRIPTS:
        r = subprocess.run([bash, "-n", rel], cwd=ROOT, capture_output=True, text=True)
        check(r.returncode == 0, f"{rel}: bash -n: {r.stderr.strip()}")
    sc = shutil.which("shellcheck")
    if sc:
        r = subprocess.run([sc, "-S", "error", *SCRIPTS], cwd=ROOT, capture_output=True, text=True)
        check(r.returncode == 0, "shellcheck errors:\n" + r.stdout)
else:
    print("  (no bash found: syntax check skipped)")

if FAILS:
    print("FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print(f"OK: {len(SCRIPTS)} scripts and the card build checked")
