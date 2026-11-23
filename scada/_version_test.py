"""The monitor version: one number (burner_ext.MONITOR_VERSION), the top entry of CHANGELOG.md, sent in /status.json
and shown at the bottom of both pages."""
import sys, os, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_ext as X
import burner_responder as R

here = os.path.dirname(os.path.abspath(__file__))
res = []
def check(name, cond, info=""): res.append(("PASS" if cond else "FAIL", name, "" if cond else info))

v = X.MONITOR_VERSION
check("version looks like 1.2.3 (release tags are v1.2.3)", re.fullmatch(r"\d+\.\d+\.\d+", v) is not None, v)

with open(os.path.join(here, "..", "CHANGELOG.md"), encoding="utf-8") as fh:
    heads = re.findall(r"^## (\d+\.\d+\.\d+)", fh.read(), re.M)
check("CHANGELOG.md top entry is the version", heads[:1] == [v], f"{heads[:1]} != {v}")

check("/status.json carries the version", R.Module(0x01).feed_snapshot().get("version") == v)
check("/api/about carries the version", R.about_info(object()).get("version") == v)

home = open(os.path.join(here, "home.html"), encoding="utf-8").read()
dash = open(os.path.join(here, "dashboard.html"), encoding="utf-8").read()
check("home.html shows the version without the auto-update", "S&&S.version" in home and 'upd_ver:"Version {v}"' in home)
check("dashboard.html shows the version", 'id="footVer"' in dash and "j.version" in dash)

for r in res:
    print(*r)
fails = [r for r in res if r[0] != "PASS"]
print(f"{len(res) - len(fails)}/{len(res)} passed")
sys.exit(1 if fails else 0)
