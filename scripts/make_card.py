#!/usr/bin/env python3
"""Build the "ready SD card" folder and zip for the easy install (works on Windows, macOS and Linux).

    python scripts/make_card.py
    python scripts/make_card.py --repo-url https://github.com/<account>/<your copy>.git     (a fork)

Writes dist/pelletpi-card/ and dist/pelletpi-card.zip. People unzip it onto the card's boot drive right after
Raspberry Pi Imager has written the card. Contents:

    vendor-data                  cloud-init hook: starts the install on the first start
    pelletpi/install.sh          hooks firstboot.sh in as a service
    pelletpi/firstboot.sh        installs everything, with retries and a status file
    pelletpi/pelletpi-firstboot.service
    pelletpi/pelletpi-setup.sh   USB-C network cable (same file as pi/pelletpi-setup.sh)
    pelletpi/install-wifi-setup.sh, pelletpi/pelletpi_wifi_setup.py   Wi-Fi setup hotspot (same files as in pi/)
    pelletpi/settings.txt        REPO_URL (for updates), AUTO_UPDATE - no passwords
    pelletpi/README.txt          short help for people
    pelletpi/app.tar.gz          the program itself (git archive of --ref), so no download from GitHub is needed
    pelletpi/app-version.txt     the version in app.tar.gz

All text files are written with LF line ends. No Wi-Fi names, passwords or keys are ever added.
"""
import argparse, os, shutil, subprocess, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FB = os.path.join(ROOT, "pi", "firstboot")

# (source relative to the repo, destination relative to the card folder)
FILES = [
    ("pi/firstboot/vendor-data", "vendor-data"),
    ("pi/firstboot/install.sh", "pelletpi/install.sh"),
    ("pi/firstboot/firstboot.sh", "pelletpi/firstboot.sh"),
    ("pi/firstboot/pelletpi-firstboot.service", "pelletpi/pelletpi-firstboot.service"),
    ("pi/firstboot/settings.txt", "pelletpi/settings.txt"),
    ("pi/firstboot/README.txt", "pelletpi/README.txt"),
    ("pi/pelletpi-setup.sh", "pelletpi/pelletpi-setup.sh"),
    ("pi/install-wifi-setup.sh", "pelletpi/install-wifi-setup.sh"),
    ("pi/pelletpi_wifi_setup.py", "pelletpi/pelletpi_wifi_setup.py"),
]
EXEC = {"vendor-data", "pelletpi/install.sh", "pelletpi/firstboot.sh", "pelletpi/pelletpi-setup.sh",
        "pelletpi/install-wifi-setup.sh"}


def lf(data):
    return data.replace(b"\r\n", b"\n")


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True).stdout


def build(out, repo_url=None, ref="HEAD", with_app=True):
    card = os.path.join(out, "pelletpi-card")
    if os.path.isdir(card):
        shutil.rmtree(card)
    os.makedirs(os.path.join(card, "pelletpi"))
    for src, dst in FILES:
        with open(os.path.join(ROOT, src), "rb") as f:
            data = lf(f.read())
        if dst.endswith("settings.txt") and repo_url:
            lines = [b"REPO_URL=" + repo_url.encode() if ln.startswith(b"REPO_URL=") else ln
                     for ln in data.split(b"\n")]
            data = b"\n".join(lines)
        with open(os.path.join(card, dst), "wb") as f:
            f.write(data)
    if with_app:
        app = os.path.join(card, "pelletpi", "app.tar.gz")
        git("archive", "--format=tar.gz", "-o", app, ref)
        version = git("describe", "--tags", "--always", ref).decode().strip()
        with open(os.path.join(card, "pelletpi", "app-version.txt"), "wb") as f:
            f.write(version.encode() + b"\n")
    zpath = os.path.join(out, "pelletpi-card.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, names in os.walk(card):
            for n in sorted(names):
                full = os.path.join(base, n)
                rel = os.path.relpath(full, card).replace(os.sep, "/")
                zi = zipfile.ZipInfo(rel, date_time=(2000, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = (0o755 if rel in EXEC else 0o644) << 16
                with open(full, "rb") as f:
                    z.writestr(zi, f.read())
    return card, zpath


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="output folder (default dist/)")
    ap.add_argument("--repo-url", help="https address of your own copy, written into settings.txt (for updates). "
                    "Default: the project's public repository, as in pi/firstboot/settings.txt")
    ap.add_argument("--ref", default="HEAD", help="git tag or commit to pack as the program (default HEAD)")
    ap.add_argument("--no-app", action="store_true", help="leave out app.tar.gz (the Pi then clones REPO_URL)")
    a = ap.parse_args()
    if a.repo_url and not a.repo_url.startswith("https://"):
        sys.exit("--repo-url must start with https:// (the Pi's updater refuses anything else)")
    card, z = build(a.out, a.repo_url, a.ref, not a.no_app)
    print(f"card folder: {card}\nzip:         {z}")


if __name__ == "__main__":
    main()
