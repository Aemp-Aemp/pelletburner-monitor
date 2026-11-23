#!/usr/bin/env python3
"""Keeps the copies inside the two web pages (scada/home.html, scada/dashboard.html) equal to their one source.
The pages stay single files that need nothing else; this tool runs only when a developer changes a source.

    py tools\\sync_pages.py            write the blocks into both pages
    py tools\\sync_pages.py --check    change nothing; exit 1 and a list if a page is not up to date

Blocks (between  /* ==NAME-BEGIN== ... */  and  /* ==NAME-END== */  in each page):
    TABLES   state names, alarm names, alarm classes, the flame limit - made from burner_common.py
    SHARED   the small JavaScript helpers both pages use - the text of scada/shared.js

Never edit a block inside a page: change burner_common.py or scada/shared.js and run this tool.
scada/_sync_test.py fails when a page is not up to date."""
import io, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
import burner_common as C

PAGES = ("scada/home.html", "scada/dashboard.html")
SHARED_JS = os.path.join(ROOT, "scada", "shared.js")
NOTE = {"TABLES": "made from burner_common.py by tools/sync_pages.py - do not edit here",
        "SHARED": "the text of scada/shared.js, copied by tools/sync_pages.py - do not edit here"}


def tables():
    """The tables the pages use, as plain data (also what scada/_sync_test.py compares)."""
    return {"states": C.STATE_PAGE,
            "alarm_names": {lang: {str(k): v for k, v in names.items()} for lang, names in C.ALARM_PAGE.items()},
            "alarm_never_auto_reset": list(C.ALARM_NEVER_AUTO_RESET),
            "alarm_auto_reset_allowed": list(C.ALARM_AUTO_RESET_ALLOWED),
            "flame_on": C.FLAME_ON}


def tables_js():
    rows = [f" {json.dumps(k)}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))}" for k, v in tables().items()]
    return "const TABLES={\n" + ",\n".join(rows) + "};"


def shared_js():
    with io.open(SHARED_JS, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n").strip("\n")


BLOCKS = {"TABLES": tables_js, "SHARED": shared_js}


def _pattern(name):
    return re.compile(r"(/\* ==" + name + r"-BEGIN== [^\n]*\*/\n)(?:.*?\n)??(/\* ==" + name + r"-END== \*/)", re.S)


def block_in(text, name):
    """The text of a block inside a page (line ends as \\n), or None when the page has no such block."""
    m = re.search(r"/\* ==" + name + r"-BEGIN== [^\n]*\*/\n(.*?)\n?/\* ==" + name + r"-END== \*/",
                  text.replace("\r\n", "\n"), re.S)
    return m.group(1) if m else None


def sync(check=False):
    """Bring both pages up to date (or only look, with check). Returns the list of what was (or would be) changed."""
    out = []
    for page in PAGES:
        path = os.path.join(ROOT, page)
        with io.open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        nl = "\r\n" if "\r\n" in raw else "\n"
        text = raw.replace("\r\n", "\n")
        for name, make in BLOCKS.items():
            want = make()
            pat = _pattern(name)
            if not pat.search(text):
                out.append(f"{page}: no {name} block")
                continue
            new = pat.sub(lambda m: f"/* =={name}-BEGIN== {NOTE[name]} */\n{want}\n{m.group(2)}", text, count=1)
            if new != text:
                out.append(f"{page}: {name} block {'is not up to date' if check else 'updated'}")
                text = new
        if not check and text.replace("\n", nl) != raw:
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text.replace("\n", nl))
    return out


if __name__ == "__main__":
    check = "--check" in sys.argv[1:]
    res = sync(check)
    print("\n".join(res) if res else "both pages are up to date")
    sys.exit(1 if res and (check or any("no " in r for r in res)) else 0)
